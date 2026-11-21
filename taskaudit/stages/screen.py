"""Statement screens, in this order: statement hygiene (step 1a), the defective-PR screen (human labels, else the
PAIChecker router, step 2), misleading statement (step 1d), solution in issue (step 1b, flags only). Prompts, user
messages and decision rules as run.

  Hygiene (one family, role ``hygiene``): drop HYGIENE_EMPTY / HYGIENE_EXTERNAL (external = 2) / HYGIENE_INCOHERENT.
  Defective PR: a human label (``dataset.human_labels``: CSV or JSONL with instance_id and human_label, PAIChecker
       format "DP-1, SC-2" or "No Misalignment") with DP (defective PR) -> drop DEFECTIVE_PR_HUMAN_LABEL; no label ->
       the router (role ``pai_router``) on the task's PAIChecker evidence record (``dataset.pai_records``), DP -> drop
       DEFECTIVE_PR_ROUTER; no record -> flag ``defective_pr_not_screened``. The other labels are kept as route hints.
  Misleading statement (both families): both misleading, each with a valid DISCUSSION QUOTE -> flag
       ``statement_misleading_both`` (the task keeps an IS repair item); both misleading otherwise -> drop
       MISLEADING_BOTH_FAMILIES; one family -> flag ``statement_misleading_claude`` / ``statement_misleading_gpt``.
  Solution in issue (both families agree): flags ``solution_in_issue``, ``f2p_unrelated``, ``f2p_partly_unrelated``.
Every call: up to 3 attempts; an attempt is accepted when the reply parses and passes the step's consistency rules;
after the last failing attempt the last parsed reply is kept. A step without any parsed reply leaves the task
undecided (complete false; replies are cached, so a re-run asks again only with ``llm.cache: false``).
Row: decision keep | drop; reasons; flags; hygiene, pai, misleading, solution_in_issue (the steps' replies);
IS_route (the task keeps an item for the repair route IS) with IS_quotes (both families' discussion quotes).
"""
from __future__ import annotations

import csv
import functools
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..gates.g1 import quote_check
from ..llm.calls import judged
from ..util import clip_mid, read_jsonl
from . import stage_key

NAME = "screen"
DEPENDS = ["prescreen"]
FAMS = ("claude", "gpt")
STEPS = ("hygiene", "pai_router", "misleading", "solution_in_issue")


def key(ctx, iid: str) -> str:
    t = ctx.effective(iid, with_repair=False)
    return stage_key(ctx, NAME, t, *STEPS, extra={"human": human_labels(ctx).get(iid), "rec": bool(pai_record(ctx, iid)),
                                                  "disc": t.get("hints_text")})


def norm(s: str) -> str:
    s = (s or "").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


def f2p_lines(t: dict) -> str:
    f = t["FAIL_TO_PASS"]
    return "\n".join(f[:80]) + (f"\n... and {len(f) - 80} more" if len(f) > 80 else "")


# ------------------------------------------------------------------ 1a hygiene
def render_1a(ctx, t: dict) -> tuple[str, str]:
    p = ctx.prompts.text("hygiene")
    i = p.index("Repository: {repo}")
    user = p[i:].replace("{repo}", t["repo"]).replace("{issue}", clip_mid(t["statement"], 40000))
    return p[:i].rstrip() + "\n", user


def parse_1a(j: dict):
    e = j.get("external")
    if not (isinstance(j.get("empty"), bool) and isinstance(e, int) and not isinstance(e, bool) and 0 <= e <= 2
            and isinstance(j.get("incoherent"), bool) and isinstance(j.get("external_refs", []), list)):
        return None, ["fields"]
    return {"empty": j["empty"], "external": e, "external_refs": [str(x) for x in j.get("external_refs") or []],
            "incoherent": j["incoherent"], "why": str(j.get("why") or "")}, []


def decide_1a(p: dict) -> list[str]:
    return (["HYGIENE_EMPTY"] * p["empty"] + ["HYGIENE_EXTERNAL"] * (p["external"] == 2)
            + ["HYGIENE_INCOHERENT"] * p["incoherent"])


# ------------------------------------------------------------------ 1b solution in issue
def render_1b(ctx, t: dict) -> tuple[str, str]:
    disc = (t.get("hints_text") or "").strip()
    user = "\n\n".join([
        f"Repository: {t['repo']}",
        f"<issue>\n{clip_mid(t['statement_original'], 40000)}\n</issue>",
        (f"<discussion>\n{clip_mid(disc, 20000)}\n</discussion>" if disc else "No discussion is given."),
        f"<reference_solution>\n{clip_mid(t['patch'], 40000)}\n</reference_solution>",
        f"<test_patch>\n{clip_mid(t['test_patch'], 50000)}\n</test_patch>",
        "FAIL_TO_PASS tests:\n" + f2p_lines(t)])
    return ctx.prompts.text("solution_in_issue"), user


def parse_1b(j: dict):
    groups = j.get("f2p_groups")
    if not (isinstance(groups, list) and j.get("tests_vs_issue") in ("related", "partly", "unrelated")
            and j.get("solution_in_issue") in ("none", "hint", "full")):
        return None, ["fields"]
    if not groups or any(not isinstance(x, dict) or x.get("in_issue") not in ("asked", "implied", "not_asked")
                         for x in groups):
        return None, ["group_fields"]
    ins = [x["in_issue"] for x in groups]
    der = "related" if "not_asked" not in ins else ("unrelated" if not ({"asked", "implied"} & set(ins)) else "partly")
    errs = []
    if der != j["tests_vs_issue"]:
        errs.append(f"tests_vs_issue: stated {j['tests_vs_issue']} derived {der}")
    q = str(j.get("solution_quote") or "").strip()
    if (j["solution_in_issue"] == "none") != (q == ""):
        errs.append(f"solution_in_issue {j['solution_in_issue']} with quote {'empty' if not q else 'non-empty'}")
    p = {k: j.get(k) for k in ("f2p_groups", "tests_vs_issue", "solution_quote", "solution_in_issue", "why")}
    p["derived"] = {"tests_vs_issue": der, "groups": dict(Counter(ins))}
    return p, errs


def labels_1b(pc: dict | None, pg: dict | None) -> list[str]:
    flags = []
    if pc["solution_in_issue"] == pg["solution_in_issue"] == "full":
        flags.append("solution_in_issue")
    for v, flag in (("unrelated", "f2p_unrelated"), ("partly", "f2p_partly_unrelated")):
        if pc["tests_vs_issue"] == pg["tests_vs_issue"] == v:
            flags.append(flag)
    return flags


# ------------------------------------------------------------------ 1d misleading
def render_1d(ctx, t: dict) -> tuple[str, str]:
    d = (t.get("hints_text") or "").strip()
    parts = [f"Repository: {t['repo']}", f"<issue>\n{clip_mid(t['statement_original'], 40000)}\n</issue>"]
    if t.get("spec_block"):
        parts.append(f"<specification>\n{t['spec_block'].strip()}\n</specification>")
    parts += [f"<discussion_not_visible_to_solver>\n{clip_mid(d, 20000) if d else '(no discussion)'}\n"
              f"</discussion_not_visible_to_solver>",
              f"<reference_solution>\n{clip_mid(t['patch'], 40000)}\n</reference_solution>",
              f"<test_patch>\n{clip_mid(t['test_patch'], 50000)}\n</test_patch>",
              "FAIL_TO_PASS tests:\n" + f2p_lines(t)]
    return ctx.prompts.text("misleading"), "\n\n".join(parts)


def parser_1d(t: dict):
    statement, hints = t["statement"], (t.get("hints_text") or "").strip()

    def parse(j: dict):
        if not isinstance(j.get("misleading"), bool) or not (j.get("resolving_quote") is None
                                                             or isinstance(j.get("resolving_quote"), str)):
            return None, ["fields"]
        mq, mc = str(j.get("misleading_quote") or "").strip(), str(j.get("misleading_conflict") or "").strip()
        rq = (j.get("resolving_quote") or "").strip() or None
        errs = []
        if j["misleading"] != bool(mq and mc):
            errs.append("misleading vs quote/conflict")
        if rq and not j["misleading"]:
            errs.append("resolving_quote while not misleading")
        qc = None
        if rq:
            if not hints:
                errs.append("resolving_quote without a discussion")
            else:
                qc = quote_check(rq, hints)
                errs += [f"quote: {x}" for x in qc["reasons"]]
        p = {"misleading": j["misleading"], "misleading_quote": mq, "misleading_conflict": mc, "resolving_quote": rq,
             "misleading_why": str(j.get("misleading_why") or ""), "quote_ok": bool(qc and qc["ok"]),
             "quote_check": qc, "misleading_quote_verbatim_in_statement": bool(mq) and norm(mq) in norm(statement)}
        return p, errs

    return parse


def decide_1d(pc: dict, pg: dict) -> dict:
    m = {"claude": pc["misleading"], "gpt": pg["misleading"]}
    q = {"claude": pc["misleading"] and pc["quote_ok"], "gpt": pg["misleading"] and pg["quote_ok"]}
    if m["claude"] and m["gpt"]:
        if q["claude"] and q["gpt"]:
            return {"decision": "route", "route": "IS", "flags": ["statement_misleading_both"],
                    "quotes": {"claude": pc["resolving_quote"], "gpt": pg["resolving_quote"]}}
        return {"decision": "delete", "codes": ["MISLEADING_BOTH_FAMILIES"], "quote_families": [f for f in FAMS if q[f]]}
    if m["claude"] or m["gpt"]:
        f = "claude" if m["claude"] else "gpt"
        return {"decision": "pass", "flags": [f"statement_misleading_{f}"]}
    return {"decision": "pass", "flags": []}


def attempts(ctx) -> int:
    return int(ctx.scfg(NAME).get("attempts", 3))


def misleading(ctx, t: dict, stage: str = "1d") -> dict:
    """The misleading-statement screen (1d) on a task view (also used by the re-audit with the specification shown).
    ``stage`` is the step id that tags the requests."""
    sysp, user = render_1d(ctx, t)
    parse = parser_1d(t)
    with ThreadPoolExecutor(2) as ex:
        res = dict(zip(FAMS, ex.map(lambda f: judged(ctx.llm, f, stage, sysp, user, parse, attempts(ctx)), FAMS)))
    if any(res[f].get("dry_run") for f in FAMS):
        return {"decision": "undecided", "dry_run": True}
    pc, pg = res["claude"]["parsed"], res["gpt"]["parsed"]
    if pc is None or pg is None:
        return {"decision": "undecided", "families": {f: _brief(res[f]) for f in FAMS}}
    return dict(decide_1d(pc, pg), families={f: _brief(res[f]) for f in FAMS})


def _brief(r: dict) -> dict:
    return {k: r.get(k) for k in ("accepted", "parsed", "errors", "attempts", "model")}


# ------------------------------------------------------------------ defective PR (PAIChecker)
@functools.lru_cache(maxsize=4)
def _labels(path: str) -> dict[str, str]:
    p = Path(path)
    if p.suffix == ".csv":
        return {r["instance_id"]: r.get("human_label") or r.get("label") or "" for r in csv.DictReader(open(p))}
    return {r["instance_id"]: r.get("human_label") or r.get("label") or "" for r in read_jsonl(p)}


def human_labels(ctx) -> dict[str, str]:
    p = (ctx.cfg.get("dataset") or {}).get("human_labels")
    return _labels(str(Path(p).expanduser())) if p else {}


def pai_main(label: str) -> set[str]:
    return {"No Misalignment" if x.strip() == "No Misalignment" else x.strip().split("-")[0]
            for x in label.split(",") if x.strip()}


@functools.lru_cache(maxsize=4)
def _records(path: str) -> dict[str, dict]:
    return {r["instance_id"]: r for r in read_jsonl(Path(path))}


def pai_record(ctx, iid: str) -> dict | None:
    p = (ctx.cfg.get("dataset") or {}).get("pai_records")
    return _records(str(Path(p).expanduser())).get(iid) if p else None


def _rclip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1500] + f"\n[... {len(s) - n + 1500} characters omitted ...]\n" + s[-1500:]


def _budget(items: list[str], n: int) -> str:
    out, used = [], 0
    for x in items:
        if used + len(x) > n:
            out.append(f"[... {len(items) - len(out)} more items omitted ...]")
            break
        out.append(x)
        used += len(x)
    return "\n\n".join(out) if out else "(none)"


def render_router(t: dict, rec: dict) -> str:
    """The router's evidence packet from a PAIChecker record (the record format of PAIChecker's collection)."""
    pd = rec["pr_description"]
    issues = rec.get("issue_number") or []
    if rec.get("issue_discussion_before_merge") or issues:
        disc = _budget([f"[{c['created_at']}] {c['author']} ({c['author_association']})"
                        + (" [posted after the PR was opened]" if c["after_pr_opened"] else "")
                        + (f" on issue #{c['issue']}" if len(issues) > 1 else "") + ":\n" + _rclip(c["body"], 6000)
                        for c in rec["issue_discussion_before_merge"]], 30000)
        disc_src = "GitHub issue comments created before the merge"
    else:
        disc = _rclip(t.get("hints_text") or "", 30000) or "(none)"
        disc_src = "no linked issue was retrieved; this is the dataset's hints_text (issue comments before the PR)"
    ism = _budget([f"issue #{i['issue']} ({i['title']!r}, opened {i['created_at']} by {i['author']}) is mentioned by: "
                   + (json.dumps(i["mentions"]) if i["mentions"] else "nothing in this repository")
                   for i in rec.get("is_issue_mentioned") or []], 8000)
    rel = _budget([json.dumps(dict(x), ensure_ascii=False) for x in rec.get("related_prs_and_issues") or []], 16000)
    prc = _budget([f"[{c['created_at']}] {c['author']} ({c['author_association']}):\n{_rclip(c['body'], 4000)}"
                   for c in rec.get("pr_comments") or []], 14000)
    rev = _budget([f"[{v['created_at']}] {v['author']} ({v['author_association']}) {v['state']}:\n{_rclip(v['body'], 3000)}"
                   + "".join(f"\n  - on {c['path']}: {_rclip(c['body'], 1500)}" for c in v["comments"])
                   for v in rec.get("review_comments") or []], 12000)
    files = "\n".join(f"{f['change_type']} {f['path']} (+{f['additions']} -{f['deletions']})"
                      for f in (rec.get("files") or [])[:100])
    return "\n\n".join([
        f"instance_id: {t['instance_id']}\nrepository: {t['repo']}\ncurrent PR number: {rec['pr_number']}\n"
        f"issue_number: {', '.join('#' + i for i in issues) if issues else '(no linked issue retrieved)'}",
        f"<problem_statement>\n{_rclip(t['statement_original'], 15000)}\n</problem_statement>",
        f"<issue_discussion source=\"{disc_src}\">\n{disc}\n</issue_discussion>",
        f"<is_issue_mentioned>\n{ism}\n</is_issue_mentioned>",
        f"<pr_description>\ntitle: {pd['title']}\nstate: {pd['state']}, merged: {pd['merged']}, merged_at: "
        f"{pd['merged_at']}, opened: {pd['created_at']} by {pd['author']}\n\n{_rclip(pd['body'], 8000)}\n</pr_description>",
        f"<commit_message>\n{_budget([_rclip(c, 1500) for c in rec.get('commit_message') or []], 5000)}\n</commit_message>",
        f"<pr_comments>\n{prc}\n</pr_comments>",
        f"<review_comments>\n{rev}\n</review_comments>",
        f"<is_pr_mentioned>\n{_budget([json.dumps(x) for x in rec.get('is_pr_mentioned') or []], 6000)}\n</is_pr_mentioned>",
        f"<related_prs_and_issues>\n{rel}\n</related_prs_and_issues>",
        f"<files>\n{files or '(none)'}\n</files>",
        f"<patch description=\"production-code patch\">\n{_rclip(t['patch'], 30000)}\n</patch>",
        f"<test_patch>\n{_rclip(t['test_patch'], 22000)}\n</test_patch>",
    ])


ROUTER_LABELS = ("SC", "FP", "DP", "IS", "UL", "Others", "No Misalignment")


def parse_router(j: dict):
    cl = j.get("classifications")
    if not isinstance(cl, list) or not cl:
        return None, ["router_parse"]
    labs = []
    for c in cl:
        if not isinstance(c, dict) or c.get("label") not in ROUTER_LABELS:
            return None, ["router_parse"]
        if c["label"] == "IS" and c.get("is_kind") not in ("IS-1", "IS-2"):
            return None, ["router_parse"]
        labs.append(c["label"])
    if len(set(labs)) != len(labs) or ("No Misalignment" in labs and len(labs) > 1):
        return None, ["router_parse"]
    return j, []


def pai(ctx, t: dict) -> dict:
    human = human_labels(ctx).get(t["instance_id"])
    if human is not None:
        labs = sorted(pai_main(human))
        return {"source": "human", "human_label": human, "labels": labs,
                "codes": ["DEFECTIVE_PR_HUMAN_LABEL"] if "DP" in labs else [], "flags": []}
    rec = pai_record(ctx, t["instance_id"])
    if not ctx.scfg(NAME).get("router", True) or not rec or not (rec.get("retrieval") or {}).get("pr_found", True):
        return {"source": "none", "labels": [], "codes": [], "flags": ["defective_pr_not_screened"]}
    r = judged(ctx.llm, "pai_router", "pai", ctx.prompts.text("pai_router"), render_router(t, rec), parse_router,
               attempts(ctx))
    if r.get("dry_run"):
        return {"source": "router", "undecided": True, "dry_run": True}
    if r["parsed"] is None:
        return {"source": "router", "undecided": True, "router": _brief(r)}
    labs = sorted({c["label"] for c in r["parsed"]["classifications"]})
    return {"source": "router", "labels": labs, "codes": ["DEFECTIVE_PR_ROUTER"] if "DP" in labs else [], "flags": [],
            "router": _brief(r)}


# ------------------------------------------------------------------ the stage
def run(ctx, iid: str) -> dict:
    t = ctx.effective(iid, with_repair=False)
    n = attempts(ctx)
    out: dict = {"prompts": ctx.prompts.shas(*STEPS), "prompt_matches_sums": all(ctx.prompts.meta(s)["matches_sums"] for s in STEPS)}
    reasons, flags, undecided, dry = [], [], [], False
    # statement hygiene (1a)
    sysp, user = render_1a(ctx, t)
    r = judged(ctx.llm, "hygiene", "1a", sysp, user, parse_1a, n)
    out["hygiene"] = _brief(r)
    if r.get("dry_run"):
        dry = True
    elif r["parsed"] is None:
        undecided.append("hygiene")
    else:
        reasons += decide_1a(r["parsed"])
    if reasons:  # a removed task reaches no later screen (as run: each screen skips tasks removed before it)
        return dict(out, decision="drop", reasons=reasons, flags=flags)
    # defective PR
    p = pai(ctx, t)
    out["pai"] = p
    dry |= bool(p.get("dry_run"))
    if p.get("undecided") and not p.get("dry_run"):
        undecided.append("defective_pr")
    reasons += p.get("codes") or []
    flags += p.get("flags") or []
    if reasons:
        return dict(out, decision="drop", reasons=reasons, flags=flags)
    # misleading statement (1d)
    m = misleading(ctx, t)
    out["misleading"] = m
    dry |= bool(m.get("dry_run"))
    if m["decision"] == "undecided" and not m.get("dry_run"):
        undecided.append("misleading")
    reasons += m.get("codes") or []
    flags += m.get("flags") or []
    if reasons:
        return dict(out, decision="drop", reasons=reasons, flags=flags)
    # solution in issue (1b; flags only)
    sysp, user = render_1b(ctx, t)
    with ThreadPoolExecutor(2) as ex:
        res = dict(zip(FAMS, ex.map(lambda f: judged(ctx.llm, f, "1b", sysp, user, parse_1b, n), FAMS)))
    out["solution_in_issue"] = {f: _brief(res[f]) for f in FAMS}
    if any(res[f].get("dry_run") for f in FAMS):
        dry = True
    elif res["claude"]["parsed"] is None or res["gpt"]["parsed"] is None:
        undecided.append("solution_in_issue")
    else:
        flags += labels_1b(res["claude"]["parsed"], res["gpt"]["parsed"])
    if dry or undecided:
        return dict(out, complete=False, decision="pending", reasons=["dry_run"] if dry else
                    [f"undecided:{x}" for x in undecided], flags=flags)
    return dict(out, decision="keep", reasons=[], flags=flags, IS_route=m.get("route") == "IS",
                IS_quotes=m.get("quotes"))
