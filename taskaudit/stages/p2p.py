"""PASS_TO_PASS audit (the P2P judge, step 4a, and the gold variant, step 4b): does a P2P test that a candidate
patch fails pin old behaviour?

Candidates: patches that pass every F2P test and fail a P2P id that the gold passes: the executed escalation patches
of the task, the difficulty-measurement rollouts (measure stage), and ``stages.p2p.candidates`` (JSONL: instance_id,
patch). A group is (task, P2P id; parametrisations failing under the same patches are one group); its representative
is re-run under the grader with gold / empty controls. The P2P judge (both families, offline REPOSITORY ACCESS tool
loop) judges each confirmed group; both ``pins_old_behaviour`` -> the gold variant (each family writes a variant of
the gold patch, executed): an item iff each family's variant runs "every F2P passes, this P2P fails". route_hint:
scope sibling (both) -> S-keep; same_input (both) -> P2P-relax; a scope disagreement -> S-keep if one says sibling.
``mixed`` from either family -> statement routes only. Both judges ``delete_task`` -> triage rule H9 (the triage
removes the task; reason H9_4A_DELETE_TASK_BOTH). Both ``flake_drop`` -> the id is dropped from grading once 3 + 3
gold / empty runs confirm the flake.
Row: decision keep | flag; items [...]; cases; groups; p2p_dropped (confirmed flakes); h9_delete_task.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from ..harness.grader import Grader, bad_ids, read_log
from ..harness.logs import failure_excerpt
from ..llm.calls import TOOLS_DOC, tool_loop
from ..util import clip_mid, read_jsonl, sha
from . import stage_key

NAME = "p2p"
DEPENDS = ["escalate"]
FAMS = ("claude", "gpt")
VERDICTS = {"pins_old_behaviour", "implementation_detail", "regression", "env_flake", "unclear"}
REPAIRS = {"state_keep", "p2p_relax", "keep", "flake_drop", "delete_task"}
SCOPES = {"same_input", "sibling", "n/a"}
_GR = None


def grader(ctx) -> Grader:
    global _GR
    if _GR is None:
        _GR = Grader(ctx.cfg, NAME, ctx.cfg["work_dir"])
    return _GR


def cleanup(ctx) -> None:
    if _GR is not None:
        _GR.docker.cleanup()


def candidates(ctx, t: dict) -> list[dict]:
    iid, out = t["instance_id"], []
    esc = (ctx.store.get("escalate", iid) or {}).get("escalation") or {}
    for f, x in (esc.get("X") or {}).items():
        if x and not x.get("f2p_failed") and x.get("p2p_failed_new"):
            p = ctx.store.work("escalate", iid) / f / "W" / "out" / f"{'witness' if x.get('role') == 'witness' else 'alternative'}.diff"
            if p.exists():
                out.append({"name": f"esc:{f}:{x.get('role')}", "patch": p.read_text(), "p2p_bad": x["p2p_failed_new"]})
    for r in (ctx.store.get("measure", iid) or {}).get("rollouts") or []:
        if r.get("patch") and not r.get("f2p_bad") and r.get("p2p_bad"):
            out.append({"name": f"rollout:{r.get('name')}", "patch": r["patch"], "p2p_bad": r["p2p_bad"]})
    src = ctx.scfg(NAME).get("candidates")
    for r in (read_jsonl(src) if src else []):
        if r.get("instance_id") == iid and (r.get("patch") or "").strip():
            out.append({"name": f"file:{sha(r['patch'])[:8]}", "patch": r["patch"], "p2p_bad": r.get("p2p_bad")})
    return out


def wants(ctx, iid: str) -> bool:
    return bool(ctx.scfg(NAME).get("enabled"))


def key(ctx, iid: str) -> str:
    t = ctx.effective(iid, with_repair=False, with_p2p=False)
    return stage_key(ctx, NAME, t, "p2p_judge", "p2p_gold_variant",
                     extra=sorted(sha(c["patch"]) for c in candidates(ctx, t)))


def groups(t: dict, cands: list[dict], runs: dict) -> list[dict]:
    eff = set(t["PASS_TO_PASS"])
    g: dict[str, dict] = {}
    for c in cands:
        r = runs[sha(c["patch"])]
        f2p_bad, p2p_bad = bad_ids(r)
        if f2p_bad or r.get("harness_error") or not r.get("tests_started"):
            continue
        for pid in p2p_bad:
            if pid in eff:
                e = g.setdefault(pid, {"p2p": pid, "patches": {}})
                e["patches"].setdefault(sha(c["patch"]), {"patch": c["patch"], "others": len(p2p_bad) - 1, "n": 0})
                e["patches"][sha(c["patch"])]["n"] += 1
    clusters: dict[tuple, list[str]] = {}
    for pid, e in g.items():
        clusters.setdefault((pid.split("[")[0], tuple(sorted(e["patches"]))), []).append(pid)
    out = []
    for _, pids in clusters.items():
        pids = sorted(pids)
        e = g[pids[0]]
        h, rep = min(e["patches"].items(), key=lambda kv: (kv[1]["others"], -kv[1]["n"], kv[0]))
        out.append({"p2p": pids[0], "p2p_ids": pids, "rep_sha256": h, "rep_patch": rep["patch"],
                    "n_patches": len(e["patches"])})
    return sorted(out, key=lambda x: (-x["n_patches"], x["p2p"]))


def id_status(run: dict, pid: str) -> str:
    if run.get("harness_error") or run.get("apply") == "fail" or not run.get("tests_started"):
        return "run error: " + str(run.get("harness_error") or ("apply failed" if run.get("apply") == "fail"
                                                                else "tests did not start"))
    return "FAILED" if pid in bad_ids(run)[1] else "passed"


def source_part(patch: str) -> tuple[str, list[str]]:
    from ..harness._guards import TEST_PATH
    from ..gates.g1 import split_patch
    keep, drop = [], []
    for p, d in split_patch(patch).items():
        (drop if TEST_PATH.search(p) else keep).append(p if TEST_PATH.search(p) else d)
    return "".join(keep), drop


def user_4a(ctx, t: dict, c: dict, run: dict, ctl: dict) -> str:
    src, dropped = source_part(c["rep_patch"])
    others = sorted(set(bad_ids(run)[1]) - {c["p2p"]})
    st = id_status(run, c["p2p"])
    fail_txt = clip_mid(failure_excerpt(read_log(run.get("log")), c["p2p"]) or "[failure block not located in the log]",
                        9000) if st == "FAILED" else f"[re-run: {st}; no failure output]"
    f2p_bad = bad_ids(run)[0]
    f2p_line = f"F2P on the re-run: {len(t['FAIL_TO_PASS']) - len(f2p_bad)} passed, {len(f2p_bad)} failed or missing"
    disc = (t.get("hints_text") or "").strip()
    parts = [
        f"# TASK {t['instance_id']} ({t['repo']}, base commit {t['base_commit']})",
        "## Statement as shown to the solver (issue title + body" + (", followed by the appended specification)"
                                                                       if t.get("spec_block") else ")"),
        t["statement"],
        "## Discussion (hints; NOT shown to the solver)", clip_mid(disc or "(none)", 6000),
        "## Reference fix (gold patch; passes every graded test)", clip_mid(t["patch"], 30000),
        "## Candidate patch (passes every F2P test; fails the P2P test below)",
        ("Test-file changes of the candidate are not shown: the grader puts the patch's test files back before "
         "testing" + (f" (dropped: {', '.join(dropped)})" if dropped else "") + "."),
        clip_mid(src, 30000) or "(no source changes)",
        "## The failing P2P test", f"id: {c['p2p']}",
        "other P2P ids this candidate also fails: " + (", ".join(others[:30]) + (" ..." if len(others) > 30 else "")
                                                        if others else "none"),
        "### Test source (after the upstream test patch)", ctx.sources(t).sources_of([c["p2p"]]),
        "### Failure output of this candidate (re-run under the current grader)", f2p_line, fail_txt,
        "### The same test id on the original code and with the reference fix (re-run under the current grader)",
        f"original code (no patch): {id_status(ctl['empty'], c['p2p'])}; reference fix: {id_status(ctl['gold'], c['p2p'])}",
        "## Code-evidence pack (mechanical, base commit)", ctx.fn_pack(t)["text"],
        "## REPOSITORY ACCESS", TOOLS_DOC]
    return "\n\n".join(parts)


def validate_4a(o):
    if not isinstance(o, dict) or o.get("verdict") not in VERDICTS or o.get("repair") not in REPAIRS:
        return None
    if o.get("scope") not in SCOPES or not isinstance(o.get("statement_support"), dict):
        return None
    if o["verdict"] == "pins_old_behaviour" and o["scope"] not in ("same_input", "sibling"):
        return None
    return o


def validate_4b(o):
    if not isinstance(o, dict) or not isinstance(o.get("feasible"), bool):
        return None
    if o["feasible"] and not (isinstance(o.get("edits"), list) and o["edits"]):
        return None
    return o


def build_variant(ctx, t: dict, edits: list) -> tuple[str, str | None]:
    import difflib
    src = ctx.sources(t)
    files = dict(src.gold["files"])
    old_files = {}
    errs = []
    for e in edits:
        path = str(e.get("path", "")).lstrip("/")
        old, new = e.get("old", ""), e.get("new", "")
        if path not in files:
            files[path] = ctx.repos.base_file(t["repo"], t["base_commit"], path)
        old_files.setdefault(path, ctx.repos.base_file(t["repo"], t["base_commit"], path))
        cur = files.get(path)
        if old == "" and cur is None:
            files[path] = new
        elif cur is None:
            errs.append(f"{path}: no such file")
        elif cur.count(old) != 1:
            errs.append(f"{path}: snippet found {cur.count(old)} times")
        else:
            files[path] = cur.replace(old, new, 1)
    if errs:
        return "", "; ".join(errs)
    for p in src.gold["files"]:
        old_files.setdefault(p, ctx.repos.base_file(t["repo"], t["base_commit"], p))
    out = []
    for p in sorted(files):
        old, new = old_files.get(p), files[p]
        if old == new or new is None:
            continue
        a, b = (old or "").splitlines(keepends=True), new.splitlines(keepends=True)
        for L in (a, b):
            if L and not L[-1].endswith("\n"):
                L[-1] += "\n"
        hdr = f"diff --git a/{p} b/{p}\n" + ("new file mode 100644\n" if old is None else "")
        out.append(hdr + "".join(difflib.unified_diff(a, b, "/dev/null" if old is None else f"a/{p}", f"b/{p}", n=3)))
    return "".join(out), None


def run(ctx, iid: str) -> dict:
    t = ctx.effective(iid, with_repair=False, with_p2p=False)
    g = grader(ctx)
    cands = candidates(ctx, t)
    runs = {}
    for c in cands:
        h = sha(c["patch"])
        if h not in runs:
            runs[h] = g.run(t, c["patch"], f"p-{h[:10]}")
    gs = groups(t, cands, runs)[: int(ctx.scfg(NAME).get("max_groups", 8))]
    if not gs:
        return {"decision": "keep", "reasons": [], "items": [], "groups": 0}
    with ThreadPoolExecutor(2) as ex:
        fg, fe = ex.submit(g.run, t, t["patch"], "gold"), ex.submit(g.run, t, "", "empty")
        ctl = {"gold": fg.result(), "empty": fe.result()}
    items, cases, drops, h9, dry = [], [], {}, False, False
    src = ctx.sources(t)
    for c in gs:
        run_ = runs[c["rep_sha256"]]
        if id_status(ctl["gold"], c["p2p"]) != "passed":
            cases.append({"p2p": c["p2p"], "outcome": "gold_fails"})
            continue
        sysp, user = ctx.prompts.text("p2p_judge"), user_4a(ctx, t, c, run_, ctl)
        with ThreadPoolExecutor(2) as ex:
            J = dict(zip(FAMS, ex.map(lambda f: tool_loop(ctx.llm, f, f"4a:{sha(c['p2p'])[:8]}", sysp, user,
                                                          validate_4a, src.answer), FAMS)))
        if any(J[f].get("dry_run") for f in FAMS):
            dry = True
            continue
        v = {f: (J[f].get("parsed") or {}) for f in FAMS}
        case = {"p2p": c["p2p"], "p2p_ids": c["p2p_ids"], "verdicts": {f: v[f].get("verdict") for f in FAMS},
                "repairs": {f: v[f].get("repair") for f in FAMS}, "scopes": {f: v[f].get("scope") for f in FAMS}}
        if all(v[f].get("repair") == "delete_task" for f in FAMS):
            h9 = True
        if all(v[f].get("repair") == "flake_drop" for f in FAMS):
            reps = [g.run(t, t["patch"] if k == "gold" else "", k) for k in ("gold", "empty") for _ in range(3)]
            st = [id_status(r, c["p2p"]) for r in reps]
            if len(set(st[:3])) > 1 or len(set(st[3:])) > 1:
                for pid in c["p2p_ids"]:
                    drops[pid] = "flaky_4a"
            case["outcome"] = "flake_drop" if c["p2p"] in drops else "flake_not_confirmed"
        if all(v[f].get("verdict") == "pins_old_behaviour" for f in FAMS):
            claims = [f"- reviewer {i + 1}: {v[f]['verdict']} (scope {v[f].get('scope')}): {v[f].get('asserted_behaviour')}"
                      f" / candidate: {v[f].get('candidate_behaviour')} / support: "
                      f"{json.dumps(v[f].get('statement_support'))[:600]}" for i, f in enumerate(FAMS)]
            vuser = user.split("## REPOSITORY ACCESS")[0] + "## Reviewer claim(s) to test\n" + "\n".join(claims) + \
                "\n\n## REPOSITORY ACCESS\n" + TOOLS_DOC
            vsys = ctx.prompts.text("p2p_gold_variant")
            with ThreadPoolExecutor(2) as ex:
                V = dict(zip(FAMS, ex.map(lambda f: tool_loop(ctx.llm, f, f"4b:{sha(c['p2p'])[:8]}", vsys, vuser,
                                                              validate_4b, src.answer), FAMS)))
            ok = {}
            for f in FAMS:
                pv = V[f].get("parsed") or {}
                if not pv.get("feasible"):
                    ok[f] = False
                    continue
                patch, err = build_variant(ctx, t, pv.get("edits") or [])
                vr = g.run(t, patch, f"variant-{f}") if not err else {}
                fb, pb = bad_ids(vr) if vr else ([], [])
                ok[f] = bool(vr) and vr.get("tests_started") and not vr.get("harness_error") and not fb and c["p2p"] in pb
            case["variants_fail_p2p"] = ok
            if all(ok.values()):
                sc = set(case["scopes"].values())
                hint = "S-keep" if sc == {"sibling"} else "P2P-relax" if sc == {"same_input"} else \
                    "S-keep" if "sibling" in sc else None
                mixed = any(v[f].get("mixed") for f in FAMS)
                items.append({"p2p": c["p2p"], "p2p_ids": c["p2p_ids"], "route_hint": hint, "scopes": case["scopes"],
                              "mixed": mixed, "restriction_statement_only": mixed or hint == "S-keep" and len(sc) > 1,
                              "candidate_sha256": c["rep_sha256"],
                              "asserted_behaviour": v["claude"].get("asserted_behaviour"),
                              "repair_note": {f: v[f].get("repair_note") for f in FAMS},
                              "statement_support": {f: v[f].get("statement_support") for f in FAMS}})
                case["outcome"] = "item"
        cases.append(case)
    if dry:
        return {"complete": False, "decision": "pending", "reasons": ["dry_run"]}
    return {"decision": "flag" if items or h9 else "keep", "reasons": ["H9_4A_DELETE_TASK_BOTH"] if h9 else [],
            "items": items, "cases": cases, "groups": len(gs), "p2p_dropped": drops, "h9_delete_task": h9}
