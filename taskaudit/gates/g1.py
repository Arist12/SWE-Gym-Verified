"""G1, the mechanical check of a repair: deterministic token-provenance and form rules on the specification that a
statement repair appends (rules/G1_rules.txt).

The as-run check in one module: the frozen tokenizer, stoplist and path / instruction / test-word regexes, and the
final rules on top of them (provenance from the issue and the routed assertions, dunders public, the form check with
templates and combined routes, discussion-quote checks, the IS frame). The logic is the as-run code; only module
plumbing changed (base-repository presence comes from ``repo.Repos.present_at_base``).

Provenance sources:
  - identifier: in the issue, OR in a routed assertion's entry point (its ``api`` entries, with keyword names and
    dotted suffixes) or checked expression (its ``code`` line). Names the F2P test code defines itself (def/class
    names, parameters, assignment targets: fixtures, locals, test helpers) have no provenance from the tests unless the
    issue or an ``api`` entry also has them.
  - literal (quoted string or number, also inside a code span): in the issue (numbers as a standalone number token),
    OR a contract value a routed assertion checks (its ``literal``: strings must be a substring of it, numbers equal to
    a number in it). Test inputs (values that are only arguments / data in the test code) have no provenance.
  - code span: verbatim in the issue or in a routed assertion's ``api`` / ``code`` text; its identifiers and literals
    are checked one by one as above.
"""
from __future__ import annotations

import re
from collections import Counter

G1_VERSION = "g1-token-provenance-v4"

# ------------------------------------------------------------------ frozen tokenizer and regexes
STOP = {
    # Python constants, keywords and builtin types/functions that carry no repo-specific information
    "None", "True", "False", "int", "float", "str", "bool", "list", "dict", "tuple", "set", "bytes", "object",
    "type", "len", "print", "self", "cls", "and", "or", "not", "is", "in", "if", "else", "for", "while", "return",
    "lambda", "def", "class", "import", "from", "as", "with", "pass", "yield", "async", "await",
    # prose abbreviations that look dotted
    "e.g", "i.e", "etc", "vs", "a.k.a",
    # camel-cased prose words and general (not repo-specific) value names
    "NaN", "NaT", "NumPy", "SciPy", "PyTorch", "GitHub", "GitLab", "PyPI", "PyCharm", "IPython", "JavaScript",
    "TypeScript", "macOS", "iOS", "JupyterLab", "OpenAPI", "PyYAML", "YAML", "JSON", "CSV", "URL", "API", "CLI",
}
EXT = r"(?:py|pyi|pyx|pxd|pyd|so|cfg|toml|ini|ya?ml|json|txt|rst|md|c|h|cpp|hpp|js|ts|csv|lock|sh|bat|ipynb|test)"
PATH_RE = re.compile(r"(?<![\w])(?:[\w.-]+/)+[\w.*-]*\w|(?<![\w])[\w-]+(?:\.[\w-]+)*\." + EXT + r"\b|https?://\S+|"
                     r"\bwww\.\S+|^\s*-?\s*Path\s*:", re.M)
INSTR_VERBS = (r"add|modify|update|replace|change|implement|refactor|rename|insert|introduce|override|patch|edit|move|fix|"
               r"remove|delete|wrap|extend|register|subclass|hook")
INSTR_RE = re.compile(
    r"(?:^|[.:;!?]\s+|\n\s*[-*]?\s*)(?:please\s+)?(?:" + INSTR_VERBS + r"|use|call|create|define|make sure|ensure)\b"
    r"|\b(?:should|must|need to|needs to|have to|has to|will|'ll|can|could|would|let's|we|you|I)\s+(?:also\s+|just\s+|simply\s+)?"
    r"(?:" + INSTR_VERBS + r")\b"
    r"|\b(?:should|must|needs? to|has to|have to)\s+be\s+(?:added|modified|updated|replaced|changed|implemented|"
    r"refactored|renamed|inserted|introduced|overridden|patched|edited|moved|registered|wrapped|extended)\s+"
    r"(?:to|in|into|from|on|inside|within)\b"
    r"|\breplac\w*\s+[^.;\n]{1,60}?\s+(?:in|with|by)\s+`",
    re.I | re.M)
TEST_RE = re.compile(r"\b(?:tests?|tested|testing|pytest|unittest|assert\w*|fixtures?|parametri[sz]\w*|xfail|doctest|"
                     r"conftest|F2P|P2P|FAIL_TO_PASS|PASS_TO_PASS)\b|\btest_\w+|(?-i:\bTest[A-Z]\w*)", re.I)
SPAN_RE = re.compile(r"`([^`\n]+)`")
EMPH_RE = re.compile(r"(?<![\w`])_[A-Za-z]+_(?![\w`])")
QUOTED_RE = re.compile(r"(?<![\w`])(\"[^\"\n]{1,80}\"|'[^'\n]{1,80}')(?![\w`])")
NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
CODEY_RE = re.compile(
    r"--[A-Za-z][\w-]*"                                         # cli flag
    r"|\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+(?:\(\))?"          # dotted
    r"|\b[A-Za-z]*_[\w]*\b"                                      # contains underscore (incl. _private)
    r"|(?<![\w])_[A-Za-z]\w*"                                    # leading underscore
    r"|\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z0-9]+[A-Z]\w*\b"            # camelCase / CamelCase
    r"|\b\w+\(\)?")                                              # call syntax
IDENT_IN_SPAN = re.compile(r"--[A-Za-z][\w-]*|[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*")
INTERNAL_SEGS = {"core", "_libs", "internals"}
SRC_TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*$|_tests?\.py$|(^|/)conftest\.py$")


def source_index(issue: str, test_text: str) -> dict:
    """Provenance sources: raw texts, word sets, and dotted forms expanded from import lines."""
    src = issue + "\n" + test_text
    words = set(re.findall(r"[A-Za-z_][\w]*", src))
    dotted = set(re.findall(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+", src))
    for m in re.finditer(r"^\s*from\s+([\w.]+)\s+import\s+\(?([^\n)]*)", test_text, re.M):
        for nm in re.split(r"\s*,\s*", m.group(2)):
            parts = nm.split(" as ")
            if parts[0].strip():
                dotted.add(m.group(1) + "." + parts[0].strip())
    aliases = {}
    for m in re.finditer(r"^\s*import\s+([\w.]+)\s+as\s+(\w+)", test_text, re.M):
        aliases[m.group(2)] = m.group(1)
    for d in list(dotted):
        head, _, rest = d.partition(".")
        if head in aliases:
            dotted.add(aliases[head] + "." + rest)
    return {"text": src, "words": words, "dotted": dotted}


def tokens(text: str) -> list[dict]:
    """Identifiers and literals of one addition sentence, with where they were found."""
    out = []
    for m in SPAN_RE.finditer(text):
        span = m.group(1).strip()
        q = re.fullmatch(r"""(["'])(.*)\1""", span)
        if q:
            out.append({"tok": span, "kind": "literal", "where": "span"})
            continue
        if re.fullmatch(r"-?\d+(?:\.\d+)?", span):
            out.append({"tok": span, "kind": "literal", "where": "span"})
            continue
        idents = IDENT_IN_SPAN.findall(span)
        if len(idents) == 1 and idents[0] == span:
            out.append({"tok": span, "kind": "identifier", "where": "span"})
        else:
            out.append({"tok": span, "kind": "code", "where": "span"})
            for x in idents:
                out.append({"tok": x, "kind": "identifier", "where": "in_code_span"})
            for x in re.findall(r"""(["'][^"'\n]+["'])""", span):
                out.append({"tok": x, "kind": "literal", "where": "in_code_span"})
            for x in NUM_RE.findall(span):
                out.append({"tok": x, "kind": "literal", "where": "in_code_span"})
    rest = SPAN_RE.sub(" ", text)
    for m in QUOTED_RE.finditer(rest):
        out.append({"tok": m.group(1), "kind": "literal", "where": "quoted"})
    rest2 = QUOTED_RE.sub(" ", rest)
    for m in CODEY_RE.finditer(rest2):
        tok = m.group(0).rstrip("(").removesuffix("()")
        if tok and tok.strip("_"):
            out.append({"tok": tok, "kind": "identifier", "where": "prose"})
    for m in NUM_RE.finditer(rest2):
        out.append({"tok": m.group(0), "kind": "literal", "where": "prose"})
    return out


def segments(t: str) -> list[str]:
    return [x for x in re.split(r"[.\s(),\[\]=:]+", t) if x]


def split_patch(patch: str) -> dict[str, str]:
    out, cur, buf = {}, None, []
    for line in (patch or "").splitlines(keepends=True):
        if line.startswith("diff --git"):
            if cur:
                out[cur] = "".join(buf)
            m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            cur, buf = (m.group(2) if m else None), [line]
        else:
            buf.append(line)
    if cur:
        out[cur] = "".join(buf)
    return out


def added_with_lines(file_patch: str) -> list[tuple[int, str]]:
    """(new-file line number, text) of every added line of one file's diff."""
    out, n = [], 0
    for line in file_patch.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m:
            n = int(m.group(1))
            continue
        if line.startswith(("+++", "---", "diff --git", "index ")):
            continue
        if line.startswith("+"):
            out.append((n, line[1:]))
            n += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
        else:
            n += 1
    return out


def gold_added_words(patch: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][\w]*", "\n".join(x for p, fp in split_patch(patch).items()
                                                        if not SRC_TEST_PATH.search(p)
                                                        for _, x in added_with_lines(fp))))


# ------------------------------------------------------------------ final rules
IFC_MAX_WORDS = 40  # per interface entry; no count / total-word limits
IFC_KEYS = ("name", "type", "input", "output", "description")
TYPES = {"class", "function", "method", "attribute", "property", "command", "configuration key"}
NO_IFC = "No new interfaces are introduced."
NA = {"n/a", "na", "none", ""}
WORD = re.compile(r"[A-Za-z_]\w*")
NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
DUNDER = re.compile(r"__[A-Za-z0-9]\w*__")
IS_FRAME = "A later comment on the issue says:"
QUOTE_MAX_CHARS = 300
ROUTES = {"S-a", "S-lit", "S-keep", "S-scope", "IS"}
TEMPLATE_ROUTES = {"S-a", "IS"}


def is_private_segment(s: str) -> bool:
    """A segment that starts with "_" and is not a dunder (`__all__`, `__init__` are public protocol names)."""
    return s.startswith("_") and s.strip("_") != "" and not DUNDER.fullmatch(s)


def test_defined(test_text: str) -> set[str]:
    """Names the F2P test code defines itself: def/class names, parameters, assignment targets."""
    out = set()
    code = re.sub(r"^\s*\d+\s", "", test_text, flags=re.M)  # rendered sources carry line-number gutters
    for m in re.finditer(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)", code, re.M):
        out.add(m.group(1))
        for p in m.group(2).split(","):
            p = p.strip().lstrip("*").split(":")[0].split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_]\w*", p):
                out.add(p)
    out |= set(re.findall(r"^\s*class\s+(\w+)", code, re.M))
    for m in re.finditer(r"^\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+=\s", code, re.M):
        out |= {x.strip() for x in m.group(1).split(",")}
    out |= set(re.findall(r"^\s*(?:for|with[^\n]*\bas)\s+([A-Za-z_]\w*)", code, re.M))
    return out


def sources(issue: str, routed_rows: list[dict], test_text: str) -> dict:
    api = [str(x) for a in routed_rows for x in (a.get("api") or [])]
    code = [str(a.get("code") or "") for a in routed_rows]
    lits = [str(a["literal"]) for a in routed_rows if a.get("literal") not in (None, "")]
    issue_S = source_index(issue, "")
    api_words = set(WORD.findall("\n".join(api)))
    side = "\n".join(api + code)
    side_words = set(WORD.findall(side))
    # identifier-shaped contract values (a type, exception class or name that an assertion checks, e.g. Int64)
    side_words |= {L for L in lits if re.fullmatch(r"[A-Za-z_]\w*", L)}
    side += "\n" + "\n".join(L for L in lits if re.fullmatch(r"[A-Za-z_][\w.]*", L))
    defined = test_defined(test_text) - issue_S["words"] - api_words
    words = issue_S["words"] | (side_words - defined)
    dotted = set(issue_S["dotted"])
    for d in re.findall(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", side):
        parts = d.split(".")
        for i in range(len(parts) - 1):
            suf = parts[i:]
            if suf[0] in defined:
                continue
            dotted.add(".".join(suf))
    return {"text": issue + "\n" + side, "issue": issue, "words": words, "dotted": dotted, "literals": lits,
            "issue_numbers": set(NUM.findall(issue)), "test_defined": sorted(defined), "api": api, "code": code}


def provenance(tok: dict, S: dict) -> str | None:
    t = tok["tok"]
    if t in STOP or t.rstrip(".") in STOP:
        return "stoplist"
    if tok["kind"] == "code":
        return "issue_or_entry_point" if t in S["text"] else None
    if tok["kind"] == "literal":
        inner = t[1:-1] if t[:1] in "\"'" and t[-1:] == t[:1] else t
        if NUM.fullmatch(inner):
            if inner in S["issue_numbers"]:
                return "issue"
            if any(inner in NUM.findall(L) or inner == L.strip() for L in S["literals"]):
                return "routed_contract_value"
            return None
        if inner and (t in S["issue"] or inner in S["issue"]):
            return "issue"
        if inner and any(inner in L for L in S["literals"]):
            return "routed_contract_value"
        return None
    if t.startswith("--"):
        return "issue_or_entry_point" if t in S["text"] else None
    if "." in t:
        ok = t in S["dotted"] or re.search(r"(?<![\w.])" + re.escape(t) + r"(?![\w])", S["text"])
        return "issue_or_entry_point" if ok else None
    return "issue_or_entry_point" if t in S["words"] else None


NUM_PROSE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?=$|[^\w.]|\.(?:\s|$))")


def prose_numbers_fixed(toks: list[dict], scan: str) -> list[dict]:
    """A prose number directly before a sentence-final period ("returns 200.") is re-scanned here (NUM_RE forbids a
    following '.')."""
    rest = QUOTED_RE.sub(" ", SPAN_RE.sub(" ", scan))
    keep = [t for t in toks if not (t["kind"] == "literal" and t["where"] == "prose")]
    return keep + [{"tok": m.group(0), "kind": "literal", "where": "prose"} for m in NUM_PROSE.finditer(rest)]


def check(text: str, S: dict, present_at_base, gold_patch: str, base_presence: bool = True) -> dict:
    """The token rules on one checked text. ``present_at_base(tokens) -> set``: whole-word presence in the base tree."""
    emph = sorted({m.group(0) for m in EMPH_RE.finditer(SPAN_RE.sub(" ", text))})
    emph_base = present_at_base(emph) if (emph and base_presence) else set()
    emphasis = [e for e in emph if e not in emph_base]
    scan = text
    for e in emphasis:
        scan = re.sub(r"(?<![\w`])" + re.escape(e) + r"(?![\w`])", e.strip("_"), scan)
    toks = prose_numbers_fixed(tokens(scan), scan)
    reasons, rows, unprov = Counter(), [], []
    for tk in toks:
        p = provenance(tk, S)
        row = dict(tk, provenance=p)
        if tk["kind"] in ("identifier", "code"):
            segs = segments(tk["tok"].lstrip("-")) if not tk["tok"].startswith("--") else []
            if any(is_private_segment(s) for s in segs):
                row["private"] = True
                reasons["private_segment"] += 1
            if any(s in INTERNAL_SEGS for s in segs):
                row["internal"] = True
                reasons["internal_segment"] += 1
        elif tk["kind"] == "literal":
            if any(is_private_segment(x) for x in re.split(r"[^\w]+", tk["tok"])):
                row["private"] = True
                reasons["private_segment"] += 1
        if p is None:
            unprov.append(row)
        rows.append(row)
    words_all = {w for r in unprov if r["kind"] != "literal" for w in WORD.findall(r["tok"])}
    present = present_at_base(words_all) if (base_presence and words_all) else set()
    gold_words = gold_added_words(gold_patch) if unprov else set()
    for r in unprov:
        if r["kind"] == "literal":
            r["unprov"] = "literal_not_issue_or_contract_value"
            reasons["unprovenanced_literal"] += 1
            continue
        ws = WORD.findall(r["tok"])
        missing = [w for w in ws if w not in S["words"] and w not in STOP]
        if any(w in S["test_defined"] for w in missing):
            r["unprov"] = "test_defined_name"
            reasons["test_defined_identifier"] += 1
        elif missing and all(w not in present for w in missing) and any(w in gold_words for w in missing):
            r["unprov"] = "gold_only"
            reasons["gold_only_identifier"] += 1
        elif missing and any(w in present for w in missing):
            r["unprov"] = "base_present"
            reasons["unprovenanced_identifier_base_present"] += 1
        else:
            r["unprov"] = "absent_everywhere" if missing else "dotted_form_unseen"
            reasons["unprovenanced_identifier_other"] += 1
    paths = [m.group(0) for m in PATH_RE.finditer(text)]
    if paths:
        reasons["path_like"] += len(paths)
    instr = [m.group(0).strip() for m in INSTR_RE.finditer(SPAN_RE.sub("`x`", text))]
    if instr:
        reasons["instruction_phrasing"] += len(instr)
    testw = [m.group(0) for m in TEST_RE.finditer(text)]
    if testw:
        reasons["test_word"] += len(testw)
    return {"pass": not reasons, "reasons": dict(reasons), "paths": paths, "instruction": instr, "test_words": testw,
            "tokens": rows, "markdown_emphasis_ignored": emphasis}


# ------------------------------------------------------------------ rendering and form
def render(requirements: list[str], interfaces: list[dict]) -> dict:
    req = "\n".join(f"- {r}" for r in requirements)
    if not interfaces:
        ifc = NO_IFC
    else:
        ifc = "\n\n".join("\n".join(f"- {k.capitalize()}: {e[k]}" for k in IFC_KEYS) for e in interfaces)
    return {"requirements": req, "interfaces": ifc}


def spec_text(fields: dict) -> str:
    """The appended specification block (SWE-bench Pro layout)."""
    return f"Requirements:\n{fields['requirements']}\n\nNew interfaces introduced:\n{fields['interfaces']}"


SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\"])")


def quoted_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of the quoted spans of text: `code`, "double", curly and 'single' quotes (a single quote opens only
    after a non-word character and closes before one, so apostrophes never quote)."""
    spans, masked = [], text
    for rx in (r"`[^`]*`", r'"[^"]*"', "“[^”]*”", r"(?<!\w)'[^'\n]*'(?!\w)"):
        for m in re.finditer(rx, masked):
            spans.append(m.span())
        masked = re.sub(rx, lambda x: "x" * len(x.group()), masked)
    return spans


def sentences(text: str) -> list[str]:
    """Split after . ! ? before an upper-case letter, backtick or quote; never inside a quoted span."""
    t = text.strip()
    qs = quoted_spans(t)
    cuts = [m for m in SPLIT.finditer(t) if not any(a < m.start() < b for a, b in qs)]
    out, cur = [], 0
    for m in cuts:
        out.append(t[cur:m.start()])
        cur = m.end()
    out.append(t[cur:])
    return [x for x in out if x]


def words(s: str) -> int:
    return len(s.split())


def form(requirements: list[str], interfaces: list[dict], template: str | list[str] | None = None,
         route: str | list[str] | None = None) -> dict:
    """SPECIFICATION FORMAT. template: the route's template requirement (S-a, IS) or None (S-lit, S-keep, S-scope).
    Several statement routes of one task are written together: route is the list of the routes and template the list
    of template requirements, which are the first requirements in the given order (the S-a templates, then the IS
    discussion-quote requirement); the rules apply to the whole specification."""
    routes = [route] if isinstance(route, str) else list(route or [])
    templates = [template] if isinstance(template, str) else [t for t in (template or []) if t]
    nt = len(templates)
    label = "/".join(dict.fromkeys(routes))
    why = []
    for r in dict.fromkeys(routes):
        if r not in ROUTES:
            why.append(f"unknown route {r!r}")
    if any(r in TEMPLATE_ROUTES for r in routes) and not templates:
        why.append(f"route {label} needs a template requirement")
    if routes and not any(r in TEMPLATE_ROUTES for r in routes) and templates:
        why.append(f"route {label} has no template requirement")
    if not requirements:
        why.append("no requirement")
    if templates and requirements[:nt] != templates:
        why.append("template requirement not first/verbatim")
    if "IS" in routes and requirements and is_quote_of(requirements[max(nt, 1) - 1]) is None:
        why.append("route IS: the first requirement is not a discussion-quote requirement")
    if "IS" in routes and len(requirements) < max(nt, 1) + 1:
        why.append(f"route IS: a bare quote is not a route (at least {max(nt, 1) + 1} requirements)")
    rw = sum(words(r) for r in requirements)
    for i, r in enumerate(requirements):
        if "\n" in r:
            why.append("multi-line requirement")
        if i >= nt and len(sentences(r)) > 1:  # only template requirements may have several
            why.append(f"requirement {i + 1} has {len(sentences(r))} sentences")
    iw = []
    for e in interfaces:
        if tuple(e) != IFC_KEYS:
            why.append(f"interface keys {list(e)} != {list(IFC_KEYS)}")
            continue
        n = sum(words(str(e[k])) for k in IFC_KEYS)
        iw.append(n)
        if n > IFC_MAX_WORDS:
            why.append(f"interface entry {n} words > {IFC_MAX_WORDS}")
        if str(e["type"]).strip().lower() not in TYPES:
            why.append(f"interface type {e['type']!r} not in {sorted(TYPES)}")
        if len(sentences(str(e["description"]))) > 1:
            why.append("interface description is more than one sentence")
        if any("\n" in str(v) for v in e.values()):
            why.append("multi-line interface field")
    return {"ok": not why, "reasons": why, "requirements": len(requirements), "requirement_words": rw,
            "interfaces": len(interfaces), "interface_words": iw}


def interface_texts(e: dict) -> list[tuple[str, str]]:
    """(field, text to check) for one interface entry; Type is checked by form(), N/A values are skipped, and the
    Name is checked as an identifier (backticked if it is not already)."""
    out = []
    for k in ("name", "input", "output", "description"):
        v = str(e.get(k, "")).strip()
        if v.lower() in NA:
            continue
        if k == "name" and "`" not in v:
            v = f"`{v}`"
        out.append((k, v))
    return out


# ------------------------------------------------------------------ discussion quotes (route IS)
def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _word(c: str) -> bool:
    return c.isalnum() or c == "_"


def cut_at_word_boundaries(q: str, h: str) -> bool:
    """Some occurrence of q in h neither starts nor ends inside a word."""
    for m in re.finditer(re.escape(q), h):
        s, e = m.start(), m.end()
        left = s == 0 or not (_word(h[s - 1]) and _word(q[0]))
        right = e == len(h) or not (_word(h[e]) and _word(q[-1]))
        if left and right:
            return True
    return False


def quote_check(quote: str, hints: str, candidates: list[str] | None = None) -> dict:
    """DISCUSSION QUOTE: verbatim in the discussion after whitespace normalisation, cut at word boundaries, <= 300
    characters (normalised), no code fence, no ellipsis; with candidates (route IS: both families' quotes), the span
    must lie inside one of them. Shortest span and clause boundary are judged by G2, not here."""
    q = norm_ws(quote)
    h = norm_ws(hints)
    why = []
    if not q:
        why.append("empty quote")
    if len(q) > QUOTE_MAX_CHARS:
        why.append(f"quote {len(q)} characters > {QUOTE_MAX_CHARS}")
    if "```" in (quote or "") or "~~~" in (quote or ""):
        why.append("code fence in quote")
    if re.search(r"\.\.\.|\u2026", q):
        why.append("ellipsis in quote")
    if q and q not in h:
        why.append("quote not verbatim in the discussion")
    elif q and not cut_at_word_boundaries(q, h):
        why.append("quote cut inside a word")
    if q and candidates is not None and not any(q in norm_ws(c) for c in candidates if c):
        why.append("span not inside a family quote")
    return {"ok": not why, "reasons": why, "quote": q}


def is_quote_of(requirement: str) -> str | None:
    """The quoted span of a discussion-quote requirement, or None if the requirement does not have the IS frame."""
    m = re.fullmatch(re.escape(IS_FRAME) + r' "(.*)"', (requirement or "").strip(), re.S)
    return m.group(1) if m else None


def is_template(quote: str) -> str:
    """The IS template requirement, built from the whitespace-normalised quote."""
    return f'{IS_FRAME} "{norm_ws(quote)}"'
