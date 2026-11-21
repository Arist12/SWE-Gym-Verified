"""False positives: label passing patches, confirm by counterexample, harden with regression tests.

Candidates: distinct (by normalised diff) passing patches that differ from the gold, from the measured rollouts that
resolved, or ``stages.fp.candidates`` (JSONL: instance_id, patch, n_samples?).
Label (the FP judge, step 5f, both families): the issue, the appended specification, the candidate-anchored code pack
and the patch; no gold patch, no tests, no test ids. Each family labels the patch correct / incorrect / uncertain; a
family without a parsable reply after 3 attempts makes the pair unusable. Every counterexample of a both-incorrect
patch runs offline on the original code, the gold patch and the candidate; confirmed = base == gold != candidate under
the comparator (stdout lines, whitespace-insensitive; an exception compared only as raised-or-not).
``label``: ``false_positive`` = both incorrect and >= 1 confirmed counterexample; ``correct`` = both correct;
``undetermined`` = the rest.
Tests: each confirmed counterexample of a false positive whose script does not raise at base becomes one candidate
PASS_TO_PASS test (one added file per task), kept iff it passes three checks. ``check1``: it passes at base and with
the gold and fails on its own false-positive patch; ``check2``: it passes on every candidate that is not a false
positive, and >= 1 such candidate that differs from the gold passes it (else the task gets no test); ``check3``: the
task's full test patch (with the added file) resolves the gold in two runs, and the script runs at base in at most
``max_base_runtime_s``. Construction / analysis split of an extra pool of passing patches (``stages.fp.check_pool``,
e.g. RL training passes), 50 / 50 per task by sha256("<split_seed>:<task>:<diff sha>"): on the construction half
every test that rejects a patch not labelled incorrect by both families is deleted; the analysis half is held out
(rejection counts only).
Row: decision keep | flag (a false positive found); patches [{patch_sha256, patch, n_samples, label, label_claude,
label_gpt, summary_claude, summary_gpt, pair, counterexamples, n_counterexamples_confirmed}]; candidate_tests [{id,
script, expected, fp_patches, check1, check2, check3, kept}]; test_file, tests (the ids of the kept tests) and
test_patch (the added file, kept tests only).
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..gates.stats import diff_differs
from ..harness.grader import Grader
from ..llm.calls import judged
from ..util import clip_tight, read_jsonl, sha
from . import fp_compare as C
from . import stage_key

NAME = "fp"
DEPENDS = ["environment"]
FAMS = ("claude", "gpt")
VERDICTS = ("correct", "incorrect", "uncertain")
CLASSES = ("relaxed_detail", "issue_requirement", "regression", "other")
ACTIVATE = "source /opt/miniconda3/bin/activate >/dev/null 2>&1 && conda activate testbed >/dev/null 2>&1; "
_GR = None


def grader(ctx) -> Grader:
    global _GR
    if _GR is None:
        _GR = Grader(ctx.cfg, NAME, ctx.cfg["work_dir"])
    return _GR


def cleanup(ctx) -> None:
    if _GR is not None:
        _GR.docker.cleanup()


def norm_diff(patch: str) -> str:
    out = []
    for ln in (patch or "").splitlines():
        if ln.startswith("index "):
            continue
        out.append(re.sub(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@.*$", "@@", ln).rstrip())
    return "\n".join(out).strip()


def candidates(ctx, t: dict) -> list[dict]:
    iid, src = t["instance_id"], ctx.scfg(NAME).get("candidates")
    rows = [r for r in (read_jsonl(src) if src else []) if r.get("instance_id") == iid]
    rows += [{"patch": r["patch"], "n_samples": 1} for r in (ctx.store.get("measure", iid) or {}).get("rollouts") or []
             if r.get("resolved") and r.get("patch")]
    out: dict[str, dict] = {}
    for r in rows:
        p = r.get("patch") or ""
        if not p.strip() or not diff_differs(p, t["patch"]):
            continue
        k = sha(norm_diff(p))
        e = out.setdefault(k, {"patch": p, "patch_sha256": sha(p), "n_samples": 0})
        e["n_samples"] += int(r.get("n_samples") or 1)
    cap = ctx.scfg(NAME).get("max_patches_per_task")
    return sorted(out.values(), key=lambda x: x["patch_sha256"])[: int(cap) if cap else None]


def wants(ctx, iid: str) -> bool:
    return bool(ctx.scfg(NAME).get("candidates") or (ctx.store.get("measure", iid) or {}).get("rollouts"))


def key(ctx, iid: str) -> str:
    t = ctx.effective(iid)
    pool = ctx.scfg(NAME).get("check_pool")
    return stage_key(ctx, NAME, t, "fp_judge", extra={"cands": [c["patch_sha256"] for c in candidates(ctx, t)],
                                                      "pool": sorted(sha(r.get("patch") or "") for r in
                                                                     (read_jsonl(pool) if pool else [])
                                                                     if r.get("instance_id") == iid)})


# ------------------------------------------------------------------ judge
def user_message(t: dict, patch: str, pack: str) -> str:
    parts = [f"Repository: {t['repo']}", f"<issue>\n{clip_tight(t['statement_original'], 30000)}\n</issue>"]
    if t.get("spec_block"):
        parts.append(f"<specification>\n{t['spec_block']}\n</specification>")
    parts += [f"<code_evidence_pack>\n{pack}\n</code_evidence_pack>",
              f"<candidate_patch>\n{clip_tight(patch, 40000)}\n</candidate_patch>"]
    return "\n\n".join(parts)


def flip_user(t: dict, change: str, patch: str, pack: str) -> str:
    from ..util import clip_mid
    parts = [f"Repository: {t['repo']}", f"<issue>\n{clip_mid(t['statement_original'], 30000)}\n</issue>"]
    if t.get("spec_block"):
        parts.append(f"<specification>\n{t['spec_block']}\n</specification>")
    parts += [f"<change_note>\n{change}\n</change_note>", f"<code_evidence_pack>\n{pack}\n</code_evidence_pack>",
              f"<candidate_patch>\n{clip_mid(patch, 40000)}\n</candidate_patch>"]
    return "\n\n".join(parts)


def parse(o, flip: bool = False):
    ok = ("correct", "incorrect") if flip else VERDICTS
    if not isinstance(o, dict) or o.get("verdict") not in ok or not isinstance(o.get("defects"), list):
        return None, ["fields"]
    if flip and any(not isinstance(d, dict) or d.get("class") not in CLASSES for d in o["defects"]):
        return None, ["defect class"]
    if (o["verdict"] == "incorrect") != bool(o["defects"]):
        return None, ["verdict vs defects"]
    return o, []


def pair(v: dict) -> str:
    a, b = v.get("claude"), v.get("gpt")
    if a is None or b is None:
        return "unusable"
    if a == b == "correct":
        return "both_correct"
    if a == b == "incorrect":
        return "both_incorrect"
    if "uncertain" in (a, b):
        return "uncertain"
    return "disagree"


def label(verdicts: dict, confirmed: int) -> str:
    p = pair(verdicts)
    if p == "both_correct":
        return "correct"
    if p == "both_incorrect" and confirmed > 0:
        return "false_positive"
    return "undetermined"


# ------------------------------------------------------------------ counterexamples
def run_script(ctx, t: dict, patch: str, script: str, timeout: int = 120) -> dict:
    """Run a (behaviour-form) script from /testbed with ``patch`` applied, offline; observe() of its run."""
    d = grader(ctx).docker
    why = d.ensure_image(t["image"])
    if why:
        return {"error": why}
    name, err = d.start(t["image"], t["instance_id"], "cx", mounts=t.get("mounts") or [])
    if not name:
        return {"error": err}
    try:
        with tempfile.TemporaryDirectory() as td:
            Path(td, "p.patch").write_text(patch or "")
            Path(td, "cx.py").write_text(script)
            for f in ("p.patch", "cx.py"):
                d.cp_in(name, f"{td}/{f}", f"/tmp/{f}")
        if (patch or "").strip():
            rc, _, _, _ = d.exec(name, "cd /testbed && (git apply /tmp/p.patch || patch --batch --fuzz=5 -p1 -i /tmp/p.patch)",
                                 timeout=300)
            if rc:
                return {"error": "patch_apply_failed"}
        t0 = time.time()
        rc, out, errs, to = d.exec(name, ACTIVATE + "python /tmp/cx.py", timeout=timeout, workdir="/testbed")
        return C.observe(out, errs, rc, round(time.time() - t0, 2), to)
    finally:
        d.rm(name)


def confirm(ctx, t: dict, candidate: str, script: str) -> dict:
    """The behaviour form of ``script`` on the original code, the gold patch and the candidate."""
    to = int(ctx.scfg(NAME).get("script_timeout_s", 120))
    try:
        bc = C.rewrite(script)
    except SyntaxError:
        return {"confirmed": False, "testable": False, "why": "script does not parse"}
    obs = {k: run_script(ctx, t, p, bc, to) for k, p in (("base", ""), ("gold", t["patch"]), ("candidate", candidate))}
    ok, why = C.confirm_regression(obs["base"], obs["gold"], obs["candidate"])
    no_test = C.no_test_reason(script, obs["base"]) if ok else None
    return {"confirmed": ok, "testable": ok and no_test is None, "why": no_test or why, "script_behaviour_form": bc,
            "expected": list(obs["base"].get("lines") or []), "base_seconds": obs["base"].get("seconds"),
            "observed": obs}


def judge_patch(ctx, t: dict, patch: str, step: str = "fp_judge", change: str | None = None) -> dict:
    """Both families' verdicts on one patch, and the execution of the counterexamples (of a both-incorrect patch;
    for the flip gate, of every incorrect verdict)."""
    pack = ctx.fp_pack(t, patch)
    flip = step == "flip_judge"
    user = flip_user(t, change or "", patch, pack) if flip else user_message(t, patch, pack)
    tag = f"{'5d' if flip else '5f'}:{sha(patch)[:12]}"
    with ThreadPoolExecutor(2) as ex:
        res = dict(zip(FAMS, ex.map(lambda f: judged(ctx.llm, f, tag, ctx.prompts.text(step), user,
                                                     lambda o: parse(o, flip)), FAMS)))
    if any(r.get("dry_run") for r in res.values()):
        return {"dry_run": True}
    verdicts = {f: (res[f]["parsed"] or {}).get("verdict") if res[f]["accepted"] else None for f in FAMS}
    p = pair(verdicts)
    cx = []
    if p == "both_incorrect" or (flip and "incorrect" in verdicts.values()):
        for f in FAMS:
            for k, dft in enumerate((res[f]["parsed"] or {}).get("defects") or []):
                if dft.get("counterexample"):
                    c = confirm(ctx, t, patch, dft["counterexample"])
                    cx.append({"id": f"{f[:1]}{k:02d}", "family": f, "script": dft["counterexample"],
                               **{x: c.get(x) for x in ("confirmed", "testable", "why", "expected", "base_seconds",
                                                        "script_behaviour_form")}})
    n = sum(1 for c in cx if c["confirmed"])
    return {"verdicts": verdicts, "pair": p, "confirmed": n, "label": label(verdicts, n), "counterexamples": cx,
            "summaries": {f: (res[f]["parsed"] or {}).get("summary") for f in FAMS}}


# ------------------------------------------------------------------ tests
def test_file(t: dict, cases: list[tuple[str, list[str]]]) -> tuple[str, str, list[str]]:
    """(path, source, ids) of the added test file: one method test_rNNN per (behaviour-form script, expected base
    lines), each checking exit 0 and the expected lines up to formatting (fp_compare.HELPER)."""
    files = [i.split("::")[0] for i in t.get("FAIL_TO_PASS") or [] if "::" in i and i.split("::")[0].endswith(".py")]
    files = files or re.findall(r"^diff --git a/(\S*test\S*\.py) ", t["test_patch"], re.M)
    folder = str(Path(files[0]).parent) if files else "tests"
    h = hashlib.sha256((t["instance_id"] + json.dumps(cases)).encode()).hexdigest()[:10]
    path = f"{folder}/test_added_regression_{h}.py"
    depth = len(Path(path).parts) - 1
    names = [f"r{i + 1:03d}" for i in range(len(cases))]
    body = (C.HELPER.replace("@@DEPTH@@", str(depth)).replace("@@TIMEOUT@@", "120").lstrip("\n")
            + "\n\nimport unittest  # noqa: E402\n\nCASES = " + json.dumps(dict(zip(names, cases)), indent=1)
            + "\n\n\nclass TestAddedRegression(unittest.TestCase):\n"
            + "".join(f"    def test_{n}(self):\n        _check(*CASES[{n!r}])\n\n" for n in names))
    return path, body.rstrip("\n") + "\n", [f"{path}::TestAddedRegression::test_{n}" for n in names]


def file_diff(path: str, body: str) -> str:
    lines = body.splitlines()
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{x}\n" for x in lines))


def passed_ids(ctx, tt: dict, patch: str, kind: str) -> dict[str, bool]:
    r = grader(ctx).run(tt, patch, kind, keep_statuses=True)
    sm = r.get("statuses") or {}
    usable = r.get("tests_started") and not r.get("harness_error") and r.get("apply") != "fail"
    return {i: bool(usable) and sm.get(i) in ("PASSED", "XFAIL") for i in tt["PASS_TO_PASS"]}


def build_tests(ctx, t: dict, labelled: list[dict]) -> dict:
    cases, owner, idx = [], [], {}
    for p in labelled:
        if p["label"] != "false_positive":
            continue
        for c in p["counterexamples"]:
            if c["confirmed"] and c["testable"] and (c.get("base_seconds") or 0) <= float(
                    ctx.scfg(NAME).get("max_base_runtime_s", 24)):
                k = (c["script_behaviour_form"], json.dumps(c["expected"]))
                if k in idx:  # one test per distinct behaviour-form script; its own false-positive patches
                    owner[idx[k]].add(p["patch_sha256"])
                    continue
                idx[k] = len(cases)
                cases.append((c["script_behaviour_form"], c["expected"]))
                owner.append({p["patch_sha256"]})
    if not cases:
        return {"tests": [], "test_patch": ""}
    path, body, ids = test_file(t, cases)
    extra = file_diff(path, body)
    tt = dict(t, test_patch=t["test_patch"] + extra, FAIL_TO_PASS=[], PASS_TO_PASS=ids)
    st = {"empty": passed_ids(ctx, tt, "", "empty"), "gold": passed_ids(ctx, tt, t["patch"], "gold")}
    for p in labelled:
        st[p["patch_sha256"]] = passed_ids(ctx, tt, p["patch"], "model")
    gold_n = norm_diff(t["patch"])
    non_fp = [p for p in labelled if p["label"] != "false_positive"]
    tests = []
    for i, tid in enumerate(ids):
        c1 = st["empty"][tid] and st["gold"][tid] and any(not st[o][tid] for o in owner[i])
        c2 = all(st[p["patch_sha256"]][tid] for p in non_fp) and \
            any(st[p["patch_sha256"]][tid] and norm_diff(p["patch"]) != gold_n for p in non_fp)
        tests.append({"id": tid, "script": cases[i][0], "expected": cases[i][1], "fp_patches": sorted(owner[i]),
                      "check1": c1, "check2": c2})
    keep = [x for x in tests if x["check1"] and x["check2"]]
    if keep:  # check3: the full test patch with the kept tests, graded twice with the gold
        kept_cases = [(x["script"], x["expected"]) for x in keep]
        path2, body2, ids2 = test_file(t, kept_cases)
        full = dict(t, test_patch=t["test_patch"] + file_diff(path2, body2), PASS_TO_PASS=t["PASS_TO_PASS"] + ids2)
        runs = [grader(ctx).run(full, t["patch"], "gold", keep_statuses=True) for _ in range(2)]
        for x, nid in zip(keep, ids2):
            ok = [r.get("resolved") or (r.get("statuses") or {}).get(nid) in ("PASSED", "XFAIL") for r in runs]
            x["check3"] = all(ok) and all(r.get("tests_started") for r in runs)
    for x in tests:
        x["kept"] = bool(x["check1"] and x["check2"] and x.get("check3"))
    return {"tests": tests}


def final_file(t: dict, tests: list[dict]) -> tuple[str | None, list[str], str]:
    """(path, test ids, diff) of the added test file with the kept tests only; (None, [], "") without one."""
    kept = [x for x in tests if x["kept"] and not x.get("deleted_on_construction_half")]
    if not kept:
        return None, [], ""
    path, body, ids = test_file(t, [(x["script"], x["expected"]) for x in kept])
    return path, ids, file_diff(path, body)


def split_half(seed: str, iid: str, patch: str) -> str:
    return "construction" if int(sha(f"{seed}:{iid}:{sha(norm_diff(patch))}")[:8], 16) % 2 == 0 else "analysis"


def check_pool(ctx, t: dict, tests: list[dict]) -> dict:
    src = ctx.scfg(NAME).get("check_pool")
    pool = [r for r in (read_jsonl(src) if src else []) if r.get("instance_id") == t["instance_id"] and r.get("patch")]
    kept = [x for x in tests if x["kept"]]
    if not pool or not kept:
        return {"n": len(pool)}
    seed = ctx.scfg(NAME).get("split_seed") or "fp-split"
    path, body, ids = test_file(t, [(x["script"], x["expected"]) for x in kept])
    tt = dict(t, test_patch=t["test_patch"] + file_diff(path, body), FAIL_TO_PASS=[], PASS_TO_PASS=ids)
    out = {"construction": {"n": 0, "rejected": 0}, "analysis": {"n": 0, "rejected": 0}}
    for r in pool:
        half = split_half(seed, t["instance_id"], r["patch"])
        st = passed_ids(ctx, tt, r["patch"], "pool")
        rej = [x for x, i in zip(kept, ids) if not st[i]]
        out[half]["n"] += 1
        out[half]["rejected"] += bool(rej)
        if half == "construction" and rej:
            j = judge_patch(ctx, t, r["patch"])
            if j.get("dry_run"):
                return {"dry_run": True}
            if j["pair"] != "both_incorrect":
                for x in rej:
                    x["deleted_on_construction_half"] = True
    return out


def run(ctx, iid: str) -> dict:
    t = ctx.effective(iid)
    labelled = []
    for c in candidates(ctx, t):
        j = judge_patch(ctx, t, c["patch"])
        if j.get("dry_run"):
            return {"complete": False, "decision": "pending", "reasons": ["dry_run"]}
        labelled.append({**c, **j})
    built = build_tests(ctx, t, labelled)
    pool = check_pool(ctx, t, built["tests"])
    if pool.get("dry_run"):
        return {"complete": False, "decision": "pending", "reasons": ["dry_run"]}
    n_fp = sum(p["label"] == "false_positive" for p in labelled)
    path, ids, test_patch = final_file(t, built["tests"])
    return {"decision": "flag" if n_fp else "keep", "reasons": ["FP_CONFIRMED"] if n_fp else [],
            "patches": [{"patch_sha256": p["patch_sha256"], "patch": p["patch"], "n_samples": p["n_samples"],
                         "label": p["label"], **{f"label_{f}": p["verdicts"].get(f) for f in FAMS},
                         **{f"summary_{f}": p["summaries"].get(f) for f in FAMS}, "pair": p["pair"],
                         "counterexamples": p["counterexamples"], "n_counterexamples_confirmed": p["confirmed"]}
                        for p in labelled],
            "candidate_tests": built["tests"], "test_file": path, "tests": ids, "test_patch": test_patch,
            "check_pool": pool,
            "counts": {"patches": len(labelled), "false_positives": n_fp, "tests_kept": len(ids)}}

