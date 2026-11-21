"""Difficulty measurement (optional): how many samples of a policy model resolve the effective task (statement and
tests).

The paper takes 8 samples of its base model per task (SWE-agent, 80 tool calls, 128K context, temperature 1) and
grades each patch with the task's grader; difficulty is the number resolved out of 8, written as the band "3/8" (0/8
never solved, 8/8 always solved; the dataset's ``base_resolved_of_8``). The policy is pluggable (``stages.measure``):

  rollouts: a JSONL of precomputed episodes {instance_id, patch, name?, statement_sha256?} (e.g. from your own
            SWE-agent / OpenHands run on the statement this tool exports with ``export``); rows whose
            statement_sha256 differs from the current statement's are ignored;
  policy:   {command: [...], n: 8, timeout_s: 3600} run once per sample on the host (``n`` samples, default
            ``stages.measure.samples``); placeholders {instance_id}
            {image} {statement_file} {task_file} {out_patch} {workdir} {attempt}; the command writes the patch to
            {out_patch} (it can call any policy endpoint, e.g. an OpenAI-compatible server of the policy model).

Every patch is graded by the grader (offline unless ``grade_network``). The measurement is keyed by the statement and
test side, so a repaired task is measured again. Row: resolved, samples, band "<resolved>/<samples>", rollouts
[{name, patch, resolved, f2p_bad, p2p_bad, log}], statement_sha256, by_statement {statement sha256: {resolved,
samples, band}} (the measurements of earlier statements are kept, so the band before a repair stays available).
"""
from __future__ import annotations

import subprocess

from ..harness.grader import Grader, bad_ids
from ..util import read_jsonl, sha, sha_json, write_json

NAME = "measure"
DEPENDS = ["environment"]
_GR = None


def grader(ctx) -> Grader:
    global _GR
    if _GR is None:
        _GR = Grader(ctx.cfg, NAME, ctx.cfg["work_dir"])
    return _GR


def cleanup(ctx) -> None:
    if _GR is not None:
        _GR.docker.cleanup()


def wants(ctx, iid: str) -> bool:
    return bool(ctx.scfg(NAME).get("enabled"))


def key(ctx, iid: str) -> str:
    t = ctx.effective(iid)
    sc = ctx.scfg(NAME)
    rows = [r for r in (read_jsonl(sc["rollouts"]) if sc.get("rollouts") else []) if r.get("instance_id") == iid]
    return sha_json({"view": ctx.view_sha(t), "settings": sc, "rollouts": sorted(sha(r.get("patch") or "") for r in rows)})


def samples(ctx, t: dict) -> list[dict]:
    sc = ctx.scfg(NAME)
    ssha = sha(t["statement"])
    if sc.get("rollouts"):
        return [{"name": r.get("name") or f"r{i}", "patch": r.get("patch") or ""}
                for i, r in enumerate(read_jsonl(sc["rollouts"]))
                if r.get("instance_id") == t["instance_id"] and r.get("statement_sha256") in (None, ssha)]
    pol = sc.get("policy") or {}
    if not pol.get("command"):
        return []
    wd = ctx.store.work(NAME, t["instance_id"])
    (wd / "statement.md").write_text(t["statement"])
    write_json(wd / "task.json", {k: t[k] for k in ("instance_id", "repo", "base_commit", "image", "version")})
    out = []
    for a in range(int(pol.get("n") or sc.get("samples") or 8)):
        op = wd / f"sample{a}.patch"
        if not op.exists():
            subs = {"instance_id": t["instance_id"], "image": t["image"], "statement_file": str(wd / "statement.md"),
                    "task_file": str(wd / "task.json"), "out_patch": str(op), "workdir": str(wd), "attempt": str(a)}
            cmd = [str(x).format(**subs) for x in pol["command"]]
            subprocess.run(cmd, cwd=wd, timeout=float(pol.get("timeout_s") or 3600), capture_output=True)
        out.append({"name": f"sample{a}", "patch": op.read_text() if op.exists() else ""})
    return out


def run(ctx, iid: str) -> dict:
    t = ctx.effective(iid)
    xs = samples(ctx, t)
    if not xs:
        return {"complete": False, "decision": "pending", "reasons": ["no rollouts and no policy command"]}
    rolls = []
    for x in xs:
        if not x["patch"].strip():
            rolls.append({"name": x["name"], "patch": "", "resolved": False, "empty": True})
            continue
        r = grader(ctx).run(t, x["patch"], f"m-{x['name']}", network="bridge" if ctx.scfg(NAME).get("grade_network") else None)
        fb, pb = bad_ids(r)
        rolls.append({"name": x["name"], "patch": x["patch"], "resolved": bool(r.get("resolved")), "f2p_bad": fb,
                      "p2p_bad": pb, "harness_error": r.get("harness_error"), "log": r.get("log")})
    k = sum(r["resolved"] for r in rolls)
    prev = (ctx.store.get(NAME, iid) or {}).get("by_statement") or {}
    ssha = sha(t["statement"])
    return {"decision": "measured", "reasons": [], "resolved": k, "samples": len(rolls), "band": f"{k}/{len(rolls)}",
            "statement_sha256": ssha, "rollouts": rolls,
            "by_statement": {**prev, ssha: {"resolved": k, "samples": len(rolls), "band": f"{k}/{len(rolls)}"}}}


def band(ctx, iid: str, statement: str | None = None) -> dict | None:
    """The measured band of the task's current (or the given) statement, if any."""
    r = ctx.store.get(NAME, iid)
    if not r or r.get("decision") != "measured":
        return None
    if statement is None:
        return {"resolved": r["resolved"], "samples": r["samples"], "band": r["band"]} if r.get("complete") else None
    return (r.get("by_statement") or {}).get(sha(statement))  # bands of earlier statements are kept

