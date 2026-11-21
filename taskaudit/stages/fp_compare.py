"""The counterexample comparator of the FP stage (the as-run logic; generated identifiers renamed).

A counterexample script is run in its *behaviour form* (``rewrite``): every ``print(...)`` inside an except handler
becomes a raised-or-not marker for its try site, and every except clause of a plain try statement is broadened: an
exception of the clause's own class(es) is caught there as before, one of a later clause's class falls through to it,
and any other exception is caught by the first clause (raised-or-not) and marks ``#TA_FOREIGN try<t>`` on stdout. At
the original code such a foreign catch counts as an uncaught raise of the original script: no test is built from it.
Two runs behave the same (``same_behaviour``) if both exit non-zero (whatever was raised), or both exit 0 with the same
number of stdout value lines, each equal up to formatting (whitespace, addresses, quote style, numeric / literal
value). Confirmed = base == gold != candidate.
"""
from __future__ import annotations

import ast
import re

PERF = re.compile(r"\b(time\.(time|perf_counter|monotonic|process_time)|timeit|tracemalloc|getrusage|psutil|cProfile|"
                  r"sys\.getsizeof|memory_usage)\b")
WARN = re.compile(r"Warning\b")
EXC = re.compile(r"^\w[\w.]*(Error|Exception|Exit|Interrupt)\b")
NOISE = re.compile(r"^(\[\d+/\d+\] .*|\+ \S*\bninja\b.*|ninja: .*)$")
FOREIGN = "#TA_FOREIGN"
STDOUT_MAX = 20000

# the equality helpers, also written into every generated test file (one source)
HELPER = r'''
import ast
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[@@DEPTH@@]
_TIMEOUT = @@TIMEOUT@@
_NO = object()
_NOISE = re.compile(r"^(\[\d+/\d+\] .*|\+ \S*\bninja\b.*|ninja: .*)$")


def _canon(s):
    s = re.sub(r"0x[0-9a-fA-F]+", "0x?", (s or "").strip())
    s = re.sub(r"/tmp/[\w./-]+", "/tmp/?", s)
    return re.sub(r"\s+", " ", s)


def _num(s):
    try:
        return complex(s.replace(" ", "")) if "j" in s else float(s)
    except (ValueError, TypeError):
        return None


def _lit(s):
    try:
        return ast.literal_eval(s)
    except Exception:  # noqa: BLE001
        return _NO


def _close(x, y):
    if isinstance(x, float) and isinstance(y, float):
        return x == y or math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12) or (math.isnan(x) and math.isnan(y))
    if isinstance(x, (list, tuple)) and isinstance(y, (list, tuple)) and type(x) is type(y) and len(x) == len(y):
        return all(_close(a, b) for a, b in zip(x, y))
    return x == y


def _same(a, b):
    """Equal up to formatting: whitespace, addresses, quote style, numeric / literal value."""
    a, b = _canon(a), _canon(b)
    if a == b or a.replace('"', "'") == b.replace('"', "'"):
        return True
    x, y = _num(a), _num(b)
    if x is not None and y is not None:
        return _close(x, y)
    x, y = _lit(a), _lit(b)
    return x is not _NO and y is not _NO and _close(x, y)


def _lines(out):
    return [x for x in (out or "").splitlines() if x.strip() and not _NOISE.match(x.strip()) and
            not x.strip().startswith("#TA_FOREIGN")]


def _observe(script):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "regression_case.py"
        p.write_text(script)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(x for x in (str(_ROOT), os.environ.get("PYTHONPATH")) if x))
        r = subprocess.run([sys.executable, str(p)], cwd=str(_ROOT), env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=_TIMEOUT)
    out, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    exc = [x for x in err.splitlines() if re.match(r"^\w[\w.]*(Error|Exception|Exit|Interrupt)\b", x)]
    return _lines(out), r.returncode, (exc[-1] if exc else "")


def _check(script, expected):
    lines, rc, exc = _observe(script)
    assert rc == 0 and len(lines) == len(expected) and all(_same(a, b) for a, b in zip(lines, expected)), (
        "regression check: expected %r (exit 0, as at the base commit), got %r (exit %s) %s"
        % (expected, lines, rc, exc))
'''
_H: dict = {"__file__": __file__}
exec(compile(HELPER.replace("@@DEPTH@@", "0").replace("@@TIMEOUT@@", "120"), "<helper>", "exec"), _H)
same = _H["_same"]

PROLOGUE = """import sys as _ta_sys
_ta_seen = []
def _ta_raised(t, e):
    if any(x[0] == t and x[1] is e for x in _ta_seen):
        return  # one marker per caught exception and try site
    _ta_seen.append((t, e))
    print('#TA_RAISED try%d' % t, flush=True)
class _TaNever(BaseException):
    pass
def _ta_isa(f):
    try:
        return isinstance(_ta_sys.exc_info()[1], f())
    except Exception:
        return False
def _ta_cls(t, own, later, bare):
    if _ta_isa(own):
        return BaseException
    if later is None or bare or any(_ta_isa(f) for f in later):
        return _TaNever
    print('#TA_FOREIGN try%d' % t, flush=True)
    return BaseException
"""
TRY_TYPES = tuple(x for x in (getattr(ast, "Try", None), getattr(ast, "TryStar", None)) if x)


def try_sites(tree) -> dict[int, int]:
    tries = [n for n in ast.walk(tree) if isinstance(n, TRY_TYPES)]
    return {id(n): i for i, n in enumerate(sorted(tries, key=lambda n: (n.lineno, n.col_offset)))}


def handler_prints(tree) -> list[tuple]:
    """(print Call node, (t, bound name) or None): a print is owned by its innermost enclosing except-handler body
    (a print to a file other than stdout is not)."""
    tidx = try_sites(tree)
    out = []

    def visit(node, owner):
        if isinstance(node, TRY_TYPES):
            t = tidx[id(node)]
            for st in node.body:
                visit(st, owner)
            for h in node.handlers:
                if h.type is not None:
                    visit(h.type, owner)
                for st in h.body:
                    visit(st, (t, h.name))
            for st in node.orelse + node.finalbody:
                visit(st, owner)
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
            kw = {k.arg: k.value for k in node.keywords}
            to_file = "file" in kw and not (isinstance(kw["file"], ast.Attribute) and kw["file"].attr == "stdout")
            if not to_file:
                out.append((node, owner))
        for ch in ast.iter_child_nodes(node):
            visit(ch, owner)
    visit(tree, None)
    return out


def rewrite(script: str) -> str:
    """The behaviour form of a counterexample script (no reindent, no unparse; raises SyntaxError when unparsable)."""
    tree = ast.parse(script)
    lines = script.split("\n")

    def off(ln, col):  # ast offsets are UTF-8 byte offsets
        return sum(len(x) + 1 for x in lines[:ln - 1]) + len(lines[ln - 1].encode("utf-8")[:col].decode("utf-8"))

    def seg(n):
        return script[off(n.lineno, n.col_offset):off(n.end_lineno, n.end_col_offset)]
    spans = []
    for node, owner in handler_prints(tree):
        if owner is None:
            continue
        a, b = off(node.lineno, node.col_offset), off(node.end_lineno, node.end_col_offset)
        if any(x <= a and b <= y for x, y, _ in spans):
            continue
        t, name = owner
        spans.append((a, b, f"_ta_raised({t}, {name or '_ta_sys.exc_info()[1]'})"))
    tidx = try_sites(tree)
    for n in ast.walk(tree):
        if isinstance(n, ast.Try):  # not except*: its clauses cannot name BaseException groups freely
            typed = [h for h in n.handlers if h.type is not None]
            bare = len(typed) < len(n.handlers)
            for k, h in enumerate(typed):
                a, b = off(h.type.lineno, h.type.col_offset), off(h.type.end_lineno, h.type.end_col_offset)
                if any(x <= a < y or x < b <= y for x, y, _ in spans):
                    continue
                later = "None"
                if k == 0:
                    later = "(" + "".join(f"lambda: ({ast.unparse(x.type)}), " for x in typed[1:]) + ")"
                spans.append((a, b, f"_ta_cls({tidx[id(n)]}, lambda: ({seg(h.type)}), {later}, {bare})"))
    out = script
    for a, b, rep in sorted(spans, reverse=True):
        out = out[:a] + rep + out[b:]
    body = tree.body
    k = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and \
            isinstance(body[0].value.value, str):
        k = 1
    while k < len(body) and isinstance(body[k], ast.ImportFrom) and body[k].module == "__future__":
        k += 1
    at = body[k - 1].end_lineno if k else 0
    ol = out.split("\n")
    out = "\n".join(ol[:at] + PROLOGUE.rstrip("\n").split("\n") + ol[at:])
    ast.parse(out)
    return out


def observe(stdout: str, stderr: str, rc: int | None, seconds: float | None = None, timeout: bool = False) -> dict:
    raw = [x for x in (stdout or "").splitlines() if x.strip() and not NOISE.match(x.strip())]
    lines = [x for x in raw if not x.strip().startswith(FOREIGN)]
    foreign = sorted({int(m.group(1)) for x in raw if (m := re.match(rf"{FOREIGN} try(\d+)$", x.strip()))})
    exc = next((x for x in reversed((stderr or "").splitlines()) if EXC.match(x)), "")
    return {"lines": lines, "rc": rc, "timeout": timeout, "exc": exc[:500], "seconds": seconds,
            "truncated": len(stdout or "") > STDOUT_MAX, "foreign": foreign}


def same_behaviour(a: dict, b: dict) -> bool:
    ra, rb = a["rc"] != 0, b["rc"] != 0
    if ra or rb:
        return ra == rb
    return len(a["lines"]) == len(b["lines"]) and all(same(x, y) for x, y in zip(a["lines"], b["lines"]))


def confirm_regression(base: dict | None, gold: dict | None, cand: dict | None) -> tuple[bool, str]:
    for n, o in (("base", base), ("gold", gold), ("candidate", cand)):
        if not o or o.get("error"):
            return False, f"{n} did not run"
        if o.get("timeout") or o.get("rc") is None:
            return False, f"{n} timeout"
    if not same_behaviour(base, gold):
        return False, "base != gold"
    if same_behaviour(cand, base):
        return False, "candidate == base"
    return True, "base == gold != candidate"


def no_test_reason(script: str, base: dict) -> str | None:
    if base["rc"] != 0:
        return "base exits non-zero (uncaught raise): no test"
    if base.get("foreign"):
        return "base raises outside the script's own except class (uncaught originally): no test"
    if PERF.search(script or ""):
        return "PERF: measures time / memory"
    if any(WARN.search(x) for x in base.get("lines") or []):
        return "WARN: a warning printed as a value"
    return None

