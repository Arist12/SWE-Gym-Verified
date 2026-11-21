"""Code-evidence packs.

fn_pack: the pack the FN rater and the witness / review / debate agents see = the frozen pack builder (build_pack.py,
  carried over unchanged) on the effective task (statement incl. an appended specification, graded F2P / P2P ids),
  64K o200k tokens; a pack with omissions or truncations is rebuilt at 128K, and a pack still degraded at 128K is
  ``degraded`` (the FN decision rule escalates such a task).
fp_pack: the pack the FP judge sees = the same builder run with the CANDIDATE patch in the gold patch's place and no
  tests, F2P / P2P ids or hints; sections 1, 5, 9, 11, 14 dropped, "gold" wording renamed, code markers removed, and
  any line naming an F2P test the candidate does not name or quoting a gold-added line (>= 20 chars, absent at base)
  withheld.

The builder needs PYTHONHASHSEED=0 (set iteration order); the CLI re-executes itself with it.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

BUILDER = Path(__file__).resolve().parent / "build_pack.py"
PACK_SEC = re.compile(r"^## (\d+)\. ", re.M)
CODE_MARK = re.compile(r"^[*>](?=\s*\d+\| )")
PACK_DROP = ("1", "5", "9", "11", "14")
CANDIDATE_WORDING = ((r"\bgold-(added|removed|changed)\b", r"candidate-\1"), (r"\(gold\+\)", "(candidate+)"),
                     (r"\bgold\+ and base\b", "candidate+ and base"), (r"\b([Tt]he) gold patch\b", r"\1 candidate patch"),
                     (r"; gold edits ", "; the candidate edits "), (r", name used by an F2P test\)", ")"))
_B = None
_LOCK = threading.Lock()
IMAGES: dict[str, str] = {}
FIELDS = ("instance_id", "repo", "base_commit", "version", "patch", "test_patch", "problem_statement", "hints_text",
          "FAIL_TO_PASS", "PASS_TO_PASS", "created_at")


def _harness_specs() -> dict:
    try:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        return dict(MAP_REPO_VERSION_TO_SPECS)
    except Exception:
        return {}


def builder(work_dir: str, repos_dir: str):
    """The frozen builder module, with its machine paths pointed at this run (read from the environment at import)."""
    global _B
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise SystemExit("code-evidence packs need PYTHONHASHSEED=0 (run through the CLI)")
    with _LOCK:
        if _B is None:
            os.environ["SGV_WORK"] = str(Path(work_dir) / "packs")
            os.environ["SGV_REPOS"] = str(repos_dir)
            spec = importlib.util.spec_from_file_location("build_pack", BUILDER)
            _B = importlib.util.module_from_spec(spec)
            sys.modules["build_pack"] = _B
            spec.loader.exec_module(_B)
            specs = _harness_specs()
            _B.load_harness = lambda: specs          # section 6: the installed harness's install specs
            _B.image_name = lambda iid: IMAGES[iid]  # the /testbed fallback copies out of the task's own image
    return _B


def _build(B, task: dict, out_dir: Path, budget: int) -> tuple[str, dict]:
    B.OUT_ROOT.mkdir(parents=True, exist_ok=True)
    IMAGES[task["instance_id"]] = task.get("image") or ""
    d = B.ensure_repo(task["repo"])
    B.ensure_commit(d, task["base_commit"])
    out_dir.mkdir(parents=True, exist_ok=True)
    ix = B.work(task, str(out_dir), budget, True)
    return (out_dir / f"{task['instance_id']}.md").read_text(), ix


def degraded(ix: dict) -> bool:
    return bool(ix.get("omissions") or ix.get("truncations"))


def fn_pack(B, task: dict, out_dir: Path, budget: int = 64000, budget_hi: int = 128000) -> dict:
    """{text, budget, degraded, sha256} of the FN pack of the effective task ``task``."""
    t = {k: task.get(k) for k in FIELDS}
    t["hints_text"] = t["hints_text"] or ""
    text, ix = _build(B, t, out_dir / "64k", budget)
    used = budget
    if degraded(ix) and budget_hi > budget:
        text, ix = _build(B, t, out_dir / f"{budget_hi // 1000}k", budget_hi)
        used = budget_hi
    return {"text": text, "budget": used, "degraded": degraded(ix), "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "tokens": ix.get("tokens_o200k")}


def split_pack(text: str) -> tuple[str, list[tuple[str, str]]]:
    ms, last = [], 0
    for m in PACK_SEC.finditer(text):
        if int(m.group(1)) > last:
            ms.append(m)
            last = int(m.group(1))
    if not ms:
        return text, []
    return text[:ms[0].start()], [(m.group(1), text[m.start():ms[i + 1].start() if i + 1 < len(ms) else len(text)])
                                  for i, m in enumerate(ms)]


def _prose(chunk: str, fn) -> str:
    out, code = [], False
    for ln in chunk.split("\n"):
        if ln.startswith("```"):
            code = not code
        out.append(fn(ln, code))
    return "\n".join(out)


def f2p_names(ids: list[str]) -> list[str]:
    out = set()
    for i in ids:
        ps = i.split("::")
        n = re.sub(r"\[.*$", "", ps[-1]).strip()
        out.add(ps[-2] if n == "test" and len(ps) >= 3 else n)
    return sorted(out - {""})


def gold_hidden(B, task: dict) -> list[str]:
    d = B.repo_dir(task["repo"])
    lines = sorted({x[1:].strip() for x in task["patch"].split("\n") if x.startswith("+") and not x.startswith("+++")})
    return [g for g in lines if len(g) >= 20 and subprocess.run(
        ["git", "-C", str(d), "grep", "-q", "-F", "-e", g, task["base_commit"]], capture_output=True).returncode]


def fp_pack(B, task: dict, patch: str, out_dir: Path, budget: int = 64000) -> str:
    t = {"instance_id": task["instance_id"], "repo": task["repo"], "base_commit": task["base_commit"],
         "version": task.get("version"), "patch": patch, "test_patch": "", "problem_statement": task["problem_statement"],
         "hints_text": "", "FAIL_TO_PASS": [], "PASS_TO_PASS": [], "created_at": task.get("created_at"),
         "image": task.get("image")}
    sub = out_dir / hashlib.sha256(patch.encode()).hexdigest()[:16]
    text, _ = _build(B, t, sub, budget)
    head, secs = split_pack(text)
    cand = {x[1:].strip() for x in patch.split("\n") if x[:1] in "+- "}
    gold = [g for g in gold_hidden(B, task) if g not in cand]
    names = [n for n in f2p_names(task["FAIL_TO_PASS"]) if not re.search(rf"(?<!\w){re.escape(n)}(?!\w)", patch)]
    words = [re.compile(rf"(?<!\w){re.escape(n)}(?!\w)") for n in names]

    def keep(ln: str) -> bool:
        x = ln.rstrip()
        return not (any(w.search(x) for w in words) or any(x.strip() == g or x.endswith((" " + g, "|" + g))
                                                            for g in gold))

    def rename(ln, code):
        if code:
            return CODE_MARK.sub(" ", ln)
        for a, b in CANDIDATE_WORDING:
            ln = re.sub(a, b, ln)
        return ln

    head_lines = [ln for ln in head.rstrip("\n").split("\n") if not ln.startswith(("- gold patch:", "- budget degr"))]
    parts = ["\n".join(head_lines + ["- judge view: built from the base tree, the issue and the candidate patch only; "
                                     "sections 1, 5, 9, 11 and 14 are not included."])]
    for n, c in secs:
        if n in PACK_DROP:
            continue
        parts.append("\n".join(ln for ln in _prose(c, rename).split("\n") if keep(ln)).rstrip("\n"))
    return "\n\n".join(parts) + "\n"
