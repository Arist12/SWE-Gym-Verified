"""Escalation (witness, step 3b -> execution -> review, 3c -> debate, 3d), with coding agents of both families in
the task container, and the per-task FN verdict.

Which tasks: fn_rate ESCALATE -> mode ``full`` (full escalation); FN_BOTH -> mode ``witness`` (witness mode, W and X
only: the FN stands, both families' witnesses are built and executed); optionally a residual-FN audit sample of OK
tasks (``stages.escalate.audit_ids``, or ``audit_n`` drawn in sha256("<audit_seed>:<id>") order) -> mode ``full``.

Per task (the letters are the keys of the escalation record and the names of the agents' work directories):
G (gold check: the gold patch graded offline; failing -> ENV_NEEDS_NETWORK, the task is removed), the final
environment image (PREP: single-commit history, the project's copies removed), W (witness round: both families write
and run a witness, or their strongest alternative), X (execution: every patch graded against the gold control),
R (review: each family reviews the executed patches), D (debate: one round, each family sees the other's outputs).
Resolution: CONFIRMED iff both families' final verdicts are FN_CONFIRMED on a common executed patch, and when the
confirmation rests on a regression guard both name the same failed test with the same basis; NOT_FN iff both NOT_FN;
anything else CONTESTED. A family whose stage stayed void (command audit) is UNSURE -> CONTESTED. A full escalation
cut short (task cap, a debate without output) is pending, never CONTESTED.
Verdict: OK (fn_rate OK, or NOT_FN), FN_CONFIRMED (CONFIRMED, or witness mode), CONTESTED.
``fn_evidence`` (the evidence behind an FN verdict): ``cross_reviewed`` = CONFIRMED after full escalation;
``two_witnesses`` = witness mode with both families' witnesses executed and failing a common F2P test;
``other_confirmed`` = any other confirmed verdict; ``contested`` = a CONTESTED verdict.
``audit_path`` (how the verdict was reached): ``cleared_by_rating`` (fn_rate OK, no execution), ``witness_mode``,
``full_escalation``, ``residual_audit`` (an OK task of the audit sample).
Row: decision OK | FN_CONFIRMED | CONTESTED | drop; reasons; mode; audit_path; fn_evidence; escalation (the record:
resolution, G, W, X, R, D summaries, work dir).
"""
from __future__ import annotations

import json
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..agents.runner import AgentRunner, run_tests_script
from ..harness import _guards as G
from ..harness.evalscript import backend_of, eval_script
from ..harness.grader import Grader, read_log
from ..harness.logs import excerpt_for, test_blocks
from ..util import fence, sha, sha_json
from . import stage_key

NAME = "escalate"
DEPENDS = ["fn_rate"]
FAMS = ("claude", "gpt")
LABEL = {"claude": "W-A", "gpt": "W-B"}  # neutral patch labels (no model names in prompts)
LETTER = {"claude": "A", "gpt": "B"}
OTHER = {"claude": "gpt", "gpt": "claude"}
STEP = {"W": "witness", "R": "review", "D": "debate"}
COUNTS = ("issue_asks", "sibling_defect")  # REGRESSION-GUARD RULE (a) / (b): a guard failure counts
WHY = {"disagree": "Two earlier independent readers disagreed on whether this task has an FN.",
       "fn1": "An earlier reader judged it borderline: only unusual or unreasonable solutions fail (fn_with_code = 1).",
       "unresolved": "An earlier reader left open a fact that could decide whether this task has an FN.",
       "guard": "An earlier reader's judgement rests on a check that already passes on the original code.",
       "rater_invalid": "An earlier reader did not return a valid assessment.",
       "pack_degraded": "The code excerpt prepared for earlier readers had to leave out part of the relevant code.",
       "both_fn": "Two earlier independent readers judged that a reasonable solution fails the graded tests.",
       "audit": "It was drawn at random for an audit.",
       "student": "A student solver who saw only the task statement wrote a patch that fails a graded test the same way "
                  "in at least two of its eight attempts."}
BIG = ("@@PACKET@@", "@@DISCUSSION@@", "@@PACK@@")
ROLE_TXT = {"witness": "witness: its author claims an FN", "alternative": "strongest alternative: its author claims no FN",
            "student": "a student solver's patch (the student saw only the task statement); it is put forward as a "
                       "candidate solution that the graded tests may wrongly reject"}
GOLD_RETRY_S = (30, 120)
SHM_BUS = ("insufficient shared memory (shm)", "Unexpected bus error encountered in worker")
_GR: dict = {}


def why_text(reasons: list[str]) -> str:
    return "This task was escalated for a closer look. " + " ".join(WHY[r] for r in reasons if r in WHY)


def grader(ctx, ns: str = NAME) -> Grader:
    if ns not in _GR:
        _GR[ns] = Grader(ctx.cfg, ns, ctx.cfg["work_dir"])
    return _GR[ns]


def cleanup(ctx) -> None:
    for g in _GR.values():
        g.docker.cleanup()


# ------------------------------------------------------------------ which tasks, which mode
def audit_ids(ctx) -> set[str]:
    sc = ctx.scfg(NAME)
    if sc.get("audit_ids"):
        return set(sc["audit_ids"])
    n = int(sc.get("audit_n") or 0)
    if not n:
        return set()
    seed = sc.get("audit_seed") or "fn-audit"
    ok = [i for i in ctx.tasks if (ctx.store.get("fn_rate", i) or {}).get("decision") == "OK" and not ctx.removed(i, NAME)]
    return set(sorted(ok, key=lambda i: sha(f"{seed}:{i}"))[:n])


def mode_of(ctx, iid: str, rate: dict | None = None) -> tuple[str | None, list[str]]:
    r = rate if rate is not None else (ctx.store.get("fn_rate", iid) or {})
    d = r.get("decision")
    if d == "ESCALATE":
        return "full", list(r.get("reasons") or [])
    if d == "FN_BOTH":
        return "witness", ["both_fn"]
    if d == "OK" and iid in audit_ids(ctx):
        return "full", ["audit"]
    return None, []


def key(ctx, iid: str) -> str:
    t = ctx.effective(iid, with_repair=False)
    mode, reasons = mode_of(ctx, iid)
    rate = ctx.store.get("fn_rate", iid) or {}
    return stage_key(ctx, NAME, t, "witness", "review", "debate",
                     extra={"mode": mode, "reasons": reasons, "rate": rate.get("key"),
                            "agents": {f: (ctx.cfg.get("agents") or {}).get(f, {}).get("model") for f in FAMS}})


# ------------------------------------------------------------------ inputs
def packet(t: dict) -> str:
    f2p = list(t["FAIL_TO_PASS"])
    return "\n".join([
        f"# {t['instance_id']}", "", f"- Repository: {t['repo']}", f"- Base commit: {t['base_commit']}",
        f"- Docker image: `{t.get('image')}` (recorded digest `{t.get('image_digest') or 'unknown'}`; the repository is "
        f"at /testbed, checked out at the base commit)", "",
        "## Issue (exactly as the solver sees it)", "", fence(t["statement"].replace("\r\n", "\n"), "text"), "",
        f"## FAIL_TO_PASS tests ({len(f2p)})", "", fence("\n".join(f2p), "text"), "",
        "## Test patch", "", fence(t["test_patch"], "diff"), "",
        "## Gold patch (reference only: the solver never sees it)", "", fence(t["patch"], "diff"), ""])


def discussion(t: dict) -> str:
    return (t.get("hints_text") or "").replace("\r\n", "\n").strip()


def render(ctx, step: str, t: dict, fam: str, sub: dict, reasons: list[str]) -> str:
    tpl = ctx.prompts.text(STEP[step])
    disc = discussion(t)
    subs = {"@@TASK@@": t["instance_id"], "@@WHY@@": why_text(reasons), "@@OWN_LABEL@@": LABEL[fam],
            "@@OTHER_LABEL@@": LABEL[OTHER[fam]], **sub, "@@PACKET@@": packet(t).strip(),
            "@@PACK@@": ctx.fn_pack(t)["text"].strip(),
            "@@DISCUSSION@@": fence(disc, "text") if disc else "(empty: no discussion is recorded for this issue)"}
    for k in BIG:  # big inputs last so that no template token inside them is substituted
        tpl = tpl.replace(k, f"\x00{k}\x00")
    for k, v in subs.items():
        if k not in BIG:
            tpl = tpl.replace(k, v)
    for k in BIG:
        tpl = tpl.replace(f"\x00{k}\x00", subs[k])
    left = re.findall(r"@@[A-Z_]+@@", re.sub(r"(?s)=== TASK PACKET ===.*", "", tpl))
    assert not left, f"unfilled placeholder in {step}: {left}"
    return tpl


def prepare_in(ctx, t: dict, wd: Path) -> Path:
    d = wd / "in_w"
    d.mkdir(parents=True, exist_ok=True)
    (d / "test.diff").write_text(t["test_patch"])
    (d / "gold.diff").write_text(t["patch"])
    (d / "f2p.txt").write_text("\n".join(t["FAIL_TO_PASS"]) + "\n")
    (d / "p2p.txt").write_text("\n".join(t["PASS_TO_PASS"]) + "\n")
    env = {**G.TASK_ENV, **((ctx.cfg.get("harness") or {}).get("env") or {})}
    (d / "run_tests.sh").write_text(run_tests_script(eval_script(t, "", t["patch"], backend_of(ctx.cfg, t)), env))
    return d


# ------------------------------------------------------------------ checks of agent outputs
def _qnorm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("`", "").replace("’", "'").replace("‘", "'")
                  .replace("“", '"').replace("”", '"')).strip().lower()


def quote_in(quote: str, src: str) -> bool:
    q, s = _qnorm(quote).strip("\"'“”"), _qnorm(src)
    pieces = [x.strip(" \"'“”") for x in re.split(r"\.\.\.|…|\[\.\.\.\]", q)]
    pieces = [x for x in pieces if len(x) >= 12] or [q]
    return all(x in s for x in pieces)


def check(step: str, out: dict | None, wd: Path, disc: str, patches=None) -> tuple[list[str], list[str]]:
    """(hard flags -> retry, soft flags -> recorded)."""
    if out is None:
        return ["no_valid_output"], []
    hard, soft = [], []
    if step == "W":
        fn = out.get("fn_with_code") or 0
        alt = out.get("strongest_alternative")
        if out.get("claim") == "FN":
            w = wd / "out" / "witness.diff"
            if out.get("witness") is None:
                hard.append("FN_without_witness_object")
            if not w.exists() or not w.read_text().strip():
                hard.append("FN_without_witness_file")
            if fn < 2:
                hard.append("FN_with_fn<2")
            if alt is not None:
                soft.append("FN_with_strongest_alternative")
            wo = out.get("witness") or {}
            if wo.get("check_passes_at_base") == "yes" and wo.get("guard_basis") not in COUNTS:
                soft.append("FN_on_regression_guard_without_basis")
        else:
            a = wd / "out" / "alternative.diff"
            if out.get("witness") is not None:
                hard.append("NOT_FN_with_witness")
            if fn >= 2:
                hard.append("NOT_FN_with_fn>=2")
            if alt is None:
                hard.append("NOT_FN_without_strongest_alternative")
            elif alt.get("exists"):
                if not a.exists() or not a.read_text().strip():
                    hard.append("alternative_without_file")
                if alt.get("why_not_fn") == "none_exists":
                    soft.append("alternative_exists_but_none_exists")
            else:
                if not (alt.get("no_alternative_reason") or "").strip():
                    hard.append("no_alternative_without_reason")
                if a.exists() and a.read_text().strip():
                    soft.append("no_alternative_but_file_written")
        for r in out.get("assertions") or []:
            sd, dq = r.get("specified_in_discussion"), r.get("discussion_quote")
            if r.get("status") == "UNDERDETERMINED":
                if sd not in ("yes", "no"):
                    soft.append(f"{r.get('id')}: specified_in_discussion missing")
                elif sd == "yes" and (not disc or not dq or not quote_in(dq, disc)):
                    soft.append(f"{r.get('id')}: discussion_quote not verbatim")
            elif sd is not None:
                soft.append(f"{r.get('id')}: DETERMINED row with specified_in_discussion")
            if isinstance(r, dict) and r.get("passes_at_base") not in ("yes", "no"):
                soft.append(f"{r.get('id')}: passes_at_base missing")
    else:
        v, s = out.get("verdict"), out.get("fn_confirmed_witnesses") or []
        if v == "FN_CONFIRMED" and not s:
            hard.append("confirmed_without_witness")
        if v != "FN_CONFIRMED" and s:
            hard.append("witnesses_without_confirmed")
        if patches is not None and any(x not in {LABEL[f] for f in patches} for x in s):
            hard.append("confirmed_nonexistent_patch")
        if v == "FN_CONFIRMED":  # a confirmation on a regression guard must name basis (a) or (b)
            if step == "D":
                if out.get("check_passes_at_base") == "yes" and out.get("guard_basis") not in COUNTS:
                    hard.append("confirmed_on_regression_guard_without_basis")
            else:
                for w in out.get("witnesses") or []:
                    if (w.get("witness") in s and w.get("check_passes_at_base") == "yes"
                            and w.get("guard_basis") not in COUNTS):
                        hard.append(f"{w.get('witness')}: confirmed_on_regression_guard_without_basis")
        sd, dq = out.get("specified_in_discussion"), out.get("discussion_quote")
        if v == "FN_CONFIRMED" and sd not in ("yes", "no"):
            soft.append("confirmed_without_specified_in_discussion")
        if sd == "yes" and (not disc or not dq or not quote_in(dq, disc)):
            soft.append("discussion_quote not verbatim")
    return hard, soft


# ------------------------------------------------------------------ execution
def summarize(r: dict, gold_f2p_bad: set, gold_p2p_bad: set) -> dict:
    f2p, p2p = r.get("f2p") or {}, r.get("p2p") or {}
    f2p_bad = sorted(set(f2p.get("failed_ids") or []) | set(f2p.get("missing_ids") or []))
    p2p_bad = sorted(set(p2p.get("failed_ids") or []) | set(p2p.get("missing_ids") or []))
    tests = G.split_tests(read_log(r.get("log")))[1] or ""
    blocks = test_blocks(tests)
    summary = [l for l in tests.splitlines() if l.startswith(("FAILED ", "ERROR "))]
    attributable = [x for x in f2p_bad if x not in gold_f2p_bad]
    new_p2p = [x for x in p2p_bad if x not in gold_p2p_bad]
    return {"kind": r.get("kind"), "applied": r.get("apply"),
            "error": r.get("error"), "timeout": r.get("timeout"), "oom": r.get("oom"),
            "tests_started": r.get("tests_started"), "test_patch_failed": r.get("test_patch_failed"),
            "test_reset": r.get("test_reset"), "seconds": r.get("seconds"), "resolved": r.get("resolved"),
            "f2p_total": (f2p.get("pass") or 0) + (f2p.get("fail") or 0) + (f2p.get("missing") or 0),
            "f2p_failed": f2p_bad, "f2p_failed_attributable": attributable,
            "p2p_total": (p2p.get("pass") or 0) + (p2p.get("fail") or 0) + (p2p.get("missing") or 0),
            "p2p_failed": p2p_bad, "p2p_failed_new": new_p2p,
            "excerpts": {x: excerpt_for(x, blocks, summary) for x in attributable[:12]}, "log": r.get("log")}


def gold_infra(g: dict) -> list[str]:
    s = g.get("summary") or {}
    out = [x for x in g.get("reasons") or [] if x == "timeout" or x.startswith("error: ")]
    if s.get("oom"):
        out.append("oom")
    if not out and s.get("f2p_failed") and any(x in read_log(s.get("log")) for x in SHM_BUS):
        out.append("shm bus error (a test worker died of the container's /dev/shm limit)")
    return out


def gold_precheck(ctx, t: dict, wd: Path, ns: str) -> dict | None:
    """G: the gold patch with the grader, offline. ok iff it applies, the tests run and every F2P test passes. A
    failure that is infrastructure only is retried, then the task stays pending (None), never ENV_NEEDS_NETWORK."""
    cache = wd / "gold.json"
    gk = sha_json([eval_script(t, t["patch"], t["patch"], backend_of(ctx.cfg, t)), t.get("mounts")])
    if cache.exists():
        c = json.loads(cache.read_text())
        if c.get("key") == gk:
            return c
    waits = GOLD_RETRY_S
    for k in range(len(waits) + 1):
        s = summarize(grader(ctx, ns).run(t, t["patch"], "gold"), set(), set())
        reasons = []
        if s["applied"] in (None, "fail"):
            reasons.append(f"gold did not apply ({s['applied']})")
        if s.get("error"):
            reasons.append(f"error: {s['error']}")
        if s.get("timeout"):
            reasons.append("timeout")
        if s.get("test_patch_failed"):
            reasons.append("test patch did not apply")
        if not s.get("tests_started"):
            reasons.append("tests did not start")
        if s["f2p_failed"]:
            reasons.append(f"F2P failing offline: {len(s['f2p_failed'])}/{s['f2p_total']}")
        g = {"ok": not reasons, "reasons": reasons, "summary": s, "key": gk}
        if g["ok"] or not gold_infra(g):
            cache.write_text(json.dumps(g, indent=1))
            return g
        if k < len(waits):
            time.sleep(waits[k])
    return None


def execute(ctx, t: dict, patches: dict, gold: dict, wd: Path, ns: str) -> dict:
    cache = wd / "exec.json"
    k = sha_json([gold["key"], sorted((f, role, sha(p)) for f, (role, p) in patches.items())])
    if cache.exists():
        c = json.loads(cache.read_text())
        if c.get("key") == k:
            return c
    with ThreadPoolExecutor(max(1, len(patches))) as ex:
        res = dict(zip(patches, ex.map(lambda f: grader(ctx, ns).run(t, patches[f][1], f"gold-w{LETTER[f]}"),
                                       list(patches))))
    g = gold["summary"]
    out = {"key": k, "gold": g, "witness": {}}
    for f, (role, _) in patches.items():
        out["witness"][f] = summarize(res[f], set(g["f2p_failed"]), set(g["p2p_failed"])) | {"role": role}
    cache.write_text(json.dumps(out, indent=1))
    return out


def no_patch_txt(W: dict, f: str) -> str:
    o = (W.get(f) or {}).get("output") or {}
    if o.get("claim") == "NOT_FN" and (o.get("strongest_alternative") or {}).get("exists") is False:
        return "none: that auditor claimed no FN and stated that no reasonable alternative to the gold exists"
    return "none: that auditor wrote no usable patch"


def exec_text(ex: dict, patches: dict, W: dict) -> str:
    g = ex["gold"]
    lines = [f"GOLD CONTROL (run offline before the witness round): applied={g['applied']}; F2P failing "
             f"{len(g['f2p_failed'])}/{g['f2p_total']}; PASS_TO_PASS failing {len(g['p2p_failed'])}/{g['p2p_total']}"
             f"{' (discounted for the patches): ' + str(g['p2p_failed'][:20]) if g['p2p_failed'] else ''}"]
    for f in FAMS:
        lab = LABEL[f]
        if f not in patches:
            lines.append(f"\n{lab}: {no_patch_txt(W, f)}.")
            continue
        s = ex["witness"][f]
        head = f"\n{lab} ({ROLE_TXT[s['role']]})"
        if s["applied"] in (None, "fail") or s.get("error"):
            lines.append(f"{head}: did NOT execute: applied={s['applied']} error={s.get('error')}")
            continue
        lines.append(f"{head}: applied={s['applied']}; tests_started={s['tests_started']}; timeout={bool(s.get('timeout'))}"
                     f"{'; test files the patch touched were put back: ' + str(s['test_reset']) if s.get('test_reset') else ''}")
        lines.append(f"  F2P tests failing with this patch and passing with the gold: {len(s['f2p_failed_attributable'])}/"
                     f"{s['f2p_total']}{': ' + str(s['f2p_failed_attributable']) if s['f2p_failed_attributable'] else ''}")
        for x, e in s["excerpts"].items():
            lines.append(f"  -- failure of {x}:\n{e}")
        lines.append(f"  PASS_TO_PASS newly failing (passing with the gold): {len(s['p2p_failed_new'])}/{s['p2p_total']}"
                     f"{': ' + str(s['p2p_failed_new'][:30]) if s['p2p_failed_new'] else ''}")
    return "\n".join(lines)


def diffs_text(patches: dict, W: dict) -> str:
    out = []
    for f in FAMS:
        lab = LABEL[f]
        if f not in patches:
            out.append(f"--- {lab}: {no_patch_txt(W, f)}")
            continue
        role, p = patches[f]
        d = p.splitlines()
        body = "\n".join(d[:500]) + (f"\n... ({len(d) - 500} more lines in /in/witness_{LETTER[f]}.diff)" if len(d) > 500 else "")
        out.append(f"--- {lab} ({ROLE_TXT[role]}; {len(d)} lines):\n{body}")
    return "\n\n".join(out)


def own_text(row: dict | None) -> str:
    if row is None:
        return "(none)"
    o = row.get("output")
    return json.dumps(o, indent=1) if o is not None else "(no valid output in that round)"


def w_claim(row) -> str:
    o = (row or {}).get("output") or {}
    if o.get("claim") == "FN":
        return "FN" if not row["flags"] else "FN_UNWITNESSED"
    if o.get("claim") == "NOT_FN":
        alt = o.get("strongest_alternative") or {}
        return "NOT_FN" + ("" if alt.get("exists") else "(no_alt)") + ("" if not row["flags"] else "!")
    return "NO_OUTPUT"


def same_test(x: str, y: str) -> bool:
    x, y = (x or "").strip(), (y or "").strip()
    if not x or not y:
        return False
    return x == y or x.endswith(y) or y.endswith(x) or x.split("::")[-1] == y.split("::")[-1]


def guard_agreement(oa: dict, ob: dict) -> tuple[bool, str]:
    """If either final confirmation rests on a check that passes at base, both must name the same failed test as such
    a guard with the same basis (a) or (b)."""
    ga, gb = oa.get("check_passes_at_base") == "yes", ob.get("check_passes_at_base") == "yes"
    if not (ga or gb):
        return True, "no regression guard"
    if not (ga and gb):
        return False, "only one family confirmed on a regression guard"
    if not same_test(oa.get("failed_test"), ob.get("failed_test")):
        return False, "the families confirmed on different failed tests"
    if oa.get("guard_basis") not in COUNTS or ob.get("guard_basis") not in COUNTS:
        return False, "a family gave no basis (a)/(b) for the regression guard"
    if oa.get("guard_basis") != ob.get("guard_basis"):
        return False, f"the families gave different bases: {oa.get('guard_basis')} / {ob.get('guard_basis')}"
    return True, f"both: {oa.get('guard_basis')} / {ob.get('guard_basis')}"


def resolve(va, sa, vb, sb, oa=None, ob=None) -> str:
    if va == vb == "FN_CONFIRMED" and set(sa) & set(sb):
        return "CONFIRMED" if guard_agreement(oa or {}, ob or {})[0] else "CONTESTED"
    if va == vb == "NOT_FN":
        return "NOT_FN"
    return "CONTESTED"


# ------------------------------------------------------------------ one task
def escalate(ctx, t: dict, mode: str, reasons: list[str], ns: str = NAME, student: dict | None = None) -> dict:
    """G, W, X (+ R, D in mode full; R, D only in mode student) on a task view. Returns the escalation record."""
    iid = t["instance_id"]
    wd = ctx.store.work(ns, iid)
    runner = AgentRunner(ctx.cfg, ns)
    deadline = time.time() + float((ctx.cfg.get("agents") or {}).get("task_cap_s") or 3600)
    disc = discussion(t)
    in_w = prepare_in(ctx, t, wd)
    rec: dict = {"mode": mode, "reasons": reasons, "work": str(wd),
                 "prompts": ctx.prompts.shas("witness", "review", "debate")}
    gold = gold_precheck(ctx, t, wd, ns)
    if gold is None:
        return dict(rec, resolution=None, incomplete=True, infra="gold precheck: infrastructure failure")
    rec["G"] = {k: gold[k] for k in ("ok", "reasons")} | {"f2p_failed": gold["summary"]["f2p_failed"],
                                                         "p2p_failed": gold["summary"]["p2p_failed"][:50]}
    if not gold["ok"]:
        return dict(rec, resolution="ENV_NEEDS_NETWORK")
    if runner.dry:
        img = {"image": "(dry run: not built)"}
    else:
        img = runner.final_image(t)
    rec["agent_env"] = img
    try:
        def stage(fam, step, prompt, in_dir, patches=None):
            return runner.stage(t, fam, step, prompt, in_dir, ctx.prompts.schema_path(STEP[step]), wd / fam / step,
                                img["image"], lambda out, d: check(step, out, d, disc, patches), deadline)

        if student is None:
            with ThreadPoolExecutor(2) as ex:
                W = dict(zip(FAMS, ex.map(lambda f: stage(f, "W", render(ctx, "W", t, f, {}, reasons), in_w), FAMS)))
            if any(W[f].get("dry_run") for f in FAMS):
                return dict(rec, resolution=None, incomplete=True, dry_run=True)
            patches = {}
            for f in FAMS:
                o = W[f].get("output") or {}
                if o.get("claim") == "FN":
                    pf, role = wd / f / "W" / "out" / "witness.diff", "witness"
                elif o.get("claim") == "NOT_FN" and (o.get("strongest_alternative") or {}).get("exists"):
                    pf, role = wd / f / "W" / "out" / "alternative.diff", "alternative"
                else:
                    continue
                if pf.exists() and pf.read_text().strip():
                    patches[f] = (role, pf.read_text())
        else:
            W = {"claude": {"output": None}, "gpt": {"output": None}}
            patches = {"claude": ("student", student["patch"])}
        in_d = wd / "in_d"
        if in_d.exists():
            shutil.rmtree(in_d)
        shutil.copytree(in_w, in_d)
        (in_d / "exec_gold.log").write_text(read_log(gold["summary"].get("log")))
        ex_res = None
        if patches:
            ex_res = execute(ctx, t, patches, gold, wd, ns)
            for f, (_, p) in patches.items():
                (in_d / f"witness_{LETTER[f]}.diff").write_text(p)
                (in_d / f"exec_{LETTER[f]}.log").write_text(read_log(ex_res["witness"][f].get("log")))
        rec["patches"] = {f: patches[f][0] for f in patches}
        rec["W"] = {f: {"claim": w_claim(W[f]), "fn_with_code": (W[f].get("output") or {}).get("fn_with_code"),
                        "witness": ((W[f].get("output") or {}).get("witness") or {}),
                        "flags": W[f].get("flags"), "soft_flags": W[f].get("soft_flags"), "unsure": W[f].get("unsure"),
                        "n_void": W[f].get("n_void")} for f in FAMS} if student is None else None
        rec["X"] = ex_res and {f: {k: v for k, v in s.items() if k != "excerpts"} for f, s in ex_res["witness"].items()}
        if mode == "witness":
            executed = [f for f in patches if patches[f][0] == "witness" and ex_res and
                        ex_res["witness"][f]["applied"] not in (None, "fail") and
                        ex_res["witness"][f]["f2p_failed_attributable"]]
            unsure = any(W[f].get("unsure") for f in FAMS)
            common = set.intersection(*(set(ex_res["witness"][f]["f2p_failed_attributable"]) for f in executed)) \
                if len(executed) == 2 else set()
            return dict(rec, resolution="CONTESTED" if unsure else "CONFIRMED",
                        resolution_source="audit_unsure" if unsure else "rating_both_fn",
                        executed_witnesses=[LABEL[f] for f in executed], common_failed=sorted(common),
                        incomplete=time.time() > deadline)
        if student is None:
            common_sub = {"@@WITNESS_DIFFS@@": diffs_text(patches, W),
                          "@@EXEC@@": exec_text(ex_res, patches, W) if ex_res else
                          "Neither auditor wrote a patch (both stated that no reasonable alternative to the gold exists); "
                          "only the gold control was run: it passes every F2P test."}
            own = {f: own_text(W[f]) for f in FAMS}
        else:
            nb = no_patch_txt(W, "gpt")
            n = student.get("n_same_way") or 2
            note_patch = (f"none: there is one patch, the student's W-A (no witness round; {n} of 8 student attempts "
                          f"failed the same way)")
            common_sub = {"@@WITNESS_DIFFS@@": diffs_text(patches, W).replace(nb, note_patch),
                          "@@EXEC@@": exec_text(ex_res, patches, W).replace(nb, note_patch)}
            note = ("(There was no witness round for this task. Patch W-A was written by a student solver who saw only "
                    "the task statement (the issue followed by its specification, when there is one); in {n} of its 8 "
                    "attempts the student's patch failed the same graded check the same way. Neither auditor wrote a "
                    "patch: W-B does not exist. Judge W-A on its merits by (i)-(iii).)").format(n=n)
            own = {f: note for f in FAMS}
        R = {}
        if patches:
            with ThreadPoolExecutor(2) as ex:
                R = dict(zip(FAMS, ex.map(lambda f: stage(f, "R", render(ctx, "R", t, f, {**common_sub,
                                                                                         "@@OWN_W@@": own[f]}, reasons),
                                                          in_d, patches), FAMS)))
        skipped = "(skipped: neither auditor wrote a patch, so there was nothing to execute)"
        with ThreadPoolExecutor(2) as ex:
            D = dict(zip(FAMS, ex.map(lambda f: stage(f, "D", render(ctx, "D", t, f, {
                **common_sub, "@@OWN_W@@": own[f], "@@OTHER_W@@": own[OTHER[f]],
                "@@OWN_R@@": own_text(R.get(f)) if R else skipped,
                "@@OTHER_R@@": own_text(R.get(OTHER[f])) if R else skipped}, reasons), in_d, patches), FAMS)))
        dv = {f: "UNSURE" if D[f].get("unsure") else (D[f].get("output") or {}).get("verdict") or "NO_OUTPUT" for f in D}
        ds = {f: list((D[f].get("output") or {}).get("fn_confirmed_witnesses") or []) for f in D}
        do = {f: D[f].get("output") or {} for f in D}
        resolution = resolve(dv["claude"], ds["claude"], dv["gpt"], ds["gpt"], do["claude"], do["gpt"])
        unsure = any(st.get("unsure") for stage_ in (W, R, D) for st in stage_.values() if isinstance(st, dict))
        if unsure:
            resolution = "CONTESTED"
        return dict(rec, resolution=resolution, resolution_source="audit_unsure" if unsure else "debate",
                    guard_rule=dict(zip(("agreed", "why"), guard_agreement(do["claude"], do["gpt"])))
                    if dv["claude"] == dv["gpt"] == "FN_CONFIRMED" else None,
                    R={f: {k: (R[f].get("output") or {}).get(k) for k in ("verdict", "fn_confirmed_witnesses",
                                                                          "changed_from_previous", "discussion_quote",
                                                                          "specified_in_discussion")}
                       | {"flags": R[f].get("flags"), "unsure": R[f].get("unsure")} for f in R},
                    D={f: {"verdict": dv[f], "fn_confirmed_witnesses": ds[f],
                           **{k: do[f].get(k) for k in ("failed_test", "failed_check", "check_passes_at_base",
                                                        "guard_basis", "issue_asks_for_change", "specified_in_discussion",
                                                        "discussion_quote", "changed_from_review")},
                           "n_concede": sum(1 for p in (do[f].get("points") or []) if p.get("response") == "CONCEDE"),
                           "n_rebut": sum(1 for p in (do[f].get("points") or []) if p.get("response") == "REBUT"),
                           "flags": D[f].get("flags")} for f in D},
                    incomplete=any(dv[f] == "NO_OUTPUT" for f in dv) or time.time() > deadline,
                    dry_run=any(D[f].get("dry_run") for f in D))
    finally:
        if img.get("image") and not img.get("reused") and not runner.dry:
            runner.drop_image(img["image"])


def fn_evidence(rec: dict) -> str | None:
    """The evidence behind a confirmed FN verdict, from the escalation record."""
    if rec.get("resolution") != "CONFIRMED":
        return None
    if rec.get("mode") == "full":
        return "cross_reviewed"
    if len(rec.get("executed_witnesses") or []) == 2 and rec.get("common_failed"):
        return "two_witnesses"
    return "other_confirmed"


def audit_path(rate: dict, mode: str | None, reasons: list[str]) -> str | None:
    """How the FN verdict was reached, from the fn_rate decision and the escalation mode."""
    if mode == "witness":
        return "witness_mode"
    if mode == "full":
        return "residual_audit" if reasons == ["audit"] else "full_escalation"
    return "cleared_by_rating" if rate.get("decision") == "OK" else None


def verdict(rate: dict, rec: dict | None) -> tuple[str, list[str]]:
    """(decision, reasons) of the stage row from the fn_rate decision and the escalation record."""
    d = rate.get("decision")
    if rec is None:
        return ("OK", ["cleared_by_rating"]) if d == "OK" else ("pending", ["no escalation"])
    res = rec.get("resolution")
    if res == "ENV_NEEDS_NETWORK":
        return "drop", ["ENV_NEEDS_NETWORK"]
    if rec.get("dry_run") or res is None:
        return "pending", ["dry_run" if rec.get("dry_run") else "infrastructure"]
    if rec["mode"] == "witness":
        return ("CONTESTED", ["witness_round_unsure"]) if res == "CONTESTED" else ("FN_CONFIRMED", ["witness_mode"])
    if res == "CONTESTED" and rec.get("incomplete"):
        return "pending", ["escalation cut short: re-run with a higher agents.task_cap_s"]
    return {"CONFIRMED": "FN_CONFIRMED", "NOT_FN": "OK", "CONTESTED": "CONTESTED"}.get(res, "pending"), \
        [f"escalation:{','.join(rec.get('reasons') or [])}"]


def run(ctx, iid: str) -> dict:
    t = ctx.effective(iid, with_repair=False)
    rate = ctx.store.get("fn_rate", iid) or {}
    mode, reasons = mode_of(ctx, iid, rate)
    rec = escalate(ctx, t, mode, reasons) if mode else None
    d, why = verdict(rate, rec)
    row = {"decision": d, "reasons": why, "mode": mode, "audit_path": audit_path(rate, mode, reasons),
           "escalation": rec, "fn_evidence": "contested" if d == "CONTESTED" else fn_evidence(rec or {}),
           "complete": d != "pending"}
    return row
