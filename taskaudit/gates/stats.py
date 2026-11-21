"""Deterministic helpers for the stage rules and the memorisation screen of the blind solve. No LLM, no containers,
standard library only (the as-run module; comments edited).

    uv run python -m taskaudit.gates.stats selftest

- wilson(k, n): Wilson 95% interval (stop rule of the residual-FN audit of cleared tasks).
- expected_agreement(bands, histogram): band-category agreement that k = 8 sampling noise alone gives.
- diff_differs(patch, gold): "differs from the gold" for the FP measurement.
- fresh_keep(m0, new, histogram): whether an older measurement (m0) still agrees with a fresh one.
- memorised(patch, gold, instance_id): the memorisation screen of the blind solve (repair check G4).
- leak_flag(reply): the flag of one leak-judge reply (check G3, step 5c; the jump read G5, step 5g).
- g5_deletes(replies): the rule of the jump read (G5): delete only if both families flag.
"""
from __future__ import annotations

import io
import re
import sys
import tokenize
from collections import Counter
from math import comb, sqrt

Z95 = 1.959964
K = 8


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def audit_stops(confirmed: int, n: int = 80, bar: float = 0.10) -> bool:
    """Residual-FN audit: stop if the Wilson 95% upper bound of confirmed / n exceeds the bar."""
    return wilson(confirmed, n)[1] > bar


# ------------------------------------------------------------------ freshness check of an older measurement (m0)

def category(band: int) -> str:
    return "0/8" if band == 0 else "8/8" if band == K else "1-7/8"


def _prior(histogram: dict[int, int], grid_n: int = 400, iters: int = 3000) -> tuple[list[float], list[float]]:
    """Grid NPMLE (EM) of the pass-rate prior from a k = 8 band histogram."""
    grid = [(i + 0.5) / grid_n for i in range(grid_n)]
    lik = [[comb(K, b) * g ** b * (1 - g) ** (K - b) for g in grid] for b in range(K + 1)]
    w = [1 / grid_n] * grid_n
    n = sum(histogram.values())
    for _ in range(iters):
        acc = [0.0] * grid_n
        for b, cnt in histogram.items():
            row = [lik[b][j] * w[j] for j in range(grid_n)]
            s = sum(row)
            for j in range(grid_n):
                acc[j] += cnt * row[j] / s
        w = [a / n for a in acc]
    return grid, w


def expected_agreement(bands: list[int], histogram: dict[int, int]) -> float:
    """Mean probability that a fresh k = 8 measurement lands in the same category (0/8, 1-7/8, 8/8) as the stored band,
    if nothing changed: posterior of each task's pass rate (prior fitted to the histogram) -> predictive band."""
    grid, w = _prior(histogram)
    lik = [[comb(K, b) * g ** b * (1 - g) ** (K - b) for g in grid] for b in range(K + 1)]
    out = []
    for b in bands:
        post = [lik[b][j] * w[j] for j in range(len(grid))]
        s = sum(post)
        post = [x / s for x in post]
        same = [c for c in range(K + 1) if category(c) == category(b)]
        out.append(sum(post[j] * sum(lik[c][j] for c in same) for j in range(len(grid))))
    return sum(out) / len(out)


def fresh_keep(m0: list[int], new: list[int], histogram: dict[int, int], margin: float = 0.07,
               bar: float | None = None) -> dict:
    """Keep the stored bands iff category agreement >= E - margin. bar: a fixed bar instead (e.g. 0.90)."""
    agree = sum(category(a) == category(b) for a, b in zip(m0, new)) / len(m0)
    e = expected_agreement(m0, histogram)
    cut = e - margin if bar is None else bar
    return {"agreement": round(agree, 3), "expected": round(e, 3), "bar": round(cut, 3),
            "keep_m0": agree >= cut}


# ------------------------------------------------------------------ patches

TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*\.py$|_tests?\.py$|(^|/)conftest\.py$")
DOC_PATH = re.compile(r"\.(rst|md|txt)$|(^|/)(docs?|changelog|changes|history|news|whatsnew)[^/]*(/|$)", re.I)
CHANGELOG_PATH = re.compile(r"(^|/)(changelog|changes|history|news|release|whatsnew)[^/]*(/|$)|whatsnew", re.I)


def _files(patch: str):
    """yield (path, sign, line) for every added / removed line of a unified diff."""
    path = ""
    for line in (patch or "").splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            path = p[2:] if p.startswith("b/") else p
            continue
        if line.startswith("--- ") or line.startswith("diff --git"):
            continue
        if line and line[0] in "+-" and not line.startswith(("+++", "---")):
            yield path, line[0], line[1:]


def _strip_comment(code: str) -> str:
    """remove a `#` comment outside string literals (tokenize; a line that does not tokenize keeps its text)."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(code + "\n").readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return code
    for t in toks:
        if t.type == tokenize.COMMENT:
            return code[: t.start[1]]
    return code


def normalised(patch: str) -> dict:
    out: dict = {}
    for path, sign, line in _files(patch):
        if TEST_PATH.search(path) or DOC_PATH.search(path):
            continue
        s = re.sub(r"\s+", "", _strip_comment(line))
        if s:
            out.setdefault(path, {"+": Counter(), "-": Counter()})[sign][s] += 1
    return out


def diff_differs(patch: str, gold: str) -> bool:
    """FP measurement: the normalised diffs differ (source files only; whitespace and # comments removed)."""
    return normalised(patch) != normalised(gold)


def _added(patch: str, skip_tests: bool) -> list[tuple[str, str]]:
    return [(p, " ".join(l.split())) for p, s, l in _files(patch) if s == "+" and not (skip_tests and TEST_PATH.search(p))]


def informative(s: str) -> bool:
    return not (len(s) < 4 or s.startswith("#") or not re.search(r"[A-Za-z0-9]", s)
                or s in ('"""', "'''", "pass", "else:", "try:", "return", "continue", "break"))


def gold_overlap(patch: str, gold: str) -> tuple[float | None, int]:
    """Share of the gold's informative added non-test lines (whitespace-normalised) that occur
    verbatim among the attempt's added lines; also the number of matched lines."""
    g = {s for _, s in _added(gold, True) if informative(s)}
    if not g:
        return None, 0
    mine = {s for _, s in _added(patch, False)}
    m = len(g & mine)
    return m / len(g), m


def memorised(patch: str, gold: str, instance_id: str, share: float = 0.5, min_lines: int = 3) -> dict:
    """Memorisation screen of the blind solve (G4): a 'satisfied' label of such an attempt counts as 'not reached'."""
    ov, m = gold_overlap(patch, gold)
    pr = instance_id.rsplit("-", 1)[-1]
    added = "\n".join(s for _, s in _added(patch, False))
    has_pr = bool(pr.isdigit() and re.search(r"(?<![\w.])" + pr + r"(?![\w])", added))
    cl_gold = {s for p, s in _added(gold, False) if CHANGELOG_PATH.search(p) and len(s) >= 4}
    cl_mine = {s for p, s in _added(patch, False) if CHANGELOG_PATH.search(p) and len(s) >= 4}
    why = []
    if ov is not None and ov >= share and m >= min_lines:
        why.append(f"gold_overlap {ov:.2f} ({m} lines)")
    if has_pr:
        why.append(f"PR number {pr}")
    if cl_gold & cl_mine:
        why.append("changelog line of the gold")
    return {"memorised": bool(why), "why": why, "gold_overlap": ov, "matched_lines": m}


# ------------------------------------------------------------------ leak verdicts (checks G3 and G5)

def leak_flag(reply: dict | None, frame_only=None) -> bool | None:
    """The flag of one leak-judge reply (G3, or the jump read G5): any finding with is_leak true (frame-only spans
    dropped), or leak_level >= 1, or solution_given = 2. None when the reply has no usable verdict."""
    if not isinstance(reply, dict) or reply.get("leak_level") not in (0, 1, 2) or isinstance(reply.get("leak_level"), bool):
        return None
    frame_only = frame_only or (lambda span: False)
    leaks = [f for f in reply.get("findings") or [] if isinstance(f, dict) and f.get("is_leak") is True
             and not frame_only(f.get("span") or "")]
    return bool(leaks) or reply["leak_level"] >= 1 or reply.get("solution_given") == 2


def g5_deletes(replies: dict, frame_only=None) -> dict:
    """Jump read (G5): one reply per family ({"claude": reply, "gpt": reply}). Delete only if BOTH families flag
    (leak_flag); a family without a usable verdict after its attempts counts as no leak. A single-family flag is
    recorded, not acted on."""
    flags = {f: leak_flag(r, frame_only) for f, r in replies.items()}
    both = len(flags) == 2 and all(v is True for v in flags.values())
    rec = "G5_both_leak" if both else "G5_one_family" if any(v is True for v in flags.values()) else \
        "G5_no_verdict" if any(v is None for v in flags.values()) else "G5_clean"
    return {"delete": both, "flags": flags, "record": rec}


# ------------------------------------------------------------------ self-test

def selftest() -> int:
    fails = []

    def expect(name, got, want):
        ok = got == want
        print(("ok  " if ok else "FAIL"), name, "->", got)
        if not ok:
            fails.append(name)

    expect("wilson 2/80 upper", round(wilson(2, 80)[1], 4), 0.0866)
    expect("wilson 3/80 upper", round(wilson(3, 80)[1], 4), 0.1045)
    expect("audit continues at 2/80", audit_stops(2), False)
    expect("audit stops at 3/80", audit_stops(3), True)
    H = {0: 764, 1: 81, 2: 70, 3: 56, 4: 87, 5: 74, 6: 127, 7: 155, 8: 373}  # a k = 8 band histogram
    pool = [b for b, n in H.items() for _ in range(n)]
    e_all = expected_agreement(pool, H)
    e_cand = expected_agreement([b for b in pool if b < 8], H)
    print(f"noise-only expected agreement: all bands {e_all:.3f}, 0/8-7/8 {e_cand:.3f}")
    expect("noise-only agreement below a fixed 90% bar", e_all < 0.90 and e_cand < 0.90, True)
    expect("unchanged bands keep M0", fresh_keep([0, 3, 8, 5], [0, 3, 8, 5], H)["keep_m0"], True)
    expect("all categories moved -> re-measure", fresh_keep([0, 0, 8, 8], [3, 3, 5, 5], H)["keep_m0"], False)
    g = ("diff --git a/pkg/m.py b/pkg/m.py\n--- a/pkg/m.py\n+++ b/pkg/m.py\n@@ -1,2 +1,2 @@\n"
         "-    return x\n+    return x + 1  # fix GH 123\n")
    same = g.replace("x + 1  # fix GH 123", "x+1")
    other = g.replace("x + 1", "x + 2")
    doc = g + "diff --git a/doc/whatsnew.rst b/doc/whatsnew.rst\n--- a/doc/whatsnew.rst\n+++ b/doc/whatsnew.rst\n@@ -1 +1,2 @@\n+- Fixed bug\n"
    tst = g + "diff --git a/pkg/tests/test_m.py b/pkg/tests/test_m.py\n--- a/pkg/tests/test_m.py\n+++ b/pkg/tests/test_m.py\n@@ -1 +1,2 @@\n+assert f(1) == 2\n"
    s = "+    msg = \"a # b\"\n"
    expect("whitespace + comment only -> same as gold", diff_differs(same, g), False)
    expect("different value -> differs", diff_differs(other, g), True)
    expect("doc / test lines ignored", (diff_differs(doc, g), diff_differs(tst, g)), (False, False))
    expect("'#' inside a string is not a comment", _strip_comment(s[1:]).strip(), 'msg = "a # b"')
    big = "".join(f"+    value_{i} = compute_{i}(arg)\n" for i in range(6))
    gold = "diff --git a/p/a.py b/p/a.py\n--- a/p/a.py\n+++ b/p/a.py\n@@ -1 +1,6 @@\n" + big
    half = "diff --git a/p/a.py b/p/a.py\n--- a/p/a.py\n+++ b/p/a.py\n@@ -1 +1,6 @@\n" + "".join(big.splitlines(True)[:3])
    two = "diff --git a/p/a.py b/p/a.py\n--- a/p/a.py\n+++ b/p/a.py\n@@ -1 +1,6 @@\n" + "".join(big.splitlines(True)[:2])
    expect("overlap 3/6 lines -> memorised", memorised(half, gold, "o__r-1")["memorised"], True)
    expect("overlap 2/6 lines -> not memorised", memorised(two, gold, "o__r-1")["memorised"], False)
    expect("PR number in added lines -> memorised", memorised(g.replace("123", "4567"), gold, "o__r-4567")["memorised"], True)
    cl = ("diff --git a/doc/whatsnew/v2.rst b/doc/whatsnew/v2.rst\n--- a/doc/whatsnew/v2.rst\n+++ b/doc/whatsnew/v2.rst\n"
          "@@ -1 +1,2 @@\n+- Fixed :meth:`f` with empty input\n")
    expect("changelog line of the gold -> memorised", memorised(cl, gold + cl, "o__r-1")["memorised"], True)
    clean = {"findings": [{"span": "Name: f", "is_leak": False}], "leak_level": 0, "solution_given": 1}
    leak = {"findings": [{"span": "in pkg/core.py", "is_leak": True}], "leak_level": 1, "solution_given": 1}
    given = {"findings": [], "leak_level": 0, "solution_given": 2}
    frame = {"findings": [{"span": "A later comment on the issue says:", "is_leak": True}], "leak_level": 0,
             "solution_given": 0}
    expect("G3 flag: clean / is_leak / solution_given 2 / no verdict",
           (leak_flag(clean), leak_flag(leak), leak_flag(given), leak_flag(None)), (False, True, True, None))
    expect("G3 flag: frame-only span dropped",
           leak_flag(frame, lambda s: s.startswith("A later comment")), False)
    expect("G5: both families flag -> delete", g5_deletes({"claude": leak, "gpt": given})["delete"], True)
    expect("G5: one family flags -> keep (recorded)",
           (g5_deletes({"claude": leak, "gpt": clean})["delete"], g5_deletes({"claude": leak, "gpt": clean})["record"]),
           (False, "G5_one_family"))
    expect("G5: no usable verdict counts as no leak", g5_deletes({"claude": leak, "gpt": None})["delete"], False)
    print("FAILURES:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(selftest() if (sys.argv[1:] or ["selftest"])[0] == "selftest" else 2)
