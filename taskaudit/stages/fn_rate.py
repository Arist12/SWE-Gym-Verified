"""FN rating (step 3a): both families rate every F2P assertion of the effective task; the two-family decision rule.

User message: sections A-F (the statement as the solver sees it, the discussion, the graded F2P ids, the test patch,
the gold patch, the code-evidence pack). A reply that breaks the prompt's rules is re-asked once in the same
conversation, with the broken rules listed; a family without a valid reply makes the task escalate.

Decision (``decide``):
  both fn >= 2                            -> FN_BOTH (witness mode: confirmed by executed witnesses), unless either
                                             family's failing row already passes at base (a regression guard)
                                             -> ESCALATE guard
  both fn == 0 and nothing unresolved     -> OK (no execution; ESCALATE pack_degraded if the pack is still degraded
                                             at 128K)
  otherwise                               -> ESCALATE (disagree / fn1 / unresolved), full escalation
Row: decision OK | FN_BOTH | ESCALATE (the stage never removes a task), reasons, fn {family: score}, families
{family: {valid, parsed, final_errors, attempts}}, pack {budget, degraded, sha256}.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from ..llm.calls import reasked
from ..util import fence
from . import stage_key

NAME = "fn_rate"
DEPENDS = ["environment", "screen"]
FAMILIES = ("claude", "gpt")
KINDS_D = ("stated_in_issue", "derivable_from_codebase", "standard_convention")
KINDS_U = ("guess_interface", "guess_literal", "guess_behaviour_choice", "beyond_issue")
REPAIRS = ("S-a", "S-lit", "S-keep", "S-scope", "IS", "T-b", "drop")


def key(ctx, iid: str) -> str:
    t = ctx.effective(iid, with_repair=False)  # the original statement: the re-audit rates a repaired one itself
    return stage_key(ctx, NAME, t, "fn_rater", extra={"disc": t.get("hints_text")})


def user_message(t: dict, pack: str) -> str:
    """Sections A-F as the rater saw them; section A is the final statement (issue + appended specification)."""
    f2p = list(t["FAIL_TO_PASS"])
    disc = (t.get("hints_text") or "").replace("\r\n", "\n").strip()
    iid = t["instance_id"]
    return "\n".join([
        f"# Task {iid} ({t['repo']}, base commit {t['base_commit']}, version {t['version']})", "",
        "## A. Issue (exactly as the solver sees it)", fence(t["problem_statement"].replace("\r\n", "\n"), "text"), "",
        "## B. Issue discussion (NOT visible to the solver; use only for specified_in_discussion)",
        fence(disc, "text") if disc else "(empty: no discussion is recorded for this issue)", "",
        f"## C. Graded FAIL_TO_PASS tests ({len(f2p)})", fence("\n".join(f2p), "text"), "",
        "## D. Test patch", fence(t["test_patch"], "diff"), "",
        "## E. Gold patch (reference only; one possible solution; the solver never sees it)", fence(t["patch"], "diff"), "",
        "## F. Code-evidence pack (mechanical excerpt of the base repository)",
        pack.replace("\n# Code-evidence pack", "\n### Code-evidence pack", 1), "",
        f"Reply with the JSON object for task {iid} only.",
    ])


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_in(quote: str, src: str) -> bool:
    """Verbatim up to whitespace/case; '...' separates pieces that must each occur (pieces < 12 chars ignored)."""
    q, s = _norm(quote).strip("\"'“”"), _norm(src)
    pieces = [x.strip(" \"'“”") for x in re.split(r"\.\.\.|…|\[\.\.\.\]", q)]
    pieces = [x for x in pieces if len(x) >= 12] or [q]
    return all(x in s for x in pieces)


def _nn(x) -> bool:
    return isinstance(x, str) and bool(x.strip())


def validate(o, t: dict) -> list[str]:
    """The reply rules the prompt states (rows, kinds and statuses, discussion quotes, fn and failing_alternative, the
    readability fields and gaps), plus passes_at_base on every row, repair IS only with specified_in_discussion yes,
    and the task id."""
    e: list[str] = []
    if not isinstance(o, dict):
        return ["the reply is not one JSON object"]
    disc = (t.get("hints_text") or "").replace("\r\n", "\n").strip()
    rows = o.get("assertions")
    if not isinstance(rows, list) or not rows:
        return _extra(o if isinstance(rows, list) else dict(o, assertions=[]), t, ["assertions must be a non-empty list"])
    ids, u_ids = set(), set()
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            e.append(f"assertions[{i}] is not an object")
            continue
        rid = r.get("id")
        if not _nn(rid) or rid in ids:
            e.append(f"assertions[{i}]: id missing or duplicated")
        ids.add(rid)
        k, st = r.get("kind"), r.get("status")
        if k not in KINDS_D + KINDS_U:
            e.append(f"{rid}: kind {k!r} not in the enum")
            continue
        want = "DETERMINED" if k in KINDS_D else "UNDERDETERMINED"
        if st != want:
            e.append(f"{rid}: kind {k} requires status {want}")
        if not _nn(r.get("evidence")) or not _nn(r.get("assertion")):
            e.append(f"{rid}: assertion and evidence must be non-empty")
        extra = ("rejected_alternative", "specified_in_discussion", "discussion_quote", "repair")
        if want == "DETERMINED":
            if any(r.get(x) not in (None, "") for x in extra):
                e.append(f"{rid}: a DETERMINED row has rejected_alternative/specified_in_discussion/discussion_quote/"
                         "repair null")
            continue
        u_ids.add(rid)
        if not _nn(r.get("rejected_alternative")):
            e.append(f"{rid}: an UNDERDETERMINED row needs a concrete rejected_alternative")
        sd, dq, rp = r.get("specified_in_discussion"), r.get("discussion_quote"), r.get("repair")
        if sd not in ("yes", "no"):
            e.append(f"{rid}: specified_in_discussion must be 'yes' or 'no'")
        elif sd == "yes":
            if not disc:
                e.append(f"{rid}: specified_in_discussion is 'yes' but section B is empty")
            elif not _nn(dq) or not quote_in(dq, disc):
                e.append(f"{rid}: discussion_quote is not a verbatim quote from section B")
        elif dq not in ("", None):
            e.append(f"{rid}: discussion_quote must be \"\" when specified_in_discussion is 'no'")
        if rp not in REPAIRS:
            e.append(f"{rid}: repair {rp!r} not in {list(REPAIRS)}")
    fn = o.get("fn")
    if fn not in (0, 1, 2, 3) or isinstance(fn, bool):
        e.append("fn must be 0, 1, 2 or 3")
    fa = o.get("failing_alternative")
    if isinstance(fn, int) and fn >= 2:
        if not isinstance(fa, dict) or not _nn(fa.get("solution")) or not _nn(fa.get("fails")):
            e.append("fn >= 2 requires failing_alternative {solution, fails, why_natural}")
        elif not any(re.search(rf"(?<![\w-]){re.escape(u)}(?![\w-])", fa["fails"]) for u in u_ids if u):
            e.append("failing_alternative.fails must name an UNDERDETERMINED row id")
    elif fa not in (None, {}):
        e.append("failing_alternative must be null when fn <= 1")
    if not (o.get("too_weak") is None or isinstance(o.get("too_weak"), str)):
        e.append("too_weak must be a string or null")
    if not isinstance(o.get("unresolved"), list) or any(not isinstance(x, str) for x in o.get("unresolved") or []):
        e.append("unresolved must be a list of strings")
    mi = o.get("missing_information")
    if not isinstance(mi, list) or any(not isinstance(x, dict) or not _nn(x.get("question")) or
                                       not isinstance(x.get("behind_reference", ""), str) for x in mi):
        e.append("missing_information must be a list of {question, behind_reference}")
        mi = []
    nca, u, g, ext = o.get("no_clear_ask"), o.get("underspecified"), o.get("goal_clear"), o.get("external_essential")
    if not isinstance(nca, bool):
        e.append("no_clear_ask must be true or false")
    if u not in (0, 1, 2, 3) or isinstance(u, bool):
        e.append("underspecified must be 0..3")
    if g not in ("yes", "partly", "no"):
        e.append("goal_clear must be yes/partly/no")
    if ext not in (0, 1, 2) or isinstance(ext, bool):
        e.append("external_essential must be 0..2")
    if nca is True:
        if g != "no" or u != 3:
            e.append("no_clear_ask true requires goal_clear 'no' and underspecified 3")
    elif nca is False:
        if mi and (g != "partly" or u != 2):
            e.append("non-empty missing_information requires goal_clear 'partly' and underspecified 2")
        if not mi and (g != "yes" or u not in (0, 1)):
            e.append("empty missing_information requires goal_clear 'yes' and underspecified 0 or 1")
    if ext == 2 and not (nca is True or any(_nn(x.get("behind_reference")) for x in mi)):
        e.append("external_essential 2 requires no_clear_ask or a missing_information item with behind_reference")
    return _extra(o, t, e + _gaps(o, mi, u))


def _extra(o: dict, t: dict, e: list[str]) -> list[str]:
    """The pipeline's additions to the rater's rules, then the task id (in the as-run order of the messages)."""
    for r in o.get("assertions") or []:
        if not isinstance(r, dict):
            continue
        if r.get("passes_at_base") not in ("yes", "no"):
            e.append(f"{r.get('id')}: passes_at_base must be 'yes' or 'no'")
        if r.get("status") == "UNDERDETERMINED" and r.get("repair") == "IS" and r.get("specified_in_discussion") != "yes":
            e.append(f"{r.get('id')}: repair IS requires specified_in_discussion 'yes'")
    if o.get("task") not in (None, t["instance_id"]):
        e.append(f"task must be {t['instance_id']}")
    return e


def _gaps(o: dict, mi: list, u) -> list[str]:
    e: list[str] = []
    gaps = o.get("gaps")
    if not isinstance(gaps, list):
        e.append("gaps must be a list")
        return e
    bad = False
    for i, x in enumerate(gaps):
        if not isinstance(x, dict) or not _nn(x.get("question")) or not isinstance(x.get("options"), list) \
                or len(x["options"]) < 2 or not isinstance(x.get("default", None), str) \
                or not isinstance(x.get("central"), bool) or not isinstance(x.get("settled_by_codebase"), bool) \
                or not isinstance(x.get("behind_reference", ""), str):
            e.append(f"gaps[{i}]: needs question, >= 2 options, default (string), central and settled_by_codebase "
                     "(booleans), behind_reference (string)")
            bad = True
        elif x["default"].strip() and not _nn(x.get("default_basis")):
            e.append(f"gaps[{i}]: a non-empty default needs a non-empty default_basis")
    if not bad:
        open_g = [x for x in gaps if not x["settled_by_codebase"] and not x["default"].strip()]
        want = len(open_g) if any(x["central"] for x in open_g) or len(open_g) >= 2 else 0
        if want != len(mi):
            e.append(f"missing_information must hold {want} item(s): all open gaps (settled_by_codebase false, "
                     "default \"\") when one of them is central or there are two or more; otherwise none")
        if u == 0 and any(not x["settled_by_codebase"] for x in gaps):
            e.append("underspecified 0 requires every gap to be settled_by_codebase (otherwise 1)")
    return e


def failing_rows(o: dict) -> list[dict]:
    txt = (o.get("failing_alternative") or {}).get("fails") or ""
    return [r for r in o.get("assertions") or [] if r.get("id")
            and re.search(rf"(?<![\w-]){re.escape(r['id'])}(?![\w-])", txt)]


def guard_fail(o: dict) -> bool:
    """The failing row is a regression-guard row (passes_at_base "yes")."""
    return (o.get("fn") or 0) >= 2 and any(r.get("passes_at_base") == "yes" for r in failing_rows(o))


def decide(parsed: dict, pack_degraded: bool = False) -> dict:
    """parsed: {family: valid reply or None} -> {decision: OK | FN_BOTH | ESCALATE, reasons}."""
    a, b = parsed.get("claude"), parsed.get("gpt")
    if not a or not b:
        return {"decision": "ESCALATE", "reasons": ["rater_invalid"]}
    fa, fb = a.get("fn") or 0, b.get("fn") or 0
    if fa >= 2 and fb >= 2:
        if guard_fail(a) or guard_fail(b):
            return {"decision": "ESCALATE", "reasons": ["guard"]}
        return {"decision": "FN_BOTH", "reasons": ["both_fn"]}
    ua, ub = a.get("unresolved") or [], b.get("unresolved") or []
    if fa == 0 and fb == 0 and not ua and not ub:
        return {"decision": "ESCALATE", "reasons": ["pack_degraded"]} if pack_degraded else {"decision": "OK", "reasons": []}
    reasons = []
    if (fa >= 2) != (fb >= 2):
        reasons.append("disagree")
    if fa == 1 or fb == 1:
        reasons.append("fn1")
    if ua or ub:
        reasons.append("unresolved")
    return {"decision": "ESCALATE", "reasons": reasons or ["disagree"]}


def rate(ctx, t: dict, stage: str = "3a") -> dict:
    """Both families' ratings of a task view and the decision (also used by the re-audit)."""
    pk = ctx.fn_pack(t)
    system, user = ctx.prompts.text("fn_rater"), user_message(t, pk["text"])
    with ThreadPoolExecutor(2) as ex:
        res = dict(zip(FAMILIES, ex.map(lambda f: reasked(ctx.llm, f, stage, system, user, lambda o: validate(o, t),
                                                         int(ctx.scfg(NAME).get("reask", 1))), FAMILIES)))
    out = {"prompt": ctx.prompts.meta("fn_rater"), "pack": {k: pk[k] for k in ("budget", "degraded", "sha256", "tokens")},
           "families": {f: {k: res[f].get(k) for k in ("valid", "parsed", "parsed_invalid", "final_errors",
                                                       "attempts", "model")} for f in FAMILIES}}
    if any(res[f].get("dry_run") for f in FAMILIES):
        return dict(out, complete=False, decision="pending", reasons=["dry_run"])
    parsed = {f: res[f]["parsed"] for f in FAMILIES}
    d = decide(parsed, bool(pk["degraded"]))
    return dict(out, **d, fn={f: (parsed[f] or {}).get("fn") for f in FAMILIES})


def run(ctx, iid: str) -> dict:
    return rate(ctx, ctx.effective(iid, with_repair=False))
