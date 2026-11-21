"""Adapters: swebench (a file of SWE-bench-style records), swegym (the public SWE-Gym dataset), jsonl, hf."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ADAPTER_DEFAULTS = {
    # the paper's source: the public SWE-Gym dataset and its per-task images
    "swegym": {"hf_name": "SWE-Gym/SWE-Gym", "hf_split": "train",
               "image_template": "xingyaoww/sweb.eval.x86_64.{iid_swegym}:latest"},
    # SWE-bench and its variants publish images as swebench/sweb.eval.x86_64.<id with __ -> _1776_>:latest
    "swebench": {"image_template": "swebench/sweb.eval.x86_64.{iid_swebench}:latest"},
    "jsonl": {},
    "hf": {},
}
LIST_FIELDS = ("FAIL_TO_PASS", "PASS_TO_PASS")
REQUIRED = ("instance_id", "repo", "base_commit")  # an empty statement, patch or test patch is the pre-screen's job


def image_fields(t: dict) -> dict:
    iid = t["instance_id"]
    return {"instance_id": iid, "iid_lower": iid.lower(), "iid_swegym": iid.lower().replace("__", "_s_"),
            "iid_swebench": iid.lower().replace("__", "_1776_"), "repo": t.get("repo", ""),
            "repo_slug": t.get("repo", "").replace("/", "__"), "version": t.get("version") or "",
            "id_short": iid.split("__", 1)[-1]}


def normalize(r: dict, field_map: dict | None = None, image_template: str | None = None, registry: str = "") -> dict:
    t = dict(r)
    for src, dst in (field_map or {}).items():
        if src in t:
            t[dst] = t.pop(src)
    for k in LIST_FIELDS:
        v = t.get(k)
        if isinstance(v, str):
            v = json.loads(v) if v.strip().startswith("[") else [x for x in v.split("\n") if x]
        t[k] = [str(x) for x in (list(v) if v is not None and not isinstance(v, float) else [])]
    for k in ("hints_text", "version", "created_at", "test_patch", "patch", "problem_statement"):
        v = t.get(k)
        if v is None or (isinstance(v, float) and v != v):
            t[k] = "" if k != "version" else None
    if t.get("version") is not None:
        t["version"] = str(t["version"])
    missing = [k for k in REQUIRED if not t.get(k)]
    if missing:
        raise ValueError(f"{t.get('instance_id')}: missing fields {missing}")
    if not t.get("image") and image_template:
        t["image"] = image_template.format(**image_fields(t))
    if t.get("image") and registry and not t["image"].startswith(registry):
        t["image"] = registry + t["image"]
    for k, v in list(t.items()):  # numpy arrays from parquet
        if hasattr(v, "tolist") and not isinstance(v, (str, bytes)):
            t[k] = v.tolist()
    return t


def _read_file(p: Path) -> list[dict]:
    if p.suffix == ".parquet":
        import pandas as pd

        return pd.read_parquet(p).to_dict("records")
    if p.suffix in (".jsonl", ".gz") or p.name.endswith(".jsonl.gz"):
        from ..util import read_jsonl

        return read_jsonl(p)
    obj = json.loads(p.read_text())
    return obj if isinstance(obj, list) else obj.get("tasks") or obj.get("instances") or []


def _hf_rows(name: str, split: str, revision: str | None, cache: Path) -> list[dict]:
    """A Hugging Face dataset: the `datasets` library if installed, else its parquet files over HTTPS."""
    try:
        import datasets  # type: ignore

        return [dict(x) for x in datasets.load_dataset(name, split=split, revision=revision)]
    except ImportError:
        pass
    api = f"https://huggingface.co/api/datasets/{name}/parquet/default/{split}"
    with urllib.request.urlopen(api, timeout=120) as resp:
        urls = json.loads(resp.read())
    rows = []
    for i, u in enumerate(urls):
        dest = cache / f"{name.replace('/', '__')}-{split}-{i}.parquet"
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            urllib.request.urlretrieve(u, tmp)
            tmp.rename(dest)
        rows += _read_file(dest)
    return rows


def load_tasks(cfg: dict, ids: list[str] | None = None) -> list[dict]:
    d = dict(cfg.get("dataset") or {})
    adapter = d.get("adapter") or "swebench"
    if adapter not in ADAPTER_DEFAULTS:
        raise SystemExit(f"dataset.adapter {adapter!r}: one of {sorted(ADAPTER_DEFAULTS)}")
    for k, v in ADAPTER_DEFAULTS[adapter].items():
        if not d.get(k):
            d[k] = v
    if d.get("path"):
        if not Path(d["path"]).expanduser().exists():
            raise SystemExit(f"dataset.path: {d['path']} does not exist")
        rows = _read_file(Path(d["path"]).expanduser())
    elif d.get("hf_name"):
        rows = _hf_rows(d["hf_name"], d.get("hf_split") or "train", d.get("hf_revision"),
                        Path(cfg["work_dir"]) / "datasets")
    else:
        raise SystemExit("dataset: set path (a .jsonl / .json / .parquet file) or hf_name")
    want = list(dict.fromkeys(ids or []))
    if want:
        src = next((s for s, dst in (d.get("field_map") or {}).items() if dst == "instance_id"), "instance_id")
        by = {str(r.get(src)): r for r in rows}
        missing = [i for i in want if i not in by]
        if missing:
            raise SystemExit(f"not in the dataset: {missing[:10]}")
        rows = [by[i] for i in want]
    out = [normalize(r, d.get("field_map"), d.get("image_template"), d.get("image_registry") or "") for r in rows]
    digests = _digests(d.get("image_digests"))
    for t in out:  # the witness / review / debate packets name the image digest when it is known
        if not t.get("image_digest") and digests.get(t["instance_id"]):
            t["image_digest"] = digests[t["instance_id"]]
    return out


def _digests(path: str | None) -> dict[str, str]:
    """instance_id -> image digest, from a JSON object or a JSONL of {instance_id, image_digest | digest}."""
    if not path:
        return {}
    p = Path(path).expanduser()
    if p.suffix == ".json":
        return dict(json.loads(p.read_text()))
    from ..util import read_jsonl
    return {r["instance_id"]: r.get("image_digest") or r.get("digest") for r in read_jsonl(p)}
