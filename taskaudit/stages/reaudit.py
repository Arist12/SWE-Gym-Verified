"""Re-audit of a repaired task, once (no second repair): any failed check removes the task.

Statement repair: the misleading-statement screen (step 1d) again with the specification shown (both families
misleading -> REAUDIT_MISLEADING_BOTH_FAMILIES); the FN rating (3a) again on the repaired statement (both fn >= 2 ->
REAUDIT_FN_CONFIRMED without a witness round; ESCALATE -> the full escalation: CONFIRMED -> REAUDIT_FN_CONFIRMED,
CONTESTED -> REAUDIT_FN_CONTESTED, gold failing -> REAUDIT_ENV_NEEDS_NETWORK); with a difficulty measurement of the
repaired task: G4, the base-model check (>= 2 of the samples violate the same routed assertion the same way -> one
violating sample is reviewed and debated by both families; CONFIRMED -> REAUDIT_G4_BASE_MODEL_FN) and G5, the jump
read (0 samples resolved before the repair and >= 6 of 8 after -> the leak re-read, step 5g, by both leak judges on
the G3 message; both flag -> REAUDIT_G5_LEAK_BOTH_JUDGES).
Test repair: the environment check (gold / empty x3) on the relaxed tests (REAUDIT_ENV_<code>), then the FN rating
and escalation as above.
Row: decision repaired | drop | pending; reasons; checks {misleading, fn_rate, escalation, environment,
G4_base_model, G5}; overlay (carried from the repair).
"""
from __future__ import annotations

from collections import Counter

from ..gates.stats import g5_deletes
from ..harness.grader import read_log
from ..harness.logs import failure_excerpt, failure_lines, norm_fail
from ..llm import DryRun
from ..util import sha, sha_json
from . import environment as ENV
from . import escalate as X
from . import fn_rate as R
from . import measure as M
from . import screen as SC
from .repair import g3_user, label_row, parse_g3, routed_sites

NAME = "reaudit"
DEPENDS = ["repair"]


def wants(ctx, iid: str) -> bool:
    return (ctx.store.get("repair", iid) or {}).get("decision") == "repaired"


def key(ctx, iid: str) -> str:
    t = ctx.effective(iid)
    return sha_json({"view": ctx.view_sha(t), "repair": (ctx.store.get("repair", iid) or {}).get("key"),
                     "settings": ctx.scfg(NAME), "measure": ctx.scfg("measure"),
                     "prompts": ctx.prompts.shas("misleading", "fn_rater", "witness", "review", "debate", "g5_jump_read")})


def same_way(t: dict, rows: list[dict], rollouts: list[dict]) -> dict:
    """G4 base-model check: labels per routed row for every sample; fail if >= 2 samples violate the same row with
    the same normalised failure message (the witness: the first such sample by name)."""
    labs, sigs = [], []
    for g in rollouts:
        bad = set(g.get("f2p_bad") or []) | set(g.get("p2p_bad") or [])
        log = read_log(g.get("log"))
        status = {tid: tid not in bad for r in rows for tid in r["tests"]}
        L = {r["row"]: label_row(r, status, lambda tid: failure_lines(log, tid)) for r in rows}
        labs.append(L)
        for r in rows:
            if L[r["row"]] == "violated":
                sig = "\n".join(norm_fail(failure_excerpt(log, tid)) for tid in r["tests"] if tid in bad)
                sigs.append((r["row"], sig, g.get("name"), g.get("patch")))
    worst = Counter((r, s) for r, s, _, _ in sigs).most_common(1)
    fail = bool(worst) and worst[0][1] >= 2
    out = {"n": len(rollouts), "labels": labs, "decision": "fail" if fail else "pass"}
    if fail:
        hits = sorted((n, p) for r, s, n, p in sigs if (r, s) == worst[0][0] and n)
        out["witness"] = {"name": hits[0][0], "patch": hits[0][1], "row": worst[0][0][0], "n_same_way": worst[0][1],
                          "sha256": sha(hits[0][1] or "")}
    return out


def fn_checks(ctx, t: dict, checks: dict) -> list[str]:
    r = R.rate(ctx, t, stage="rr-3a")
    checks["fn_rate"] = {k: r.get(k) for k in ("decision", "reasons", "fn")}
    if r.get("decision") == "pending":
        return ["pending"]
    if r["decision"] == "OK":
        return []
    if r["decision"] == "FN_BOTH":
        return ["REAUDIT_FN_CONFIRMED"]
    rec = X.escalate(ctx, t, "full", r["reasons"], ns=NAME)
    checks["escalation"] = {k: rec.get(k) for k in ("resolution", "incomplete", "D", "work")}
    if rec.get("dry_run") or rec.get("resolution") is None:
        return ["pending"]
    res = rec["resolution"]
    if res == "ENV_NEEDS_NETWORK":
        return ["REAUDIT_ENV_NEEDS_NETWORK"]
    if res == "CONTESTED" and rec.get("incomplete"):
        return ["pending"]
    return {"CONFIRMED": ["REAUDIT_FN_CONFIRMED"], "CONTESTED": ["REAUDIT_FN_CONTESTED"]}.get(res, [])


def run(ctx, iid: str) -> dict:
    rep = ctx.store.get("repair", iid)
    t = ctx.effective(iid)
    checks, codes = {}, []
    if rep["overlay"].get("test_patch"):  # test repair: the environment check on the relaxed tests first
        tv = dict(ctx.tasks[iid], test_patch=rep["overlay"]["test_patch"], mounts=t.get("mounts") or [])
        g = ENV.grader(ctx)
        gold = [g.run(tv, tv["patch"], "gold") for _ in range(3)]
        empty = [g.run(tv, "", "empty") for _ in range(3)]
        v = ENV.classify(tv, gold, empty)
        checks["environment"] = {k: v.get(k) for k in ("verdict", "codes")}
        if v["verdict"] != "kept":
            codes += [f"REAUDIT_ENV_{c.removeprefix('ENV_')}" for c in (v.get("codes") or ["CHECK_FAILED"])]
    else:
        m = SC.misleading(ctx, t, stage="rr-1d")
        checks["misleading"] = {k: m.get(k) for k in ("decision", "codes", "flags")}
        if m.get("dry_run") or m["decision"] == "undecided":
            return {"complete": False, "decision": "pending",
                    "reasons": ["dry_run" if m.get("dry_run") else "undecided:misleading"], "checks": checks}
        if m["decision"] in ("delete", "route"):
            codes.append("REAUDIT_MISLEADING_BOTH_FAMILIES")
    if not codes:
        fc = fn_checks(ctx, t, checks)
        if fc == ["pending"]:
            return {"complete": False, "decision": "pending", "reasons": ["dry_run_or_incomplete"], "checks": checks}
        codes += fc
    if not codes and not rep["overlay"].get("test_patch") and ctx.scfg("measure").get("enabled"):
        from . import run_stage
        run_stage(ctx, "measure", [iid])
        mrow = ctx.store.get("measure", iid) or {}
        after = M.band(ctx, iid, t["statement"])
        if after is None:
            return {"complete": False, "decision": "pending", "reasons": ["no measurement of the repaired task"],
                    "checks": checks}
        rows = routed_sites(rep["record"], rep["routed"])
        sw = same_way(t, rows, mrow.get("rollouts") or [])
        checks["G4_base_model"] = {k: sw.get(k) for k in ("decision", "n")} | {
            "witness": {k: v for k, v in (sw.get("witness") or {}).items() if k != "patch"}}
        if sw["decision"] == "fail":  # "student": the prompts' word for the base model whose sample is escalated
            rec = X.escalate(ctx, t, "student", ["student"], ns=f"{NAME}-student", student=sw["witness"])
            checks["G4_base_model"]["resolution"] = rec.get("resolution")
            if rec.get("dry_run") or rec.get("resolution") is None:
                return {"complete": False, "decision": "pending", "reasons": ["dry_run"], "checks": checks}
            if rec["resolution"] == "CONFIRMED":
                codes.append("REAUDIT_G4_BASE_MODEL_FN")
        before = M.band(ctx, iid, t["statement_original"]) or (ctx.store.get("triage", iid) or {}).get("record", {}).get("band")
        before_k = before["resolved"] if isinstance(before, dict) else int(str(before).split("/")[0]) if before else None
        if not codes and before_k == 0 and after["resolved"] >= round(6 * after["samples"] / 8):
            user = g3_user(ctx, {"task": t, "block": rep["block"], "routed": rep["routed"], "rows": []})
            replies = {}
            for f, who in (("claude", "leak_judge_a"), ("gpt", "leak_judge_b")):
                p = None
                for k in range(3):
                    try:
                        res = ctx.llm.chat(who, ctx.prompts.text("g5_jump_read"), user, tag=f"g5:{iid}:a{k}", stage=NAME)
                    except DryRun:
                        return {"complete": False, "decision": "pending", "reasons": ["dry_run"], "checks": checks}
                    p = parse_g3(res.get("text")) if res.get("model_ok") else None
                    if p:
                        break
                replies[f] = p
            g5 = g5_deletes(replies)
            checks["G5"] = g5
            if g5["delete"]:
                codes.append("REAUDIT_G5_LEAK_BOTH_JUDGES")
    if codes:
        return {"decision": "drop", "reasons": codes, "checks": checks}
    return {"decision": "repaired", "reasons": ["REAUDIT_PASS"], "checks": checks, "overlay": rep["overlay"]}

