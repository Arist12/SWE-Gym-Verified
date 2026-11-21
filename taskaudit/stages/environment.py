"""Environment: the gold patch and the empty patch, ``reps`` (3) runs each, offline, in fresh containers after PREP
(single-commit history at the base commit, the project's copies removed), graded with the guarded eval script.

Decision (``classify``, every F2P / P2P id over the runs):
  drop   ENV_GOLD_APPLY_FAIL, ENV_TEST_PATCH_FAIL, ENV_NO_TEST_OUTPUT, FLAKY_F2P (an F2P id passes in only some gold
         or only some empty runs), EMPTY_PASSES_ALL_F2P, ENV_GOLD_F2P_FAIL / ENV_GOLD_P2P_FAIL (gold fails an id in
         every run; with ``probe_network`` one online gold run decides ENV_NEEDS_NETWORK instead when it passes),
         and a run slot that stays infrastructure after ``max_infra_retries`` (ENV_PREP_FAIL, ENV_TIMEOUT,
         ENV_RESOURCE_LIMIT, ENV_RUNTIME_CONFIG, ENV_INFRA_PERSISTENT)
  keep   flags ``gold_patch_applies_with_fuzz``, ``some_f2p_pass_without_patch``, ``many_p2p_dropped``
P2P ids dropped from grading (not a removal; ``p2p_dropped`` {id: why}): flaky / flaky_empty, unreported, skipped,
fails_without_patch. Row fields: decision, reasons, flags, p2p_dropped, f2p_passes_empty, history, runs (summaries).
"""
from __future__ import annotations

import json

from ..harness.grader import Grader, infra_reason, test_data_mounts
from ..util import sha_json

NAME = "environment"
DEPENDS = ["prescreen"]
INFRA_CODE = {"prep": "ENV_PREP_FAIL", "timeout": "ENV_TIMEOUT", "resource": "ENV_RESOURCE_LIMIT",
              "runtime_config": "ENV_RUNTIME_CONFIG", "oom": "ENV_TIMEOUT", "test_reset": "ENV_INFRA_PERSISTENT",
              "error": "ENV_INFRA_PERSISTENT"}
_GRADER = None


def grader(ctx) -> Grader:
    global _GRADER
    if _GRADER is None:
        _GRADER = Grader(ctx.cfg, NAME, ctx.cfg["work_dir"])
    return _GRADER


def cleanup(ctx) -> None:
    if _GRADER is not None:
        _GRADER.docker.cleanup()


def view(ctx, iid: str) -> dict:
    """The task this stage verifies: the FULL PASS_TO_PASS list (this stage decides the drops)."""
    t = dict(ctx.tasks[iid])
    t["mounts"] = test_data_mounts(ctx.cfg, t)
    return t


def key(ctx, iid: str) -> str:
    t = view(ctx, iid)
    return sha_json({"t": [t["test_patch"], t["FAIL_TO_PASS"], t["PASS_TO_PASS"], t["patch"], t["image"],
                           t["base_commit"], t.get("test_cmd")], "settings": ctx.scfg(NAME),
                     "harness": {k: (ctx.cfg.get("harness") or {}).get(k) for k in ("backend", "timeout_s", "network")}})


def status_of(r: dict, part: str, tid: str) -> str:
    d = r.get(part) or {}
    if tid in (d.get("missing_ids") or []):
        return "missing"
    if tid in (d.get("skipped_ids") or []):
        return "skipped"
    if tid in (d.get("failed_ids") or []):
        return "fail"
    return "pass"


def classify(tk: dict, gold: list[dict], empty: list[dict], reps: int = 3, exhausted: list[str] | None = None) -> dict:
    """Verdict from ``reps`` valid gold and empty runs. exhausted: infra reasons of slots that never produced a valid
    run. verdict: kept | delete | probe | pending."""
    if exhausted:
        return {"verdict": "delete", "codes": sorted({INFRA_CODE.get(x, "ENV_INFRA_PERSISTENT") for x in exhausted})}
    if len(gold) < reps or len(empty) < reps:
        return {"verdict": "pending"}
    codes, flags, drop, why = [], [], {}, {}
    if any(r.get("apply") == "fail" for r in gold):
        codes.append("ENV_GOLD_APPLY_FAIL")
    if any(r.get("test_patch_failed") for r in gold + empty):
        codes.append("ENV_TEST_PATCH_FAIL")
    if any(not r.get("tests_started") or not r.get("n_parsed") for r in gold):
        codes.append("ENV_NO_TEST_OUTPUT")
    if codes:
        return {"verdict": "delete", "codes": codes}
    if any(r.get("apply") == "fuzz" for r in gold):
        flags.append("gold_patch_applies_with_fuzz")
    f2p, p2p = tk["FAIL_TO_PASS"], tk["PASS_TO_PASS"]
    flaky_f2p, gold_f2p_fail, empty_pass = [], [], []
    for tid in f2p:
        gp = sum(status_of(r, "f2p", tid) == "pass" for r in gold)
        ep = sum(status_of(r, "f2p", tid) == "pass" for r in empty)
        if 0 < gp < reps or 0 < ep < reps:
            flaky_f2p.append(tid)
        elif gp == 0:
            gold_f2p_fail.append(tid)
        if ep == reps:
            empty_pass.append(tid)
    if f2p and any(all(status_of(r, "f2p", x) == "pass" for x in f2p) for r in empty):
        codes.append("EMPTY_PASSES_ALL_F2P")
    if flaky_f2p:
        codes.append("FLAKY_F2P")
        why["flaky_f2p"] = flaky_f2p[:50]
    gold_p2p_fail = []
    for tid in p2p:
        gs = [status_of(r, "p2p", tid) for r in gold]
        es = [status_of(r, "p2p", tid) for r in empty]
        gp, ep = gs.count("pass"), es.count("pass")
        if gp == reps:
            if 0 < ep < reps:
                drop[tid] = "flaky_empty"
        elif gp > 0:
            drop[tid] = "flaky"
        elif all(s == "missing" for s in gs):
            drop[tid] = "unreported"
        elif all(s in ("missing", "skipped") for s in gs):
            drop[tid] = "skipped"
        elif ep == 0:
            drop[tid] = "fails_without_patch"
        elif ep < reps:
            drop[tid] = "flaky_empty"
        else:
            gold_p2p_fail.append(tid)
    if codes:
        return {"verdict": "delete", "codes": codes, **why}
    if gold_f2p_fail or gold_p2p_fail:
        return {"verdict": "probe", "codes": ["GOLD_F2P_FAIL"] * bool(gold_f2p_fail) + ["GOLD_P2P_FAIL"] * bool(gold_p2p_fail),
                "gold_f2p_fail": gold_f2p_fail[:100], "gold_p2p_fail": gold_p2p_fail[:100], "p2p_dropped": drop}
    if empty_pass:
        flags.append("some_f2p_pass_without_patch")
    if len(drop) >= 10 and len(drop) > 0.5 * max(1, len(p2p)):
        flags.append("many_p2p_dropped")
    hist = {json.dumps((r.get("prep") or {}).get("history"), sort_keys=True) for r in gold + empty}
    return {"verdict": "kept", "codes": [], "flags": flags, "p2p_dropped": drop, "f2p_passes_empty": empty_pass[:100],
            "history": json.loads(sorted(hist)[0]) if hist else None}


def summary(r: dict) -> dict:
    return {k: r.get(k) for k in ("kind", "apply", "resolved", "tests_started", "timeout", "oom", "error", "seconds",
                                  "harness_error", "log")} | {
        "f2p_failed": (r.get("f2p") or {}).get("failed_ids"), "f2p_missing": (r.get("f2p") or {}).get("missing_ids"),
        "p2p_bad": len((r.get("p2p") or {}).get("failed_ids") or []) + len((r.get("p2p") or {}).get("missing_ids") or [])}


def run(ctx, iid: str) -> dict:
    sc = ctx.scfg(NAME)
    reps, retries, harden = int(sc.get("reps", 3)), int(sc.get("max_infra_retries", 2)), int(sc.get("prep_harden", 1))
    t = view(ctx, iid)
    g = grader(ctx)
    runs = {"gold": [], "empty": []}
    exhausted, allruns = [], []
    for kind in ("gold", "empty"):
        for _ in range(reps):
            for attempt in range(1 + retries):
                r = g.run(t, t["patch"] if kind == "gold" else "", kind, harden=harden)
                allruns.append(r)
                why = infra_reason(r)
                if why is None:
                    runs[kind].append(r)
                    break
                if attempt == retries:
                    exhausted.append(why)
            if exhausted:
                break
        if exhausted:
            break
    v = classify(t, runs["gold"], runs["empty"], reps, exhausted)
    out = {"runs": [summary(r) for r in allruns], "classify": {k: v.get(k) for k in v if k != "p2p_dropped"}}
    if v["verdict"] == "probe":
        codes = []
        if sc.get("probe_network"):
            r = g.run(t, t["patch"], "gold-online", harden=harden, network="bridge")
            out["runs"].append(summary(r))
            failing = set(v.get("gold_f2p_fail") or []) | set(v.get("gold_p2p_fail") or [])
            bad = set((r.get("f2p") or {}).get("failed_ids") or []) | set((r.get("p2p") or {}).get("failed_ids") or [])
            codes = ["ENV_NEEDS_NETWORK"] if r.get("tests_started") and not (failing & bad) else []
        codes = codes or (["ENV_GOLD_F2P_FAIL"] if v.get("gold_f2p_fail") else ["ENV_GOLD_P2P_FAIL"])
        return dict(out, decision="drop", reasons=codes, p2p_dropped=v.get("p2p_dropped") or {})
    if v["verdict"] == "delete":
        return dict(out, decision="drop", reasons=v["codes"])
    return dict(out, decision="keep", reasons=[], flags=v.get("flags") or [], p2p_dropped=v.get("p2p_dropped") or {},
                f2p_passes_empty=v.get("f2p_passes_empty"), history=v.get("history"), mounts=t["mounts"])
