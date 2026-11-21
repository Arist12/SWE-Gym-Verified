"""Export: the audited task set and the audit records, from the stage records (no LLM, no container). Files, fields
and values follow the data card of SWE-Gym-Verified wherever they describe the same thing.

<out>/tasks.jsonl            released tasks (``decision`` kept or repaired) as full SWE-bench rows: ``problem_statement``
                             is the final statement (the issue followed by ``statement_appendix``, the requirements a
                             repair appended; the unedited issue is ``problem_statement_original``), ``PASS_TO_PASS`` is
                             the graded list (``p2p_dropped``: the ids left out of grading), ``test_patch`` is the final
                             one (``test_patch_relaxed``: a test repair applied; ``test_patch_with_fp_tests`` adds the new
                             tests against false positives); ``difficulty`` {resolved, samples, band}, ``fn_verdict``,
                             ``fn_evidence``, ``flags``, ``has_false_positive``
<out>/audit_records.jsonl    every audited task: ``decision`` (kept | repaired | removed | pending), ``removal_stage`` and
                             ``removal_reasons`` (the first stage that removed the task and its reason codes) or
                             ``pending_stage``, ``rating_claude`` / ``rating_gpt``, ``audit_path``, ``fn_verdict``,
                             ``fn_evidence``, ``flags``, ``difficulty``, and under each stage's name the main fields of
                             its row (both families' ratings, the escalation record with the executed patches, review
                             and debate, the triage decision, the repair and its checks, the re-audit checks)
<out>/witness_patches.jsonl  every executed escalation patch: ``family``, ``role`` (witness | alternative), ``patch`` and
                             its execution outcome
<out>/fp_labels.jsonl        every labelled passing patch: ``label`` (false_positive | correct | undetermined), both
                             families' labels and summaries, the counterexamples with their outcomes
<out>/fp_tests.jsonl         per task with candidate tests: ``test_file``, ``tests`` and ``test_patch`` (the added test
                             file with the kept tests) and ``candidate_tests`` (every candidate test with its checks)
<out>/removals.json          removal counts by stage and reason, and task counts by decision

``fn_verdict``: no_fn | fn_confirmed | fn_contested (the escalate stage's OK | FN_CONFIRMED | CONTESTED), or not_reached
for a task removed before a verdict.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from ..util import write_json
from . import ORDER, REMOVING

NAME = "export"
TABLES = ("tasks", "audit_records", "witness_patches", "fp_labels", "fp_tests")
FN_VERDICT = {"OK": "no_fn", "FN_CONFIRMED": "fn_confirmed", "CONTESTED": "fn_contested"}


def status(ctx, iid: str) -> dict:
    """The task's ``decision`` (kept | repaired | removed | pending), with ``removal_stage`` and ``removal_reasons`` of a
    removed task and ``pending_stage`` of a pending one."""
    d = ctx.removed(iid)
    if d:
        return {"decision": "removed", "removal_stage": d["stage"], "removal_reasons": d.get("reasons") or []}
    st = ctx.store
    if (st.get("reaudit", iid) or {}).get("decision") == "repaired":
        return {"decision": "repaired"}
    for s in REMOVING:
        r = st.get(s, iid)
        if r is None and s in ("prescreen", "environment", "screen", "fn_rate", "escalate"):
            return {"decision": "pending", "pending_stage": s}
        if r is not None and not r.get("complete"):
            return {"decision": "pending", "pending_stage": s}
    if (st.get("repair", iid) or {}).get("decision") == "repaired":
        return {"decision": "pending", "pending_stage": "reaudit"}
    if (st.get("triage", iid) or {}).get("decision") == "repair":
        return {"decision": "pending", "pending_stage": "repair"}
    from . import triage
    if st.get("triage", iid) is None and triage.wants(ctx, iid):
        return {"decision": "pending", "pending_stage": "triage"}
    return {"decision": "kept"}


def fn_verdict(esc: dict, removed: bool) -> str | None:
    """The escalate row's decision in the data card's words."""
    return FN_VERDICT.get(esc.get("decision")) or ("not_reached" if removed else None)


def run_all(ctx, ids=None, force=False, log=print) -> dict:
    out = Path(ctx.scfg(NAME).get("out") or Path(ctx.cfg["work_dir"]) / "export")
    out.mkdir(parents=True, exist_ok=True)
    files = {n: open(out / f"{n}.jsonl", "w") for n in TABLES}
    removals, counts = Counter(), Counter()
    try:
        for iid in ids or list(ctx.tasks):
            s = status(ctx, iid)
            counts[s["decision"]] += 1
            if s["decision"] == "removed":
                for r in s["removal_reasons"][:1] or ["(none)"]:
                    removals[f"{s['removal_stage']}:{r}"] += 1
            st = ctx.store
            rate = st.get("fn_rate", iid) or {}
            esc = st.get("escalate", iid) or {}
            fp = st.get("fp", iid) or {}
            measured = (st.get("measure", iid) or {})
            difficulty = {k: measured.get(k) for k in ("resolved", "samples", "band")} \
                if measured.get("decision") == "measured" else None
            flags = sorted({f for n in ORDER for f in ((st.get(n, iid) or {}).get("flags") or [])})
            verdict = fn_verdict(esc, s["decision"] == "removed")
            rec = {"instance_id": iid, "repo": (ctx.tasks.get(iid) or {}).get("repo"), **s,
                   "rating_claude": (rate.get("fn") or {}).get("claude"), "rating_gpt": (rate.get("fn") or {}).get("gpt"),
                   "audit_path": esc.get("audit_path"), "fn_verdict": verdict, "fn_evidence": esc.get("fn_evidence"),
                   "flags": flags, "difficulty": difficulty,
                   "screen": {k: (st.get("screen", iid) or {}).get(k) for k in ("decision", "reasons", "flags", "pai")},
                   "environment": {k: (st.get("environment", iid) or {}).get(k) for k in ("decision", "reasons", "flags",
                                                                                           "p2p_dropped")},
                   "fn_rate": {k: rate.get(k) for k in ("decision", "reasons", "fn", "families")},
                   "escalate": {k: esc.get(k) for k in ("decision", "reasons", "mode", "escalation")},
                   "triage": {k: (st.get("triage", iid) or {}).get(k) for k in ("decision", "reasons", "plan", "detail")},
                   "repair": {k: (st.get("repair", iid) or {}).get(k) for k in ("decision", "reasons", "block", "gates",
                                                                                "failed", "flags")},
                   "reaudit": {k: (st.get("reaudit", iid) or {}).get(k) for k in ("decision", "reasons", "checks")}}
            files["audit_records"].write(json.dumps(rec, default=str) + "\n")
            for f, x in (((esc.get("escalation") or {}).get("X")) or {}).items():
                role = x.get("role")
                p = ctx.store.work("escalate", iid) / f / "W" / "out" / f"{'witness' if role == 'witness' else 'alternative'}.diff"
                files["witness_patches"].write(json.dumps({
                    "instance_id": iid, "family": f, "role": role, "patch": p.read_text() if p.exists() else None,
                    **{k: x.get(k) for k in ("applied", "f2p_failed_attributable", "p2p_failed_new", "resolved")}}) + "\n")
            for p in fp.get("patches") or []:
                files["fp_labels"].write(json.dumps({"instance_id": iid, **p}, default=str) + "\n")
            if fp.get("candidate_tests"):
                files["fp_tests"].write(json.dumps({
                    "instance_id": iid, "test_file": fp.get("test_file"), "tests": fp.get("tests") or [],
                    "test_patch": fp.get("test_patch"), "candidate_tests": fp["candidate_tests"]}, default=str) + "\n")
            if s["decision"] not in ("kept", "repaired"):
                continue
            t = ctx.effective(iid)
            files["tasks"].write(json.dumps({
                **{k: t.get(k) for k in ("instance_id", "repo", "base_commit", "version", "created_at", "image",
                                         "patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "p2p_dropped",
                                         "hints_text")},
                "problem_statement": t["statement"], "problem_statement_original": t["statement_original"],
                "statement_repaired": bool(t.get("spec_block")), "statement_appendix": t.get("spec_block"),
                "test_patch_relaxed": bool(((st.get("repair", iid) or {}).get("overlay") or {}).get("test_patch")),
                "test_patch_with_fp_tests": (t["test_patch"] + fp["test_patch"]) if fp.get("test_patch") else None,
                "difficulty": difficulty, "fn_verdict": verdict, "fn_evidence": esc.get("fn_evidence"),
                "decision": s["decision"], "flags": flags, "has_false_positive": fp.get("decision") == "flag"}) + "\n")
    finally:
        for f in files.values():
            f.close()
    write_json(out / "removals.json", {"by_stage_and_reason": dict(sorted(removals.items())), "decisions": dict(counts)})
    log(f"[export] {dict(counts)} -> {out}")
    return dict(counts)
