"""Repair: one attempt per task, by the triage plan.

Statement routes (S-a, S-lit, S-keep, S-scope, IS, combined): the repair writer (step 5a, role ``writer``) sees the
issue, the routes, the S-a template requirements, the IS quote candidates, the routed assertions and the hidden test
sources (never the gold patch or the rest of the discussion); up to 3 attempts on a reply that does not parse or
fails the form check of G1. Then the repair checks (the prompts call them gates), all required:
  G1  mechanical check: deterministic token and form rules (gates/g1.py);
  G2  traceability (step 5b, both families): every routed assertion covered and traced, every item accurate and
      mapped to a routed assertion; extra content is recorded (flag ``repair_untraced_content``), not failed; a judge
      without a usable verdict after 3 attempts fails;
  G3  leak judges (step 5c, roles ``leak_judge_a`` / ``leak_judge_b``): a judge flags on any leak finding (the IS
      frame excepted), leak level >= 1 or solution_given 2; a flag from either fails; no usable verdict = flag;
  G4  the base-model check runs in the re-audit (reaudit.py). With ``stages.repair.blind_solve: true`` G4 also runs
      the blind solve (step 6a) here, by the writer's family: a violated routed assertion, or an "ambiguous" note on a
      covering item when no attempt satisfied a routed assertion -> fail (BLIND_SOLVE_FAILED).
  Pass -> decision repaired, overlay {spec_block}; fail -> drop REPAIR_CHECK_FAILED (``failed``: the checks that
  failed); REPAIR_WRITER_FAILED: no usable draft; REPAIR_INPUTS_INCOMPLETE: the triage plan lacks a template or quote.
Test routes (T-b, P2P-relax), from the read's relaxation (verified.diff, negatives, positives): gold passes and the
empty patch fails 3 / 3 on the relaxed tests, every positive (incl. the executed witness) passes, every negative
(incl. the accept-and-ignore patch) fails; the strict flip gate: a measured rollout that fails the original tests
and passes the relaxed ones is judged by the flip judge (step 5d, both families, counterexamples executed); a
confirmed defect or both families incorrect fails the repair. Pass -> repaired, overlay {test_patch}; fail -> drop
RELAX_CHECK_FAILED (``failed_checks``: why).
"""
from __future__ import annotations

import copy
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..gates import g1 as G1
from ..gates.stats import leak_flag
from ..harness.grader import Grader, bad_ids
from ..llm import DryRun
from ..util import clip_mid, parse_json, sha, sha_json
from . import fp as FP
from .escalate import discussion

NAME = "repair"
DEPENDS = ["triage"]
FAMS = ("claude", "gpt")
ROUTE_ORDER = ["S-a", "IS", "S-lit", "S-keep", "S-scope"]
TRACE = {"traced", "partly", "not_traced"}
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
    return (ctx.store.get("triage", iid) or {}).get("decision") == "repair"


def key(ctx, iid: str) -> str:
    tr = ctx.store.get("triage", iid) or {}
    return sha_json({"triage": tr.get("key"), "plan": tr.get("plan"), "settings": ctx.scfg(NAME),
                     "prompts": ctx.prompts.shas("spec_writer", "g2_traceability", "g3_leak", "blind_solver", "flip_judge"),
                     "roles": {r: ctx.llm.model(r) for r in ("writer", "leak_judge_a", "leak_judge_b")}})


# ------------------------------------------------------------------ the case
def s3_rows(rec: dict) -> list[dict]:
    out = []
    for r in rec["canonical"]:
        if r.get("p2p"):
            beh = r.get("texts") or ""
        else:
            m = next((m for m in r["members"] if m["src"].startswith("3a:claude") and m.get("assertion")), None) or \
                next((m for m in r["members"] if m.get("assertion")), {})
            beh = m.get("assertion") or ""
        site = (r.get("sites") or [{}])[0] if r.get("sites") else {}
        lits = r.get("literals") or []
        out.append({"id": r["id"], "behaviour": beh, "literal": lits[0] if lits else "", "literal_kind": "",
                    "file": site.get("file", "?"), "line": site.get("line", "?"), "code": site.get("code", ""),
                    "tests": " ".join(r["tests"]), "api": r.get("api") or [], "_all_literals": lits,
                    "_p2p": bool(r.get("p2p"))})
    return out


def render_assertions(rows: list[dict]) -> str:
    out_ = []
    for x in rows:
        lines = [f"{x['id']}. {x.get('behaviour', '')}"]
        if x.get("literal"):
            lines.append(f"    pinned literal ({x.get('literal_kind') or 'value'}): {x['literal']}")
        lines.append(f"    location: {x['file']}:{x['line']}   {x.get('code', '')}")
        lines.append(f"    tests: {x.get('tests', '')}")
        if x.get("api"):
            lines.append(f"    api under test: {', '.join(map(str, x['api']))}")
        out_.append("\n".join(lines))
    return "\n\n".join(out_)


def make_case(ctx, t: dict, tr: dict) -> dict:
    plan, rec = tr["plan"], tr["record"]
    routes = sorted(dict.fromkeys(plan.get("routes") or [plan["route"]]), key=lambda x: ROUTE_ORDER.index(x)
                    if x in ROUTE_ORDER else 99)
    routed = list(plan["routed"])
    sa = list(plan.get("templates") or []) if "S-a" in routes else []
    c = {"task": t, "route": "+".join(routes), "routes": routes, "routed": routed,
         "row_routes": dict(plan.get("row_routes") or {r: routes[0] for r in routed}), "sa_templates": sa,
         "templates": list(sa), "quote_candidates": list(plan.get("quote_candidates") or []), "quote": None,
         "rows": s3_rows(rec), "restriction": plan.get("restriction_statement_only")}
    if "IS" in routes and len(c["quote_candidates"]) < 2:
        c["error"] = "IS needs a valid quote from both families"
    elif "S-a" in routes and not sa:
        c["error"] = "S-a without a template requirement"
    elif [r for r in routed if r not in {x["id"] for x in c["rows"]}]:
        c["error"] = "routed rows not in the canonical list"
    return c


def routed_rows(c: dict) -> list[dict]:
    return [r for r in c["rows"] if r["id"] in c["routed"]]


def p2p_block(ctx, c: dict) -> tuple[list[str], str]:
    ids = [x for r in routed_rows(c) if r["_p2p"] for x in r["tests"].split()]
    return ids, (ctx.sources(c["task"]).sources_of(ids) if ids else "")


def tests_text(ctx, c: dict) -> str:
    _, extra = p2p_block(ctx, c)
    return ctx.sources(c["task"]).f2p_sources() + (("\n\n" + extra) if extra else "")


def _form_args(c: dict) -> tuple:
    if len(c["routes"]) == 1:
        return (c["templates"][0] if c["templates"] else None), c["routes"][0]
    return list(c["templates"]), list(c["routes"])


# ------------------------------------------------------------------ writer (5a)
def draft_user(ctx, c: dict) -> str:
    t = c["task"]
    parts = [f"Repository: {t['repo']}", f"<issue>\n{clip_mid(t['statement_original'], 40000)}\n</issue>",
             f"ROUTE: {c['route']}"]
    if len(c["routes"]) > 1 or len({c["row_routes"].get(r) for r in c["routed"]}) > 1:
        parts.append("ROUTE of each routed assertion:\n" + "\n".join(f"- {r}: {c['row_routes'].get(r)}" for r in c["routed"]))
    for tpl in c["sa_templates"]:
        parts.append(f"<template_requirement>\n{tpl}\n</template_requirement>")
    if "IS" in c["routes"]:
        parts.append("<quote_candidates>\n" + "\n".join(f"- {q}" for q in c["quote_candidates"]) + "\n</quote_candidates>")
    parts += [f"<routed_assertions>\n{render_assertions(routed_rows(c))}\n</routed_assertions>",
              f"<hidden_test_sources>\n{clip_mid(tests_text(ctx, c), 30000)}\n</hidden_test_sources>"]
    return "\n\n".join(parts)


def parse_draft(reply: str | None, c: dict) -> dict | None:
    j = parse_json(reply)
    if not j or not isinstance(j.get("requirements"), list) or not all(isinstance(x, str) for x in j["requirements"]):
        return None
    ifc = j.get("interfaces") or []
    if not isinstance(ifc, list) or not all(isinstance(e, dict) and set(e) == set(G1.IFC_KEYS) for e in ifc):
        return None
    reqs = [x.strip() for x in j["requirements"] if x.strip()]
    ifc = [{k: str(e[k]).strip() for k in G1.IFC_KEYS} for e in ifc]
    templates, span = list(c["sa_templates"]), None
    if "IS" in c["routes"]:
        k = len(templates)
        span = G1.is_quote_of(reqs[k]) if len(reqs) > k else None
        if span is None:
            return None
        reqs[k] = G1.is_template(span)
        templates.append(reqs[k])
    tpl, rt = _form_args(dict(c, templates=templates))
    if not G1.form(reqs, ifc, tpl, rt)["ok"]:
        return None
    return {"requirements": reqs, "interfaces": ifc, "why": j.get("why", ""), "templates": templates, "span": span}


def chat(ctx, who: str, system: str, user: str, tag: str) -> dict:
    try:
        return ctx.llm.chat(who, system, user, tag=tag, stage=NAME)
    except DryRun:
        return {"dry_run": True}


def write(ctx, c: dict) -> dict:
    sysp, user = ctx.prompts.text("spec_writer"), draft_user(ctx, c)
    attempts = []
    for k in range(int(ctx.scfg(NAME).get("writer_attempts", 3))):
        res = chat(ctx, "writer", sysp, user, f"5a:{c['task']['instance_id']}:a{k}")
        if res.get("dry_run"):
            return {"dry_run": True}
        p = parse_draft(res.get("text"), c) if res.get("model_ok") else None
        attempts.append({"attempt": k, "model_ok": res.get("model_ok"), "parsed": p, "cached": res.get("cached")})
        if p:
            return {"parsed": p, "attempts": attempts}
    return {"parsed": None, "attempts": attempts,
            "transport_failed": all(not a["model_ok"] for a in attempts)}


def items_of(c: dict, reqs: list[str], ifc: list[dict]) -> dict:
    n_sa = len(c["sa_templates"])
    is_idx = n_sa if "IS" in c["routes"] else -1
    items = []
    for i, r in enumerate(reqs):
        cls = "C" if i == is_idx else "A"
        it = {"sid": f"S{len(items) + 1}", "kind": "requirement", "class": cls, "text": r}
        if cls == "C":
            it["quote"] = G1.norm_ws(c["quote"])
        items.append(it)
    for e in ifc:
        items.append({"sid": f"S{len(items) + 1}", "kind": "interface", "class": "I", "entry": e,
                      "text": "; ".join(f"{x.capitalize()}: {e[x]}" for x in G1.IFC_KEYS)})
    block = G1.spec_text(G1.render(reqs, ifc))
    return {"items": items, "requirements": reqs, "interfaces": ifc, "block": block, "block_sha256": sha(block)}


# ------------------------------------------------------------------ gates
def g1(ctx, c: dict) -> dict:
    t = c["task"]
    rows = [dict(r, literal=L) for r in routed_rows(c) for L in (r["_all_literals"] or [""])]
    Sx = G1.sources(t["statement_original"], rows, tests_text(ctx, c))
    present = lambda toks: ctx.repos.present_at_base(t["repo"], t["base_commit"], toks)  # noqa: E731
    per = []
    for it in c["items"]:
        texts = ([("requirement", it["quote"] if it["class"] == "C" else it["text"])] if it["kind"] == "requirement"
                 else G1.interface_texts(it["entry"]))
        for fld, text in texts:
            r = G1.check(text, Sx, present, t["patch"])
            per.append({"sid": it["sid"], "field": fld, "checked_text": text, "pass": r["pass"], "reasons": r["reasons"]})
    tpl, rt = _form_args(c)
    f = G1.form(c["requirements"], c["interfaces"], tpl, rt)
    qc = G1.quote_check(c["quote"], discussion(t), c["quote_candidates"]) if "IS" in c["routes"] else None
    return {"pass": all(p["pass"] for p in per) and f["ok"] and (qc is None or qc["ok"]), "items": per, "form": f,
            "quote_check": qc, "gate": G1.G1_VERSION}


def g2_user(ctx, c: dict) -> str:
    t, src = c["task"], ctx.sources(c["task"])
    lab = {"requirement": "requirement", "interface": "new interface entry"}
    items = "\n".join(f"{it['sid']} ({lab[it['kind']]}). {it['text']}" for it in c["items"])
    ids, extra = p2p_block(ctx, c)
    f = t["FAIL_TO_PASS"]
    return "\n\n".join([
        f"Repository: {t['repo']}",
        f"<issue>\n{clip_mid(t['statement_original'], 40000)}\n</issue>",
        f"<specification_as_rendered>\n{c['block']}\n</specification_as_rendered>",
        f"<specification_items>\n{items}\n</specification_items>",
        f"<canonical_assertions>\n{render_assertions(c['rows'])}\n</canonical_assertions>",
        "ROUTED assertions (the specification may state only their requirements and pinned details): "
        + ", ".join(c["routed"]),
        f"<reference_solution>\n{clip_mid(t['patch'], 40000)}\n</reference_solution>",
        f"<test_patch_with_post_image_line_numbers>\n{clip_mid(src.numbered_patch(), 50000)}\n"
        "</test_patch_with_post_image_line_numbers>",
        f"<fail_to_pass_test_sources>\n{clip_mid(src.f2p_sources(), 30000)}\n</fail_to_pass_test_sources>"]
        + ([f"<pass_to_pass_test_sources>\n{extra}\n</pass_to_pass_test_sources>"] if extra else [])
        + ["FAIL_TO_PASS tests:\n" + "\n".join(f[:80]) + (f"\n... and {len(f) - 80} more" if len(f) > 80 else "")])


def parse_g2(reply: str | None, c: dict) -> dict | None:
    j = parse_json(reply)
    if not j or not isinstance(j.get("items"), list) or not isinstance(j.get("routed"), list):
        return None
    sids, routed, all_ids = [it["sid"] for it in c["items"]], c["routed"], {r["id"] for r in c["rows"]}
    got_s, got_a = {}, {}
    for x in j["items"]:
        if not isinstance(x, dict) or x.get("id") not in sids or x["id"] in got_s:
            return None
        if not isinstance(x.get("maps_to"), list) or not set(map(str, x["maps_to"])) <= all_ids:
            return None
        if not isinstance(x.get("accurate"), bool) or not isinstance(x.get("extra_content"), list):
            return None
        got_s[x["id"]] = x
    for x in j["routed"]:
        if not isinstance(x, dict) or x.get("id") not in routed or x["id"] in got_a:
            return None
        if not isinstance(x.get("covered_by"), list) or x.get("trace_with_spec") not in TRACE:
            return None
        got_a[x["id"]] = x
    if set(got_s) != set(sids) or set(got_a) != set(routed):
        return None
    return {"sentences": j["items"], "routed": j["routed"], "summary": j.get("summary", "")}


def _fnorm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\"'“”‘’`]", "", s or "")).strip().strip(".:;,").strip().lower()


def frame_filter(p: dict, c: dict) -> tuple[dict, list]:
    q = copy.deepcopy(p)
    cls = {it["sid"]: it["class"] for it in c["items"]}
    frame, dropped = _fnorm(G1.IS_FRAME), []
    for s in q["sentences"]:
        if cls.get(s["id"]) != "C":
            continue
        keep = []
        for e in s["extra_content"]:
            span = e.get("span") if isinstance(e, dict) else str(e)
            n = _fnorm(span)
            (dropped.append({"sid": s["id"], "span": span}) if (not n or n in frame) else keep.append(e))
        s["extra_content"] = keep
    return q, dropped


def g2_rule(p: dict | None, c: dict) -> dict:
    """Pass iff every routed assertion is covered and traced, every item accurate and mapped to a routed assertion;
    extra-content spans are returned (logged), not failed."""
    if p is None:
        return {"pass": False, "reasons": ["no_verdict"], "extra_content": []}
    q, dropped = frame_filter(p, c)
    why, extra, mapped = [], [], set()
    for x in q["sentences"]:
        m = set(map(str, x["maps_to"]))
        mapped |= m
        if not m & set(c["routed"]):
            why.append(f"{x['id']}: maps to no routed assertion (maps_to {sorted(m)})")
        if x["accurate"] is not True:
            why.append(f"{x['id']}: not accurate ({x.get('accuracy_note', '')})")
        extra += [{"item": x["id"], "span": e.get("span") if isinstance(e, dict) else str(e)} for e in x["extra_content"]]
    for x in q["routed"]:
        if not x["covered_by"] or x["id"] not in mapped:
            why.append(f"{x['id']}: no covering sentence (covered_by {x['covered_by']}, in maps_to {x['id'] in mapped})")
        if x["trace_with_spec"] != "traced":
            why.append(f"{x['id']}: {x['trace_with_spec']} with the specification (missing: {x.get('still_missing', '')})")
    return {"pass": not why, "reasons": why, "extra_content": extra, "frame_spans_dropped": dropped}


def g3_user(ctx, c: dict) -> str:
    t = c["task"]
    f = t["FAIL_TO_PASS"]
    user = (f"Repository: {t['repo']}\n\n<issue>\n{clip_mid(t['statement_original'] or '', 40000)}\n</issue>\n\n"
            f"<specification>\n{c['block']}\n</specification>\n\n<reference_solution>\n"
            f"{clip_mid(t['patch'], 60000)}\n</reference_solution>\n\n<hidden_tests>\n{clip_mid(t['test_patch'], 60000)}\n"
            f"</hidden_tests>\n\nHidden tests that must go from failing to passing:\n" + "\n".join(f[:80])
            + (f"\n... and {len(f) - 80} more" if len(f) > 80 else ""))
    ids, extra = p2p_block(ctx, c)
    if extra:
        user += ("\n\nHidden tests that must keep passing (PASS_TO_PASS) and are graded with the tests above:\n"
                 + "\n".join(ids) + f"\n\n<pass_to_pass_test_sources>\n{extra}\n</pass_to_pass_test_sources>")
    return user


def parse_g3(reply: str | None) -> dict | None:
    j = parse_json(reply)
    if not j or not isinstance(j.get("findings"), list) or j.get("leak_level") not in (0, 1, 2) \
            or isinstance(j.get("leak_level"), bool) or j.get("solution_given") not in (0, 1, 2):
        return None
    return j


def g3_flag(p: dict | None, c: dict) -> dict:
    frame = _fnorm(G1.IS_FRAME)
    has_quote = any(it["class"] == "C" for it in c["items"])
    f = leak_flag(p, (lambda s: not _fnorm(s) or _fnorm(s) in frame) if has_quote else None)
    return {"flag": True if f is None else f, "no_verdict": f is None,
            "leak_level": (p or {}).get("leak_level"), "solution_given": (p or {}).get("solution_given")}


def judge(ctx, c: dict, gate: str, who: str) -> dict:
    sysp = ctx.prompts.text("g2_traceability" if gate == "G2" else "g3_leak")
    user = g2_user(ctx, c) if gate == "G2" else g3_user(ctx, c)
    p, attempts = None, []
    for k in range(3):
        res = chat(ctx, who, sysp, user, f"{gate}:{c['task']['instance_id']}:{c['block_sha256'][:12]}:a{k}")
        if res.get("dry_run"):
            return {"dry_run": True}
        p = (parse_g2(res.get("text"), c) if gate == "G2" else parse_g3(res.get("text"))) if res.get("model_ok") else None
        attempts.append({"attempt": k, "model_ok": res.get("model_ok"), "parsed_ok": p is not None})
        if p is not None:
            break
    v = g2_rule(p, c) if gate == "G2" else g3_flag(p, c)
    return {"judge": who, "model": ctx.llm.model(who), "verdict": v, "parsed": p, "attempts": attempts,
            "infra": p is None and all(not a["model_ok"] for a in attempts)}


def run_gates(ctx, c: dict) -> dict:
    out = {"G1": g1(ctx, c)}
    jobs = [("G2", "claude"), ("G2", "gpt"), ("G3", "leak_judge_a"), ("G3", "leak_judge_b")]
    with ThreadPoolExecutor(len(jobs)) as ex:
        res = dict(zip(jobs, ex.map(lambda x: judge(ctx, c, *x), jobs)))
    if any(r.get("dry_run") for r in res.values()):
        return {"dry_run": True}
    out["G2"] = {j: res[("G2", j)] for j in ("claude", "gpt")}
    out["G3"] = {j: res[("G3", j)] for j in ("leak_judge_a", "leak_judge_b")}
    out["gates"] = {"G1": out["G1"]["pass"], "G2": all(v["verdict"]["pass"] for v in out["G2"].values()),
                    "G3": not any(v["verdict"]["flag"] for v in out["G3"].values())}
    out["failed"] = [g for g in ("G1", "G2", "G3") if not out["gates"][g]]
    out["infra"] = [f"{g}:{j}" for (g, j), r in res.items() if r.get("infra")]
    return out


# ------------------------------------------------------------------ G4: blind solve (optional) and labels
def label_row(row: dict, status: dict, lines_of) -> str:
    """satisfied (the test passes, or fails after the assertion), violated (fails at the assertion itself),
    not_reached (fails before it, or the run crashed)."""
    labs = []
    for tid in row["tests"]:
        if status.get(tid):
            labs.append("satisfied")
            continue
        site = next((s for s in row["sites"] if s.get("test") == tid), None) or (row["sites"][0] if row["sites"] else None)
        fl = lines_of(tid)
        if not site or not fl:
            labs.append("not_reached")
            continue
        inside = [x for x in fl if x["file"].endswith(site["file"]) or site["file"].endswith(x["file"])]
        if not inside:
            labs.append("not_reached")
            continue
        ln = inside[-1]["line"]
        labs.append("violated" if ln == site["line"] else "satisfied" if ln > site["line"] else "not_reached")
    if "violated" in labs:
        return "violated"
    if labs and all(x == "satisfied" for x in labs):
        return "satisfied"
    return "not_reached"


def routed_sites(rec: dict, routed: list[str]) -> list[dict]:
    rows = {r["id"]: r for r in rec["canonical"]}
    return [{"row": rid, "p2p": bool((rows.get(rid) or {}).get("p2p")), "tests": (rows.get(rid) or {}).get("tests") or [],
             "sites": (rows.get(rid) or {}).get("sites") or []} for rid in routed]


def labels_for(rows: list[dict], gr: dict | None) -> dict:
    from ..harness.grader import read_log
    from ..harness.logs import failure_lines
    if not gr or gr.get("harness_error") or gr.get("apply") == "fail" or not gr.get("tests_started") or gr.get("timeout"):
        return {r["row"]: "not_reached" for r in rows}
    log = read_log(gr.get("log"))
    fb, pb = bad_ids(gr)
    bad = set(fb) | set(pb)
    status = {tid: tid not in bad for r in rows for tid in r["tests"]}
    return {r["row"]: label_row(r, status, lambda tid: failure_lines(log, tid)) for r in rows}


def blind_solve(ctx, t: dict, c: dict, st: dict) -> dict:
    """The blind solve of G4 with the writer's family (the step-6a prompt, verbatim, placeholders filled),
    ``blind_attempts`` attempts (+1 when inconclusive). Requires ``agents.<writer family>.blind_command``: an agent CLI
    whose only tool is the MCP shell of data/box_mcp.py; see "Agents" in docs/WORKFLOW.md."""
    from ..agents.runner import AgentRunner
    from ..gates.stats import memorised
    fam = ctx.cfg["llm"]["roles"].get("writer") if isinstance(ctx.cfg["llm"]["roles"].get("writer"), str) else "claude"
    runner = AgentRunner(ctx.cfg, NAME)
    if runner.dry:
        return {"dry_run": True}
    prompt = ctx.prompts.text("blind_solver").replace("@@WORKING_DIR@@", "/testbed").replace(
        "@@PROBLEM_STATEMENT@@", (t["statement_original"].rstrip("\n") + "\n\n" + c["block"]).rstrip("\n"))
    rows = routed_sites(st["record"], c["routed"])
    cov: dict[str, set] = {}
    for j in (st.get("G2") or {}).values():
        for x in ((j.get("parsed") or {}).get("routed") or []):
            for sid in x.get("covered_by") or []:
                cov.setdefault(str(sid), set()).add(x["id"])
    atts = []
    n = int(ctx.scfg(NAME).get("blind_attempts", 3))
    for k in range(n + 1):
        if k == n and atts and verdict_g4(atts, c["routed"]) != "inconclusive":
            break
        r = runner.blind_attempt(t, fam, prompt, ctx.store.work(NAME, t["instance_id"]) / f"blind{k}")
        patch = r.get("patch") or ""
        gr = grader(ctx).run(t, patch, f"blind{k}") if patch.strip() else None
        labs = labels_for(rows, gr)
        mem = memorised(patch, t["patch"], t["instance_id"]) if patch else {"memorised": False}
        if mem["memorised"]:
            labs = {k_: ("not_reached" if v == "satisfied" else v) for k_, v in labs.items()}
        amb = []
        for note in ((r.get("notes") or {}).get("spec_notes") or []):
            if isinstance(note, dict) and note.get("reading") == "ambiguous":
                sid = next((it["sid"] for it in c["items"] if G1.norm_ws(it["text"]) == G1.norm_ws(note.get("sentence"))
                            or (it["kind"] == "interface" and G1.norm_ws(note.get("sentence") or "").endswith(
                                G1.norm_ws(it["entry"].get("name", ""))))), None)
                amb.append({"sid": sid, "covers": sorted(cov.get(sid or "", []))})
        atts.append({"attempt": k, "labels": labs, "ambiguous": amb, "memorised": mem["memorised"], "error": r.get("error")})
    return {"decision": verdict_g4(atts, c["routed"]), "attempts": atts}


def verdict_g4(atts: list[dict], routed: list[str]) -> str:
    labels = [a["labels"] for a in atts]
    if any(v == "violated" for L in labels for v in L.values()):
        return "fail"
    sat = any(L.get(r) == "satisfied" for L in labels for r in routed)
    if not sat and any(x["covers"] for a in atts for x in a["ambiguous"]):
        return "fail"
    if routed and all(any(L.get(r) == "satisfied" for L in labels) for r in routed):
        return "pass"
    return "inconclusive"


# ------------------------------------------------------------------ test routes
def relax(ctx, t: dict, tr: dict) -> dict:
    plan = tr["plan"]
    rx, d = plan.get("relax") or {}, Path(plan.get("relax_dir") or ".")
    vp = d / str(rx.get("verified_file") or "relax/verified.diff")
    if not rx or not vp.exists():
        return {"decision": "drop", "reasons": ["RELAX_NO_SPEC"]}
    tt = dict(t, test_patch=vp.read_text())
    g = grader(ctx)
    why = []
    gold = [g.run(tt, t["patch"], "gold") for _ in range(3)]
    empty = [g.run(tt, "", "empty") for _ in range(3)]
    if not all(r.get("resolved") for r in gold):
        why.append("gold does not pass the relaxed tests 3/3")
    if any(r.get("resolved") for r in empty):
        why.append("the empty patch passes the relaxed tests")
    pos = [(p, d / p["file"]) for p in rx.get("positives") or []]
    esc_wd = ctx.store.work("escalate", t["instance_id"])
    pos += [({"key": f"esc_{f}"}, esc_wd / f / "W" / "out" / "witness.diff") for f in FAMS]
    for p, f in pos:
        if f.exists() and f.read_text().strip() and not g.run(tt, f.read_text(), f"pos-{p.get('key')}").get("resolved"):
            why.append(f"positive {p.get('key')} fails")
    negs = rx.get("negatives") or []
    if not negs and not rx.get("accept_and_ignore_na"):
        why.append("no negative")
    for p in negs:
        f = d / p["file"]
        if not f.exists() or g.run(tt, f.read_text(), f"neg-{p.get('key')}").get("resolved"):
            why.append(f"negative {p.get('key')} passes")
    flips = []
    for r in (ctx.store.get("measure", t["instance_id"]) or {}).get("rollouts") or []:
        if r.get("patch") and not r.get("resolved") and g.run(tt, r["patch"], f"flip-{r['name']}").get("resolved"):
            flips.append(r)
    change = rx.get("description") or ""
    for fl in flips:
        res = FP.judge_patch(ctx, t, fl["patch"], step="flip_judge", change=change)
        if res.get("dry_run"):
            return {"complete": False, "decision": "pending", "reasons": ["dry_run"]}
        if res["pair"] == "both_incorrect" or res["confirmed"]:
            why.append(f"flip {fl['name']}: {res['pair']}, {res['confirmed']} confirmed counterexample(s)")
    if why:
        return {"decision": "drop", "reasons": ["RELAX_CHECK_FAILED"], "failed_checks": why, "flips": len(flips)}
    return {"decision": "repaired", "reasons": ["TEST_RELAXED"], "flips": len(flips),
            "overlay": {"test_patch": tt["test_patch"], "change_note": change}}


# ------------------------------------------------------------------ the stage
def run(ctx, iid: str) -> dict:
    tr = ctx.store.get("triage", iid)
    t = ctx.effective(iid, with_repair=False)
    if tr["plan"].get("kind") == "test":
        return relax(ctx, t, tr)
    c = make_case(ctx, t, tr)
    if c.get("error"):
        return {"decision": "drop", "reasons": ["REPAIR_INPUTS_INCOMPLETE"], "detail": c["error"]}
    w = write(ctx, c)
    if w.get("dry_run"):
        return {"complete": False, "decision": "pending", "reasons": ["dry_run"]}
    if not w["parsed"]:
        if w.get("transport_failed"):
            return {"complete": False, "decision": "pending", "reasons": ["writer: no model reply"]}
        return {"decision": "drop", "reasons": ["REPAIR_WRITER_FAILED"], "writer": w["attempts"]}
    c["templates"], c["quote"] = w["parsed"]["templates"], w["parsed"]["span"]
    c.update(items_of(c, w["parsed"]["requirements"], w["parsed"]["interfaces"]))
    g = run_gates(ctx, c)
    if g.get("dry_run"):
        return {"complete": False, "decision": "pending", "reasons": ["dry_run"]}
    if g["infra"]:
        return {"complete": False, "decision": "pending", "reasons": [f"judge without a model reply: {g['infra']}"]}
    row = {"route": c["route"], "routed": c["routed"], "block": c["block"], "block_sha256": c["block_sha256"],
           "items": c["items"], "G1": g["G1"], "G2": {j: {k: v[k] for k in ("model", "verdict", "parsed")} for j, v in g["G2"].items()},
           "G3": {j: {k: v[k] for k in ("model", "verdict", "parsed")} for j, v in g["G3"].items()}, "gates": g["gates"],
           "record": tr["record"], "flags": ["repair_untraced_content"] if any(
               v["verdict"].get("extra_content") for v in g["G2"].values()) else []}
    if g["failed"]:
        return dict(row, decision="drop", reasons=["REPAIR_CHECK_FAILED"], failed=g["failed"])
    if ctx.scfg(NAME).get("blind_solve"):
        b = blind_solve(ctx, t, c, row)
        row["G4"] = b
        if b.get("dry_run"):
            return dict(row, complete=False, decision="pending", reasons=["dry_run"])
        if b["decision"] == "fail":
            return dict(row, decision="drop", reasons=["BLIND_SOLVE_FAILED"])
    return dict(row, decision="repaired", reasons=["STATEMENT_REPAIRED"], overlay={"spec_block": c["block"]})

