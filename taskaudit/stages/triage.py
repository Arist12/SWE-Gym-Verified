"""Triage: repair or removal of every task with an FN verdict, a misleading-statement IS item or a P2P pin.

1. Defect record (mechanical): canonical assertion rows = the rows of every source (the FN rating of both families,
   the witness rounds of both families) clustered by their F2P test sets (Jaccard >= 0.5); per row the failure site
   (an executed witness's failure line on the test, else the empty-patch run's), the literals at the site and the
   public names the tests use; items = the rows a family calls UNDERDETERMINED / the failing-alternative rows / the
   debated tests (per verdict: CONFIRMED -> rows named by the debate or underdetermined in both families; FN_BOTH ->
   failing-alternative rows or both families; CONTESTED -> anything either family raised), plus an IS item of the
   misleading-statement screen and P2P items.
2. Mechanical triage. The rule ids (P2, H1-H9) and the repair routes (S-a, S-lit, S-keep, S-scope, IS, T-b,
   P2P-relax) are those of the triage rules quoted in prompts/7a_triage_reader.md, sections 3-5; the reason codes
   start with the rule id. P2 (an FN-flawed task the base model always solves, 8/8, is removed without a repair
   attempt), H1 environment, H2 private / internal pin, H3 mixed statement and test routes, H6 every F2P flagged with
   a both-family beyond_issue item, H9 (both P2P judges say delete_task); the route of each item (S-a with the
   mechanical interface sentence, S-lit, S-keep, S-scope, IS, T-b, P2P-relax), the restriction (a CONTESTED item takes
   statement routes only). Cases the rules leave open become ``needs_read``.
3. Triage read (step 7a, both families as agents in the task container): each family answers the open questions and
   decides repair (a route per row) or delete (the rule); the two answers are combined by fixed rules (agree; per-row
   merge; one family unsure; ...); disagreement deletes. Reasons decided by the read start with ``READ:``.
Row: decision repair | drop | no_defect | pending; reasons; plan {kind statement|test, routes, routed, row_routes,
templates, quote_candidates, restriction_statement_only}; record (the defect record); items; read.
"""
from __future__ import annotations

import builtins
import collections
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from ..agents.runner import AgentRunner
from ..gates.g1 import is_private_segment, quote_check, split_patch
from ..harness.grader import read_log
from ..harness.logs import failure_lines
from ..util import fence, sha_json
from . import measure as M
from .escalate import LETTER, discussion, prepare_in

NAME = "triage"
DEPENDS = ["escalate"]
FAMS = ("claude", "gpt")
UD_KINDS = {"guess_interface", "guess_literal", "guess_behaviour_choice", "beyond_issue"}
STMT_ROUTES = {"S-a", "S-lit", "S-keep", "S-scope", "IS"}
TEST_ROUTES = {"T-b", "P2P-relax"}
ROUTE_ORDER = ["S-a", "IS", "S-lit", "S-keep", "S-scope", "T-b", "P2P-relax"]
INTERNAL_SEG = {"core", "_libs", "internals"}
PIN_KINDS = {"guess_interface", "guess_literal"}
H6_READ = "H6_EVERY_F2P_FLAGGED_READ"


def wants(ctx, iid: str) -> bool:
    v = (ctx.store.get("escalate", iid) or {}).get("decision")
    s = ctx.store.get("screen", iid) or {}
    p = ctx.store.get("p2p", iid) or {}
    return v in ("FN_CONFIRMED", "CONTESTED") or bool(s.get("IS_route")) or bool(p.get("items")) or \
        bool(p.get("h9_delete_task"))


def key(ctx, iid: str) -> str:
    t = ctx.effective(iid, with_repair=False)
    return sha_json({"view": ctx.view_sha(t), "esc": (ctx.store.get("escalate", iid) or {}).get("key"),
                     "p2p": (ctx.store.get("p2p", iid) or {}).get("key"), "band": M.band(ctx, iid, t["statement"]),
                     "prompts": ctx.prompts.shas("triage_reader"), "settings": ctx.scfg(NAME)})


# ------------------------------------------------------------------ defect record
def full_ids(t: dict, tests) -> list[str]:
    f2p, out = t["FAIL_TO_PASS"], []
    for x in tests or []:
        x = str(x).strip()
        if not x:
            continue
        hit = [f for f in f2p if f == x] or [f for f in f2p if f.endswith(x) or f.endswith("::" + x)]
        if not hit:
            last = x.split("::")[-1]
            hit = [f for f in f2p if f.split("::")[-1] == last] or \
                  [f for f in f2p if f.split("::")[-1].split("[")[0] == last.split("[")[0]] or \
                  [f for f in f2p if last in f]
        out += hit
    return sorted(set(out))


WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}|\d+")


def text_sim(a, b) -> float:
    wa, wb = set(WORD.findall((a or "").lower())), set(WORD.findall((b or "").lower()))
    return len(wa & wb) / len(wa | wb) if (wa | wb) else 0.0


def jacc(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a | b) else 0.0


def canonical_rows(t: dict, a3: dict, w_rows: dict) -> list[dict]:
    srcs = [(f"3a:{f}", f, (a3.get(f) or {}).get("assertions") or []) for f in FAMS if a3.get(f)]
    srcs += [(f"W:{f}", f, w_rows.get(f) or []) for f in FAMS if w_rows.get(f)]
    rows: list[dict] = []
    for src, fam, items in srcs:
        for a in items:
            tests = set(full_ids(t, a.get("tests")))
            best, score = None, 0.0
            for r in rows:
                if src in r["_srcs"]:
                    continue
                j = jacc(tests, set(r["tests"]))
                if j < 0.5:
                    continue
                sc = j + 0.5 * max(text_sim(a.get("assertion"), m.get("assertion")) for m in r["members"])
                if sc > score:
                    best, score = r, sc
            if best is None:
                best = {"tests": sorted(tests), "members": [], "_srcs": set()}
                rows.append(best)
            best["tests"] = sorted(set(best["tests"]) | tests)
            best["_srcs"].add(src)
            best["members"].append({k: a.get(k) for k in ("assertion", "kind", "status", "evidence",
                                                         "rejected_alternative", "specified_in_discussion",
                                                         "discussion_quote", "passes_at_base")}
                                   | {"src": src, "family": fam, "row_id": a.get("id"), "repair_hint": a.get("repair")})
    for i, r in enumerate(rows, 1):
        r["id"] = f"A{i}"
        r.pop("_srcs")
    return rows


STR_LIT = re.compile(r"""(?:[rRbBuUfF]{0,2})("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""")
RAISES = re.compile(r"(?:raises|warns|assertRaises\w*|assertWarns\w*|deprecated_call|assert_produces_warning)\s*\(\s*"
                    r"([A-Za-z_][\w.]*)")


def literals_of(code: str) -> list[str]:
    """String constants and the raises / warns types of a site line (a simplification of the as-run AST rule)."""
    out = [m.group(1)[1:-1] for m in STR_LIT.finditer(code or "") if len(m.group(1)) > 2]
    out += [m.group(1) for m in RAISES.finditer(code or "")]
    return list(dict.fromkeys(x for x in out if x.strip()))


def interface_names(ctx, t: dict, texts: list[str]) -> list[dict]:
    """Public names a reader's row text names in code form, with the fields the triage rules read (simplified
    extraction): kind name | kwarg (with owner), private, internal segment, exists at base, used by the tests."""
    test_text = "\n".join(x[1:] for x in t["test_patch"].splitlines() if x.startswith("+") and not x.startswith("+++"))
    out, seen = [], set()
    for text in texts:
        for m in re.finditer(r"`([^`\n]+)`", text or ""):
            span = m.group(1).strip()
            km = re.fullmatch(r"(?:([A-Za-z_][\w.]*)\s*\(.*?)?\b([A-Za-z_]\w*)\s*=.*", span)
            if km and not span.startswith(("==", "!=")):
                cand = [("kwarg", km.group(2), km.group(1))]
            else:
                nm = re.match(r"([A-Za-z_][\w.]*)", span)
                cand = [("name", nm.group(1).rstrip("."), None)] if nm else []
            for kind, name, owner in cand:
                if (name, kind, owner) in seen or name in ("True", "False", "None"):
                    continue
                seen.add((name, kind, owner))
                segs = name.split(".")
                last = segs[-1]
                present = ctx.repos.present_at_base(t["repo"], t["base_commit"], {last})
                ext = segs[0] in getattr(sys, "stdlib_module_names", ()) and not present
                out.append({"name": name, "kind": kind, "owner": owner, "expanded": name,
                            "private": any(is_private_segment(s) for s in segs),
                            "internal_segment": bool(set(segs) & INTERNAL_SEG), "external": ext,
                            "builtin": last in dir(builtins),
                            "exists_at_base": "yes" if last in present else "no",
                            "used_by_tests": bool(re.search(rf"(?<![\w]){re.escape(last)}(?![\w])", test_text)),
                            "public_entry_point_used_by_tests": False})
    for n in out:
        n["public_entry_point_used_by_tests"] = n["used_by_tests"] and not n["private"]
    return out


def site_of(log_text: str, tid: str, test_files: set[str]) -> dict | None:
    inside = [x for x in failure_lines(log_text, tid)
              if any(x["file"].endswith(f) or f.endswith(x["file"]) for f in test_files)]
    return dict(inside[-1]) if inside else None


def build_record(ctx, t: dict) -> dict:
    iid = t["instance_id"]
    rate = ctx.store.get("fn_rate", iid) or {}
    erow = ctx.store.get("escalate", iid) or {}
    esc = erow.get("escalation") or {}
    verdict = erow.get("decision")
    a3 = {f: ((rate.get("families") or {}).get(f) or {}).get("parsed") for f in FAMS}
    w_rows = {}
    for f in FAMS:
        p = ctx.store.work("escalate", iid) / f / "W" / "stage.json"
        if p.exists():
            w_rows[f] = (json.loads(p.read_text()).get("output") or {}).get("assertions") or []
    rows = canonical_rows(t, a3, w_rows)
    test_files = set(split_patch(t["test_patch"]))
    src = ctx.sources(t)
    xlogs = [(f, x, read_log(x.get("log"))) for f, x in (esc.get("X") or {}).items() if x]
    env = ctx.store.get("environment", iid) or {}
    empty_log = next((read_log(r.get("log")) for r in env.get("runs") or [] if r.get("kind") == "empty" and r.get("log")),
                     "")
    for r in rows:
        sites = []
        for tid in r["tests"]:
            s = None
            for f, x, lg in xlogs:
                if tid in (x.get("f2p_failed") or []):
                    s = site_of(lg, tid, test_files)
                    if s:
                        s.update(how=f"escalation_{f}_{x.get('role')}", test=tid)
                        break
            if s is None and empty_log:
                s = site_of(empty_log, tid, test_files)
                if s:
                    s.update(how="empty_patch", test=tid)
            if s:
                s["code"] = src.test_line(s["file"], s["line"]) or s.get("text", "")
                sites.append(s)
        r["sites"] = sites
        r["site_keys"] = sorted({f"{s['file']}:{s['line']}" for s in sites})
        r["literals"] = list(dict.fromkeys(L for s in sites for L in literals_of(s.get("code", ""))))
    D = esc.get("D") or {}
    d_failed = {f: (D.get(f) or {}).get("failed_test") for f in FAMS}
    lab = {"W-A": "claude", "W-B": "gpt"}
    conf_w = {lab.get(w) for f in FAMS for w in ((D.get(f) or {}).get("fn_confirmed_witnesses") or [])
              if (D.get(f) or {}).get("verdict") == "FN_CONFIRMED"}
    exec_failed = {f: set(x.get("f2p_failed_attributable") or []) for f, x in (esc.get("X") or {}).items() if f in conf_w}
    fails_rows = {f: ((a3.get(f) or {}).get("failing_alternative") or {}).get("fails") for f in FAMS}
    kind_v = "CONFIRMED" if verdict == "FN_CONFIRMED" and esc.get("mode") == "full" else \
        "FN_BOTH" if verdict == "FN_CONFIRMED" else verdict
    items = []
    if kind_v in ("FN_BOTH", "CONFIRMED", "CONTESTED"):
        def ud_of(r):
            return [m for m in r["members"] if m["status"] == "UNDERDETERMINED" or m["kind"] in UD_KINDS]

        def in_d_of(r):
            hit = {f for f in FAMS if d_failed.get(f) and set(full_ids(t, [d_failed[f]])) & set(r["tests"])}
            return hit | {f for f, ts in exec_failed.items() if ts & set(r["tests"])}

        f_tests = {x for f in FAMS if d_failed.get(f) for x in full_ids(t, [d_failed[f]])} | \
            {x for ts in exec_failed.values() for x in ts}
        d_tests_with_ud = {x for r in rows if in_d_of(r) and ud_of(r) for x in r["tests"]}
        for r in rows:
            ud = ud_of(r)
            ud_fams = {m["family"] for m in ud}
            in_d = in_d_of(r)
            if in_d and not ud and not ((set(r["tests"]) & f_tests) - d_tests_with_ud):
                in_d = set()
            in_fail = {f for f in FAMS if fails_rows.get(f) and any(m["src"] == f"3a:{f}" and m["row_id"] == fails_rows[f]
                                                                     for m in r["members"])}
            take = (bool(in_d) or ud_fams >= set(FAMS)) if kind_v == "CONFIRMED" else \
                (bool(in_fail) or ud_fams >= set(FAMS)) if kind_v == "FN_BOTH" else (bool(ud_fams) or bool(in_d))
            if not take:
                continue
            kinds = collections.Counter(m["kind"] for m in ud)
            kind_by_fam = {f: sorted({m["kind"] for m in ud if m["family"] == f}) for f in FAMS}
            common = set(kind_by_fam["claude"]) & set(kind_by_fam["gpt"])
            guard_votes = {f: (D.get(f) or {}).get("guard_basis") for f in FAMS if f in in_d}
            quotes = [{"family": m["family"], "src": m["src"], "quote": m["discussion_quote"]}
                      for m in r["members"] if m.get("discussion_quote")]
            for st in ("R", "D"):
                for f in FAMS:
                    q = ((esc.get(st) or {}).get(f) or {}).get("discussion_quote")
                    if q and (in_d or st == "R"):
                        quotes.append({"family": f, "src": f"{st}:{f}", "quote": q})
            for q in quotes:
                q["quote_check"] = quote_check(q["quote"], discussion(t))
            texts = [m.get("assertion") or "" for m in r["members"]]
            names = interface_names(ctx, t, texts)
            for f in FAMS:
                keys = {n["name"].split(".")[-1] for n in interface_names(ctx, t, [m.get("assertion") or "" for m in ud
                                                                                   if m["family"] == f])}
                for n in names:
                    if n["name"].split(".")[-1] in keys:
                        n.setdefault("ud_families", []).append(f)
            site_code = " ".join(s.get("code", "") for s in r["sites"])
            items.append({
                "item_id": f"I{len(items) + 1}", "type": "fn_row", "rows": [r["id"]], "tests": r["tests"],
                "sites": r["site_keys"], "kind_votes": dict(kinds), "kind_by_family": kind_by_fam,
                "kind_agreed": (sorted(common)[0] if len(common) == 1 else sorted(common) if common else
                                (next(iter(kinds)) if len(kinds) == 1 else None)),
                "ud_families": sorted(ud_fams), "named_in_debate_by": sorted(in_d), "failing_alternative_of": sorted(in_fail),
                "contested": kind_v == "CONTESTED",
                "check_passes_at_base": {"rows_yes": sorted({m["src"] for m in r["members"] if m.get("passes_at_base") == "yes"})},
                "guard_basis": guard_votes,
                "guard_agreed": (len(guard_votes) == 2 and len(set(guard_votes.values())) == 1
                                 and next(iter(guard_votes.values())) in ("issue_asks", "sibling_defect")),
                "discussion_quotes": quotes,
                "route_hints": {m["src"]: m.get("repair_hint") for m in r["members"] if m.get("repair_hint")},
                "interface_names": names,
                "all_public_used": bool(names) and all(n.get("public_entry_point_used_by_tests") for n in names
                                                       if not n.get("external") and not n.get("builtin")),
                "mock_call_check": bool(re.search(r"assert_called|assert_any_call|method_calls|call_args|mock_calls",
                                                  site_code))})
        if not items:
            items.append({"item_id": "I1", "type": "fn_unlocated", "rows": [], "sites": [], "kind_agreed": None,
                          "contested": kind_v == "CONTESTED", "discussion_quotes": [], "interface_names": [],
                          "tests": sorted({x for f in FAMS for x in full_ids(t, [d_failed[f]] if d_failed.get(f) else [])}
                                          | {x for ts in exec_failed.values() for x in ts})})
    scr = ctx.store.get("screen", iid) or {}
    if scr.get("IS_route"):
        items.append({"item_id": f"I{len(items) + 1}", "type": "misleading_1d", "rows": [], "tests": [], "sites": [],
                      "kind_agreed": "misleading", "discussion_quotes": [
                          {"family": f, "src": f"1d:{f}", "quote": q, "quote_check": quote_check(q, discussion(t))}
                          for f, q in (scr.get("IS_quotes") or {}).items()]})
    p2p = ctx.store.get("p2p", iid) or {}
    for it in p2p.get("items") or []:
        pid = f"P{sum(1 for r in rows if r['id'].startswith('P')) + 1}"
        rows.append({"id": pid, "tests": it.get("p2p_ids") or [it["p2p"]], "members": [], "p2p": True,
                     "texts": it.get("asserted_behaviour"), "sites": [], "site_keys": [], "literals": []})
        items.append({"item_id": f"I{len(items) + 1}", "type": "p2p_pin", "rows": [pid], "tests": rows[-1]["tests"],
                      "sites": [], "kind_agreed": "pins_old_behaviour", "route_hint": it.get("route_hint"),
                      "restriction_statement_only": it.get("restriction_statement_only"), "mixed": it.get("mixed"),
                      "repair_note": it.get("repair_note"), "interface_names": [], "discussion_quotes": []})
    for r in rows:
        names = [n for it in items if r["id"] in it.get("rows", []) for n in it.get("interface_names") or []]
        r["api"] = sorted({n.get("expanded") or n["name"] for n in names if n.get("used_by_tests") and not n.get("private")})
    band = M.band(ctx, iid, t["statement"])
    return {"verdict": kind_v, "band": band and band["band"], "canonical": rows, "items": items,
            "h9_delete_task": bool(p2p.get("h9_delete_task")), "escalation": {
                "resolution": esc.get("resolution"), "work": esc.get("work"),
                "D": {f: {k: (D.get(f) or {}).get(k) for k in ("verdict", "failed_test", "failed_check",
                                                               "check_passes_at_base", "guard_basis")} for f in FAMS}},
            "n_f2p": len(t["FAIL_TO_PASS"]), "n_p2p": len(t["PASS_TO_PASS"])}


# ------------------------------------------------------------------ mechanical triage
def _owner_phrase(owner: str) -> str:
    parts = owner.split(".")
    if len(parts) >= 2 and parts[-1][:1].islower() and parts[-2][:1].isupper():
        return f"The `{parts[-1]}` method of `{parts[-2]}`"
    return f"`{parts[-1] if parts[-1][:1].isupper() else owner}`"


def interface_template(names: list[dict]) -> str | None:
    """The mechanical interface sentence from the names the tests call: new keyword arguments grouped by owner, new
    public functions / classes / attributes with the prefix the tests import them from."""
    kw = collections.OrderedDict()
    news = []
    for n in names:
        if n.get("external") or n.get("builtin") or n.get("private") or not n.get("used_by_tests"):
            continue
        if n["kind"] == "kwarg" and n.get("owner") and n.get("exists_at_base") in ("no", "unknown"):
            kw.setdefault(n["owner"], []).append(n["name"])
        elif n["kind"] == "name" and n.get("exists_at_base") in ("no", "elsewhere_only"):
            news.append(n)
    sents = []
    for owner, ks in kw.items():
        ks = sorted(set(ks))
        what = (f"the keyword argument `{ks[0]}`" if len(ks) == 1 else
                "the keyword arguments " + ", ".join(f"`{k}`" for k in ks[:-1]) + f" and `{ks[-1]}`")
        sents.append(f"{_owner_phrase(owner)} accepts {what}.")
    for n in news:
        parts = (n.get("expanded") or n["name"]).split(".")
        last, prefix = parts[-1], ".".join(parts[:-1])
        if last.startswith("__") and last.endswith("__"):
            sents.append(f"The `{prefix or last}` package has the attribute `{last}`." if prefix else None)
        elif not prefix:
            continue
        elif last[:1].isupper() and last.endswith(("Error", "Exception", "Warning")):
            sents.append(f"`{prefix}` provides the exception class `{last}`.")
        elif last[:1].isupper():
            sents.append(f"`{prefix}` provides the class `{last}`.")
        else:
            sents.append(f"`{prefix}` also provides the function `{last}`.")
    sents = [s for s in dict.fromkeys(sents) if s]
    return " ".join(sents) if sents else None


SRC_ORDER = ("1d", "3a", "W", "D", "R")


def family_quotes(cands: list[dict]) -> dict[str, dict]:
    out = {}
    for pre in SRC_ORDER:
        for c in cands:
            if c["src"].split(":")[0] == pre and c["family"] not in out and (c.get("quote_check") or {}).get("ok"):
                out[c["family"]] = c
    return out


def is_candidates(cands: list[dict]) -> list[str] | None:
    fq = family_quotes(cands)
    if set(fq) >= {"claude", "gpt"}:
        return [re.sub(r"\s+", " ", fq[f]["quote"]).strip() for f in ("claude", "gpt")]
    return None


def item_route(rec: dict, it: dict) -> dict:
    if it["type"] == "p2p_pin":
        r = "S-keep" if it.get("route_hint") == "S-keep" else "P2P-relax"
        return {"route": r, "restriction": bool(it.get("restriction_statement_only"))}
    if it["type"] == "misleading_1d":
        cands = is_candidates(it.get("discussion_quotes") or [])
        return {"route": "IS", "quote_candidates": cands} if cands else {"route": None, "hard": "MISLEADING_NO_VALID_QUOTES"}
    if it["type"] == "fn_unlocated":
        return {"route": None, "read": "UNLOCATED_FN_ITEM", "restriction": bool(it.get("contested"))}
    kind = it.get("kind_agreed")
    kinds = set((it.get("kind_votes") or {}).keys())
    restriction = bool(it.get("contested"))
    names = [n for n in it.get("interface_names") or [] if not n.get("external") and n.get("used_by_tests")]
    if any(n.get("private") for n in names) or any(n.get("internal_segment") for n in names):
        return {"route": None, "hard": "H2_PRIVATE_OR_INTERNAL"}
    if it.get("mock_call_check"):
        return {"route": None, "hard": "H2_MOCK_CALL_LIST"}
    gb = set((it.get("guard_basis") or {}).values())
    if it.get("guard_agreed") and gb == {"sibling_defect"}:
        return {"route": "S-keep", "restriction": restriction}
    if it.get("guard_agreed") and gb == {"issue_asks"}:
        return {"route": "T-b", "read": "H7_TB_ALWAYS_READ", "restriction": restriction}
    hints = it.get("route_hints") or {}
    fam_is = {s.split(":")[1] for s, h in hints.items() if h == "IS"}
    if fam_is:
        cands = is_candidates(it.get("discussion_quotes") or [])
        if fam_is >= {"claude", "gpt"} and cands:
            return {"route": "IS", "quote_candidates": cands, "restriction": restriction}
        if cands:
            return {"route": "IS", "quote_candidates": cands, "read": "IS_CONTRADICTION_ONE_FAMILY",
                    "restriction": restriction}
    if kind is None or isinstance(kind, list):
        return {"route": None, "read": "KIND_CONFLICT:" + ",".join(sorted(kinds)), "restriction": restriction}
    if kind == "guess_interface":
        real = [n for n in it.get("interface_names") or [] if not n.get("external") and not n.get("builtin")]
        if not real:
            return {"route": "S-a", "read": "H2_KIND_UNKNOWN_NO_NAME", "restriction": restriction}
        if not it.get("all_public_used"):
            return {"route": "S-a", "read": "S-a_NAME_NOT_USED_BY_TESTS", "restriction": restriction}
        fams = {f for f, ks in (it.get("kind_by_family") or {}).items() if "guess_interface" in ks}
        agreed = [n for n in real if fams and fams <= set(n.get("ud_families") or [])]
        tpl = interface_template(agreed) or interface_template(real)
        if not tpl:
            return {"route": "S-a", "read": "S-a_NO_TEMPLATE", "restriction": restriction}
        return {"route": "S-a", "template": tpl, "restriction": restriction}
    if kind == "guess_literal":
        if it.get("_literals"):
            return {"route": "S-lit", "restriction": restriction}
        return {"route": "T-b", "read": "H7_TB_ALWAYS_READ_COMPUTED_VALUE", "restriction": restriction}
    if kind == "guess_behaviour_choice":
        return {"route": "S-scope", "read": "H5_REVERSES_OR_NATURAL_EXTENSION", "restriction": restriction}
    if kind == "beyond_issue":
        return {"route": "S-scope", "read": "H6_NATURAL_EXTENSION", "restriction": restriction}
    return {"route": None, "read": f"UNMAPPED_KIND:{kind}", "restriction": restriction}


def item_kinds(it: dict) -> set[str]:
    k = it.get("kind_agreed")
    return {k} if isinstance(k, str) else set(k or []) | set((it.get("kind_votes") or {}).keys())


def family_votes(rec: dict, it: dict) -> dict[str, collections.Counter]:
    can = {r["id"]: r for r in rec.get("canonical") or []}
    out = {f: collections.Counter() for f in FAMS}
    for rid in it.get("rows") or []:
        for m in (can.get(rid) or {}).get("members") or []:
            if m.get("family") in out and (m.get("status") == "UNDERDETERMINED" or m.get("kind") in UD_KINDS):
                out[m["family"]][m.get("kind")] += 1
    return out


def beyond_agreed(it: dict, rec: dict | None = None) -> bool:
    """beyond_issue is the strict-majority (or only) kind in EACH family's own votes on the item."""
    if it.get("type", "fn_row") != "fn_row":
        return False
    fv = family_votes(rec or {}, it)
    if all(sum(c.values()) for c in fv.values()):
        return all(2 * c["beyond_issue"] > sum(c.values()) for c in fv.values())
    if any(sum(c.values()) for c in fv.values()):
        return False
    fam = it.get("kind_by_family") or {}
    return all((fam.get(f) or []) == ["beyond_issue"] for f in FAMS)


def every_f2p_flagged(rec: dict) -> dict | None:
    canon = {r["id"] for r in rec.get("canonical") or [] if str(r.get("id", "")).startswith("A")}
    fn = [it for it in rec.get("items") or [] if set(it.get("rows") or []) & canon]
    if not canon or not canon <= {x for it in fn for x in it.get("rows") or []}:
        return None
    non_pin = [it["item_id"] for it in fn if item_kinds(it) - PIN_KINDS]
    if not non_pin:
        return None
    agreed = [it["item_id"] for it in fn if beyond_agreed(it, rec)]
    return {"rows": sorted(canon, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0), "non_pin_items": non_pin,
            "beyond_agreed_items": agreed, "delete": bool(agreed)}


def statement_plan(per: list[dict]) -> dict:
    row_routes = {}
    for r in per:
        for rid in r["rows"]:
            row_routes.setdefault(rid, r["route"])
    routes = sorted({r["route"] for r in per}, key=ROUTE_ORDER.index)
    templates = list(dict.fromkeys(r["template"] for r in per if r.get("route") == "S-a" and r.get("template")))
    cands = list(dict.fromkeys(q for r in per if r.get("route") == "IS" for q in (r.get("quote_candidates") or [])))
    return {"route": "+".join(routes), "routes": routes, "kind": "statement", "items": [r["item_id"] for r in per],
            "routed": sorted(row_routes, key=lambda x: (x[0], int(x[1:]) if x[1:].isdigit() else 0)),
            "row_routes": row_routes, "templates": templates, "quote_candidates": cands or None}


def decide(rec: dict) -> dict:
    if rec.get("h9_delete_task"):
        return {"decision": "delete", "reason": "H9_4A_DELETE_TASK_BOTH"}
    items = rec.get("items") or []
    if not items:
        return {"decision": "no_defect", "reason": rec["verdict"]}
    if rec.get("band") and rec["band"].split("/")[0] == rec["band"].split("/")[1]:
        return {"decision": "delete", "reason": "P2_DEFECTIVE_ALWAYS_SOLVED"}
    rows = {r["id"]: r for r in rec.get("canonical") or []}
    per = []
    for it in items:
        lits = sorted({L for rid in it.get("rows") or [] for L in (rows.get(rid) or {}).get("literals") or []
                       if not re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", L)})
        r = item_route(rec, dict(it, _literals=lits))
        r.update(item_id=it["item_id"], type=it["type"], rows=it.get("rows") or [], sites=it.get("sites") or [],
                 kind=it.get("kind_agreed"))
        per.append(r)
    out = {"items": per}
    hard = [r for r in per if r.get("hard")]
    if hard:
        return dict(out, decision="delete", reason=hard[0]["hard"])
    ef = every_f2p_flagged(rec)
    if ef and ef["delete"]:
        return dict(out, decision="delete", every_f2p_flagged=ef, reason="H6_EVERY_F2P_FLAGGED")
    reads = sorted({r["read"] for r in per if r.get("read")} | ({H6_READ} if ef else set()))
    stmt = [r for r in per if r["route"] in STMT_ROUTES]
    test = [r for r in per if r["route"] in TEST_ROUTES]
    unrouted = [r for r in per if r["route"] is None]
    restriction = any(r.get("restriction") for r in per)
    if stmt and test:
        reason = "H3_MIXED_STATEMENT_AND_TEST_ROUTES:" + ",".join(sorted({r["route"] for r in per if r["route"]}))
        if reads:
            return dict(out, decision="needs_read", reason=";".join(reads + [reason]),
                        proposal={"decision": "delete", "reason": reason})
        return dict(out, decision="delete", reason=reason)
    if restriction and test:
        return dict(out, decision="delete", reason="RESTRICTION_STATEMENT_ONLY:" + test[0]["route"])
    if stmt:
        plan = dict(statement_plan(stmt), restriction_statement_only=restriction)
    elif test:
        tr = sorted({r["route"] for r in test})
        plan = {"route": "T-b" if "T-b" in tr else tr[0], "routes": tr, "kind": "test", "items": [r["item_id"] for r in test],
                "routed": sorted({x for r in test for x in r["rows"]}), "restriction_statement_only": False}
        reads = reads or ["H7_TEST_ROUTE_ALWAYS_READ"]  # a test-side relaxation always needs a read (its spec)
    else:
        plan = {"route": None, "kind": None, "items": [r["item_id"] for r in per],
                "routed": sorted({x for r in per for x in r["rows"]})}
    if unrouted or reads:
        return dict(out, decision="needs_read", reason=";".join(reads) or "NO_ROUTE", proposal=plan)
    return dict(out, decision="repair", reason=plan["route"], plan=plan)


# ------------------------------------------------------------------ the 7a read
def read_cells(prompt: str) -> dict[str, str]:
    """Hard-rule id -> its "Read needed when" cell, from the triage rules quoted in the 7a prompt."""
    sec = prompt[prompt.index("## 4. "):prompt.index("## 5. ")] if "## 4. " in prompt else ""
    out = {}
    for line in sec.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and re.fullmatch(r"H\d", cells[0]):
            out[cells[0]] = cells[3]
    return out


QUESTIONS = {
    "KIND_CONFLICT": "the sources' kinds for the item disagree ({arg}), so its §5 route (column \"When (kind -> route)\") "
                     "is not decided mechanically; {H2}",
    "H2_KIND_UNKNOWN_NO_NAME": "kind guess_interface, but no public name the tests use was extracted; {H2}; §5 S-a",
    "S-a_NAME_NOT_USED_BY_TESTS": "kind guess_interface, but the extracted new names are not all marked as used by the "
                                  "tests; §5 S-a (\"a public entry point ... that the F2P calls directly\")",
    "S-a_NO_TEMPLATE": "kind guess_interface, but no mechanical interface sentence could be built; §5 S-a",
    "H5_REVERSES_OR_NATURAL_EXTENSION": "kind guess_behaviour_choice; {H5}; §5 S-scope",
    "H6_NATURAL_EXTENSION": "kind beyond_issue; {H6}; §5 S-scope",
    "H6_EVERY_F2P_FLAGGED_READ": "every canonical F2P assertion is flagged and no item has beyond_issue agreed by both "
                                 "families; {H6}",
    "H7_TB_ALWAYS_READ": "a regression-guard row with basis (a) agreed by both families (route T-b); {H7}",
    "H7_TB_ALWAYS_READ_COMPUTED_VALUE": "kind guess_literal without a pinned literal at the site (route T-b); {H7}",
    "H7_TEST_ROUTE_ALWAYS_READ": "mechanical test route without a relaxation; {H7}; §5 P2P-relax",
    "IS_CONTRADICTION_ONE_FAMILY": "one family's route hint is IS (the needed statement would contradict the issue), and "
                                   "both families gave a valid discussion quote; §5 IS (ii) or the S-route of the kind",
    "UNLOCATED_FN_ITEM": "a confirmed / contested FN item whose failing check was not located on a canonical row",
    "UNMAPPED_KIND": "the item's kind ({arg}) has no mechanical route",
    "NO_ROUTE": "an item has no mechanical route",
    "H3_MIXED_STATEMENT_AND_TEST_ROUTES": "with the mechanical routes the task mixes statement and test routes ({arg}); "
                                          "{H3}; a read may move an item to the other side"}


def question(reason: str, cells: dict) -> str:
    code, _, arg = reason.partition(":")
    q = QUESTIONS.get(code)
    if not q:
        return f"{reason}: (no description)"
    hs = {k: f'§4 {k}, "Read needed when": "{cells.get(k, "?")}"' for k in ("H2", "H3", "H5", "H6", "H7")}
    return f"{reason}: " + q.format(arg=arg, **hs)


def _c(s, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def rowkey(r: str):
    return (r[:1], int(r[1:]) if r[1:].isdigit() else 0, r)


def card(rec: dict, d: dict, t: dict, cells: dict, patches: dict) -> dict:
    per = {r["item_id"]: r for r in d.get("items") or []}
    canon = {r["id"]: r for r in rec.get("canonical") or []}
    plan = d.get("proposal") or {}
    reasons = [x for x in (d.get("reason") or "").split(";") if x]
    rows = sorted({x for r in per.values() for x in r.get("rows") or []}, key=rowkey)
    for it in rec["items"]:
        if it["type"] == "fn_unlocated":
            rows = sorted(set(rows) | {rid for rid, r in canon.items() if set(r.get("tests") or []) & set(it.get("tests") or [])},
                          key=rowkey)
    templates = list(plan.get("templates") or [])
    restriction = bool(plan.get("restriction_statement_only")) or any(r.get("restriction") for r in per.values())
    esc = rec.get("escalation") or {}
    L = [f"TASK {t['instance_id']}   repository {t['repo']}   band {rec.get('band') or '-'}   verdict {rec.get('verdict')}"
         f"   escalation {esc.get('resolution') or '-'}   F2P {rec.get('n_f2p')}  PASS_TO_PASS {rec.get('n_p2p')}",
         "", "OPEN QUESTIONS"]
    for r in reasons:
        its = [i for i, p in per.items() if p.get("read") == r]
        L.append(f"- {question(r, cells)}" + (f" (items {', '.join(its)})" if its else ""))
    L += ["", "MECHANICAL PROPOSAL"]
    if plan.get("decision") == "delete":
        L.append(f"- mechanical decision: delete {plan.get('reason')}")
    rr = plan.get("row_routes") or {}
    by_item = ", ".join(f"{i} ({'/'.join(p.get('rows') or []) or 'no row'}): "
                        f"{p.get('route') or ('HARD ' + p['hard'] if p.get('hard') else 'none')}" for i, p in per.items())
    L.append(f"- route per row: {', '.join(f'{k} {v}' for k, v in sorted(rr.items(), key=lambda x: rowkey(x[0]))) or '-'}"
             f"   (per item: {by_item or plan.get('route') or '-'})")
    L.append(f"- S-a templates (mechanical): {json.dumps(templates) if templates else 'none'}")
    qc = plan.get("quote_candidates") or []
    L.append("- QUOTE CANDIDATES (IS): " + ("; ".join(f'{i + 1}. "{q}"' for i, q in enumerate(qc)) if qc else "none"))
    L.append(f"- restriction of §5 (statement routes only): {'yes' if restriction else 'no'}")
    L += ["", f"ROWS TO ROUTE: {', '.join(rows) or '(none)'}", "", "ITEMS"]
    shown = set()
    for it in rec["items"]:
        p = per.get(it["item_id"]) or {}
        L.append(f"{it['item_id']} {it['type']}  rows {', '.join(it.get('rows') or []) or '-'}  tests "
                 f"{len(it.get('tests') or [])}: {', '.join(_c(x, 160) for x in (it.get('tests') or [])[:4])}")
        if it.get("kind_votes") is not None:
            L.append(f"   kind: agreed {it.get('kind_agreed')}; votes {json.dumps(it['kind_votes'])}; by family "
                     f"{json.dumps(it.get('kind_by_family'))}; mechanical route {p.get('route')}"
                     + (f"; HARD {p['hard']}" if p.get("hard") else "") + (f"; open {p['read']}" if p.get("read") else ""))
        gb = it.get("guard_basis") or {}
        cpb = it.get("check_passes_at_base") or {}
        if gb or cpb.get("rows_yes"):
            L.append(f"   regression guard: rows passing at base {cpb.get('rows_yes')}; basis votes {json.dumps(gb)}; "
                     f"agreed {it.get('guard_agreed')}")
        if it.get("contested"):
            L.append("   CONTESTED escalation item (statement routes only)")
        if it["type"] == "p2p_pin":
            L.append(f"   4a/4b PASS_TO_PASS pin: route hint {it.get('route_hint')}; {_c(json.dumps(it.get('repair_note')), 600)}")
        for n in it.get("interface_names") or []:
            if not (n.get("external") or n.get("builtin")):
                L.append(f"   name `{n.get('expanded') or n.get('name')}` ({n.get('kind')}"
                         f"{', owner ' + n['owner'] if n.get('owner') else ''}): used by tests {n.get('used_by_tests')}, "
                         f"private {n.get('private')}, internal segment {n.get('internal_segment')}, exists at base "
                         f"{n.get('exists_at_base')}")
        if it.get("mock_call_check"):
            L.append("   mock_call_check: yes")
        if it.get("route_hints"):
            L.append(f"   route hints: {json.dumps(it['route_hints'])}")
        for q in it.get("discussion_quotes") or []:
            L.append(f"   discussion quote {q.get('src')} ({q.get('family')}; quote check "
                     f"{'ok' if (q.get('quote_check') or {}).get('ok') else 'fails'}): \"{_c(q.get('quote'), 320)}\"")
        for rid in it.get("rows") or []:
            if rid in canon and rid not in shown:
                shown.add(rid)
                L += row_lines(canon[rid])
    for rid in rows:
        if rid in canon and rid not in shown:
            L += row_lines(canon[rid])
    L += ["", "ESCALATION"]
    if esc.get("resolution"):
        L.append(f"- resolution {esc.get('resolution')}")
        for f, dd in (esc.get("D") or {}).items():
            if dd.get("verdict"):
                L.append(f"- debate {f}: {dd.get('verdict')}; failed test {_c(dd.get('failed_test'), 160)}; failed check "
                         f"{_c(dd.get('failed_check'), 300)}; passes at base {dd.get('check_passes_at_base')}; guard "
                         f"basis {dd.get('guard_basis')}")
    L.append("- executed patches in /in: " + (", ".join(f"/in/{n} ({w})" for n, (w, _) in patches.items()) or "none"))
    return {"card": "\n".join(L), "rows": rows, "templates": templates, "restriction": restriction,
            "proposal": {"decision": plan.get("decision") or "repair", "kind": plan.get("kind"),
                         "row_routes": dict(plan.get("row_routes") or {}), "routes": list(plan.get("routes") or [])}}


def row_lines(r: dict) -> list[str]:
    out = [f"   row {r['id']}: tests {', '.join(_c(x, 160) for x in (r.get('tests') or [])[:4])}"]
    for s in (r.get("sites") or [])[:4]:
        out.append(f"      failure site {s.get('file')}:{s.get('line')}: {_c(s.get('code'), 300)}")
    if r.get("literals"):
        out.append(f"      literals at the site: {_c(json.dumps(r['literals']), 400)}")
    if r.get("api"):
        out.append(f"      entry points the tests call: {_c(json.dumps(r['api']), 300)}")
    for m in r.get("members") or []:
        bits = [f"{m.get('src')} {m.get('row_id')}", f"{m.get('kind')} {m.get('status')}",
                f"passes_at_base {m.get('passes_at_base')}", f"\"{_c(m.get('assertion'), 400)}\""]
        for k, lab, n in (("evidence", "evidence", 450), ("rejected_alternative", "rejected alternative", 400)):
            if m.get(k):
                bits.append(f"{lab}: {_c(m[k], n)}")
        if m.get("discussion_quote"):
            bits.append(f"discussion quote: \"{_c(m['discussion_quote'], 320)}\"")
        if m.get("repair_hint"):
            bits.append(f"route hint {m['repair_hint']}")
        out.append("      - " + " | ".join(bits))
    return out


def read_packet(t: dict) -> str:
    f2p = list(t["FAIL_TO_PASS"])
    return "\n".join([f"# {t['instance_id']}", "", f"- Repository: {t['repo']}", f"- Base commit: {t['base_commit']}", "",
                      "## Issue (exactly as the solver sees it)", "", fence(t["statement"].replace("\r\n", "\n"), "text"),
                      "", f"## FAIL_TO_PASS tests ({len(f2p)})", "", fence("\n".join(f2p), "text"), "",
                      "## Test patch", "", fence(t["test_patch"], "diff"), ""])


def normalized(out: dict | None, ci: dict) -> dict | None:
    """A family's answer under the structural rules: mixed routes -> delete H3, a test route on a restricted task ->
    delete RESTRICTION; a repair must route exactly the rows to route."""
    if not out:
        return None
    if out.get("decision") == "delete":
        return {"decision": "delete", "hard_rule": out.get("hard_rule"), "row_routes": {}}
    rr = {x["row"]: x["route"] for x in out.get("row_routes") or []}
    routes = set(rr.values())
    if routes & STMT_ROUTES and routes & TEST_ROUTES:
        return {"decision": "delete", "hard_rule": "H3", "row_routes": {}}
    if routes & TEST_ROUTES and ci["restriction"]:
        return {"decision": "delete", "hard_rule": "RESTRICTION", "row_routes": {}}
    if sorted(rr, key=rowkey) != ci["rows"]:
        return None
    return {"decision": "repair", "hard_rule": "none", "row_routes": rr, "templates": out.get("templates") or [],
            "relax": out.get("relax")}


def merge_route(x: str, y: str, have_tpl: bool) -> str | None:
    if x == y:
        return x
    if "S-a" in (x, y):
        return "S-a" if have_tpl else (y if x == "S-a" else x)
    if {x, y} <= {"S-lit", "S-keep", "S-scope"}:
        return "S-scope"
    if "IS" in (x, y) and {x, y} <= STMT_ROUTES:
        return y if x == "IS" else x
    if (x in STMT_ROUTES) != (y in STMT_ROUTES):
        return x if x in STMT_ROUTES else y
    return None


def combine(ci: dict, N: dict) -> dict:
    """Two families' normalised answers -> repair (row routes, the family whose templates / relaxation are used) or
    delete. READ_AGREE; (a) both repair with different routes: per row the more permissive route; (b) one family
    unsure: the other's answer; (c) one repairs, the other deletes citing H3 or H5: the repair; otherwise delete."""
    ok = [f for f in FAMS if N.get(f)]
    if not ok:
        return {"decision": "delete", "code": "READ_DISAGREE", "why": "both families unsure"}

    def split(rule, rr, src):
        routes = set(rr.values())
        if routes & TEST_ROUTES and routes & STMT_ROUTES:
            return {"decision": "delete", "code": "READ_DISAGREE", "rule": rule, "why": "mixed routes (H3)"}
        if routes & TEST_ROUTES and ci["restriction"]:
            return {"decision": "delete", "code": "READ_DISAGREE", "rule": rule, "why": "test route on a restricted task"}
        if "S-a" in routes and not ci["templates"] and not (src and N[src].get("templates")):
            return {"decision": "delete", "code": "READ_DISAGREE", "rule": rule, "why": "S-a row without a template"}
        if routes & TEST_ROUTES and not (src and N[src].get("relax")):
            return {"decision": "delete", "code": "READ_DISAGREE", "rule": rule, "why": "test route without a relaxation"}
        return {"decision": "repair", "code": "READ_SPLIT_REPAIRED" if rule else "READ_AGREE", "rule": rule,
                "row_routes": dict(rr), "src": src}

    if len(ok) == 1:
        n = N[ok[0]]
        return split("b", n["row_routes"], ok[0]) if n["decision"] == "repair" else \
            {"decision": "delete", "code": "READ_DISAGREE", "rule": "b", "why": f"follows {ok[0]}: delete {n['hard_rule']}"}
    nc, ng = N["claude"], N["gpt"]
    if nc["decision"] == ng["decision"] == "delete":
        return {"decision": "delete", "code": "READ_DELETE", "why": f"claude {nc['hard_rule']} / gpt {ng['hard_rule']}"}
    if nc["decision"] == ng["decision"] == "repair":
        if nc["row_routes"] == ng["row_routes"]:
            return split(None, nc["row_routes"], "claude")
        have_tpl = bool(ci["templates"]) or bool(nc.get("templates"))
        rr = {r: merge_route(nc["row_routes"].get(r), ng["row_routes"].get(r), have_tpl) for r in nc["row_routes"]}
        if set(rr) != set(ng["row_routes"]) or None in rr.values():
            return {"decision": "delete", "code": "READ_DISAGREE", "rule": "a", "why": "no combination rule"}
        return split("a", rr, "claude")
    rep, dl = ("claude", ng) if nc["decision"] == "repair" else ("gpt", nc)
    if dl["hard_rule"] in ("H3", "H5"):
        return split("c", N[rep]["row_routes"], rep)
    return {"decision": "delete", "code": "READ_DISAGREE", "why": f"one family deletes citing {dl['hard_rule']}"}


def check_read(out: dict | None, wd, ci: dict):
    if out is None:
        return ["no_valid_output"], []
    hard = []
    if out.get("decision") == "repair":
        rr = sorted({x.get("row") for x in out.get("row_routes") or []}, key=rowkey)
        if rr != ci["rows"]:
            hard.append(f"row_routes rows {rr} != ROWS TO ROUTE {ci['rows']}")
        if out.get("hard_rule") != "none":
            hard.append("repair with a hard rule")
    elif out.get("hard_rule") in (None, "none"):
        hard.append("delete without a hard rule")
    rx = out.get("relax")
    if rx and not (wd / "out" / str(rx.get("verified_file") or "")).exists():
        hard.append("relaxation without verified_file")
    return hard, []


def read(ctx, t: dict, rec: dict, d: dict) -> dict:
    iid = t["instance_id"]
    wd = ctx.store.work(NAME, iid)
    esc_wd = ctx.store.work("escalate", iid)
    patches = {}
    for f in FAMS:
        for role in ("witness", "alternative"):
            p = esc_wd / f / "W" / "out" / f"{role}.diff"
            if p.exists() and p.read_text().strip():
                patches[f"esc_{LETTER[f]}_{role}.diff"] = (f"{role} of escalation reader {LETTER[f]}", p.read_text())
    tpl = ctx.prompts.text("triage_reader")
    ci = card(rec, d, t, read_cells(tpl), patches)
    disc = discussion(t)
    subs = {"@@TASK@@": iid, "@@CARD@@": ci["card"], "@@PACKET@@": read_packet(t).strip(),
            "@@DISCUSSION@@": fence(disc, "text") if disc else "(empty: no discussion is recorded for this issue)"}
    prompt = re.sub(r"@@(TASK|CARD|PACKET|DISCUSSION)@@", lambda m: subs[m.group(0)], tpl)
    in_d = prepare_in(ctx, dict(t, patch=""), wd)
    (in_d / "gold.diff").unlink(missing_ok=True)  # the reader does not see the gold patch
    for n, (_, p) in patches.items():
        (in_d / n).write_text(p)
    runner = AgentRunner(ctx.cfg, NAME)
    img = {"image": "(dry run)"} if runner.dry else runner.final_image(t)
    try:
        with ThreadPoolExecutor(2) as ex:
            R = dict(zip(FAMS, ex.map(lambda f: runner.stage(t, f, "T", prompt, in_d, ctx.prompts.schema_path("triage_reader"),
                                                             wd / f / "T", img["image"],
                                                             lambda out, w: check_read(out, w, ci)), FAMS)))
    finally:
        if not runner.dry and not img.get("reused"):
            runner.drop_image(img["image"])
    if any(R[f].get("dry_run") for f in FAMS):
        return {"dry_run": True, "card": ci["card"]}
    N = {f: None if R[f].get("unsure") or R[f].get("flags") else normalized(R[f].get("output"), ci) for f in FAMS}
    comb = combine(ci, N)
    if comb["decision"] == "repair":
        src = comb.get("src")
        comb["templates"] = ci["templates"] or ((N.get(src) or {}).get("templates") or [])
        comb["relax"] = (N.get(src) or {}).get("relax")
        comb["relax_dir"] = str(wd / src / "T" / "out") if src else None
    return {"combination": comb, "card": ci["card"], "families": {f: R[f].get("output") for f in FAMS}}


def run(ctx, iid: str) -> dict:
    t = ctx.effective(iid, with_repair=False)
    rec = build_record(ctx, t)
    d = decide(rec)
    row = {"record": rec, "items": d.get("items"), "mechanical": {k: d.get(k) for k in ("decision", "reason", "proposal")}}
    if d["decision"] == "no_defect":
        return dict(row, decision="no_defect", reasons=[d["reason"]])
    if d["decision"] == "delete":
        return dict(row, decision="drop", reasons=[d["reason"].split(":")[0]], detail=d["reason"])
    if d["decision"] == "repair":
        return dict(row, decision="repair", reasons=[d["reason"]], plan=d["plan"])
    if not ctx.scfg(NAME).get("read", True):
        return dict(row, decision="drop", reasons=["READ_DISABLED"], detail=d["reason"])
    r = read(ctx, t, rec, d)
    row["read"] = r
    if r.get("dry_run"):
        return dict(row, complete=False, decision="pending", reasons=["dry_run"])
    comb = r["combination"]
    if comb["decision"] == "delete":
        return dict(row, decision="drop", reasons=[f"READ:{comb['code']}"], detail=comb.get("why"))
    plan = dict(d.get("proposal") or {})
    rr = comb["row_routes"]
    routes = sorted(set(rr.values()) | ({"IS"} if "IS" in (plan.get("routes") or []) and
                                        "IS" not in (plan.get("row_routes") or {}).values() else set()), key=ROUTE_ORDER.index)
    plan.update(row_routes=rr, routes=routes, routed=sorted(rr, key=rowkey), route="+".join(routes),
                kind="test" if set(routes) <= TEST_ROUTES else "statement", templates=comb.get("templates") or [],
                relax=comb.get("relax"), relax_dir=comb.get("relax_dir"))
    if "S-a" not in routes:
        plan["templates"] = []
    if "IS" not in routes:
        plan["quote_candidates"] = None
    return dict(row, decision="repair", reasons=[f"READ:{plan['route']}"], plan=plan)
