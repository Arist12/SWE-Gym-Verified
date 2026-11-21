#!/usr/bin/env python
"""Mechanical code-evidence packs v4.1. No LLM, no network call in the normal path, no container.

v4.1 = v4 plus the fixes of a readability review:
  - header marker legend says that in section 5 `*` / `>` mark test-patch edits; S5 has its own legend line;
  - S10 single-definition fallback only when a word of the call's receiver occurs in the definition's class name;
  - S5 non-F2P bodies labelled "in PASS_TO_PASS" or "not graded by the harness" (in neither list); for the latter
    only the test-patch hunks +-3 lines are shown; a base test the test patch renames is labelled with its new name;
  - dataset ids cut at a space (an unclosed `[`) carry the note "id cut at a space in the dataset";
  - S12 pointers state the quoted base line;
  - zero-hit trims: S11 full bodies of section-9 symbols defined in test-utility modules (test paths, pandas/_testing)
    are left to section 9's signature / head; S11 class docstrings of <= 7 lines are dropped unless the gold patch
    edits the class body itself (not only its methods); S10 classes whose body is only a docstring / pass / `...`.

v4 = v3 plus five evidence sections and an exact section 5. Every section starts with a one-line purpose.

Changes against v3:
  S5  exact: one status line per F2P test function (ids that resolve to the same base def are collapsed); every F2P
      id listed (lossless compression: numeric suffix ranges, cartesian products of parameter components); ids of the
      same test that are PASS_TO_PASS listed as such ("passes at base"); parametrize rows mapped to their source when
      the ids are derivable; F2P bodies are filled before the non-F2P test-patch regions; F2P methods inherited from
      a base class (same file, or imported) are located.
  S10 callee bodies: base definitions that the gold-changed lines call (hop 1: resolved through self / super / the
      module / imports, else the single source definition of the name), and module-level functions of the callee's
      own module that a hop-1 body calls (hop 2).
  S11 touched members: full bodies of definitions the gold-changed lines or the F2P tests reference by name that
      the other sections show only as a signature, a head or a member list; docstrings of the edited classes.
  S12 traceback: base code at the innermost repository frame of each Python traceback quoted in the issue (section
      A only: the discussion is not solver-visible).
  S13 repository docs: prose doc paragraphs (rst/md, changelogs and versioned copies excluded) that mention a message
      the gold patch removes, or adds without the issue quoting it.
  S14 test-file context: in each file the test patch edits, the units the F2P tests use (setUp, same-file helpers,
      module constants) in full up to 12 lines, and the 10 other tests nearest the F2P tests as signature + first
      assertion line.
  Rules whose evidence density on the 120 calibration / fresh tasks fell below the weakest v3 section were dropped
  before freezing (other-module hop 2, outer traceback frames, doc grep for identifiers / F2P strings).
  Sections 10-14 share one item format (`### path:start-end `symbol` (why)` + numbered code block), skip any line
  range an earlier section already shows (a pointer names the section), and have a content ceiling (NEW_CEILING).

Budget (o200k tokens, header excluded): pass 1 builds every section at its content rules. If the pack exceeds
TOKEN_BUDGET, sections 10-14 are cut first, one after the other in CUT_ORDER, each down to nothing; only if that is not
enough do sections 1-9 degrade exactly as in v3 (guaranteed SECTION_BUDGET shares, water-filled pool, per-item
degradation, every omission stated in the text and in index.jsonl). The whole pack is never cut.

    python pipeline/packs/build_pack.py --ids ids.json [--jobs 8]

v1-v3 description of the shared machinery: tasks are read at their base commit from a local bare clone
($SGV_REPOS/<owner>__<name>.git, git ls-tree / cat-file, no checkout); a commit missing from the clone is
fetched, else /testbed is copied out of the task image (docker create --pull never, labeled, never started). Task data:
the SWE-Gym parquet (the command line only; the package calls work() directly). Sections 1-9 as in v3, except S5.
"""
from __future__ import annotations

import argparse
import ast
import bisect
import builtins
import hashlib
import io
import json
import keyword
import math
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
import warnings
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# machine paths (environment; see the workflow README)
WORK = Path(os.environ.get("SGV_WORK", "work")).resolve()
OUT_ROOT = WORK / "packs_v41"
OUT = OUT_ROOT / "packs"  # --out overrides
REPOS = Path(os.environ.get("SGV_REPOS", str(WORK / "repos")))  # bare clones <owner>__<name>.git
SCRATCH_CLONES = WORK / "scratch_clones"
PARQUET = Path(os.environ.get("SWEGYM_PARQUET", str(WORK / "SWE-Gym/train-00000-of-00001.parquet")))
HARNESS = Path(os.environ.get("SWEBENCH_FORK", str(WORK / "SWE-Bench-Fork"))) / "swebench/harness/constants.py"
CLEAN_CONFIG = Path(os.environ.get("SGV_SCOPE_CONFIG", str(WORK / "scope.json")))  # --scope only
DOCKER_LABELS = ["sgv.release=pack-builder"]
BUILDER_VERSION = "packs-v4.1"

TOKEN_BUDGET = 64000  # whole pack, o200k tokens (--budget overrides; section budgets scale with it)
# guaranteed per-section token budgets at TOKEN_BUDGET 64K (sum 62,000; the header takes the rest). A section that
# needs less releases the rest to the sections that need more (see work()).
SECTION_BUDGET = {"s1": 14000, "s2": 16000, "s3": 3500, "s4": 2500, "s5": 10000, "s6": 1000, "s7": 5000,
                  "s8": 5000, "s9": 5000}
OLD_SECTIONS = ["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9"]
NEW_SECTIONS = ["s10", "s11", "s12", "s13", "s14"]
SECTIONS = OLD_SECTIONS + NEW_SECTIONS  # render order
# sections 10-14: content ceilings (tokens; a content rule, not a budget cut) and the order in which they are cut when
# the pack exceeds TOKEN_BUDGET (each down to nothing before the next; sections 1-9 degrade only after all five)
NEW_CEILING = {"s10": 6000, "s11": 6000, "s12": 2500, "s13": 2500, "s14": 3000}
CUT_ORDER = ["s13", "s14", "s11", "s10", "s12"]
NOTICE_RESERVE = 150  # tokens per section kept for the explicit omission notices
UNLIMITED = 10 ** 9
# content rules (caps that are not budget cuts; stated in the pack text when they bind)
S1_VALUE_CHARS = 400
S2_BODY_LINES, S2_BODY_FLOOR, S2_MODULE_LINES = 300, 40, 60
S3_TOP_K, S3_FLOOR = 10, 3
S4_PER_MODULE = 80
S5_BODY_LINES, S5_BODY_FLOOR, S5_STMT_LINES = 120, 20, 60
S6_PIN_CHARS = 3000
S10_HOP1, S10_HOP2, S10_BODY_LINES, S10_FLOOR = 10, 4, 60, 12
S11_ITEMS, S11_BODY_LINES, S11_FLOOR = 10, 80, 12
S12_FRAMES, S12_BODY_LINES = 3, 25
S13_PARAS, S13_PER_FILE, S13_MAX_DF, S13_RADIUS = 4, 2, 8, 8
S14_TESTS_PER_FILE, S14_CONTEXT_LINES = 10, 12
BUILTIN_METHODS = set().union(*(dir(t_) for t_ in (list, dict, set, str, bytes, tuple, int, float, frozenset, object)))
SHOWN_MARK = "!"  # code-block marker of the focus line in sections 10-14 (traceback frame line, call site)
# v4.1: S11 shows no full body of a section-9 symbol defined in a test-utility module (section 9 keeps its head)
TEST_UTIL_PATH = re.compile(r"(^|/)_testing(/|\.py$)|(^|/)test_?utils?(/|\.py$)")
S11_SHORT_CLASS_DOC = 7  # class docstring items of at most this many lines: dropped unless the class body is edited
S5_UNGRADED_RADIUS = 3  # non-graded test bodies: only the test-patch hunks +- this many base lines
ID_CUT_NOTE = "id cut at a space in the dataset"
CHANGELOG_PATH = re.compile(r"whatsnew|change[-_ ]?log|changes|history|news|release[-_ ]?notes", re.IGNORECASE)

# verify_tasks.TEST_PATH / DOC_PATH (copied: this script stays importable without the verify venv)
TEST_PATH = re.compile(
    r"(^|/)(tests?|testing|test-data)(/|$)|(^|/)test_[^/]*$|_tests?\.py$|(^|/)conftest\.py$")
DOC_PATH = re.compile(
    r"(^|/)(docs?|doc|changelog\.d|changes)(/|$)|\.(md|rst|txt|adoc)$|(^|/)(CHANGELOG|CHANGES|HISTORY|NEWS|AUTHORS)",
    re.IGNORECASE)
SEARCH_EXT = (".py", ".pyx", ".pxd", ".pxi", ".pyi", ".test", ".cfg", ".toml", ".ini", ".txt", ".rst", ".md",
              ".yaml", ".yml")
MAX_FILE = 1_500_000
SKIP_NAMES = set(keyword.kwlist) | set(getattr(keyword, "softkwlist", [])) | set(dir(builtins)) | {"self", "cls"}
TRIVIAL_NUMBERS = {"0", "1", "2", "-1", "0.0", "1.0", "2.0"}
COMMON_HITS = 300  # items with at least this many base hits are listed compactly
ASSERT_CALL = re.compile(r"^(assert|fail|raises|warns|produces_warning|deprecated_call|expect)", re.IGNORECASE)
ROLE_RANK = {"F2P-assert": 0, "F2P-param": 1, "F2P-body": 2, "F2P-const": 2, "gold+": 3}
CAT_RANK = {"src": 0, "test": 1, "doc": 2}


# ------------------------------------------------------------------ repos and trees

def repo_dir(repo: str) -> Path:
    return REPOS / (repo.replace("/", "__") + ".git")


def git(d, *args, input=None, check=True, timeout=600) -> bytes:
    return subprocess.run(["git", "-C", str(d), *args], input=input, capture_output=True, check=check,
                          timeout=timeout).stdout


def ensure_repo(repo: str) -> Path:
    d = repo_dir(repo)
    if d.is_dir():
        return d
    REPOS.mkdir(parents=True, exist_ok=True)
    tmp = d.parent / (d.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    src = SCRATCH_CLONES / d.name
    url = f"https://github.com/{repo}"
    subprocess.run(["git", "clone", "-q", "--bare", *(["--local", str(src)] if src.is_dir() else [url]), str(tmp)],
                   check=True, timeout=3600)
    git(tmp, "remote", "set-url", "origin", url)
    os.rename(tmp, d)
    return d


def has_commit(d: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(d), "cat-file", "-e", sha + "^{commit}"], capture_output=True).returncode == 0


def ensure_commit(d: Path, sha: str) -> bool:
    if has_commit(d, sha):
        return True
    subprocess.run(["git", "-C", str(d), "fetch", "-q", "origin", sha], capture_output=True, timeout=900)
    return has_commit(d, sha)


def category(path: str) -> str:
    if TEST_PATH.search(path):
        return "test"
    if DOC_PATH.search(path):
        return "doc"
    return "src"


def decode(b: bytes) -> str | None:
    if b"\x00" in b[:8192]:
        return None
    return b.decode("utf-8", errors="replace")


class GitTree:
    def __init__(self, d: Path, sha: str):
        self.d, self.sha, self.source = d, sha, f"git:{d.name}@{sha}"
        self.files: dict[str, tuple[str, int]] = {}
        for ent in git(d, "ls-tree", "-r", "-z", "--long", sha).split(b"\0"):
            if not ent:
                continue
            meta, path = ent.split(b"\t", 1)
            mode, typ, obj, size = meta.split()
            if typ == b"blob" and mode in (b"100644", b"100755"):
                self.files[path.decode("utf-8", "surrogateescape")] = (obj.decode(), int(size))
        self._cache: dict[str, str | None] = {}

    def read_many(self, paths):
        need = [p for p in paths if p not in self._cache and p in self.files]
        if need:
            shas = [self.files[p][0] for p in need]
            out = git(self.d, "cat-file", "--batch", input="".join(s + "\n" for s in shas).encode())
            i, blobs = 0, {}
            for s in shas:
                j = out.index(b"\n", i)
                head = out[i:j].split()
                if len(head) < 3 or head[1] == b"missing":
                    i = j + 1
                    continue
                size = int(head[2])
                blobs[s] = out[j + 1:j + 1 + size]
                i = j + 1 + size + 1
            for p, s in zip(need, shas):
                self._cache[p] = decode(blobs[s]) if s in blobs else None
        return {p: self._cache.get(p) for p in paths}

    def read(self, path):
        return self.read_many([path])[path] if path in self.files else None

    def close(self):
        pass


class DirTree:
    """/testbed copied out of the task image (fallback when the commit is not in the clone)."""

    def __init__(self, root: Path, tmp: Path, image: str):
        self.root, self.tmp, self.source = root, tmp, f"image:{image}:/testbed"
        self.files = {}
        for dp, dn, fn in os.walk(root):
            dn[:] = sorted(x for x in dn if x != ".git")
            for f in sorted(fn):
                p = Path(dp) / f
                if p.is_file() and not p.is_symlink():
                    self.files[str(p.relative_to(root))] = ("", p.stat().st_size)
        self._cache = {}

    def read_many(self, paths):
        for p in paths:
            if p not in self._cache and p in self.files:
                self._cache[p] = decode((self.root / p).read_bytes())
        return {p: self._cache.get(p) for p in paths}

    def read(self, path):
        return self.read_many([path])[path] if path in self.files else None

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def image_name(iid: str) -> str:
    return "xingyaoww/sweb.eval.x86_64." + iid.lower().replace("__", "_s_") + ":latest"


def testbed_tree(iid: str) -> DirTree:
    image = image_name(iid)
    tmp = Path(tempfile.mkdtemp(prefix="packs_v4_testbed_", dir=str(OUT_ROOT)))
    cid = subprocess.run(["docker", "create", "--pull", "never", "--network", "none", "--cpus", "2",
                          *sum((["--label", x] for x in DOCKER_LABELS), []), image, "true"],
                         capture_output=True, text=True, check=True).stdout.strip()
    try:
        subprocess.run(["docker", "cp", f"{cid}:/testbed", str(tmp / "testbed")], check=True, capture_output=True,
                       timeout=1800)
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    return DirTree(tmp / "testbed", tmp, image)


# ------------------------------------------------------------------ patches

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(patch: str) -> list[dict]:
    files, cur, hunk, rem_old, rem_new = [], None, None, 0, 0
    for line in patch.split("\n"):
        if hunk is not None and (rem_old > 0 or rem_new > 0):
            tag = line[:1]
            if tag == " " or line == "":
                hunk["lines"].append((" ", line[1:]))
                rem_old, rem_new = rem_old - 1, rem_new - 1
                continue
            if tag == "-":
                hunk["lines"].append(("-", line[1:]))
                rem_old -= 1
                continue
            if tag == "+":
                hunk["lines"].append(("+", line[1:]))
                rem_new -= 1
                continue
            if tag == "\\":
                continue
            hunk = None  # malformed count: fall through to header parsing
        if line.startswith("\\"):
            continue
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = {"old": m.group(1) if m else None, "new": m.group(2) if m else None, "is_new": False,
                   "is_deleted": False, "binary": False, "hunks": []}
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue
        m = HUNK.match(line)
        if m:
            ol = int(m.group(2)) if m.group(2) is not None else 1
            nl = int(m.group(4)) if m.group(4) is not None else 1
            hunk = {"old_start": int(m.group(1)), "old_len": ol, "new_start": int(m.group(3)), "new_len": nl,
                    "lines": []}
            rem_old, rem_new = ol, nl
            cur["hunks"].append(hunk)
        elif line.startswith("--- "):
            cur["is_new"] = cur["is_new"] or line[4:].strip() == "/dev/null"
            if line[4:].strip() != "/dev/null":
                cur["old"] = line[4:].strip().removeprefix("a/")
        elif line.startswith("+++ "):
            cur["is_deleted"] = cur["is_deleted"] or line[4:].strip() == "/dev/null"
            if line[4:].strip() != "/dev/null":
                cur["new"] = line[4:].strip().removeprefix("b/")
        elif line.startswith(("GIT binary patch", "Binary files")):
            cur["binary"] = True
        elif line.startswith("new file mode"):
            cur["is_new"] = True
        elif line.startswith("deleted file mode"):
            cur["is_deleted"] = True
    for f in files:
        f["path"] = f["new"] if not f["is_deleted"] else f["old"]
        if f["is_new"]:
            f["old"] = None
    return files


def split_lines(text: str | None) -> list[str]:
    if not text:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def apply_file_patch(base_text: str | None, fp: dict) -> dict:
    """Apply one file's hunks. Returns post lines, removed old line numbers, insertion anchors (old line after which
    lines are inserted -> inserted lines) and added new line numbers (1-based)."""
    base = split_lines(base_text)
    out, removed, added, inserts = [], set(), set(), defaultdict(list)
    pos, ok = 0, True
    for h in fp["hunks"]:
        old_block = [t for tag, t in h["lines"] if tag in " -"]
        start = h["old_start"] - 1 if h["old_len"] > 0 else h["old_start"]
        if base[start:start + len(old_block)] != old_block:
            found = None
            for delta in range(1, 400):
                for s in (start - delta, start + delta):
                    if s >= pos and base[s:s + len(old_block)] == old_block:
                        found = s
                        break
                if found is not None:
                    break
            if found is None:
                ok = False
                continue
            start = found
        out.extend(base[pos:start])
        oldno = start + 1
        for tag, t in h["lines"]:
            if tag == " ":
                out.append(t)
                oldno += 1
            elif tag == "-":
                removed.add(oldno)
                oldno += 1
            else:
                out.append(t)
                added.add(len(out))
                inserts[oldno - 1].append(t)
        pos = start + len(old_block)
    out.extend(base[pos:])
    return {"post": out, "removed": removed, "added": added, "inserts": dict(inserts), "ok": ok, "base": base}


# ------------------------------------------------------------------ python helpers

def parse_py(text: str | None):
    if text is None:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return None


def node_start(n) -> int:
    decs = getattr(n, "decorator_list", None) or []
    return min([n.lineno] + [d.lineno for d in decs])


def def_index(tree) -> list[dict]:
    """Every FunctionDef / ClassDef with qualname, start (decorators included), end, depth, parent kinds."""
    out = []

    def walk(node, prefix, parents):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                kind = "class" if isinstance(ch, ast.ClassDef) else "func"
                q = prefix + ch.name
                out.append({"node": ch, "name": ch.name, "qual": q, "kind": kind, "start": node_start(ch),
                            "end": ch.end_lineno, "parents": parents, "col": ch.col_offset})
                walk(ch, q + ".", parents + [kind])
            else:
                walk(ch, prefix, parents)

    walk(tree, "", [])
    return out


def unit_of(defs, line):
    """Top-level function or method (first function in the parent chain) containing line; else innermost class."""
    best_func, best_cls = None, None
    for d in defs:
        if d["start"] <= line <= d["end"]:
            if d["kind"] == "func" and "func" not in d["parents"]:
                best_func = d
            elif d["kind"] == "class" and "func" not in d["parents"]:
                if best_cls is None or d["start"] >= best_cls["start"]:
                    best_cls = d
    return best_func, best_cls


def signature(d) -> str:
    n = d["node"]
    if d["kind"] == "class":
        bases = ", ".join(ast.unparse(b) for b in n.bases + [k for k in n.keywords])
        return f"class {d['qual']}({bases})" if bases else f"class {d['qual']}"
    args = ast.unparse(n.args)
    ret = f" -> {ast.unparse(n.returns)}" if n.returns is not None else ""
    pre = "async def" if isinstance(n, ast.AsyncFunctionDef) else "def"
    return f"{pre} {d['qual']}({args}){ret}"


def clip(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def string_expr_ranges(tree) -> list[tuple[int, int]]:
    """Line ranges of bare string statements (docstrings)."""
    out = []
    if tree is None:
        return out
    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            out.append((n.lineno, n.end_lineno))
    return out


PY_STRING = re.compile(r"""(?:\b[rRbBuUfF]{1,2})?("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')""")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER = re.compile(r"(?<![\w.])(?:0[xXoObB][0-9a-fA-F_]+|\d[\d_]*(?:\.\d*)?(?:[eE][-+]?\d+)?j?|\.\d+(?:[eE][-+]?\d+)?)")


def literal_value(src: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            v = ast.literal_eval(src)
        if isinstance(v, bytes):
            return v.decode("latin-1")
        if isinstance(v, str):
            return v
    except Exception:
        pass
    m = re.match(r"^[rRbBuUfF]*('''|\"\"\"|'|\")(.*)\1$", src, re.S)
    return m.group(2) if m else src


def regex_tokens(line: str):
    """Fallback tokenizer for one line (Cython, mypy test programs, untokenizable files)."""
    code = line
    out = []
    for m in PY_STRING.finditer(line):
        out.append(("string", literal_value(m.group(0))))
    code = PY_STRING.sub(" ", line)
    code = code.split("#", 1)[0]
    for m in IDENT.finditer(code):
        out.append(("ident", m.group(0)))
    for m in NUMBER.finditer(code):
        out.append(("number", m.group(0)))
    return out


class PyTokens:
    """tokenize once per file; tokens by line with docstrings and comments dropped."""

    def __init__(self, text: str, tree=None):
        self.lines = split_lines(text)
        self.ok = True
        self.by_line: dict[int, list[tuple[str, str]]] = defaultdict(list)
        doc = string_expr_ranges(tree)
        in_doc = lambda a, b: any(s <= a and b <= e for s, e in doc)  # noqa: E731
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, SyntaxError, IndentationError):
            self.ok = False
            for i, l in enumerate(self.lines, 1):
                self.by_line[i].extend(regex_tokens(l))
            return
        fstart = None
        for t in toks:
            typ = t.type
            if typ == getattr(tokenize, "FSTRING_START", -1):
                if fstart is None:
                    fstart = t.start
                continue
            if typ == getattr(tokenize, "FSTRING_END", -2):
                if fstart is not None:
                    raw = self.span(fstart, t.end)
                    if not in_doc(fstart[0], t.end[0]):
                        self.by_line[fstart[0]].append(("string", literal_value_f(raw)))
                    fstart = None
                continue
            if typ == tokenize.NAME:
                self.by_line[t.start[0]].append(("ident", t.string))
            elif typ == tokenize.STRING:
                if not in_doc(t.start[0], t.end[0]):
                    self.by_line[t.start[0]].append(("string", literal_value(t.string)))
            elif typ == tokenize.NUMBER:
                self.by_line[t.start[0]].append(("number", t.string))

    def span(self, a, b):
        (l1, c1), (l2, c2) = a, b
        if l1 == l2:
            return self.lines[l1 - 1][c1:c2]
        parts = [self.lines[l1 - 1][c1:]] + self.lines[l1:l2 - 1] + [self.lines[l2 - 1][:c2]]
        return "\n".join(parts)

    def tokens(self, lines):
        out = []
        for ln in sorted(lines):
            out.extend(self.by_line.get(ln, []))
        return out


def literal_value_f(raw: str) -> str:
    m = re.match(r"^[rRbBuUfF]*('''|\"\"\"|'|\")(.*)\1$", raw, re.S)
    return m.group(2) if m else raw


# ------------------------------------------------------------------ corpus search

class Corpus:
    def __init__(self, tree, edited: set[str], test_files: set[str] = frozenset()):
        paths = sorted(p for p, (_, size) in tree.files.items() if p.endswith(SEARCH_EXT) and size <= MAX_FILE)
        texts = tree.read_many(paths)
        self.paths, parts, self.starts = [], [], []
        off = 0
        for p in paths:
            t = texts.get(p)
            if t is None:
                continue
            self.paths.append(p)
            self.starts.append(off)
            parts.append(t)
            off += len(t) + 1
        self.big = "\x00".join(parts)
        self.ends = [s + len(t) for s, t in zip(self.starts, parts)]
        self.cats = [category(p) for p in self.paths]
        self.edited, self.test_files = edited, set(test_files)
        self._nl: dict[int, list[int]] = {}

    def file_of(self, off: int) -> int:
        return bisect.bisect_right(self.starts, off) - 1

    def text(self, fi: int) -> str:
        return self.big[self.starts[fi]:self.ends[fi]]

    def lineno(self, fi: int, off: int) -> int:
        if fi not in self._nl:
            t = self.text(fi)
            self._nl[fi] = [i for i, c in enumerate(t) if c == "\n"] if len(t) < 50000 else \
                [m.start() for m in re.finditer("\n", t)]
        return bisect.bisect_left(self._nl[fi], off - self.starts[fi]) + 1

    def line_text(self, off: int) -> str:
        a = self.big.rfind("\n", 0, off) + 1
        a = max(a, self.big.rfind("\x00", 0, off) + 1)
        b = self.big.find("\n", off)
        c = self.big.find("\x00", off)
        b = min(x for x in (b, c, len(self.big)) if x >= 0)
        return self.big[a:b]

    def window(self, fi: int, line: int, radius: int = 1) -> list[tuple[int, str]]:
        lines = self.text(fi).split("\n")
        lo, hi = max(1, line - radius), min(len(lines), line + radius)
        return [(i, lines[i - 1]) for i in range(lo, hi + 1)]

    def find(self, needle: str, word: bool, cap: int = 20000) -> list[int]:
        out, i, big = [], 0, self.big
        if not needle:
            return out
        n = len(needle)
        while len(out) < cap:
            i = big.find(needle, i)
            if i < 0:
                break
            if word:
                a = big[i - 1] if i > 0 else " "
                b = big[i + n] if i + n < len(big) else " "
                if (a.isalnum() or a == "_" or (a == "." and needle[0].isdigit())) or (b.isalnum() or b == "_"):
                    i += 1
                    continue
            out.append(i)
            i += n
        return out


DEF_LINE = "^[ \\t]*(?:(?:async[ \\t]+)?def|class|cdef|cpdef)[ \\t]+(?:[\\w.]+[ \\t]+)*{n}\\b|^[ \\t]*(?:self\\.)?{n}[ \\t]*(?::[^\\n=]*)?=[^=]"


def rank_hits(corpus: Corpus, offs: list[int], name: str | None):
    """Return (counts by category, files count, best locations [(fi, line, is_def)], hits inside test-patch files)."""
    cats, files = Counter(), set()
    defre = re.compile(DEF_LINE.format(n=re.escape(name))) if name else None
    n_patch = 0
    for off in offs:
        fi = corpus.file_of(off)
        files.add(fi)
        cats[corpus.cats[fi]] += 1
        n_patch += corpus.paths[fi] in corpus.test_files
    # rank: gold-edited file first, outside the test-patch files, category, definition first, path, offset
    per = []
    seen_files = set()
    for off in offs:
        fi = corpus.file_of(off)
        is_def = bool(defre and defre.search(corpus.line_text(off)))
        per.append(((0 if corpus.paths[fi] in corpus.edited else 1), int(corpus.paths[fi] in corpus.test_files),
                    CAT_RANK.get(corpus.cats[fi], 3), 0 if is_def else 1, corpus.paths[fi], off, fi, is_def))
    per.sort()
    per = [(a, c, d, e, f, g, h, b) for a, b, c, d, e, f, g, h in per]  # -> (edited, cat, def, path, off, fi, is_def, inpatch)
    best = []
    defs = [p for p in per if p[6]]
    if defs:
        d = min(defs, key=lambda p: (p[7], p[1], p[0], p[3], p[4]))  # src definition outside the test-patch files first
        best.append(d)
    for p in per:
        if len(best) >= 2:
            break
        if best and p[5] == best[0][5] and abs(p[4] - best[0][4]) < 200:
            continue
        if p[5] in seen_files and len(best) >= 1 and p[5] == best[-1][5]:
            continue
        best.append(p)
        seen_files.add(p[5])
    locs = [(p[5], corpus.lineno(p[5], p[4]), p[6]) for p in best[:2]]
    return cats, len(files), locs, n_patch


FRAG_SPLIT = re.compile(r"\{[^}]*\}|%[-#0-9.]*[a-zA-Z]|\\.|\"[^\"]*\"|'[^']*'|[\^$|*+?\[\]()]|\n")


def fragments(value: str) -> list[str]:
    fr = sorted({f.strip() for f in FRAG_SPLIT.split(value) if len(f.strip()) >= 10}, key=lambda s: (-len(s), s))
    return fr[:4]


# ------------------------------------------------------------------ F2P tests

def parse_test_id(tid: str):
    path, _, rest = tid.partition("::")
    i = rest.find("[")
    if i >= 0:
        rest = rest[:i]
    parts = [p for p in rest.split("::") if p]
    return path, parts


def find_py_test(tree, parts):
    """(def entry, note) for class chain + function parts in a parsed test file."""
    if tree is None or not parts:
        return None, "unparseable test file" if tree is None else "no test name"
    defs = def_index(tree)
    func = parts[-1]
    qual = ".".join(parts)
    for d in defs:
        if d["qual"] == qual and d["kind"] == "func":
            return d, ""
    # parameterized.expand suffix (test_x_0, test_x_1_name): longest def name that prefixes the id
    prefix = ".".join(parts[:-1])
    cands = [d for d in defs if d["kind"] == "func" and (not prefix or d["qual"].rsplit(".", 1)[0] == prefix)
             and func.startswith(d["name"] + "_") and re.match(r"^_\d+", func[len(d["name"]):])]
    if cands:
        return max(cands, key=lambda d: len(d["name"])), "parameterized suffix stripped"
    same = [d for d in defs if d["kind"] == "func" and d["name"] == func]
    if same:
        return same[0], f"found as {same[0]['qual']} (class in id: {prefix or '-'}; inherited or generated)"
    return None, "test function not found"


CASE_HDR = re.compile(r"^\[case ([^\]\s]+)\]", re.M)


def find_case(text: str, name: str):
    """(start line, end line, header) of a mypy data-driven case."""
    lines = split_lines(text)
    starts = [(i + 1, m.group(1)) for i, l in enumerate(lines) for m in [CASE_HDR.match(l)] if m]
    for k, (ln, nm) in enumerate(starts):
        base = re.sub(r"-(xfail|skip|writescache|only_when_cache|only_when_nocache)$", "", nm)
        if nm == name or base == name or re.sub(r"_cached$", "", name) == base:
            end = (starts[k + 1][0] - 1) if k + 1 < len(starts) else len(lines)
            return ln, end, nm
    return None


MYPY_MSG = re.compile(r"#\s*([ENW])(?::\d+)?:\s*(.*)$")
OUT_MSG = re.compile(r"^[\w./-]+:\d+(?::\d+)?: (?:error|note|warning): (.*?)(?:\s+\[[\w-]+\])?$")


def mypy_case_tokens(lines: list[str], start: int, end: int):
    """[(line, role, kind, value)] for a [case] block; # E:/N:/W: messages and [out] lines are assertions."""
    out, sect = [], "case"
    for ln in range(start, end + 1):
        l = lines[ln - 1]
        if l.startswith("["):
            sect = l.strip("[] ").split()[0] if l.strip("[] ") else sect
            continue
        if sect in ("out", "out1", "out2", "out3") or sect.startswith("out"):
            m = OUT_MSG.match(re.sub(r"\s*\\$", "", l.strip()))
            if m:
                out.append((ln, "F2P-assert", "string", m.group(1)))
            elif l.strip():
                out.append((ln, "F2P-assert", "string", l.strip()))
            continue
        if sect.startswith(("builtins", "typing", "fixture")):
            continue
        m = MYPY_MSG.search(l)
        code = l
        if m:
            for msg in re.split(r"\s+\\\s*#\s*[ENW]:\s*", m.group(2)):
                msg = re.sub(r"\s*\\$", "", msg.strip()).strip()
                msg = re.sub(r"\s+\[[\w-]+\]$", "", msg)
                if msg:
                    out.append((ln, "F2P-assert", "string", msg))
            code = l[:m.start()]
        fl = re.match(r"^#\s*flags:\s*(.*)$", l)
        if fl:
            for f in fl.group(1).split():
                out.append((ln, "F2P-param", "string", f))
            continue
        for kind, v in regex_tokens(code):
            out.append((ln, "F2P-body", kind, v))
    return out


def assert_lines(fn_node) -> set[int]:
    """Lines of assertion statements in a test function: assert, assert*/raises/warns calls (and their with-blocks),
    sure-style `.should`, and assignments to expected*/exp*."""
    lines = set()
    for st in ast.walk(fn_node):
        if not isinstance(st, ast.stmt) or st is fn_node:
            continue
        hit = isinstance(st, ast.Assert)
        if not hit and isinstance(st, (ast.With, ast.AsyncWith)):
            for it in st.items:
                c = it.context_expr
                nm = c.func.attr if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) else \
                    c.func.id if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) else ""
                if ASSERT_CALL.match(nm) or nm in ("assertRaises", "assertRaisesRegex", "assertWarns"):
                    hit = True
        if not hit and isinstance(st, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for sub in ast.walk(st):
                if isinstance(sub, ast.Call):
                    nm = sub.func.attr if isinstance(sub.func, ast.Attribute) else \
                        sub.func.id if isinstance(sub.func, ast.Name) else ""
                    if ASSERT_CALL.match(nm) or nm.startswith("assert"):
                        hit = True
                        break
                if isinstance(sub, ast.Attribute) and sub.attr in ("should", "should_not"):
                    hit = True
                    break
            if not hit and isinstance(st, (ast.Assign, ast.AnnAssign)):
                tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                if any(isinstance(t, ast.Name) and re.match(r"^(exp|expected)(_\w+)?$", t.id) for t in tg):
                    hit = True
        if hit:
            if isinstance(st, (ast.With, ast.AsyncWith)):
                lines.update(range(st.lineno, st.end_lineno + 1))
            else:
                lines.update(range(st.lineno, st.end_lineno + 1))
    return lines


def local_names(node, own_args: bool) -> set[str]:
    """Names bound only locally inside a function (variables, loop / comprehension targets, nested defs, own args if
    own_args) and never used as an attribute, keyword or import there: not interface, so not listed in section 1."""
    bound, iface = set(), set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.Attribute):
            iface.add(n.attr)
        elif isinstance(n, ast.keyword) and n.arg:
            iface.add(n.arg)
        elif isinstance(n, ast.alias):
            iface.update({n.name.split(".")[0], n.name.split(".")[-1]})
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not node:
            bound.add(n.name)
    if not own_args and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = node.args
        bound -= {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if x is not None}
    return bound - iface


# ------------------------------------------------------------------ pack sections

def code_block(rows: list[tuple[int, str]], marks: dict[int, str] | None = None) -> list[str]:
    marks = marks or {}
    out = ["```"]
    for ln, t in rows:
        out.append(f"{marks.get(ln, ' ')}{ln:>5}| {t}" if ln else f"       {t}")
    out.append("```")
    return out


def cap_body(lines: list[str], start: int, end: int, focus: set[int], cap: int) -> list[tuple[int, str]]:
    """Body lines start..end; if longer than cap keep the header, windows around focus lines and the tail."""
    n = end - start + 1
    if n <= cap:
        return [(i, lines[i - 1]) for i in range(start, end + 1)]
    keep = set(range(start, min(end, start + 12) + 1)) | set(range(max(start, end - 4), end + 1))
    rad = 12
    for f in sorted(focus):
        keep |= set(range(max(start, f - rad), min(end, f + rad) + 1))
    while len(keep) > cap and rad > 3:
        rad -= 3
        keep = set(range(start, min(end, start + 12) + 1)) | set(range(max(start, end - 4), end + 1))
        for f in sorted(focus):
            keep |= set(range(max(start, f - rad), min(end, f + rad) + 1))
    rows, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            rows.append((0, f"... [base lines {prev + 1}-{i - 1} elided] ..."))
        rows.append((i, lines[i - 1]))
        prev = i
    return rows


def cost(lines: list[str]) -> int:
    """o200k tokens of a block of lines plus its trailing newline (its share of the joined section text)."""
    return ntokens("\n".join(lines)) + 1 if lines else 0


class Budget:
    """Token budget of one section. add() refuses a block that does not fit, keeping `reserve` tokens for the omission
    notices; force() always appends (section headers and omission notices)."""

    def __init__(self, n, reserve=0):
        self.cap, self.left, self.out, self.truncated = n, n, [], False
        self.reserve = reserve if n < UNLIMITED else 0

    def fits(self, c: int) -> bool:
        return c <= self.left - self.reserve

    def add(self, lines: list[str]) -> bool:
        c = cost(lines)
        if not self.fits(c):
            self.truncated = True
            return False
        self.out.extend(lines)
        self.left -= c
        return True

    def force(self, lines):
        self.out.extend(lines)
        self.left -= cost(lines)

    def absorb(self, sub: "Budget"):
        """Append a sub-budget's lines, charged at the sub-budget's own accounting."""
        self.out.extend(sub.out)
        self.left -= sub.used

    @property
    def used(self) -> int:
        return self.cap - self.left


def indent_of(s: str) -> int:
    return len(s) - len(s.lstrip(" \t"))


DEF_RE = re.compile(r"^([ \t]*)(?:async[ \t]+)?(?:def|cdef|cpdef)[ \t]+(?:[\w.\[\]*]+[ \t]+)*?(\w+)[ \t]*\(")


def regex_unit(lines: list[str], line: int):
    """Enclosing def by indentation (non-Python / unparseable files): (name, start, end) or None."""
    for i in range(min(line, len(lines)), 0, -1):
        m = DEF_RE.match(lines[i - 1])
        if m and (i == line or indent_of(lines[i - 1]) < max(indent_of(lines[line - 1]) if lines[line - 1].strip()
                                                                else 99, 1)):
            ind = len(m.group(1).expandtabs())
            end = i
            for j in range(i + 1, len(lines) + 1):
                l = lines[j - 1]
                if l.strip() and len(l) - len(l.lstrip()) <= ind and not l.lstrip().startswith((")", "#")):
                    break
                end = j
            if end >= line:
                return m.group(2), i, end
    return None


def edited_units(base_text, ap, path):
    """Units of a base file the patch edits: functions (top-level / methods), class bodies, module level.
    Returns tree, info {key: span or None}, focus {key: lines}, marks, rem {key: removed lines}, ins {key: anchors}."""
    lines = ap["base"]
    tree = parse_py(base_text) if path.endswith((".py", ".pyi")) else None
    defs = def_index(tree) if tree is not None else None
    focus, rem, ins = defaultdict(set), defaultdict(set), defaultdict(set)
    info = {}
    anchors = [(r, "*") for r in sorted(ap["removed"])] + [(k, ">") for k in sorted(ap["inserts"])]
    marks = {}
    for ln, mk in anchors:
        marks.setdefault(ln, mk)
        if defs is not None:
            f, c = unit_of(defs, ln) if ln > 0 else (None, None)
            if mk == ">":
                new = [t for t in ap["inserts"][ln] if t.strip()]
                ind = indent_of(new[0]) if new else None
                if f is not None and ln == f["end"] and (ind is None or ind <= f["col"]):
                    f = None
                if c is not None and ln == c["end"] and (ind is None or ind <= c["col"]):
                    c = None
                if f is None and c is None and ind and ln > 0:
                    # inserted after blank lines that follow a body: attribute by indentation
                    j = ln
                    while j >= 1 and not lines[j - 1].strip():
                        j -= 1
                    f2, c2 = unit_of(defs, j) if j >= 1 else (None, None)
                    if f2 is not None and ind > f2["col"]:
                        f = f2
                    elif c2 is not None and ind > c2["col"]:
                        c = c2
            if f is not None:
                key = ("func", f["qual"])
                info[key] = (f["start"], f["end"], f["name"], f)
            elif c is not None:
                key = ("class", c["qual"])
                info[key] = (c["start"], c["end"], c["name"], c)
            else:
                key = ("module", path)
                info[key] = None
        else:
            u = regex_unit(lines, ln) if ln > 0 else None
            if u:
                key = ("func", u[0])
                info[key] = (u[1], u[2], u[0], None)
            else:
                key = ("module", path)
                info[key] = None
        focus[key].add(max(ln, 1))
        (rem if mk == "*" else ins)[key].add(ln)
    return tree, info, focus, marks, rem, ins


def insert_summary(ap, anchors) -> str:
    out = []
    for k in sorted(anchors):
        new = [t.strip() for t in ap["inserts"].get(k, []) if t.strip()]
        n = len(ap["inserts"].get(k, []))
        out.append(f"after base L{k} (+{n}: {clip(new[0], 70) if new else 'blank'})")
    return "; ".join(out)


def public_sigs(tree, edited_quals: set[str]):
    defs = def_index(tree)
    rows = []
    for d in defs:
        if "func" in d["parents"]:
            continue
        pub = not d["name"].startswith("_") or d["name"] in ("__init__", "__call__", "__new__")
        if d["kind"] == "class" and len(d["parents"]) > 0 and d["name"].startswith("_"):
            continue
        if not pub and d["qual"] not in edited_quals:
            continue
        owner = d["qual"].rsplit(".", 1)[0] if "." in d["qual"] else ""
        prio = 0 if (d["qual"] in edited_quals or owner and (owner in edited_quals or any(
            q.startswith(owner + ".") for q in edited_quals))) else 1
        rows.append((prio, d["start"], d))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def raises_in(tree, text_lines, repo, edited_ranges):
    out = []
    for n in ast.walk(tree):
        msg = None
        if isinstance(n, ast.Raise) and n.exc is not None:
            msg = "raise " + ast.unparse(n.exc)
        elif isinstance(n, ast.Call):
            nm = n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id if isinstance(n.func, ast.Name) else ""
            if nm in ("warn", "warn_explicit"):
                msg = ast.unparse(n)
            elif repo == "python/mypy" and nm in ("fail", "note") and n.args:
                msg = ast.unparse(n)
        if msg:
            inside = any(a <= n.lineno <= b for a, b in edited_ranges)
            out.append((0 if inside else 1, n.lineno, msg, inside))
    out.sort(key=lambda r: (r[0], r[1]))
    return out


def enclosing_name(lines: list[str], line: int) -> str:
    ind = indent_of(lines[line - 1]) if 0 < line <= len(lines) else 0
    for i in range(line - 1, 0, -1):
        m = DEF_RE.match(lines[i - 1]) or re.match(r"^([ \t]*)class[ \t]+(\w+)", lines[i - 1])
        if m and len(m.group(1)) < ind:
            return m.group(2)
    return "<module>"


REQ_FILE = re.compile(r"(?:-r\s+|--requirement\s+)([\w./-]+\.txt)|\b([\w./-]*requirements[\w./-]*\.txt)\b")
PIN = re.compile(r"(?<![\w.-])([A-Za-z][A-Za-z0-9_.\-\[\]]*)\s*(==|>=|<=|~=|!=|<|>)\s*([0-9][A-Za-z0-9_.*]*)")


# ------------------------------------------------------------------ v2 helpers (sections 7-9)

def name_tokens(name: str) -> set[str]:
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower().split("_")
    return {p for p in parts if len(p) > 1}


def code_idents(lines: list[str], lo: int, hi: int, skip: set[int] = frozenset()) -> list[str]:
    """Identifiers of base lines lo..hi (1-based), strings and comments dropped, lines in skip ignored."""
    out = []
    for ln in range(max(1, lo), min(len(lines), hi) + 1):
        if ln in skip:
            continue
        out.extend(v for k, v in regex_tokens(lines[ln - 1]) if k == "ident")
    return out


def doc_lines(tree) -> set[int]:
    return {ln for a, b in string_expr_ranges(tree) for ln in range(a, b + 1)}


class SrcIndex:
    """Identifier document frequency over the base tree's source .py/.pyx files (test and doc paths excluded)."""

    def __init__(self, corpus: "Corpus"):
        self.corpus = corpus
        self.files = [i for i, p in enumerate(corpus.paths) if corpus.cats[i] == "src" and p.endswith((".py", ".pyx"))]
        self.sets: dict[int, set[str]] = {}
        df = Counter()
        for fi in self.files:
            s = set(IDENT.findall(corpus.text(fi)))
            self.sets[fi] = s
            df.update(s)
        self.df, self.n = df, max(1, len(self.files))
        self._parsed: dict[int, tuple] = {}

    def idf(self, name: str) -> float:
        return math.log((self.n + 1) / (self.df.get(name, 0) + 1)) + 0.1

    def informative(self, name: str) -> bool:
        return name not in SKIP_NAMES and len(name) > 2 and self.df.get(name, 0) <= 0.3 * self.n

    def parsed(self, fi: int):
        if fi not in self._parsed:
            text = self.corpus.text(fi)
            t = parse_py(text) if self.corpus.paths[fi].endswith(".py") else None
            self._parsed[fi] = (t, split_lines(text), def_index(t) if t is not None else [],
                                doc_lines(t) if t is not None else set())
        return self._parsed[fi]


def excerpt_rows(lines: list[str], s: int, e: int, focus: set[int], cap: int, head: int = 2) -> list[tuple[int, str]]:
    """Header lines plus +-1 windows around focus lines, at most cap lines (whole span if it fits)."""
    if e - s + 1 <= cap:
        return [(i, lines[i - 1]) for i in range(s, e + 1)]
    keep = set(range(s, min(e, s + head - 1) + 1))
    for f in sorted(focus):
        if len(keep) >= cap:
            break
        keep |= {i for i in range(f - 1, f + 2) if s <= i <= e}
    keep = set(sorted(keep)[:cap])
    keep |= {i + 1 for i in keep if i + 2 in keep and i + 1 not in keep}
    rows, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            rows.append((0, f"... [lines {prev + 1}-{i - 1}] ..."))
        rows.append((i, lines[i - 1]))
        prev = i
    if prev is not None and prev < e:
        rows.append((0, f"... [lines {prev + 1}-{e}] ..."))
    return rows


def def_head_rows(lines: list[str], node, doc_cap: int = 6, code_cap: int = 8) -> list[tuple[int, str]]:
    """Decorators + def/class header, the first doc_cap docstring lines, the first code_cap code lines."""
    s, e = node_start(node), node.end_lineno
    body = getattr(node, "body", [])
    first = body[0].lineno if body else e
    keep = list(range(s, max(node.lineno, first - 1) + 1))
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        d0, d1 = body[0].lineno, body[0].end_lineno
        keep += list(range(d0, min(d1, d0 + doc_cap - 1) + 1))
        if d1 > d0 + doc_cap - 1:
            keep.append(-d1)  # marker: docstring elided up to d1
        rest = body[1:]
    else:
        rest = body
    if rest and code_cap:
        c0 = rest[0].lineno
        keep += list(range(c0, min(e, c0 + code_cap - 1) + 1))
    rows, prev = [], None
    for i in keep:
        if i < 0:
            rows.append((0, f"... [docstring continues to line {-i}] ..."))
            prev = -i
            continue
        if prev is not None and i != prev + 1 and i > prev:
            rows.append((0, f"... [lines {prev + 1}-{i - 1}] ..."))
        if prev is not None and i <= prev:
            continue
        rows.append((i, lines[i - 1]))
        prev = i
    if prev is not None and prev < e:
        rows.append((0, f"... [lines {prev + 1}-{e}] ..."))
    return rows


def short_member(lines: list[str], m) -> str:
    """One-line view of a class member: signature, plus the body when it is one short statement."""
    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
        decs = [ast.unparse(x) for x in m.decorator_list]
        pre = "".join(f"@{clip(x, 40)} " for x in decs)
        ret = f" -> {ast.unparse(m.returns)}" if m.returns is not None else ""
        sig = f"{pre}{'async def' if isinstance(m, ast.AsyncFunctionDef) else 'def'} {m.name}({ast.unparse(m.args)}){ret}"
        body = [b for b in m.body if not (isinstance(b, ast.Expr) and isinstance(getattr(b, "value", None), ast.Constant)
                                           and isinstance(b.value.value, str))]
        if len(body) == 1:
            one = ast.unparse(body[0])
            if "\n" not in one and len(one) <= 110:
                sig += f": {one}"
        return f"L{m.lineno} {clip(sig, 260)}"
    if isinstance(m, ast.ClassDef):
        return f"L{m.lineno} class {m.name}"
    return f"L{m.lineno} {clip(ast.unparse(m), 160)}"


def class_member_rows(lines: list[str], node, cap: int = 25) -> list[str]:
    out = []
    members = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign,
                                                       ast.AnnAssign))]
    for m in members[:cap]:
        out.append("    " + short_member(lines, m))
    if len(members) > cap:
        out.append(f"    ... {len(members) - cap} more members")
    return out


def module_map(tree) -> dict[str, str]:
    """dotted module name -> base-tree path (.py preferred over .pyi; a leading src/ is also stripped)."""
    idx = {}
    for p in sorted((p for p in tree.files if p.endswith((".py", ".pyi"))),
                    key=lambda p: (p.endswith(".pyi"), len(p), p)):
        parts = p[: -4 if p.endswith(".pyi") else -3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        names = [".".join(parts)]
        if parts[0] in ("src", "lib") and len(parts) > 1:
            names.append(".".join(parts[1:]))
        for n in names:
            idx.setdefault(n, p)
    return idx


def path_module(path: str) -> tuple[list[str], bool]:
    parts = path[: -4 if path.endswith(".pyi") else -3].split("/")
    if parts and parts[0] in ("src", "lib"):
        parts = parts[1:]
    is_pkg = bool(parts) and parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    return parts, is_pkg


def abs_module(module: str | None, level: int, path: str) -> str:
    if not level:
        return module or ""
    parts, is_pkg = path_module(path)
    pkg = parts if is_pkg else parts[:-1]
    base = pkg[:len(pkg) - (level - 1)] if level > 1 else pkg
    return ".".join(base + (module.split(".") if module else []))


def top_stmts(body):
    for st in body:
        if isinstance(st, ast.If):
            yield from top_stmts(st.body)
            yield from top_stmts(st.orelse)
        elif isinstance(st, ast.Try) or type(st).__name__ == "TryStar":
            yield from top_stmts(st.body)
            for h in st.handlers:
                yield from top_stmts(h.body)
            yield from top_stmts(st.orelse)
            yield from top_stmts(st.finalbody)
        else:
            yield st


class Resolver:
    """Resolve (module, attr) to its base definition, following from-imports / star re-exports (<= 5 hops)."""

    def __init__(self, tree):
        self.tree, self.mods, self._p = tree, module_map(tree), {}

    def parsed(self, path):
        if path not in self._p:
            text = self.tree.read(path)
            t = parse_py(text)
            self._p[path] = (t, split_lines(text))
        return self._p[path]

    def resolve(self, mod: str, attr: str, hops: int = 0, seen=None):
        seen = set() if seen is None else seen
        if hops > 5 or (mod, attr) in seen:
            return None
        seen.add((mod, attr))
        p = self.mods.get(mod)
        if p is None:
            return None
        t, lines = self.parsed(p)
        if t is None:
            return None
        stmts = list(top_stmts(t.body))
        assign = None
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and st.name == attr:
                return ("def", p, st, lines)
            if assign is None and isinstance(st, (ast.Assign, ast.AnnAssign)):
                tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                if any(isinstance(x, ast.Name) and x.id == attr for x in tg):
                    assign = ("assign", p, st, lines)
        for st in stmts:
            if isinstance(st, ast.ImportFrom):
                base = abs_module(st.module, st.level, p)
                for a in st.names:
                    if a.name != "*" and (a.asname or a.name) == attr:
                        r = self.resolve(base, a.name, hops + 1, seen)
                        if r:
                            return r
                        if f"{base}.{a.name}" in self.mods:
                            return ("module", self.mods[f"{base}.{a.name}"], None, None)
            elif isinstance(st, ast.Import):
                for a in st.names:
                    if a.asname == attr and a.name in self.mods:
                        return ("module", self.mods[a.name], None, None)
        if assign:
            return assign
        for st in stmts:
            if isinstance(st, ast.ImportFrom) and any(a.name == "*" for a in st.names):
                r = self.resolve(abs_module(st.module, st.level, p), attr, hops + 1, seen)
                if r:
                    return r
        if f"{mod}.{attr}" in self.mods:
            return ("module", self.mods[f"{mod}.{attr}"], None, None)
        return None


def attr_chain(n) -> list[str] | None:
    out = []
    while isinstance(n, ast.Attribute):
        out.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        out.append(n.id)
        return out[::-1]
    return None


def test_symbol_uses(ttree, nodes, path: str, mods: dict[str, str]) -> dict[tuple[str, str], list[int]]:
    """(module, attr) -> lines where the test nodes use a repository symbol imported in the test module."""
    imports = {}
    for st in ast.walk(ttree):
        if isinstance(st, ast.ImportFrom):
            base = abs_module(st.module, st.level, path)
            for a in st.names:
                if a.name != "*":
                    imports[a.asname or a.name] = (base, a.name)
        elif isinstance(st, ast.Import):
            for a in st.names:
                if a.asname:
                    imports[a.asname] = (a.name, None)
                else:
                    imports.setdefault(a.name.split(".")[0], (a.name.split(".")[0], None))
    uses: dict[tuple[str, str], list[int]] = defaultdict(list)
    for node in nodes:
        consumed = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Attribute) and id(n) not in consumed:
                ch = attr_chain(n)
                if not ch or ch[0] not in imports:
                    continue
                m, a = imports[ch[0]]
                head = m if a is None else f"{m}.{a}"
                if a is not None and head not in mods:
                    continue
                dotted = [head] + ch[1:]
                # longest module prefix, then the next component is the symbol
                for k in range(len(dotted) - 1, 0, -1):
                    pre = ".".join(dotted[:k]) if k > 1 else dotted[0]
                    if pre in mods:
                        uses[(pre, dotted[k])].append(n.lineno)
                        break
                sub = n.value
                while isinstance(sub, ast.Attribute):
                    consumed.add(id(sub))
                    sub = sub.value
                if isinstance(sub, ast.Name):
                    consumed.add(id(sub))
            elif isinstance(n, ast.Name) and id(n) not in consumed and isinstance(n.ctx, ast.Load) \
                    and n.id in imports and imports[n.id][1] is not None:
                m, a = imports[n.id]
                if f"{m}.{a}" in mods and mods.get(f"{m}.{a}") != mods.get(m):
                    continue  # a module imported by name, used without an attribute
                uses[(m, a)].append(n.lineno)
    return uses


def assert_feed_lines(fn, alines: set[int]) -> set[int]:
    """Lines of statements that assign (transitively) a name an assertion line reads: x = f(); y = x[0]; assert y."""
    aread = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
             and n.lineno in alines}
    feed: set[int] = set()
    for _ in range(4):
        before = len(feed)
        for st in ast.walk(fn):
            if isinstance(st, ast.Assign):
                tg = st.targets
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                tg = [st.target]
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                tg = [it.optional_vars for it in st.items if it.optional_vars is not None]
            else:
                continue
            if tg and st.lineno not in alines and \
                    {n.id for x in tg for n in ast.walk(x) if isinstance(n, ast.Name)} & aread:
                end = st.end_lineno if not isinstance(st, (ast.With, ast.AsyncWith)) else \
                    max(it.context_expr.end_lineno for it in st.items)
                feed |= set(range(st.lineno, end + 1))
        aread |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                  and n.lineno in feed}
        if len(feed) == before:
            break
    return feed


FIXTURE_DEF = "^[ \\t]*(?:async[ \\t]+)?def[ \\t]+{n}[ \\t]*\\("


def find_fixture(corpus: "Corpus", name: str, test_path: str):
    """(file index, line) of a pytest fixture `name` defined in the base tree (nearest conftest first)."""
    offs = corpus.find(f"def {name}", word=True, cap=200)
    best = None
    rx = re.compile(FIXTURE_DEF.format(n=re.escape(name)))
    for off in offs:
        if not rx.match(corpus.line_text(off)):
            continue
        fi = corpus.file_of(off)
        ln = corpus.lineno(fi, off)
        lines = corpus.text(fi).split("\n")
        above = "\n".join(lines[max(0, ln - 5):ln - 1])
        if "fixture" not in above:
            continue
        p = corpus.paths[fi]
        common = len(os.path.commonprefix([os.path.dirname(p) + "/", os.path.dirname(test_path) + "/"]))
        key = (-common, p, ln)
        if best is None or key < best[0]:
            best = (key, fi, ln)
    return (best[1], best[2]) if best else None


# ------------------------------------------------------------------ v4 helpers (exact S5, sections 10-14)

class OverlayTree:
    """The base tree with the test patch applied: F2P tests whose base classes the test patch adds are located here."""

    def __init__(self, tree, post: dict[str, str]):
        self._tree, self._post = tree, post
        self.files = dict(tree.files)
        self.files.update({p: ("", len(t)) for p, t in post.items()})

    def read(self, path):
        return self._post[path] if path in self._post else self._tree.read(path)


def quoted_line(flines: list[str], s: int, e: int, code: str, ln: int):
    """(line, exact?) of a traceback's quoted code line inside s..e: exact text, then whitespace-insensitive, then
    containment, then >= 75% of its identifiers; None if nothing matches."""
    norm = lambda x: re.sub(r"\s+", "", x)  # noqa: E731
    c, rng = norm(code), range(s, e + 1)
    for exact, test in ((True, lambda l: l.strip() == code.strip()), (True, lambda l: norm(l) == c),
                        (False, lambda l: len(c) >= 12 and (c in norm(l) or (len(norm(l)) >= 12 and norm(l) in c)))):
        hits = [i for i in rng if test(flines[i - 1])]
        if hits:
            return min(hits, key=lambda i: abs(i - ln)), exact
    ids = set(IDENT.findall(code)) - SKIP_NAMES
    if len(ids) >= 2:
        best = max(rng, key=lambda i: (len(ids & set(IDENT.findall(flines[i - 1]))), -abs(i - ln)))
        if len(ids & set(IDENT.findall(flines[best - 1]))) >= 0.75 * len(ids):
            return best, False
    return None


def file_imports(t, path: str) -> dict[str, tuple[str, str | None]]:
    """Local name -> (absolute module, imported attribute or None for a module import), over all scopes of a file."""
    out = {}
    if t is None:
        return out
    for st in ast.walk(t):
        if isinstance(st, ast.ImportFrom):
            base = abs_module(st.module, st.level, path)
            for a in st.names:
                if a.name != "*":
                    out[a.asname or a.name] = (base, a.name)
        elif isinstance(st, ast.Import):
            for a in st.names:
                if a.asname:
                    out[a.asname] = (a.name, None)
                else:
                    out.setdefault(a.name.split(".")[0], (a.name.split(".")[0], None))
    return out


def module_name(path: str) -> str:
    return ".".join(path_module(path)[0])


def resolve_chain(rs, mod: str, names: list[str]):
    """(path, node) of the base definition mod.names[0].names[1]... (sub-modules, re-exports, nested classes)."""
    if not names or not mod:
        return None
    r = rs.resolve(mod, names[0])
    if r is None:
        return None
    if r[0] == "module":
        return resolve_chain(rs, module_name(r[1]), names[1:]) if len(names) > 1 else None
    if r[0] != "def":
        return None
    node = r[2]
    for nm in names[1:]:
        node = next((m for m in getattr(node, "body", []) if isinstance(m, (ast.ClassDef, ast.FunctionDef,
                                                                               ast.AsyncFunctionDef))
                     and m.name == nm), None) if isinstance(node, ast.ClassDef) else None
        if node is None:
            return None
    return r[1], node


def same_node(t, node):
    """The node of parse t that corresponds to `node` from another parse of the same file (same kind, name, line)."""
    if t is None:
        return None
    best = None
    for n in ast.walk(t):
        if type(n) is type(node) and getattr(n, "name", None) == getattr(node, "name", None):
            d = abs(n.lineno - node.lineno)
            if best is None or d < best[0]:
                best = (d, n)
    return best[1] if best else None


def class_bases(path: str, cnode, get_tree, rs) -> list[tuple[str, ast.ClassDef]]:
    """Base classes of a class as (path, ClassDef of get_tree(path)): same-file classes (dotted names allowed) or
    classes imported into the file; other bases are skipped."""
    t = get_tree(path)
    if t is None:
        return []
    quals = {d["qual"]: d["node"] for d in def_index(t) if d["kind"] == "class"}
    imps = file_imports(t, path)
    out = []
    for bn in cnode.bases:
        ch = attr_chain(bn)
        if not ch:
            continue
        q = ".".join(ch)
        if q in quals:
            out.append((path, quals[q]))
            continue
        if ch[0] in imps:
            m, a = imps[ch[0]]
            r = resolve_chain(rs, m, ([a] if a else []) + ch[1:])
            if r and isinstance(r[1], ast.ClassDef):
                n2 = same_node(get_tree(r[0]), r[1])
                if n2 is not None:
                    out.append((r[0], n2))
    return out


def method_in_class(cnode, func: str):
    """(method node, note) for a test name in a class body: exact name, or a parameterized.expand suffix."""
    meths = [m for m in cnode.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for m in meths:
        if m.name == func:
            return m, ""
    c = [m for m in meths if func.startswith(m.name + "_") and re.match(r"^_\d+", func[len(m.name):])]
    if c:
        return max(c, key=lambda m: len(m.name)), "parameterized suffix stripped"
    return None, ""


def locate_py(path: str, parts: list[str], get_tree, rs):
    """(def entry, def path, note) of a pytest / unittest id. When the id's class does not define the method, its
    base classes are searched (same file or imported, depth <= 6): pandas extension tests, MONAI reader tests."""
    t = get_tree(path)
    d, note = find_py_test(t, parts)
    if d is not None:
        return d, path, note
    if t is None or len(parts) < 2:
        return None, None, note
    cq = ".".join(parts[:-1])
    cd = next((x for x in def_index(t) if x["kind"] == "class" and x["qual"] == cq), None)
    if cd is None:
        return None, None, note
    queue, seen = [(path, cd["node"], 0)], set()
    while queue:
        p, cnode, depth = queue.pop(0)
        if depth > 6:
            continue
        for p2, base in class_bases(p, cnode, get_tree, rs):
            if (p2, base.lineno) in seen:
                continue
            seen.add((p2, base.lineno))
            m, n2 = method_in_class(base, parts[-1])
            if m is not None:
                ent = next((x for x in def_index(get_tree(p2)) if x["node"] is m), None)
                if ent is not None:
                    owner = ent["qual"].rsplit(".", 1)[0]
                    return ent, p2, f"inherited from {p2}::{owner}" + (f"; {n2}" if n2 else "")
            queue.append((p2, base, depth + 1))
    return None, None, note


def pytest_idval(node, argname: str, idx: int) -> str:
    """pytest's id part for one parametrize value when the source fixes it (scalars, class / function names), else
    argname + index (pytest's fallback for other objects)."""
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, str):
            return v if v.isascii() and v.isprintable() else v.encode("unicode_escape").decode("ascii")
        if isinstance(v, (bool, int, float)) or v is None:
            return str(v)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
            and isinstance(node.operand.value, (int, float)) and not isinstance(node.operand.value, bool):
        return str(-node.operand.value)
    if isinstance(node, (ast.Name, ast.Attribute)):
        ch = attr_chain(node)
        if ch:
            return ch[-1]
    return f"{argname}{idx}"


def param_rows(fn, mod_tree):
    """[(id, source, line)] of the rows of a test's single @pytest.mark.parametrize, or None when the ids are not
    derivable from the source (several parametrize decorators, computed values, ids= callable, duplicate ids)."""
    decs = [d for d in fn.decorator_list if isinstance(d, ast.Call) and (attr_chain(d.func) or [""])[-1] == "parametrize"]
    if len(decs) != 1:
        return None
    dec = decs[0]
    args = list(dec.args) + [k.value for k in dec.keywords if k.arg in ("argnames", "argvalues")]
    if len(args) < 2:
        return None
    an, vals = args[0], args[1]
    if isinstance(an, ast.Constant) and isinstance(an.value, str):
        names = [x.strip() for x in an.value.split(",") if x.strip()]
    elif isinstance(an, (ast.List, ast.Tuple)) and all(isinstance(x, ast.Constant) for x in an.elts):
        names = [x.value for x in an.elts]
    else:
        return None
    if isinstance(vals, ast.Name) and mod_tree is not None:
        asg = [st.value for st in mod_tree.body if isinstance(st, ast.Assign)
               and any(isinstance(x, ast.Name) and x.id == vals.id for x in st.targets)]
        vals = asg[-1] if asg else vals
    if not isinstance(vals, (ast.List, ast.Tuple)) or not names:
        return None
    ids = None
    kw_ids = next((k.value for k in dec.keywords if k.arg == "ids"), None)
    if kw_ids is not None:
        if isinstance(kw_ids, (ast.List, ast.Tuple)) and len(kw_ids.elts) == len(vals.elts) and \
                all(isinstance(x, ast.Constant) and isinstance(x.value, str) for x in kw_ids.elts):
            ids = [x.value for x in kw_ids.elts]
        else:
            return None
    rows = []
    for i, el in enumerate(vals.elts):
        if isinstance(el, ast.Starred):
            return None
        explicit = None
        if isinstance(el, ast.Call) and (attr_chain(el.func) or [""])[-1] == "param":
            ex = next((k.value for k in el.keywords if k.arg == "id"), None)
            if ex is not None:
                if not (isinstance(ex, ast.Constant) and isinstance(ex.value, str)):
                    return None
                explicit = ex.value
            inner = list(el.args)
        else:
            inner = [el] if len(names) == 1 else list(el.elts) if isinstance(el, (ast.Tuple, ast.List)) else None
        if ids is not None:
            rid = ids[i]
        elif explicit is not None:
            rid = explicit
        elif inner is not None and len(inner) == len(names):
            rid = "-".join(pytest_idval(v, a, i) for v, a in zip(inner, names))
        else:
            return None
        rows.append((rid, clip(ast.unparse(el), 90), el.lineno))
    if len({r[0] for r in rows}) != len(rows):
        return None
    return rows


def id_is_cut(s: str) -> bool:
    """A test id (or id variant) whose parameter part the dataset cut at a space: '[' opened, never closed."""
    i = s.find("[")
    return i >= 0 and not s.endswith("]")


def natkey(s: str):
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"(\d+)", s) if x]


def product_cover(tuples) -> list[tuple]:
    """Lossless cover of a set of equal-length tuples by a union of cartesian products: rows that agree on every
    position but one are merged (their sets at that position united) until nothing merges."""
    rows = list({tuple(frozenset([x]) for x in t) for t in tuples})
    n = len(rows[0]) if rows else 0
    changed = True
    while changed:
        changed = False
        for i in range(n):
            groups: dict[tuple, set] = defaultdict(set)
            for r in rows:
                groups[r[:i] + r[i + 1:]] |= r[i]
            if len(groups) < len(rows):
                rows = [k[:i] + (frozenset(v),) + k[i:] for k, v in groups.items()]
                changed = True
    return rows


def render_set(vals) -> str:
    vs = sorted(vals, key=natkey)
    if len(vs) == 1:
        return vs[0]
    if all(re.fullmatch(r"-?\d+", v) for v in vs):
        nums, runs = sorted(int(v) for v in vs), []
        for x in nums:
            if runs and x == runs[-1][1] + 1:
                runs[-1][1] = x
            else:
                runs.append([x, x])
        vs = [f"{a}..{b}" if b > a + 1 else f"{a},{b}" if b == a + 1 else f"{a}" for a, b in runs]
    return "{" + ",".join(vs) + "}"


def compress_ids(name: str, variants: list[str], cap: int = 2000) -> str:
    """Lossless compact rendering of the ids of one test: name + variant (variant = '[params]' or a parameterized
    suffix '_003'). `a..b` = every integer in the range; `{x,y}` = each of the values; bracketed ids are written as a
    union of cartesian products of their '-'-separated components. Longer than cap characters: cut (stated).
    v4.1: ids the dataset cut at a space (an unclosed '[') are listed after the others, each with ID_CUT_NOTE."""
    vs = sorted(set(variants), key=natkey)
    cut_vs = [v for v in vs if id_is_cut(v)]
    if cut_vs:
        vs = [v for v in vs if v not in cut_vs]
        out = (compress_ids(name, vs, UNLIMITED) + ", " if vs else "") + \
            ", ".join(f"{name}{v} ({ID_CUT_NOTE})" for v in cut_vs)
        if len(out) > cap:
            cut = out[:cap].rsplit(", ", 1)[0]
            out = f"{cut}, ... (list cut at {cap} characters, content cap; {len(vs) + len(cut_vs)} ids in total)"
        return out
    if vs == [""]:
        return name
    if len(vs) >= 3 and all(re.fullmatch(r"_\d+", v) for v in vs):
        nums = sorted((int(v[1:]), v) for v in vs)
        runs, cur = [], [nums[0]]
        for x in nums[1:]:
            if x[0] == cur[-1][0] + 1 and len(x[1]) == len(cur[-1][1]):
                cur.append(x)
            else:
                runs.append(cur)
                cur = [x]
        runs.append(cur)
        out = ", ".join(f"{name}{r[0][1]}" if len(r) == 1 else f"{name}{r[0][1]}..{name}{r[-1][1]}" for r in runs)
    elif len(vs) >= 4 and all(v.startswith("[") and v.endswith("]") for v in vs):
        by_n = defaultdict(list)
        for v in vs:
            comps = []
            for tok in v[1:-1].split("-"):  # an empty piece is the sign of the next one ('a--1' = a, -1)
                if comps and comps[-1] in ("", "-"):
                    comps[-1] = "-" + tok if comps[-1] == "" else comps[-1] + tok
                else:
                    comps.append(tok)
            by_n[len(comps)].append(tuple(comps))
        parts = []
        for n_, tl in sorted(by_n.items()):
            if n_ == 1 and len(tl) <= 12:
                parts.extend(f"{name}[{t[0]}]" for t in sorted(tl, key=lambda t: natkey(t[0])))
                continue
            rows = product_cover(tl)
            rows.sort(key=lambda r: [natkey(min(x, key=natkey)) for x in r])
            parts.extend(name + "[" + "-".join(render_set(x) for x in r) + "]" for r in rows)
        out = ", ".join(parts)
    else:
        out = ", ".join(name + v for v in vs)
    if len(out) > cap:
        cut = out[:cap].rsplit(", ", 1)[0]
        out = f"{cut}, ... (list cut at {cap} characters, content cap; {len(vs)} ids in total)"
    return out


CODE_ROW = re.compile(r"^.\s*(\d+)\| ")
PATH_TOKEN = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.[A-Za-z]{1,5})(?![\w])")


def register_shown(out_lines: list[str], files, sec_name: str, reg: dict):
    """Record the numbered code lines a section shows (fenced blocks; the file is the latest path named outside a
    block): reg[path][line] = section."""
    cur, fence = None, False
    for line in out_lines:
        if line.startswith("```"):
            fence = not fence
            continue
        if fence:
            m = CODE_ROW.match(line)
            if m and cur:
                reg[cur].setdefault(int(m.group(1)), sec_name)
            continue
        for pm in PATH_TOKEN.finditer(line):
            if pm.group(1) in files:
                cur = pm.group(1)
                break


def node_doc(node):
    """(first, last) line of a def / class docstring, or None."""
    b_ = getattr(node, "body", None)
    if b_ and isinstance(b_[0], ast.Expr) and isinstance(getattr(b_[0], "value", None), ast.Constant) \
            and isinstance(b_[0].value.value, str):
        return b_[0].lineno, b_[0].end_lineno
    return None


def head_rows(lines: list[str], s: int, e: int, cap: int, focus: set[int] = frozenset(), doc=None,
              doc_cap: int | None = None) -> list[tuple[int, str]]:
    """Lines s..e, a docstring cut to doc_cap lines; if still longer than cap: the header and windows around the
    focus lines, or (no focus) the first lines and the last 3; elided ranges are marked."""
    pool = list(range(s, e + 1))
    if doc is not None and doc_cap is not None and doc[1] - doc[0] + 1 > doc_cap:
        cut = set(range(doc[0] + doc_cap, doc[1] + 1))
        pool = [i for i in pool if i not in cut]
    if len(pool) <= cap:
        sel = pool
    elif focus and any(f in pool for f in focus):
        pos = {ln: k for k, ln in enumerate(pool)}
        fp = sorted(pos[f] for f in focus if f in pos)
        rad = max(1, (cap - 2) // (2 * len(fp)))
        ks = {0, 1}
        for k in fp:
            ks |= set(range(max(0, k - rad), min(len(pool), k + rad + 1)))
        sel = [pool[k] for k in sorted(ks)][:cap]
    else:
        sel = sorted(set(pool[:max(1, cap - 3)] + pool[-3:]))
    rows, prev = [], None
    for i in sel:
        if prev is not None and i != prev + 1:
            in_doc = doc is not None and doc[0] <= prev + 1 and i - 1 <= doc[1]
            rows.append((0, f"... [docstring continues to line {i - 1}] ..." if in_doc
                         else f"... [lines {prev + 1}-{i - 1} elided] ..."))
        rows.append((i, lines[i - 1]))
        prev = i
    if prev is not None and prev < e:
        rows.append((0, f"... [lines {prev + 1}-{e} elided] ..."))
    return rows


def drop_shown(rows: list[tuple[int, str]], shown: dict[int, str], min_run: int = 3) -> list[tuple[int, str]]:
    """Replace runs of >= min_run consecutive rows that an earlier section already shows by one pointer row."""
    out, i = [], 0
    while i < len(rows):
        ln = rows[i][0]
        if ln and ln in shown:
            j = i
            while j + 1 < len(rows) and rows[j + 1][0] == rows[j][0] + 1 and rows[j + 1][0] in shown:
                j += 1
            if j - i + 1 >= min_run:
                secs = sorted({shown[rows[k][0]] for k in range(i, j + 1)}, key=lambda x: int(x[1:]))
                out.append((0, f"... [lines {rows[i][0]}-{rows[j][0]}: shown in section "
                               f"{', '.join(x[1:] for x in secs)}] ..."))
                i = j + 1
                continue
        out.append(rows[i])
        i += 1
    return out


TB_FRAME = re.compile(r'File "([^"\n]+)", line (\d+), in ([^\s]+)')
PYTEST_FRAME = re.compile(r"^\s*([\w./\\-]+\.pyx?):(\d+): in ([\w.<>]+)\s*$")


def issue_frames(text: str) -> list[tuple[str, int, str, str, int]]:
    """(path, line, function, quoted code line or '', traceback index) of every traceback frame in the text."""
    out, tb = [], 0
    lines = text.split("\n")
    for i, l in enumerate(lines):
        if "Traceback (most recent call last)" in l or "During handling of the above exception" in l or \
                "The above exception was the direct cause" in l:
            tb += 1
        m = TB_FRAME.search(l)
        if m:
            code = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if TB_FRAME.search(code) or code.startswith(("Traceback", "During handling")) or len(code) < 3:
                code = ""
            out.append((m.group(1), int(m.group(2)), m.group(3), code, tb))
            continue
        m = PYTEST_FRAME.match(l)
        if m:
            out.append((m.group(1), int(m.group(2)), m.group(3), "", tb))
    return out


HEADING_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")


def doc_heading(lines: list[str], ln: int) -> str:
    """Nearest heading at or above line ln of a .md / .rst file."""
    for i in range(ln, 0, -1):
        l = lines[i - 1]
        if re.match(r"^#{1,6}\s+\S", l):
            return l.lstrip("#").strip()
        if i < len(lines) and HEADING_UNDERLINE.match(lines[i]) and l.strip() and not HEADING_UNDERLINE.match(l):
            return l.strip()
    return ""


# ------------------------------------------------------------------ build one task

def fill_cap(render, lo: int, hi: int, avail: int):
    """Largest cap in [lo, hi] whose blocks (render(cap) -> list of blocks) fit `avail` tokens; None if lo does not
    fit (binary search; the returned cap was checked to fit)."""
    tot = lambda c: sum(cost(x) for x in render(c))  # noqa: E731
    if hi <= lo or tot(hi) <= avail:
        return hi if tot(hi) <= avail else (None if tot(lo) > avail else lo)
    if tot(lo) > avail:
        return None
    a, z = lo, hi
    while z - a > 1:
        m = (a + z) // 2
        if tot(m) <= avail:
            a = m
        else:
            z = m
    return a


def water(demands: list[int], total: int) -> list[int]:
    """Equal shares of total, each capped at its demand; what a capped item leaves goes to the others."""
    alloc, left, open_ = [0] * len(demands), total, list(range(len(demands)))
    while open_ and left > 0:
        share = left // len(open_)
        if share == 0:
            break
        nxt = []
        for i in open_:
            give = min(share, demands[i] - alloc[i])
            alloc[i] += give
            left -= give
            if alloc[i] < demands[i]:
                nxt.append(i)
        if len(nxt) == len(open_):
            break
        open_ = nxt
    return alloc


def build(task: dict, harness: dict, budgets: dict, ctx: dict, force_fallback: bool = False) -> tuple[str, dict]:
    """One pack at the given per-section token budgets. ctx caches the base tree and corpus across passes."""
    t0 = time.time()
    iid, repo, sha = task["instance_id"], task["repo"], task["base_commit"]
    missing: list[str] = []
    if "tree" not in ctx:
        d = repo_dir(repo)
        if not force_fallback and d.is_dir() and has_commit(d, sha):
            ctx["tree"], ctx["missing0"] = GitTree(d, sha), []
        else:
            ctx["tree"] = testbed_tree(iid)
            ctx["missing0"] = ["base commit not in local clone: used /testbed of the task image"]
    missing.extend(ctx["missing0"])
    return _build(task, ctx["tree"], harness, budgets, missing, t0, ctx)


def _build(task, tree, harness, budgets, missing, t0, ctx):
    iid, repo, sha = task["instance_id"], task["repo"], task["base_commit"]
    issue = task["problem_statement"] or ""
    hints = task["hints_text"] or ""
    gold = [f for f in parse_patch(task["patch"]) if not f["binary"]]
    tests = [f for f in parse_patch(task["test_patch"]) if not f["binary"]]
    gold_code = [f for f in gold if not DOC_PATH.search(f["path"]) or f["path"].endswith((".py", ".pyx"))]
    gold_docs = [f["path"] for f in gold if f not in gold_code]
    edited_paths = {f["path"] for f in gold_code}
    if "corpus" not in ctx:
        ctx["corpus"] = Corpus(tree, edited_paths, {f["old"] for f in tests if f["old"]})
    corpus = ctx["corpus"]
    stats = {"corpus_files": len(corpus.paths), "corpus_mb": round(len(corpus.big) / 1e6, 1)}
    # budget degradations: omissions (an item not shown) and truncations (an item shown in part); every one is also
    # stated in the pack text
    omissions: list[dict] = []
    truncations: list[dict] = []

    def omit(sec_, typ, n, detail=""):
        if n > 0:
            omissions.append({"section": sec_, "type": typ, "n": n, "detail": detail})
            missing.append(f"{sec_}: {n} {typ} omitted (budget){': ' + detail if detail else ''}")

    def trunc(sec_, typ, n, detail=""):
        if n > 0:
            truncations.append({"section": sec_, "type": typ, "n": n, "detail": detail})
            missing.append(f"{sec_}: {n} {typ} (budget){': ' + detail if detail else ''}")

    base_texts = tree.read_many(sorted({f["old"] for f in gold + tests if f["old"]}))
    gold_ap = {}
    for f in gold_code:
        ap = apply_file_patch(base_texts.get(f["old"]) if f["old"] else None, f)
        if not ap["ok"]:
            missing.append(f"gold patch hunk did not apply at base: {f['path']}")
        gold_ap[f["path"]] = (f, ap)
    test_ap = {}
    for f in tests:
        ap = apply_file_patch(base_texts.get(f["old"]) if f["old"] else None, f)
        if not ap["ok"]:
            missing.append(f"test patch hunk did not apply at base: {f['path']}")
        test_ap[f["path"]] = (f, ap)

    def post_text(path):
        if path in test_ap:
            return "\n".join(test_ap[path][1]["post"]) + "\n"
        return tree.read(path)

    # ---------------------------------------------------------- items (section 1)
    items: dict[tuple[str, str], dict] = {}
    n_local = Counter()

    def add(kind, value, role, where, local=frozenset()):
        if kind == "ident" and (value in SKIP_NAMES or len(value) <= 1):
            return
        if kind == "ident" and value in local:
            n_local[value] += 1
            return
        if kind == "number" and (value in TRIVIAL_NUMBERS or value.lstrip("-") in TRIVIAL_NUMBERS):
            return
        if kind == "string":
            if len(value.strip()) < 2 or len(value) > 400:
                if len(value) > 400:
                    value = value[:400]
                else:
                    return
        it = items.setdefault((kind, value), {"kind": kind, "value": value, "roles": set(), "where": where})
        it["roles"].add(role)

    f2p = list(task["FAIL_TO_PASS"])
    seen_tests = {}
    py_trees, py_toks = {}, {}

    def ptree(path):
        if path not in py_trees:
            py_trees[path] = parse_py(post_text(path))
        return py_trees[path]

    def ptoks(path):
        if path not in py_toks:
            txt = post_text(path) or ""
            py_toks[path] = PyTokens(txt, ptree(path))
        return py_toks[path]

    if "rs" not in ctx:
        ctx["rs"] = Resolver(tree)
        ctx["rs_post"] = Resolver(OverlayTree(tree, {p_: "\n".join(ap_["post"]) + "\n"
                                                     for p_, (f_, ap_) in test_ap.items() if not f_["is_deleted"]}))
    rs, rs_post = ctx["rs"], ctx["rs_post"]
    for tid in f2p:
        path, parts = parse_test_id(tid)
        key = (path, tuple(parts))
        if key in seen_tests:
            seen_tests[key]["n"] += 1
            seen_tests[key]["ids"].append(tid)
            continue
        rec = {"id": tid, "ids": [tid], "path": path, "dpath": path, "parts": parts, "n": 1, "kind": None,
               "loc": None, "note": ""}
        seen_tests[key] = rec
        text = post_text(path)
        if text is None:
            rec["note"] = "test file not found at base or in the test patch"
            missing.append(f"F2P test not located: {tid}")
            continue
        d, dpath, note = (None, path, "")
        if path.endswith(".py"):
            d, dpath, note = locate_py(path, parts, ptree, rs_post)
        if d is not None:
            rec.update(kind="py", loc=(d["start"], d["end"]), qual=d["qual"], note=note, dpath=dpath)
            fn = d["node"]
            alines = assert_lines(fn)
            rec.update(node=fn, alines=alines, mod_names=None)
            dec_lines = set()
            for dec in fn.decorator_list:
                dec_lines.update(range(dec.lineno, dec.end_lineno + 1))
            tk = ptoks(dpath)
            added = test_ap[dpath][1]["added"] if dpath in test_ap else set()
            local = local_names(fn, own_args=True) | {fn.name, d["name"]} | set(parts)
            for ln in range(d["start"], d["end"] + 1):
                role = "F2P-assert" if ln in alines else "F2P-param" if ln in dec_lines else "F2P-body"
                for kind, v in tk.tokens([ln]):
                    add(kind, v, role, f"{dpath}:{ln}", local)
            # module-level constants the test references that the test patch adds or edits
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            for st in ptree(dpath).body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                    if any(isinstance(t, ast.Name) and t.id in names for t in tg):
                        span = set(range(st.lineno, st.end_lineno + 1)) & added
                        for ln in sorted(span):
                            for kind, v in tk.tokens([ln]):
                                add(kind, v, "F2P-const", f"{dpath}:{ln}")
            continue
        # mypy-style data-driven case
        cname = parts[-1] if parts else ""
        datafile = next((p for p in reversed(parts[:-1]) if p.endswith(".test")), None)
        cands = [p for p in test_ap if p.endswith(".test")] + \
            sorted(p for p in tree.files if p.endswith(".test") and (datafile is None or p.endswith("/" + datafile)))
        if datafile:
            cands = [p for p in cands if p.endswith("/" + datafile)] + [p for p in cands if not p.endswith("/" + datafile)]
        found = None
        for p in dict.fromkeys(cands):
            t = post_text(p)
            if t and f"[case {re.sub(r'_cached$', '', cname)}" in t:
                fc = find_case(t, cname)
                if fc:
                    found = (p, fc, split_lines(t))
                    break
        if found:
            p, (a, b, hdr), lines = found
            rec.update(kind="case", path=p, dpath=p, loc=(a, b), qual=hdr, note=note)
            for ln, role, kind, v in mypy_case_tokens(lines, a, b):
                add(kind, v, role, f"{p}:{ln}")
        else:
            rec["note"] = note or "not found"
            missing.append(f"F2P test not located: {tid}")

    for path, (f, ap) in sorted(gold_ap.items()):
        if not ap["added"]:
            continue
        txt = "\n".join(ap["post"]) + "\n"
        if path.endswith((".py", ".pyi", ".pyx", ".pxd", ".pxi")):
            t = parse_py(txt) if path.endswith((".py", ".pyi")) else None
            tk = PyTokens(txt, t)
            defs = def_index(t) if t is not None else []
            loc_cache = {}
            for ln in sorted(ap["added"]):
                f_, _c = unit_of(defs, ln) if defs else (None, None)
                local = frozenset()
                if f_ is not None:
                    if f_["qual"] not in loc_cache:
                        loc_cache[f_["qual"]] = local_names(f_["node"], own_args=False)
                    local = loc_cache[f_["qual"]]
                for kind, v in tk.tokens([ln]):
                    add(kind, v, "gold+", f"{path}:{ln}", local)
        else:
            for ln in sorted(ap["added"]):
                for kind, v in regex_tokens(ap["post"][ln - 1]):
                    add(kind, v, "gold+", f"{path}:{ln}")

    def in_text(kind, v, text):
        if not text:
            return False
        if kind == "string":
            return v in text
        return re.search(r"(?<![A-Za-z0-9_])" + re.escape(v) + r"(?![A-Za-z0-9_])", text) is not None

    kept = []
    n_in_issue = 0
    for it in items.values():
        if in_text(it["kind"], it["value"], issue):
            n_in_issue += 1
            continue
        it["in_hints"] = in_text(it["kind"], it["value"], hints)
        kept.append(it)
    hit_cache = ctx.setdefault("s1_hits", {})  # budget-independent search results, reused across passes
    for it in kept:
        v, kind = it["value"], it["kind"]
        if (kind, v) in hit_cache:
            it.update(hit_cache[(kind, v)])
            roles = it["roles"]
            it["rank"] = min(ROLE_RANK[r] for r in roles)
            it["both"] = any(r.startswith("F2P") for r in roles) and "gold+" in roles
            continue
        offs = corpus.find(v, word=kind != "string")
        it["hits"] = len(offs)
        it["frag"] = None
        if offs:
            it["cats"], it["nfiles"], it["locs"], it["inpatch"] = rank_hits(corpus, offs, v if kind == "ident" else None)
        else:
            it["cats"], it["nfiles"], it["locs"], it["inpatch"] = Counter(), 0, [], 0
        if kind == "string" and len(v) >= 16 and not it["cats"].get("src"):
            # exact string absent from the source: where does the source build it? (longest fragments first)
            for fr in fragments(v):
                fo = [o for o in corpus.find(fr, word=False, cap=5000) if corpus.cats[corpus.file_of(o)] == "src"]
                if fo:
                    fi = corpus.file_of(fo[0])
                    it["frag"] = (fr, len(fo), corpus.paths[fi], corpus.lineno(fi, fo[0]))
                    break
        hit_cache[(kind, v)] = {x: it[x] for x in ("hits", "frag", "cats", "nfiles", "locs", "inpatch")}
        roles = it["roles"]
        it["rank"] = min(ROLE_RANK[r] for r in roles)
        it["both"] = any(r.startswith("F2P") for r in roles) and "gold+" in roles

    kind_rank = {"string": 0, "ident": 1, "number": 2}

    def head(it):
        v = it["value"]
        shown = json.dumps(v, ensure_ascii=False) if it["kind"] == "string" else f"`{v}`"
        roles = ",".join(sorted(it["roles"], key=lambda r: (ROLE_RANK[r], r)))
        return f"{clip(shown, 2 * S1_VALUE_CHARS + 2)} [{it['kind']}; {roles}; first {it['where']}]" + \
            ("; in hints" if it["in_hints"] else "")

    absent = sorted([i for i in kept if not i["hits"]], key=lambda i: (i["rank"], not i["both"],
                                                                        kind_rank[i["kind"]], i["value"]))
    in_patch_only = sorted([i for i in kept if i["hits"] and i["hits"] == i["inpatch"]],
                           key=lambda i: (i["rank"], kind_rank[i["kind"]], i["value"]))
    present = sorted([i for i in kept if i["hits"] and i["hits"] < COMMON_HITS and i["hits"] != i["inpatch"]],
                     key=lambda i: (i["rank"], not i["both"], kind_rank[i["kind"]], i["hits"], i["value"]))
    common = sorted([i for i in kept if i["hits"] >= COMMON_HITS and i["hits"] != i["inpatch"]],
                    key=lambda i: (i["rank"], i["value"]))

    # names the test patch itself defines (helpers, test classes): absent at base by construction
    test_defined = set()
    for path, (f, ap) in test_ap.items():
        for ln in ap["added"]:
            m = re.match(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)|^(\w+)\s*(?::[^=]*)?=", ap["post"][ln - 1])
            if m:
                test_defined.add(m.group(1) or m.group(2))

    def frag_note(it):
        if it["kind"] == "ident" and it["value"] in test_defined and not it["hits"]:
            return " -- defined by the test patch itself"
        if not it.get("frag"):
            return ""
        fr, n, p, ln = it["frag"]
        return f" -- exact string not in the source; fragment {json.dumps(fr, ensure_ascii=False)} in source {n}x, " \
               f"first {p}:{ln}"

    sec = {}

    def take(key):
        if key in NEW_CEILING:  # content ceiling of sections 10-14 (a content rule, stated as such)
            return Budget(min(budgets[key], NEW_CEILING[key]), budgets.get("_reserve", {}).get(key, NOTICE_RESERVE))
        return Budget(budgets[key], budgets.get("_reserve", {}).get(key, NOTICE_RESERVE))

    def done(key, b):
        sec[key] = b

    def list_items(b, title, entries, sep, what, sec_, typ):
        """One line `title` + entries joined by sep, as many entries as fit; the rest stated as omitted."""
        if not entries:
            return
        k = len(entries)
        while k > 0 and not b.fits(cost(["", title + sep.join(entries[:k])])):
            # shrink by the token overshoot, at least one entry
            over = cost(["", title + sep.join(entries[:k])]) - (b.left - b.reserve)
            per = max(1, cost([sep.join(entries[:k])]) // k)
            k = max(0, min(k - 1, k - over // per))
        if k:
            b.add(["", title + sep.join(entries[:k])])
            if k < len(entries):
                b.force([f"- ... {len(entries) - k} more {what} omitted (budget)"])
        else:
            b.force(["", title + f"all {len(entries)} {what} omitted (budget)"])
        omit(sec_, typ, len(entries) - k)

    # ---- S1: 1a absent items first; then 1b present items (windows for as many as fit after every present item is
    # listed compactly); then 1c / 1d lists
    b = take("s1")
    b.force(["## 1. F2P-test and gold-patch tokens not in the issue: presence at base",
             "Purpose: whether each name, message or value the F2P tests or the gold patch use (and the issue does not "
             "mention) already exists at base, and where.",
             f"{len(items)} distinct tokens ({sum(n_local.values())} uses of {len(n_local)} test/gold-local variable "
             f"names not counted); {n_in_issue} occur in the issue text (omitted); {len(kept)} listed: "
             f"{len(absent)} ABSENT AT BASE, {len(present)} present outside the test-patch files (<{COMMON_HITS} "
             f"hits), {len(in_patch_only)} present only inside the files the test patch edits, {len(common)} common.",
             "Roles: F2P-assert = assertion / pytest.raises / expected-value statement of an F2P test (mypy: # E/N/W "
             "and [out] messages), F2P-param = decorator (parametrize data; mypy: # flags), F2P-body / F2P-const = "
             "rest of the test / module constant the test uses (added lines), gold+ = added line of the gold patch. "
             "Strings match as substrings, identifiers and numbers as whole words, over the base tree's text files. "
             "Hit counts: src / test / doc paths.", ""])
    b.force([f"### 1a. ABSENT AT BASE ({len(absent)})"])
    absent_l = [[f"- {head(it)}{frag_note(it)}"] for it in absent]
    if b.cap < UNLIMITED:  # 1a may not starve 1b: at most 60% of the room unless 1b (compact) needs less
        room = b.left - b.reserve
        c1b = sum(cost([f"- {head(it)} -- {it['hits']} hits / {it['nfiles']} files"]) for it in present) + 60
        a_avail = max(int(0.6 * room), room - c1b)
    else:
        a_avail = UNLIMITED
    used = 0
    for k, x in enumerate(absent_l):
        c = cost(x)
        if used + c > a_avail or not b.add(x):
            b.force([f"- ... {len(absent) - k} more ABSENT items omitted (budget)"])
            omit("s1", "absent token", len(absent) - k)
            break
        used += c
    hdr_1b = ["", f"### 1b. Present at base outside the test-patch files, <{COMMON_HITS} hits ({len(present)}; "
                  "rarest first within role; 3-line windows)"]
    full_l, compact_l = [], []
    for it in present:
        c = it["cats"]
        cnt = f"{it['hits']} hits / {it['nfiles']} files (src {c.get('src', 0)}, test {c.get('test', 0)}, " \
              f"doc {c.get('doc', 0)}" + (f"; {it['inpatch']} in test-patch files" if it["inpatch"] else "") + ")"
        lines = [f"- {head(it)} -- {cnt}{frag_note(it)}"]
        for fi, ln, is_def in it["locs"]:
            lines.append(f"    {corpus.paths[fi]}:{ln}" + (" (definition)" if is_def else ""))
            lines.extend(f"      {i:>5}| {clip(t, 200)}" for i, t in corpus.window(fi, ln, 1))
        fi, ln, _ = it["locs"][0]
        full_l.append(lines)
        compact_l.append([f"- {head(it)} -- {cnt}; first {corpus.paths[fi]}:{ln}{frag_note(it)}"])
    b.force(hdr_1b)
    fc = [cost(x) for x in full_l]
    cc = [cost(x) for x in compact_l]
    room = b.left - b.reserve - sum(cc)
    n_full = 0
    if room >= 0:
        while n_full < len(present) and fc[n_full] - cc[n_full] <= room:
            room -= fc[n_full] - cc[n_full]
            n_full += 1
    for k in range(len(present)):
        if not b.add(full_l[k] if k < n_full else compact_l[k]):
            b.force([f"- ... {len(present) - k} more present items omitted (budget)"])
            omit("s1", "present token", len(present) - k)
            break
    else:
        k = len(present)
    if n_full < min(k, len(present)):
        trunc("s1", "present token without context windows", min(k, len(present)) - n_full)
    if in_patch_only:
        list_items(b, f"### 1c. Present at base only inside the files the test patch edits (pre-existing test "
                      f"content; {len(in_patch_only)}): ",
                   [clip(json.dumps(i["value"], ensure_ascii=False) if i["kind"] == "string" else i["value"], 70)
                    + f" [{','.join(sorted(i['roles'], key=lambda r: (ROLE_RANK[r], r)))}]" for i in in_patch_only],
                   "; ", "test-patch-file-only tokens", "s1", "test-patch-file-only token")
    if common:
        list_items(b, f"### 1d. Common at base (>= {COMMON_HITS} hits; {len(common)}): ",
                   [clip(json.dumps(i["value"], ensure_ascii=False) if i["kind"] == "string" else i["value"], 60)
                    + f"({i['hits'] if i['hits'] < 20000 else '20000+'})" for i in common],
                   ", ", "common tokens", "s1", "common token")
    done("s1", b)

    # ---------------------------------------------------------- S2 / S3 / S4 (gold edits)
    units = []  # (path, key, span, focus, marks, base lines, removed, inserts, ap)
    module_info = {}
    for path, (f, ap) in sorted(gold_ap.items()):
        if f["is_new"]:
            module_info[path] = {"new": True}
            continue
        base_text = base_texts.get(f["old"])
        if base_text is None:
            missing.append(f"gold-edited file unreadable at base: {path}")
            continue
        ptree_, info, focus, marks, rem, ins = edited_units(base_text, ap, path)
        module_info[path] = {"tree": ptree_, "lines": ap["base"], "info": info, "focus": focus, "marks": marks}
        if ptree_ is None and path.endswith((".py", ".pyi")):
            missing.append(f"base parse failed (lexical fallback): {path}")
        for key in sorted(info, key=lambda k: (min(focus[k]), k)):
            units.append((path, key, info[key], focus[key], marks, ap["base"], rem[key], ins[key], ap))
    # functions / classes the gold patch adds (qualname absent from the base file)
    new_defs = []
    for path, (f, ap) in sorted(gold_ap.items()):
        if path.endswith((".py", ".pyi")):
            t = parse_py("\n".join(ap["post"]) + "\n")
            bt = module_info.get(path, {}).get("tree")
            base_quals = {d["qual"] for d in def_index(bt)} if bt is not None else set()
            if t is not None:
                for d in def_index(t):
                    if "func" in d["parents"] or d["qual"] in base_quals:
                        continue
                    if d["node"].lineno in ap["added"]:
                        new_defs.append(f"{path}: {clip(signature(d), 220)}")

    b = take("s2")
    b.force(["## 2. Base bodies of what the gold patch edits",
             "Purpose: the base code the fix has to change, and the public interface of the edited modules.",
             "Markers: `*` base line the gold patch removes or "
             "rewrites, `>` the gold patch inserts lines after this base line."])
    if gold_docs:
        b.force([f"Gold patch also edits docs (not shown): {clip(', '.join(gold_docs), 600)}"])
    for path, info in sorted(module_info.items()):
        if info.get("new"):
            b.force([f"- NEW FILE in the gold patch (absent at base): {path}"])
    if new_defs:
        b.force(["Functions / classes the gold patch adds (absent at base):"])
        for k, x in enumerate(new_defs[:40]):
            if not b.add([f"- {x}"]):
                b.force([f"- ... {min(40, len(new_defs)) - k} more gold-added definitions omitted (budget)"])
                omit("s2", "gold-added definition", min(40, len(new_defs)) - k)
                break
        if len(new_defs) > 40:
            b.force([f"- ... {len(new_defs) - 40} more (first 40 listed)"])
    # edited units: module / class units are fixed blocks; function bodies are elastic (whole up to S2_BODY_LINES,
    # elided around the edit down to S2_BODY_FLOOR when the budget is short)
    edited_funcs = []
    ublocks = []
    for path, key, val, focus, marks, lines, rem_, ins_, ap in units:
        kind = key[0]
        if kind == "module":
            blk = []
            if ins_:
                blk.append(f"- {path} module level: insertions {insert_summary(ap, ins_)}")
            if rem_:
                rows = []
                tr = module_info[path]["tree"]
                stmts = [(node_start(st), st.end_lineno) for st in (tr.body if tr is not None else [])
                         if any(node_start(st) <= r <= st.end_lineno for r in rem_)]
                if stmts:
                    for s_, e_ in stmts:
                        rows.extend(cap_body(lines, s_, e_, rem_, S2_MODULE_LINES))
                else:
                    for f_ in sorted(rem_):
                        rows.extend((i, lines[i - 1]) for i in range(max(1, f_ - 3), min(len(lines), f_ + 3) + 1))
                    rows = sorted(set(rows))
                blk += [f"### {path} (module-level statements the gold patch edits)"] + code_block(rows, marks)
            ublocks.append({"name": f"{path} (module level)", "fixed": blk})
        elif kind == "class":
            s, e, name, d = val
            blk = [f"### {path}::{key[1]} (class body edited, base L{s}-{e})"]
            if d is not None:
                cls = d["node"]
                members = [f"L{m.lineno} " + (f"def {m.name}" if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                                              else clip(ast.unparse(m), 80))
                           for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign,
                                                               ast.AnnAssign))]
                blk += [clip(signature(d), 300) + "; members: " + clip(", ".join(members), 6000)]
            if ins_:
                blk.append(f"- insertions in the class body: {insert_summary(ap, ins_)}")
            if rem_ or d is None:
                rows = []
                for f_ in sorted(rem_ or focus):
                    rows.extend((i, lines[i - 1]) for i in range(max(s, f_ - 4), min(e, f_ + 4) + 1))
                rows = sorted(set(rows))
                blk += code_block(rows, marks)
            ublocks.append({"name": f"{path}::{key[1]}", "fixed": blk})
        else:
            s, e, name, d = val
            edited_funcs.append((path, key[1], name, d, s, e))

            def render(L, path=path, q=key[1], s=s, e=e, focus=focus, marks=marks, lines=lines):
                n = e - s + 1
                note = "" if n <= L else \
                    f"; elided to ~{L} lines around the edit ({'content cap' if L >= S2_BODY_LINES else 'budget'})"
                return [f"### {path}::{q} (base L{s}-{e}, {n} lines{note})"] + \
                    code_block(cap_body(lines, s, e, focus, L), marks)
            ublocks.append({"name": f"{path}::{key[1]}", "fixed": None, "render": render, "n": val[1] - val[0] + 1})
    # signatures (computed first: their demand sets the reserve the bodies leave)
    sig_rows = []
    for path, info in sorted(module_info.items()):
        t = info.get("tree")
        if t is None:
            continue
        quals = {q for (p, q, *_r) in edited_funcs if p == path}
        quals |= {k[1] for k in info["info"] if k[0] == "class"}
        sig_rows.append((path, public_sigs(t, quals)))
    sig_lines = [(path, [f"- L{ln} {clip(signature(d), 220)}" for prio, ln, d in rows]) for path, rows in sig_rows]
    sig_demand = sum(cost(["", f"### Public signatures in {p} ({len(r)}; classes of edited methods first)"]) +
                     sum(cost([x]) for x in r) for p, r in sig_lines)
    sig_reserve = 0 if b.cap >= UNLIMITED else min(sig_demand, int(0.3 * b.cap))
    avail = b.left - b.reserve - sig_reserve

    def render_all(L):
        return [u["fixed"] if u["fixed"] is not None else u["render"](L) for u in ublocks]

    hi = max([S2_BODY_FLOOR] + [min(u["n"], S2_BODY_LINES) for u in ublocks if u["fixed"] is None])
    L = fill_cap(render_all, S2_BODY_FLOOR, hi, avail) if ublocks else hi
    s2_omitted = []
    if L is not None:
        for blk in render_all(L):
            b.force(blk)
        n_el = sum(1 for u in ublocks if u["fixed"] is None and u["n"] > L and L < S2_BODY_LINES and
                   min(u["n"], S2_BODY_LINES) > L)
        trunc("s2", "edited function body elided", n_el, f"to ~{L} lines")
    else:
        used = 0
        for u, blk in zip(ublocks, render_all(S2_BODY_FLOOR)):
            c = cost(blk)
            if used + c <= avail:
                b.force(blk)
                used += c
            else:
                s2_omitted.append(u["name"])
        n_el = sum(1 for u in ublocks if u["fixed"] is None and u["n"] > S2_BODY_FLOOR and u["name"] not in s2_omitted)
        trunc("s2", "edited function body elided", n_el, f"to ~{S2_BODY_FLOOR} lines")
    if s2_omitted:
        b.force([f"- edited units omitted (budget): {clip(', '.join(s2_omitted), 600)}"])
        omit("s2", "edited unit body", len(s2_omitted), clip(", ".join(s2_omitted), 300))
    # signatures: per edited module, water-filled shares of what is left (a module that needs less leaves the rest)
    hdrs_sig = [["", f"### Public signatures in {path} ({len(lines_)}; classes of edited methods first)"]
                for path, lines_ in sig_lines]
    dem = [cost(h) + sum(cost([x]) for x in lines_) for h, (path, lines_) in zip(hdrs_sig, sig_lines)]
    al_sig = water(dem, b.left - b.reserve) if b.cap < UNLIMITED else [UNLIMITED] * len(dem)
    for (path, lines_), hdr, quota in zip(sig_lines, hdrs_sig, al_sig):
        used = cost(hdr)
        b.force(hdr)
        shown = 0
        for line in lines_:
            c = cost([line])
            if used + c > quota or not b.add([line]):
                break
            used += c
            shown += 1
        if shown < len(lines_):
            b.force([f"- ... {len(lines_) - shown} more signatures omitted (budget)"])
            omit("s2", "public signature", len(lines_) - shown, path)
    done("s2", b)

    # ---- S5 (exact): one status entry per F2P test function (ids that resolve to the same base def are collapsed;
    # every F2P id listed losslessly; the same test's PASS_TO_PASS ids listed), then base bodies: F2P tests first, then
    # the other test-patch regions (whole up to S5_BODY_LINES; elided down to S5_BODY_FLOOR; then omitted, stated)
    b = take("s5")
    base_trees = {}

    def btree(path):
        if path not in base_trees:
            base_trees[path] = parse_py(tree.read(path)) if path.endswith(".py") else None
        return base_trees[path]

    btree_fn = btree  # (S8 below rebinds the name btree)

    def id_variant(tid, def_name):
        """'[params]' and / or the parameterized suffix of an id, relative to its def name."""
        _p, _, rest = tid.partition("::")
        i = rest.find("[")
        br = rest[i:] if i >= 0 else ""
        last = (rest[:i] if i >= 0 else rest).split("::")[-1]
        return (last[len(def_name):] if def_name and last.startswith(def_name) else "") + br

    p2p_idx = defaultdict(list)  # (path, class parts) -> [(last part, id)]
    for tid in task.get("PASS_TO_PASS") or []:
        pp, pparts = parse_test_id(tid)
        if pparts:
            p2p_idx[(pp, tuple(pparts[:-1]))].append((pparts[-1], tid))
    groups: dict[tuple, dict] = {}
    for key, rec in seen_tests.items():
        if rec["kind"] == "py":
            gk = ("py", rec["path"], tuple(rec["parts"][:-1]), rec["dpath"], rec["loc"][0])
        elif rec["kind"] == "case":
            gk = ("case", rec["path"], rec["loc"][0])
        else:
            gk = ("none", rec["path"], tuple(rec["parts"]))
        g = groups.setdefault(gk, {"recs": [], "ids": []})
        g["recs"].append(rec)
        g["ids"].extend(rec["ids"])
    status: list[list[str]] = []
    shown_regions = set()
    f2p_base_bodies = []
    f2p_regions, f2p_cases = set(), set()  # base (path, def start) of F2P tests; F2P mypy case names
    n_inherited = 0
    for gk, g in groups.items():
        rec = g["recs"][0]
        path, parts = rec["path"], rec["parts"]
        if rec["kind"] is None:
            status.append([f"- {compress_ids('', g['ids'])}: NOT LOCATED ({rec['note']})"])
            continue
        if rec["kind"] == "case":
            f2p_cases.add(re.sub(r"-(xfail|skip|writescache|only_when_cache|only_when_nocache)$", "", rec["qual"]))
            ids = f" ({len(g['ids'])} ids: " + ", ".join(x + (f" ({ID_CUT_NOTE})" if id_is_cut(x) else "")
                                                          for x in g["ids"]) + ")" if len(g["ids"]) > 1 else ""
            base_t = tree.read(rec["path"])
            fc = find_case(base_t, parts[-1]) if base_t else None
            if fc:
                tap = test_ap.get(rec["path"], (None, {"removed": set(), "inserts": {}}))[1]
                changed = any(fc[0] <= r <= fc[1] for r in tap["removed"]) or \
                    any(fc[0] <= k < fc[1] for k in tap["inserts"])
                flag = re.search(r"-(xfail|skip)$", fc[2])
                status.append([f"- {rec['id']}{ids}: EXISTS AT BASE as [case {fc[2]}] {rec['path']}:{fc[0]}-{fc[1]}; "
                               f"{'edited' if changed else 'unchanged'} by the test patch"
                               + (f"; marked -{flag.group(1)} at base" if flag else "")])
                if not changed and (rec["path"], fc[0]) not in shown_regions:
                    f2p_base_bodies.append((rec["path"], fc[0], fc[1], split_lines(base_t), f"[case {fc[2]}]"))
                    shown_regions.add((rec["path"], fc[0]))
            else:
                status.append([f"- {rec['id']}{ids}: ABSENT AT BASE (case added by the test patch: [case {rec['qual']}])"])
            continue
        fn = rec["node"]
        def_name = fn.name
        cls_parts = list(parts[:-1])
        name = f"{path}::" + "::".join(cls_parts + [def_name])
        variants = [id_variant(t_, def_name) for t_ in g["ids"]]
        own_defs = {x["name"] for x in def_index(ptree(rec["dpath"]))} if ptree(rec["dpath"]) is not None else set()
        sib = []
        for last, tid in p2p_idx.get((path, tuple(cls_parts)), []):
            if last == def_name or (last.startswith(def_name + "_") and re.match(r"^_\d+", last[len(def_name):])
                                    and last not in own_defs):
                sib.append(id_variant(tid, def_name))
        if rec["dpath"] != path or "inherited" in rec["note"]:
            n_inherited += 1
        # base location and status
        if tree.read(path) is None:
            st = "ABSENT AT BASE (test file added by the test patch)"
        else:
            d0, dp0, note0 = locate_py(path, parts, btree, rs)
            if d0 is None:
                st = f"ABSENT AT BASE (function added by the test patch{'; ' + rec['note'] if rec['note'] else ''})"
            else:
                decs = [clip(ast.unparse(x), 160) for x in d0["node"].decorator_list]
                xf = [x for x in decs if re.search(r"xfail|skip", x)]
                tap = test_ap[dp0][1] if dp0 in test_ap else {"removed": set(), "inserts": {}}
                changed = any(d0["start"] <= r <= d0["end"] for r in tap["removed"]) or \
                    any(d0["start"] <= k < d0["end"] for k in tap["inserts"])
                st = (f"EXISTS AT BASE {dp0}:{d0['start']}-{d0['end']} as {d0['qual']}"
                      f"{' (' + note0 + ')' if note0 else ''}; {'edited' if changed else 'unchanged'} by the test patch"
                      + (f"; marks at base: {'; '.join(xf)}" if xf else ""))
                f2p_regions.add((dp0, d0["start"]))
                if not changed and (dp0, d0["start"]) not in shown_regions:
                    f2p_base_bodies.append((dp0, d0["start"], d0["end"], split_lines(tree.read(dp0)), d0["qual"]))
                    shown_regions.add((dp0, d0["start"]))
        entry = [f"- {name}: {st}"]
        if variants != [""] or len(g["ids"]) > 1:
            pre = "" if all(v.startswith("[") for v in variants + sib) else def_name
            rows_ = param_rows(fn, ptree(rec["dpath"]))
            rmap = {f"[{r[0]}]": r for r in rows_} if rows_ else {}
            mapped = bool(rmap) and all(v in rmap for v in variants + sib)
            if mapped:
                def show(vs):
                    return "; ".join(f"{v} = `{rmap[v][1]}`" for v in sorted(set(vs), key=lambda v: rmap[v][2]))
                entry.append(f"    F2P ids ({len(set(variants))}): {show(variants)}")
                if sib:
                    entry.append(f"    PASS_TO_PASS ids of this test ({len(set(sib))}; pass at base with the test patch, "
                                 f"i.e. regression guards): {show(sib)}")
                rest_ = [v for v in rmap if v not in set(variants) | set(sib)]
                if rest_:
                    entry.append(f"    parametrize rows in neither list ({len(rest_)}; xfail / skip, or failing with "
                                 f"the gold): {show(rest_)}")
            else:
                entry.append(f"    F2P ids ({len(set(variants))}): {compress_ids(pre, variants)}")
                if sib:
                    entry.append(f"    PASS_TO_PASS ids of this test ({len(set(sib))}; pass at base with the test patch, "
                                 f"i.e. regression guards): {compress_ids(pre, sib)}")
        status.append(entry)
    n_groups = len(groups)
    b.force(["## 5. F2P tests at base",
             "Purpose: which F2P tests exist at base, every F2P id and the same tests' PASS_TO_PASS ids, and the base "
             "bodies (F2P tests first, then the other tests the test patch edits).",
             f"FAIL_TO_PASS: {len(f2p)} ids in {n_groups} distinct test{'s' if n_groups != 1 else ''}. Id lists: "
             "`a..b` = every integer in the range, `{x,y}` = each listed value, and a bracketed id with sets stands "
             "for every combination of its components."])

    # v4.1: graded status of a base test region the test patch edits, under its post-patch name
    graded_by_name: dict[str, set] = defaultdict(set)  # last id part (and parameterized-suffix-stripped) -> ids
    for kind_, ids_ in (("F2P", f2p), ("P2P", task.get("PASS_TO_PASS") or [])):
        for tid in ids_:
            pp, pparts = parse_test_id(tid)
            if pparts:
                for nm_ in {pparts[-1], re.sub(r"_\d+.*$", "", pparts[-1])}:
                    graded_by_name[nm_].add((kind_, pp, tuple(pparts)))

    def post_map(ap):
        """base line -> post-patch line, for the base lines the patch keeps."""
        m_, j = {}, len(ap["inserts"].get(0, []))
        for i in range(1, len(ap["base"]) + 1):
            if i not in ap["removed"]:
                j += 1
                m_[i] = j
            j += len(ap["inserts"].get(i, []))
        return m_

    def post_unit(ap, s, e, head, unit_at):
        """Post-patch name of a base test region: the unit at the kept head line (def / case header line); if the
        test patch rewrites the head, the post unit holding most of the region's kept non-blank lines, provided they
        are at least half of the region's non-blank lines (a rename); else None (the test patch removes the test)."""
        m_ = post_map(ap)
        if head in m_:
            u = unit_at(m_[head])
            if u is not None and u[1] == m_[head]:
                return u[0]
        nb = [i for i in range(s, e + 1) if ap["base"][i - 1].strip()]
        votes = Counter(u for u in (unit_at(m_[i]) for i in nb if i in m_) if u is not None)
        if votes:
            (nm_, _l), c = min(votes.items(), key=lambda kv: (-kv[1], kv[0][1]))
            if 2 * c >= len(nb):
                return nm_
        return None

    def graded_py(path, ap, s, e, head):
        """(post-patch qualname, 'F2P' / 'P2P' / None); qualname None = the test patch removes the test, '?' = the
        post-patch file does not parse."""
        pt = ptree(path)
        if pt is None:
            return "?", None
        pdefs = def_index(pt)

        def unit_at(j):
            f_, _c = unit_of(pdefs, j)
            return (f_["qual"], f_["node"].lineno) if f_ is not None else None
        pq = post_unit(ap, s, e, head, unit_at)
        if pq is None:
            return None, None
        hit = None
        for kind_, pp, pparts in sorted(graded_by_name.get(pq.split(".")[-1], ())):
            if hit == "F2P" or (hit == "P2P" and kind_ == "P2P"):
                break
            d_, dp_, _n = locate_py(pp, list(pparts), ptree, rs_post)
            if d_ is not None and dp_ == path and d_["qual"] == pq:
                hit = kind_
        return pq, hit

    def graded_case(path, ap, s, e):
        phdr = [(j + 1, mm.group(1)) for j, l in enumerate(ap["post"]) for mm in [CASE_HDR.match(l)] if mm]
        starts = [h[0] for h in phdr]

        def unit_at(j):
            k = bisect.bisect_right(starts, j) - 1
            return (phdr[k][1], phdr[k][0]) if k >= 0 else None
        pn = post_unit(ap, s, e, s, unit_at)
        if pn is None:
            return None, None
        pn = re.sub(r"-(xfail|skip|writescache|only_when_cache|only_when_nocache)$", "", pn)
        kinds = {kind_ for nm_ in (pn, pn + "_cached") for kind_, pp, pparts in graded_by_name.get(nm_, ())
                 if not any(x.endswith(".test") for x in pparts) or os.path.basename(path) in pparts}
        return pn, "F2P" if "F2P" in kinds else "P2P" if "P2P" in kinds else None

    s5_labels = Counter()  # v4.1 statistics: labels of the edited base test regions

    def s5_label(is_f2p, base_name, post_name, kind_):
        """(label, hunks only?) of an edited base test region."""
        ren = f" as `{post_name}` (the test patch renames it)" if post_name not in (None, "?", base_name) else ""
        if is_f2p or kind_ == "F2P":
            return "F2P" + ren, False
        if post_name == "?":
            return "not F2P", False
        if post_name is None:
            return "not F2P; removed by the test patch, so not graded", True
        if kind_ == "P2P":
            return "not F2P; in PASS_TO_PASS" + ren, False
        return "not F2P; not graded by the harness (in neither FAIL_TO_PASS nor PASS_TO_PASS)" + ren, True

    def elastic(name, hdr, lines, s, e, focus, marks, is_f2p, hunks=False):
        if hunks:  # v4.1: a test the harness does not grade: only the test-patch hunks +- S5_UNGRADED_RADIUS lines
            sel = sorted({i for f_ in focus for i in range(max(s, f_ - S5_UNGRADED_RADIUS),
                                                            min(e, f_ + S5_UNGRADED_RADIUS) + 1)}) or [s]
            nh = len(sel)

            def render_h(L):
                keep = sel[:L]
                note = "" if nh <= L else \
                    f" [elided to ~{L} of {nh} hunk lines ({'content cap' if L >= S5_BODY_LINES else 'budget'})]"
                rows, prev = [], s - 1
                for i in keep:
                    if i != prev + 1:
                        rows.append((0, f"... [base lines {prev + 1}-{i - 1} not shown] ..."))
                    rows.append((i, lines[i - 1]))
                    prev = i
                if prev < e:
                    rows.append((0, f"... [base lines {prev + 1}-{e} not shown] ..."))
                return [hdr + note] + code_block(rows, marks)
            return {"name": name, "fixed": None, "render": render_h, "n": nh, "f2p": is_f2p}
        n = e - s + 1

        def render(L):
            note = "" if n <= L else \
                f" [elided to ~{L} of {n} lines ({'content cap' if L >= S5_BODY_LINES else 'budget'})]"
            if focus:
                rows = cap_body(lines, s, e, focus, L)
            elif n <= L:
                rows = [(i, lines[i - 1]) for i in range(s, e + 1)]
            else:  # no focus: head and tail
                h = max(1, L - 5)
                rows = [(i, lines[i - 1]) for i in range(s, s + h)] + \
                    [(0, f"... [base lines {s + h}-{e - 5} elided] ...")] + \
                    [(i, lines[i - 1]) for i in range(e - 4, e + 1)]
            return [hdr + note] + code_block(rows, marks)
        return {"name": name, "fixed": None, "render": render, "n": n, "f2p": is_f2p}

    items5 = []
    for path, (f, ap) in sorted(test_ap.items()):
        if f["is_new"]:
            items5.append({"name": path, "f2p": False, "fixed": [f"### {path}: NEW FILE in the test patch (absent at "
                                                                 f"base, +{len(ap['added'])} lines)"]})
            continue
        if f["is_deleted"]:
            items5.append({"name": path, "f2p": False, "fixed": [f"### {path}: deleted by the test patch"]})
            continue
        base_text = base_texts.get(f["old"])
        lines = ap["base"]
        if path.endswith(".test"):
            hdrs = [(i + 1, m.group(1)) for i, l in enumerate(lines) for m in [CASE_HDR.match(l)] if m]
            regions = {}
            anchors = sorted(ap["removed"]) + sorted(ap["inserts"])
            for a in anchors:
                prev = [h for h in hdrs if h[0] <= max(a, 1)]
                if prev and a >= 1:
                    k = hdrs.index(prev[-1])
                    end = hdrs[k + 1][0] - 1 if k + 1 < len(hdrs) else len(lines)
                    if a < end or a in ap["removed"]:
                        regions[prev[-1][0]] = (prev[-1][0], end, prev[-1][1])
            marks = {r: "*" for r in ap["removed"]} | {k: ">" for k in ap["inserts"] if k not in ap["removed"]}
            if not regions:
                items5.append({"name": path, "f2p": False, "fixed": [f"### {path}: test patch only appends new cases "
                                                                     "(no base case edited)"]})
            for s, e, cn in sorted(regions.values()):
                if (path, s) in shown_regions:
                    continue
                shown_regions.add((path, s))
                cn0 = re.sub(r"-(xfail|skip|writescache|only_when_cache|only_when_nocache)$", "", cn)
                is_f2p = cn0 in f2p_cases
                pn, gkind = (cn0, "F2P") if is_f2p else graded_case(path, ap, s, e)
                label, hunks = s5_label(is_f2p, cn0, pn, gkind)
                is_f2p = is_f2p or gkind == "F2P"
                # v4.1: an insertion that starts a new case is not an edit of this one (it only follows it)
                foc = {a for a in anchors if s <= a <= e}
                added_after = [m_.group(1) for a in sorted(foc) if a not in ap["removed"]
                               for t_ in [next((x for x in ap["inserts"].get(a, []) if x.strip()), "")]
                               for m_ in [CASE_HDR.match(t_)] if m_]
                real = {a for a in foc if a in ap["removed"] or not CASE_HDR.match(
                    next((x for x in ap["inserts"].get(a, []) if x.strip()), ""))}
                if real:
                    how = "edited by the test patch"
                else:
                    added_l = ", ".join(f"[case {x}]" + (" (F2P)" if re.sub(r"-\w+$", "", x) in f2p_cases else "")
                                        for x in added_after)
                    how, hunks = f"not edited; the test patch adds {clip(added_l, 300)} right after it", False
                s5_labels[label.split(" as `")[0] + ("; neighbour of an added case" if not real else "")] += 1
                items5.append(elastic(f"{path} [case {cn}]", f"### {path} [case {cn}] at base (L{s}-{e}; "
                                      f"{label}; {how}"
                                      + (f"; only the test-patch hunks +-{S5_UNGRADED_RADIUS} lines shown)" if hunks
                                         else ")"),
                                      lines, s, e, real or foc, marks, is_f2p, hunks))
            continue
        ptree_, info, focus, marks, rem, ins = edited_units(base_text, ap, path)
        for key in sorted(info, key=lambda k: (min(focus[k]), k)):
            val = info[key]
            if key[0] in ("module", "class"):
                where = f"{path} (module level)" if key[0] == "module" else f"{path}::{key[1]} (class body)"
                blk = []
                if ins[key]:
                    blk.append(f"- {where}: insertions {insert_summary(ap, ins[key])}")
                if rem[key]:
                    rows = []
                    stmts = []
                    if key[0] == "module" and ptree_ is not None:
                        stmts = [(node_start(st), st.end_lineno) for st in ptree_.body
                                 if any(node_start(st) <= r <= st.end_lineno for r in rem[key])]
                    elif key[0] == "class" and val[3] is not None:
                        stmts = [(node_start(st), st.end_lineno) for st in val[3]["node"].body
                                 if any(node_start(st) <= r <= st.end_lineno for r in rem[key])]
                    if stmts:
                        for s_, e_ in stmts:
                            rows.extend(cap_body(lines, s_, e_, rem[key], S5_STMT_LINES))
                    else:
                        for f_ in sorted(rem[key]):
                            rows.extend((i, lines[i - 1]) for i in range(max(1, f_ - 3), min(len(lines), f_ + 3) + 1))
                        rows = sorted(set(rows))
                    blk += [f"### {where}: statements the test patch edits, at base"] + code_block(rows, marks)
                if blk:
                    items5.append({"name": where, "f2p": False, "fixed": blk})
                continue
            s, e, name, d = val
            if (path, s) in shown_regions:
                continue
            shown_regions.add((path, s))
            is_f2p = (path, s) in f2p_regions
            if not is_f2p and not key[1].split(".")[-1].startswith("test"):
                label, hunks = "not F2P; not a test (helper, fixture or set-up)", False
            else:
                head = d["node"].lineno if d is not None else s
                pq, gkind = (key[1], "F2P") if is_f2p else graded_py(path, ap, s, e, head)
                label, hunks = s5_label(is_f2p, key[1], pq, gkind)
                is_f2p = is_f2p or gkind == "F2P"
            s5_labels[label.split(" as `")[0]] += 1
            xf = [l.strip() for l in lines[s - 1:e] if re.search(r"xfail|\bskip", l)]
            hdr = f"### {path}::{key[1]} at base (L{s}-{e}; {label}; edited by the test patch" \
                + (f"; only the test-patch hunks +-{S5_UNGRADED_RADIUS} lines shown" if hunks else "") + ")" \
                + (f"; xfail/skip at base: {clip(' | '.join(xf), 200)}" if xf else "")
            items5.append(elastic(f"{path}::{key[1]}", hdr, lines, s, e, focus[key], marks, is_f2p, hunks))
    for path, s, e, lines, nm in f2p_base_bodies:
        items5.append(elastic(f"{path}::{nm}", f"### {path}::{nm} at base (L{s}-{e}; F2P; unchanged by the test patch)",
                              lines, s, e, set(), {}, True))
    # F2P first (stable within each group)
    items5 = [u for u in items5 if u["f2p"]] + [u for u in items5 if not u["f2p"]]

    def render5(L, its):
        return [u["fixed"] if u["fixed"] is not None else u["render"](L) for u in its]

    if any(re.match(r"^[*>] *\d+\| ", l_) for blk in render5(S5_BODY_LINES, items5) for l_ in blk):
        b.force(["Markers in this section: `*` base line the test patch removes or rewrites, `>` the test patch "
                 "inserts lines after this base line."])  # v4.1: the header legend is about the gold patch
    room = b.left - b.reserve
    floor_demand = sum(cost(x) for x in render5(S5_BODY_FLOOR, [u for u in items5 if u["f2p"]]))
    st_avail = max(room // 2, room - floor_demand) if b.cap < UNLIMITED else UNLIMITED
    used = 0
    for k, x in enumerate(status):
        c = cost(x)
        if used + c > st_avail or not b.add(x):
            b.force([f"- ... {len(status) - k} more F2P status entries omitted (budget)"])
            omit("s5", "F2P status entry", len(status) - k)
            break
        used += c
    b.force([""])
    s5_omitted = []
    for grp in ([u for u in items5 if u["f2p"]], [u for u in items5 if not u["f2p"]]):
        if not grp:
            continue
        avail = b.left - b.reserve
        hi = max([S5_BODY_FLOOR] + [min(u["n"], S5_BODY_LINES) for u in grp if u["fixed"] is None])
        L = fill_cap(lambda L_, g_=grp: render5(L_, g_), S5_BODY_FLOOR, hi, avail) if b.cap < UNLIMITED else hi
        if L is not None:
            for blk in render5(L, grp):
                b.force(blk)
            n_el = sum(1 for u in grp if u["fixed"] is None and min(u["n"], S5_BODY_LINES) > L)
            trunc("s5", "test body elided", n_el, f"to ~{L} lines")
        else:
            used = 0
            for u, blk in zip(grp, render5(S5_BODY_FLOOR, grp)):
                c = cost(blk)
                if used + c <= avail:
                    b.force(blk)
                    used += c
                else:
                    s5_omitted.append(u["name"])
            n_el = sum(1 for u in grp if u["fixed"] is None and u["n"] > S5_BODY_FLOOR and u["name"] not in s5_omitted)
            trunc("s5", "test body elided", n_el, f"to ~{S5_BODY_FLOOR} lines")
    if s5_omitted:
        b.force([f"- base test bodies / test-patch regions omitted (budget): {clip(', '.join(s5_omitted), 600)}"])
        omit("s5", "test body", len(s5_omitted), clip(", ".join(s5_omitted), 300))
    done("s5", b)


    # ---- S3 callers: top S3_TOP_K per edited function; the budget may lower the per-function count to S3_FLOOR
    b = take("s3")
    b.force([f"## 3. Callers at base of the edited functions (top {S3_TOP_K} each; lexical match)",
             "Purpose: who depends on the edited functions (a fix there changes these call sites too)."])
    n_callers = 0
    blocks3 = []
    for path, qual, name, d, s, e in edited_funcs:
        if name.startswith("__") and name.endswith("__"):
            if name == "__init__" and "." in qual:
                cname = qual.split(".")[-2]
                pat, label = cname, f"{cname}(...) instantiations"
            else:
                blocks3.append({"name": f"{path}::{qual}", "fixed": [f"### {path}::{qual}: special method (implicit "
                                                                     "calls; not searched)"]})
                continue
        else:
            pat, label = name, f"{name}(...) calls"
        offs = corpus.find(pat, word=True, cap=50000)
        hits = []
        decos = [ast.unparse(x) for x in d["node"].decorator_list] if d is not None else []
        is_prop = any(re.search(r"property|cache_readonly|readonly|\.setter|\.getter|cached", x) for x in decos)
        if is_prop:
            label = f".{name} attribute accesses"
        for off in offs:
            j = off + len(pat)
            rest = corpus.big[j:j + 40]
            if not (rest.lstrip(" ").startswith("(") or (is_prop and corpus.big[off - 1] == ".")):
                continue
            lt = corpus.line_text(off)
            if re.match(r"^\s*(async\s+)?(def|class|cdef|cpdef)\s", lt) and re.search(r"\b(def|class)\s+" + re.escape(pat) + r"\b", lt):
                continue
            fi = corpus.file_of(off)
            if corpus.cats[fi] == "doc":
                continue
            remark = bool(re.match(r"^\s*(#|>>>|\.\.\.)", lt))
            hits.append((0 if corpus.paths[fi] == path else 1, CAT_RANK[corpus.cats[fi]], remark, corpus.paths[fi], off,
                         fi))
        hits.sort()
        rows3 = []
        for _, _, _, p, off, fi in hits[:S3_TOP_K]:
            ln = corpus.lineno(fi, off)
            enc = enclosing_name(corpus.text(fi).split("\n"), ln)
            rows3.append(f"- {p}:{ln} [in {enc}] {clip(corpus.line_text(off).strip(), 180)}")
        blocks3.append({"name": f"{path}::{qual}", "fixed": None, "hdr": f"### {path}::{qual}: {len(hits)} {label}",
                        "rows": rows3, "n": len(hits)})

    def render3(K):
        out = []
        for u in blocks3:
            if u["fixed"] is not None:
                out.append(u["fixed"])
                continue
            k = min(K, len(u["rows"]))
            note = "" if u["n"] <= k else f" (top {k} shown{'' if k >= S3_TOP_K else ', budget'})"
            out.append([u["hdr"] + note] + u["rows"][:k])
        return out

    avail = b.left - b.reserve
    K = fill_cap(render3, S3_FLOOR, S3_TOP_K, avail) if blocks3 else S3_TOP_K
    s3_omitted = []
    if K is not None:
        for blk in render3(K):
            b.force(blk)
        n_callers = sum(min(K, len(u["rows"])) for u in blocks3 if u["fixed"] is None)
        trunc("s3", "caller list shortened", sum(1 for u in blocks3 if u["fixed"] is None and
                                                              min(u["n"], S3_TOP_K) > K), f"to top {K}")
    else:
        used = 0
        for u, blk in zip(blocks3, render3(S3_FLOOR)):
            c = cost(blk)
            if used + c <= avail:
                b.force(blk)
                used += c
                n_callers += min(S3_FLOOR, len(u.get("rows", [])))
            else:
                s3_omitted.append(u["name"])
        trunc("s3", "caller list shortened", sum(1 for u in blocks3 if u["fixed"] is None and
                                                              min(u["n"], S3_TOP_K) > S3_FLOOR and
                                                              u["name"] not in s3_omitted), f"to top {S3_FLOOR}")
    if s3_omitted:
        b.force([f"- callers of these edited functions omitted (budget): {clip(', '.join(s3_omitted), 600)}"])
        omit("s3", "caller list", len(s3_omitted), clip(", ".join(s3_omitted), 300))
    if not edited_funcs:
        b.force(["(no edited function at base: the gold patch edits only module / class level code or new files)"])
    done("s3", b)

    # ---- S4 raise / warn: first S4_PER_MODULE per edited module (edited functions first); budget: equal share of
    # what is left per module
    b = take("s4")
    b.force(["## 4. raise / warn statements in the edited modules (base; `*` = inside an edited function)",
             "Purpose: the error / warning types and messages the edited modules already use."])
    n_raises = 0
    mods4 = [(path, info) for path, info in sorted(module_info.items()) if not info.get("new")]
    data4 = []
    for path, info in mods4:
        t = info.get("tree")
        ranges = [(s, e) for (p, q, n, d, s, e) in edited_funcs if p == path]
        if t is None:
            lines = info["lines"]
            rows = [(0 if any(a <= i <= z for a, z in ranges) else 1, i, l.strip(), any(a <= i <= z for a, z in ranges))
                    for i, l in enumerate(lines, 1) if re.match(r"^\s*raise\s", l) or re.search(r"\bwarnings\.warn\(", l)]
            rows.sort(key=lambda r: (r[0], r[1]))
        else:
            rows = raises_in(t, info["lines"], repo, ranges)
        lines4 = [f"{'*' if inside else '-'} L{ln} [{enclosing_name(info['lines'], ln)}] {clip(msg, 220)}"
                  for _, ln, msg, inside in rows[:S4_PER_MODULE]]
        data4.append((path, [f"### {path} ({len(rows)})"], lines4, len(rows)))
    dem = [cost(h) + sum(cost([x]) for x in l4) for _, h, l4, _ in data4]
    al4 = water(dem, b.left - b.reserve) if b.cap < UNLIMITED else [UNLIMITED] * len(dem)
    for (path, blk, lines4, n_rows), quota in zip(data4, al4):
        used = cost(blk)
        b.force(blk)
        shown = 0
        for line in lines4:
            c = cost([line])
            if used + c > quota or not b.add([line]):
                break
            used += c
            shown += 1
        n_raises += shown
        cut_budget = len(lines4) - shown
        if cut_budget:
            b.force([f"- ... {cut_budget} more raise / warn lines omitted (budget)"
                     + (f"; {n_rows - S4_PER_MODULE} beyond the first {S4_PER_MODULE} not listed"
                        if n_rows > S4_PER_MODULE else "")])
            omit("s4", "raise/warn line", cut_budget, path)
        elif n_rows > shown:
            b.force([f"- ... {n_rows - shown} more not listed (first {S4_PER_MODULE} per module, edited functions "
                     "first)"])
    done("s4", b)

    # ---- S6 install record
    b = take("s6")
    b.force(["## 6. Image install record",
             "Purpose: the dependency versions the task image installs (library behaviour the tests rely on)."])
    spec = (harness.get(repo) or harness.get(repo.lower()) or {}).get(task.get("version"))
    if spec is None:
        b.force([f"No SWE-Gym harness spec for {repo} version {task.get('version')}; no pip freeze recorded."])
    else:
        flat = json.dumps(spec)
        pins = sorted({f"{m.group(1)}{m.group(2)}{m.group(3)}" for m in PIN.finditer(flat)})
        b.force([f"SWE-Gym harness spec ({repo} {task.get('version')}): python {spec.get('python', 'unspecified')}; "
                 f"packages: {spec.get('packages', '-')}; test_cmd: {spec.get('test_cmd', '-')}",
                 f"Pins in the install commands: {', '.join(pins) if pins else 'none'}"])
        reqs = set()
        for m in REQ_FILE.finditer(flat):
            reqs.add(m.group(1) or m.group(2))
        if isinstance(spec.get("packages"), str) and spec["packages"].endswith((".yml", ".yaml", ".txt")):
            reqs.add(spec["packages"])
        for r in sorted(reqs):
            t = tree.read(r)
            if t is None:
                b.add([f"- base-tree {r}: absent"])
                continue
            pins = []
            for l in t.split("\n"):
                l2 = l.split("#", 1)[0].strip().lstrip("- ").strip()
                if PIN.search(l2) or re.match(r"^python\s*[=<>]", l2):
                    pins.append(l2)
            if not b.add([f"- base-tree {r}: {len(pins)} pinned of {sum(1 for l in t.split(chr(10)) if l.strip() and not l.strip().startswith('#'))} lines: "
                          + clip(", ".join(pins), S6_PIN_CHARS)]):
                b.force([f"- base-tree {r}: pins omitted (budget)"])
                omit("s6", "requirement-file pin list", 1, r)
        b.add(["(Versions come from the install recipe and the base tree's requirement files; the images' actual "
               "`pip freeze` is not recorded.)"])
    done("s6", b)
    # ---------------------------------------------------------- sections 7-9 (v2 content rules, own budgets)
    f2p_idents = {it["value"] for it in items.values()
                  if it["kind"] == "ident" and any(r.startswith("F2P") for r in it["roles"])}
    edited_spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for path_, qual_, name_, d_, s_, e_ in edited_funcs:
        edited_spans[path_].append((s_, e_))

    def in_s2(path, line):
        return any(a <= line <= z for a, z in edited_spans.get(path, []))

    # ---- S7 unedited members of the classes the gold patch edits
    b = take("s7")
    b.force(["## 7. Unedited members of the classes the gold patch edits (base)",
             "Purpose: the rest of each edited class (state and protocol the fix must stay consistent with).",
             "Methods and attributes of each edited class that the gold patch does not touch, most coupled to the edited "
             "lines first (shares a self.<attribute> with the edited lines, calls or is called by an edited method, "
             "implicit protocol method, name used by an F2P test): the top members with their first lines, the rest as "
             "signatures."])
    classes = {}
    for path, info in sorted(module_info.items()):
        t = info.get("tree")
        if t is None:
            continue
        cdefs = {d["qual"]: d for d in def_index(t) if d["kind"] == "class" and "func" not in d["parents"]}
        for key in info["info"]:
            cq = key[1] if key[0] == "class" else key[1].rsplit(".", 1)[0] if key[0] == "func" and "." in key[1] else None
            if cq in cdefs:
                classes.setdefault((path, cq), cdefs[cq])
    n_s7_full = n_s7_sig = 0
    s7_class_lines, s7_full_shown = set(), set()
    cls_data, s7_rule_full = [], set()
    for (path, cq), cd in sorted(classes.items(), key=lambda kv: (kv[0][0], kv[1]["start"])):
        info, ap = module_info[path], gold_ap[path][1]
        lines, cls = info["lines"], cd["node"]
        s7_class_lines.add((path, cls.lineno))
        edited_names = {k[1].rsplit(".", 1)[1] for k in info["info"]
                        if k[0] == "func" and k[1].startswith(cq + ".") and k[1].count(".") == cq.count(".") + 1}
        touched = [lines[r - 1] for r in ap["removed"] if cd["start"] <= r <= cd["end"]]
        touched += [x for k, v in ap["inserts"].items() if cd["start"] <= k <= cd["end"] for x in v]
        attrs = {a for l in touched for a in re.findall(r"\bself\.(\w+)", l)}
        meths = [m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        ed_text = "\n".join("\n".join(lines[m.lineno - 1:m.end_lineno]) for m in meths if m.name in edited_names)
        if not attrs:
            attrs = set(re.findall(r"\bself\.(\w+)", ed_text))
        called_by_edited = set(re.findall(r"\bself\.(\w+)\s*\(", ed_text))
        rows = []
        for m in meths:
            if m.name in edited_names:
                continue
            txt = "\n".join(lines[node_start(m) - 1:m.end_lineno])
            score, why = 0, []
            sh = sorted(set(re.findall(r"\bself\.(\w+)", txt)) & attrs)
            if sh:
                score += 3
                why.append("uses " + ", ".join("self." + x for x in sh[:4]))
            calls = set(re.findall(r"\bself\.(\w+)\s*\(", txt)) & edited_names
            if calls:
                score += 2
                why.append("calls " + ", ".join(sorted(calls)))
            if m.name in called_by_edited:
                score += 2
                why.append("called by an edited method")
            if m.name in f2p_idents:
                score += 2
                why.append("name in an F2P test")
            if m.name.startswith("__") and m.name.endswith("__"):
                score += 1
                why.append("implicit protocol")
            elif not m.name.startswith("_"):
                score += 1
            rows.append((-score, m.lineno, m, why))
        rows.sort(key=lambda r: (r[0], r[1]))
        cattrs = [m for m in cls.body if isinstance(m, (ast.Assign, ast.AnnAssign))
                  and not any(m.lineno <= r <= m.end_lineno for r in ap["removed"])]
        hdr = [f"### {path}::{cq} (base L{cd['start']}-{cd['end']}; gold edits "
               f"{', '.join(sorted(edited_names)) or 'the class body'}; {len(rows)} unedited methods, "
               f"{len(cattrs)} class attributes)"]
        if cattrs:
            hdr.append("- class attributes: " + clip("; ".join(f"L{a.lineno} {ast.unparse(a)}" for a in cattrs), 700))
        full = [r for r in rows if -r[0] >= 2][:4] or rows[:1]
        s7_rule_full |= {(path, r[2].lineno) for r in full}
        shown_full = {id(r[2]) for r in full}
        rest = [r for r in rows if id(r[2]) not in shown_full]
        cls_data.append((path, cq, lines, hdr, full, rest))

    def render_cls(cdata, cb, rec):
        path, cq, lines, hdr, full, rest = cdata
        if not cb.add(hdr):
            cb.force([f"### {path}::{cq}: members omitted (budget)"])
            rec["omit"].append(("edited-class member list", 1, f"{path}::{cq}"))
            return
        for r in full:
            blk = [f"- {r[2].name} [{'; '.join(r[3]) or 'unedited'}]"] + code_block(def_head_rows(lines, r[2], 3, 10))
            if cb.add(blk):
                rec["full"] += 1
                rec["shown"].add((path, r[2].lineno))
            elif cb.add([f"- {short_member(lines, r[2])} [{'; '.join(r[3])}]"]):
                rec["sig"] += 1
                rec["trunc"] += 1
            else:
                cb.force([f"- coupled member {r[2].name} omitted (budget)"])
                rec["omit"].append(("coupled member", 1, f"{path}::{cq}.{r[2].name}"))
        for k, r in enumerate(rest[:30]):
            if not cb.add([f"- {short_member(lines, r[2])}" + (f" [{'; '.join(r[3])}]" if r[3] and -r[0] >= 2 else "")]):
                cb.force([f"- ... {min(30, len(rest)) - k} more member signatures omitted (budget)"])
                rec["omit"].append(("member signature", min(30, len(rest)) - k, f"{path}::{cq}"))
                break
            rec["sig"] += 1
        if len(rest) > 30:
            cb.force([f"- ... {len(rest) - 30} more methods (first 30 signatures listed)"])

    def new_rec():
        return {"full": 0, "sig": 0, "trunc": 0, "omit": [], "shown": set()}

    demands = []
    for cdata in cls_data:
        cb = Budget(UNLIMITED)
        render_cls(cdata, cb, new_rec())
        demands.append(cb.used)
    alloc = water(demands, b.left - b.reserve) if b.cap < UNLIMITED else [UNLIMITED] * len(cls_data)
    n_trunc7 = 0
    for cdata, a_ in zip(cls_data, alloc):
        cb, rec = Budget(a_), new_rec()
        render_cls(cdata, cb, rec)
        b.absorb(cb)
        n_s7_full += rec["full"]
        n_s7_sig += rec["sig"]
        n_trunc7 += rec["trunc"]
        s7_full_shown |= rec["shown"]
        for typ, n, det in rec["omit"]:
            omit("s7", typ, n, det)
    trunc("s7", "coupled member as signature only", n_trunc7)
    if not classes:
        b.force(["(the gold patch edits no class: module-level functions or new files only)"])
    done("s7", b)

    # ---- S9 contracts of the repository symbols the F2P tests use
    b = take("s9")
    b.force(["## 9. Contracts of the repository symbols the F2P tests use (base definitions, any module)",
             "Purpose: what the repository code the F2P tests call promises (signatures, docstrings, fixtures).",
             "Names the F2P tests import from the repository, resolved through re-exports to their base definitions, "
             "and the tests' pytest fixtures: signature, docstring head and first lines (classes: members as one-line "
             "signatures). Roles: in an assertion / feeds an assertion (assigned to a name an assertion reads) / on "
             "test-patch lines. Definitions whose bodies section 2 shows, or classes section 7 covers, are only named."])
    if "sidx" not in ctx:
        ctx["sidx"] = SrcIndex(corpus)
    sidx = ctx["sidx"]
    syms: dict[tuple[str, int], dict] = {}
    seen_fn = set()
    for key, rec in seen_tests.items():
        if rec.get("kind") != "py" or rec.get("node") is None or id(rec["node"]) in seen_fn:
            continue
        seen_fn.add(id(rec["node"]))
        tpath, fn, alines = rec["dpath"], rec["node"], rec["alines"]
        aread = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                 and n.lineno in alines}
        feed = assert_feed_lines(fn, alines)
        added = test_ap[tpath][1]["added"] if tpath in test_ap else set()
        uses = test_symbol_uses(ptree(tpath), [fn], tpath, rs.mods)
        found = []
        for (m, a), lns in sorted(uses.items()):
            r = rs.resolve(m, a)
            if r is None or r[0] == "module" or r[1] == tpath:
                continue
            found.append((a, m, r, lns))
        pnames = set()
        for dec in fn.decorator_list:
            if isinstance(dec, ast.Call) and (attr_chain(dec.func) or [""])[-1] == "parametrize" and dec.args \
                    and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                pnames |= {x.strip() for x in dec.args[0].value.split(",")}
            elif isinstance(dec, ast.Call) and (attr_chain(dec.func) or [""])[-1] == "parametrize" and dec.args \
                    and isinstance(dec.args[0], (ast.List, ast.Tuple)):
                pnames |= {x.value for x in dec.args[0].elts if isinstance(x, ast.Constant)}
        fa = fn.args
        for arg in [x.arg for x in fa.posonlyargs + fa.args + fa.kwonlyargs]:
            if arg in ("self", "cls", "request") or arg in pnames:
                continue
            fx = find_fixture(corpus, arg, tpath)
            if fx is None:
                continue
            fpath = corpus.paths[fx[0]]
            ft, flines = rs.parsed(fpath)
            node = next((n for n in ast.walk(ft) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n.lineno == fx[1]), None) if ft is not None else None
            if node is not None:
                found.append((arg, "fixture", ("fixture", fpath, node, flines), [fn.lineno]))
        for a, m, r, lns in found:
            kind, dp, node, dlines = r
            e = syms.setdefault((dp, node.lineno), {"name": a, "mod": m, "r": r, "lines": set(), "tests": set(),
                                                    "assert": False, "feed": False, "added": False})
            e["lines"] |= set(lns)
            e["tests"].add(fn.name)
            e["assert"] |= any(l in alines for l in lns)
            e["feed"] |= any(l in feed for l in lns)
            e["added"] |= any(l in added for l in lns)
    order = sorted(syms.items(), key=lambda kv: (
        0 if kv[1]["assert"] or kv[1]["feed"] else 1, 1 if kv[1]["mod"] == "fixture" else 0,
        0 if category(kv[0][0]) == "src" else 1, 0 if kv[1]["added"] else 1, sidx.df.get(kv[1]["name"], 0),
        min(kv[1]["lines"]), kv[0]))
    n_s9 = 0
    named_only, blocks9 = [], []
    for k, ((dp, dl), e) in enumerate(order[:16]):
        kind, _, node, dlines = e["r"]
        roles = ", ".join(x for x, f in (("in an assertion", e["assert"]), ("feeds an assertion", e["feed"]),
                                         ("test-patch lines", e["added"])) if f) or "test body"
        where = f"{dp}:L{node_start(node)}-{node.end_lineno}"
        label = "fixture" if kind == "fixture" else "class" if isinstance(node, ast.ClassDef) else \
            "function" if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else "assignment"
        src_name = "pytest fixture" if kind == "fixture" else e["mod"]
        if in_s2(dp, node.lineno):
            named_only.append(f"`{e['name']}` -> {where} (body in section 2; {roles})")
            continue
        if (dp, node.lineno) in s7_class_lines:
            named_only.append(f"`{e['name']}` -> {where} (class edited by the gold patch; section 7; {roles})")
            continue
        hdr = f"### `{e['name']}` ({src_name}) -> {where} ({label}; used in {', '.join(sorted(e['tests']))[:120]}; {roles})"
        if isinstance(node, ast.ClassDef):
            rows = def_head_rows(dlines, node, 8, 0)
            mem = class_member_rows(dlines, node, 25)
            blk = [hdr] + code_block(rows) + (["members:"] + mem if mem else [])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            blk = [hdr] + code_block(def_head_rows(dlines, node, 8, 8))
        else:
            blk = [hdr] + code_block([(i, dlines[i - 1]) for i in range(node.lineno, min(node.end_lineno,
                                                                                         node.lineno + 5) + 1)])
        short = [hdr + " [shortened: budget]"] + code_block(
            def_head_rows(dlines, node, 2, 2) if hasattr(node, "body") else [(node.lineno, dlines[node.lineno - 1])])
        blocks9.append((e["name"], blk, short))
    tail9 = []
    if named_only:
        tail9.append(["- " + x for x in named_only])
    if len(order) > 16:
        tail9.append([f"- ... {len(order) - 16} more symbols (first 16 shown): "
                      + clip(", ".join(e["name"] for _, e in order[16:]), 400)])
    tail_cost = sum(cost(x) for x in tail9)
    omitted9, n_short9 = [], 0
    for name9, blk, short in blocks9:
        if b.fits(cost(blk) + tail_cost):
            b.add(blk)
            n_s9 += 1
        elif b.fits(cost(short) + tail_cost):
            b.add(short)
            n_s9 += 1
            n_short9 += 1
        else:
            omitted9.append(name9)
    if omitted9:
        b.force([f"- symbols omitted (budget): {clip(', '.join(omitted9), 400)}"])
        omit("s9", "test-symbol contract", len(omitted9), clip(", ".join(omitted9), 300))
    trunc("s9", "test-symbol contract shortened", n_short9)
    for x in tail9:
        b.force(x)
    if not syms:
        b.force(["(no repository symbol or fixture resolved: the F2P tests are data-driven cases or use only "
                 "standard-library / third-party names)"])
    done("s9", b)

    # ---- S8 sibling code
    b = take("s8")
    b.force(["## 8. Sibling code at base (code the gold patch does not edit that does the same kind of thing)",
             "Purpose: how the codebase already does the same kind of thing elsewhere (conventions a fix would copy)."])
    new_def_names = {re.sub(r"^.*\.", "", m.group(1)) for x in new_defs
                     for m in [re.search(r": (?:async def|def|class) ([\w.]+)", x)] if m}
    edited_names_all = {nm for (_p, _q, nm, _d, _s, _e) in edited_funcs}
    helpers: dict[str, dict] = {}
    E_ids: set[str] = set()

    def call_name(n):
        return n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id if isinstance(n.func, ast.Name) else None

    def recv_tail(n):
        """`path` for fs.path.relpath(...), `` for a bare call, None for other receivers (calls, subscripts)."""
        if isinstance(n.func, ast.Name):
            return ""
        v = n.func.value
        return v.attr if isinstance(v, ast.Attribute) else v.id if isinstance(v, ast.Name) else None

    def keep_helper(nm):
        return nm and nm not in SKIP_NAMES and len(nm) > 2 and nm not in edited_names_all and nm not in new_def_names \
            and not (nm.startswith("__") and nm.endswith("__"))

    for path, (f, ap) in sorted(gold_ap.items()):
        if not path.endswith(".py"):
            continue
        info = module_info.get(path, {})
        base_lines, btree = info.get("lines") or [], info.get("tree")
        anchors = set(ap["removed"]) | {k for k in ap["inserts"] if k > 0}
        cons = set(ap["removed"])  # edited code at base: removed lines, small edited functions, +-3 around anchors
        for (p_, q_, nm_, d_, s_, e_) in edited_funcs:
            if p_ != path:
                continue
            if e_ - s_ + 1 <= 30:
                cons |= set(range(s_, e_ + 1))
            else:
                cons |= {i for a in anchors if s_ <= a <= e_ for i in range(a - 3, a + 4) if s_ <= i <= e_}
        cons |= {i for a in anchors if not in_s2(path, a) for i in (a - 1, a, a + 1) if 1 <= i <= len(base_lines)}
        E_ids |= {x for ln in cons for x in code_idents(base_lines, ln, ln)}
        base_calls = set()
        if btree is not None:
            for n in ast.walk(btree):
                if isinstance(n, ast.Call) and n.lineno in cons:
                    nm = call_name(n)
                    if keep_helper(nm):
                        base_calls.add(nm)
                        h = helpers.setdefault(nm, {"gold": False, "where": f"{path}:{n.lineno} (base)", "path": path,
                                                    "recv": set()})
                        h["recv"].add(recv_tail(n))
        if ap["added"]:
            t = parse_py("\n".join(ap["post"]) + "\n")
            E_ids |= {v for ln in ap["added"] for k_, v in regex_tokens(ap["post"][ln - 1]) if k_ == "ident"}
            if t is not None:
                for n in sorted(ast.walk(t), key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))):
                    if isinstance(n, ast.Call) and n.lineno in ap["added"]:
                        nm = call_name(n)
                        if keep_helper(nm):
                            h = helpers.setdefault(nm, {"gold": True, "where": f"{path}:{n.lineno} (gold+)",
                                                        "path": path, "recv": set()})
                            h["gold"] = True
                            h["recv"].add(recv_tail(n))
                            h["new"] = h.get("new", False) or nm not in base_calls
    E_inf = {x for x in E_ids if sidx.informative(x)}
    hrows, common_h = [], []
    for nm, h in sorted(helpers.items()):
        sites, seen_l = [], set()
        for off in corpus.find(nm, word=True, cap=20000):
            if not corpus.big[off + len(nm):off + len(nm) + 40].lstrip(" ").startswith("("):
                continue
            fi = corpus.file_of(off)
            if corpus.cats[fi] != "src":
                continue
            lt = corpus.line_text(off)
            if re.match(r"^\s*(async\s+)?(def|class|cdef|cpdef)\s", lt) or re.match(r"^\s*(#|>>>|\.\.\.)", lt):
                continue
            ln = corpus.lineno(fi, off)
            p = corpus.paths[fi]
            if (p, ln) in seen_l or in_s2(p, ln):
                continue
            seen_l.add((p, ln))
            win = {x for _, l in corpus.window(fi, ln, 1) for k_, x in regex_tokens(l) if k_ == "ident"}
            sim = sum(sidx.idf(x) for x in (win & E_inf) - {nm})
            pre = corpus.big[max(0, off - 80):off]
            m_ = re.search(r"(\w+)\s*\.\s*$", pre)
            tail = m_.group(1) if m_ else ("" if not re.search(r"[.\w)\]]\s*$", pre) else None)
            same_recv = tail in h["recv"]
            prox = len(os.path.commonpath([os.path.dirname(p) or ".", os.path.dirname(h["path"]) or "."]).split("/")) \
                if os.path.dirname(p) and os.path.dirname(h["path"]) and \
                p.split("/")[0] == h["path"].split("/")[0] else 0
            sites.append((-(sim + (4.0 if same_recv else 0.0) + 1.5 * prox), p, ln, off, fi))
        if not sites:
            continue
        if len(sites) > 80:
            common_h.append(f"{nm}({len(sites)})")
            continue
        sites.sort(key=lambda r: (r[0], r[1], r[2]))
        hrows.append((0 if h.get("new") else 1, sites[0][0], len(sites), nm, h, sites))
    hrows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    hdr8a = ["### 8a. Other source call sites (outside the edited functions) of the helpers the edited code calls "
             "(gold-removed lines, gold-added lines, the edited functions' lines near the edit); per helper the sites "
             "most like the edited code first (same receiver, shared names, nearby package); `new` = called by the "
             "gold patch but not by the edited code at base"]
    blocks8a, tail8a = [], []
    for new, _sim, n_sites, nm, h, sites in hrows[:8]:
        picks, files_used = [], set()
        for r in sites:  # one site per file first
            if len(picks) < 3 and r[1] not in files_used:
                picks.append(r)
                files_used.add(r[1])
        for r in sites:
            if len(picks) < 3 and r not in picks:
                picks.append(r)
        nf = len({r[1] for r in sites})
        tag = "new" if new == 0 else "gold+ and base" if h["gold"] else "base"
        blk = [f"- `{nm}` ({tag}; first {h['where']}): {n_sites} other call sites in {nf} files"]
        for _s, p, ln, off, fi in picks:
            enc = enclosing_name(corpus.text(fi).split("\n"), ln)
            blk.append(f"    {p}:{ln} [in {enc}] {clip(corpus.line_text(off).strip(), 170)}")
        blocks8a.append((nm, blk))
    if len(hrows) > 8:
        tail8a.append([f"- {len(hrows) - 8} more helpers (first 8 listed): "
                       + clip(", ".join(r[3] for r in hrows[8:]), 300)])
    if common_h:
        tail8a.append([f"- common helpers (> 80 source call sites, not listed): {clip(', '.join(common_h), 300)}"])
    if not hrows and not common_h:
        tail8a.append(["(no helper call in the edited code with other source call sites)"])
    fcache: dict[int, list] = {}

    def file_funcs(fi):
        if fi not in fcache:
            t, flines, defs, docs = sidx.parsed(fi)
            out = []
            for d in defs:
                if d["kind"] != "func" or "func" in d["parents"] or d["end"] - d["start"] < 1:
                    continue
                ids = {x for x in code_idents(flines, d["start"], d["end"], docs) if sidx.informative(x)}
                out.append((d, ids))
            fcache[fi] = (flines, out)
        return fcache[fi]

    def func_query(path, s, e, name):
        info, ap = module_info[path], gold_ap[path][1]
        docs = doc_lines(info["tree"]) if info.get("tree") is not None else set()
        ids = code_idents(info["lines"], s, e, docs)
        for k_, v_ in ap["inserts"].items():
            if s <= k_ <= e:
                for l in v_:
                    ids.extend(v for kk, v in regex_tokens(l) if kk == "ident")
        Q = {q for q in ids if sidx.informative(q)} - {name}
        w = {q: sidx.idf(q) for q in Q}
        return Q, w, math.sqrt(sum(v * v for v in w.values()))

    def analog_block(p, fi, fd, sh, w, note, cap=16):
        flines, _ = file_funcs(fi)
        top = sorted(sh, key=lambda x: (-w[x], x))
        rare = set(top[:6])
        focus = {ln for ln in range(fd["start"], fd["end"] + 1)
                 if rare & {v for kk, v in regex_tokens(flines[ln - 1]) if kk == "ident"}}
        return [f"- {p}::{fd['qual']} (L{fd['start']}-{fd['end']}; {note}"
                + (f"; shared names: {', '.join(top[:8])}" if top else "") + ")"] + \
            code_block(excerpt_rows(flines, fd["start"], fd["end"], focus, cap))

    def norm_name(q, dtok):
        stem = re.escape(dtok[:-1] if dtok.endswith("s") and len(dtok) > 3 else dtok)
        return q.lower() if not dtok else re.sub(stem + "s?", "<D>", q.lower())

    tgt = [x for x in edited_funcs if x[0].endswith(".py") and x[3] is not None]
    tgt.sort(key=lambda x: (-len(module_info[x[0]]["focus"].get(("func", x[1]), ())), x[0], x[4]))
    # functions section 7 shows by its content rule (not by what its budget let through: S8 stays independent of S7's
    # budget)
    shown_an, n_s8_analogs, n_s8_parallel = set(s7_rule_full), 0, 0
    queries = {(x[0], x[1]): func_query(x[0], x[4], x[5], x[2]) for x in tgt[:6]}

    hdr8b = ["### 8b. Counterparts in parallel modules (a file of the same name in a sibling directory): functions "
             "whose name matches the edited function's once the directory name is abstracted "
             "(e.g. metrics/show.py::_read_metrics ~ params/show.py::_read_params)"]
    blocks8b = []
    for path, qual, name, d, s, e in tgt[:6]:
        dirn = os.path.dirname(path)
        if not dirn or (name.startswith("__") and name.endswith("__")):
            continue
        Q, w, qn = queries[(path, qual)]
        target = norm_name(qual, os.path.basename(dirn).lower())
        found_p = []
        for fi in sidx.files:
            p = corpus.paths[fi]
            if os.path.basename(p) != os.path.basename(path) or os.path.dirname(p) == dirn or \
                    os.path.dirname(os.path.dirname(p)) != os.path.dirname(dirn):
                continue
            flines, funcs = file_funcs(fi)
            for fd, F in funcs:
                if norm_name(fd["qual"], os.path.basename(os.path.dirname(p)).lower()) == target and \
                        (p, fd["node"].lineno) not in shown_an and not in_s2(p, fd["node"].lineno):
                    sim = sum(w[x] for x in Q & F)
                    found_p.append((-sim, p, fd["start"], fi, fd, Q & F))
        found_p.sort(key=lambda r: (r[0], r[1], r[2]))
        for negs, p, st, fi, fd, sh in found_p[:2]:
            blocks8b.append((f"counterpart {p}::{fd['qual']}",
                             [f"#### counterpart of {path}::{qual}"] + analog_block(p, fi, fd, sh, w, "parallel module",
                                                                                     24)))
            shown_an.add((p, fd["node"].lineno))

    hdr8c = ["### 8c. Analogous functions elsewhere (idf-weighted identifier overlap with each edited function, score "
             ">= 0.25; excerpts around the shared names)"]
    blocks8c = []  # (target header, [(label, block)])
    for path, qual, name, d, s, e in tgt[:4]:
        Q, w, qn = queries[(path, qual)]
        if len(Q) < 3:
            continue
        fsc = sorted(((-sum(w[q] for q in Q if q in sidx.sets[fi]), corpus.paths[fi], fi) for fi in sidx.files),
                     key=lambda r: (r[0], r[1]))
        cands = [fi for sc, p, fi in fsc[:40] if sc < 0]
        base_dir = os.path.dirname(path)
        scored = []
        for fi in cands:
            p = corpus.paths[fi]
            flines, funcs = file_funcs(fi)
            for fd, F in funcs:
                if in_s2(p, fd["node"].lineno) or (p, fd["node"].lineno) in shown_an:
                    continue
                sh = Q & F
                if len(sh) < 3:
                    continue
                fn_ = math.sqrt(sum(sidx.idf(x) ** 2 for x in F))
                cos = sum(w[x] ** 2 for x in sh) / (qn * fn_) if fn_ else 0.0
                sib = os.path.basename(p) == os.path.basename(path) and os.path.dirname(p) != base_dir
                nt1, nt2 = name_tokens(fd["name"]), name_tokens(name)
                stem = len(nt1 & nt2) / len(nt1 | nt2) if nt1 | nt2 else 0.0
                scored.append((-(cos + (0.08 if sib else 0.0) + 0.1 * stem), p, fd["start"], fi, fd, sh, cos, sib))
        scored.sort(key=lambda r: (r[0], r[1], r[2]))
        picked, files_used = [], set()
        for r in scored:
            if -r[0] < 0.25 or len(picked) >= 2:
                break
            if r[1] in files_used:
                continue
            files_used.add(r[1])
            picked.append(r)
        if not picked:
            continue
        group = []
        for negs, p, st, fi, fd, sh, cos, sib in picked:
            note = f"score {-negs:.2f}" + ("; same file name in a sibling directory" if sib else "")
            group.append((f"analog {p}::{fd['qual']}", analog_block(p, fi, fd, sh, w, note)))
            shown_an.add((p, fd["node"].lineno))
        blocks8c.append(([f"#### analogs of {path}::{qual}"], group))

    # budget: 8a vs 8b + 8c by water-filling their demands (measured by rendering both parts unbudgeted); inside
    # each part, blocks in order, the ones that do not fit are omitted and named
    def emit8(a8a, a8bc):
        om = []
        pa = Budget(a8a)
        pa.force(hdr8a)
        tail_cost = sum(cost(x) for x in tail8a)
        om8a, n_h = [], 0
        for nm, blk in blocks8a:
            if pa.fits(cost(blk) + tail_cost):
                pa.add(blk)
                n_h += 1
            else:
                om8a.append(nm)
        if om8a:
            pa.force([f"- call sites of these helpers omitted (budget): {clip(', '.join(om8a), 300)}"])
            om.append(("helper call-site list", len(om8a), clip(", ".join(om8a), 300)))
        for x in tail8a:
            pa.force(x)
        pb = Budget(a8bc)
        pb.force(hdr8b)
        om8, n_p = [], 0
        for label, blk in blocks8b:
            if pb.add(blk):
                n_p += 1
            else:
                om8.append(label)
        if om8:
            pb.force([f"- omitted (budget): {clip(', '.join(om8), 300)}"])
            om.append(("parallel-module counterpart", len(om8), clip(", ".join(om8), 300)))
        if not blocks8b:
            pb.force(["(no counterpart in a parallel module)"])
        pb.force(hdr8c)
        om8, n_a = [], 0
        for h, group in blocks8c:
            shown_h = False
            for label, blk in group:
                if pb.add(blk if shown_h else h + blk):
                    shown_h = True
                    n_a += 1
                else:
                    om8.append(label)
        if om8:
            pb.force([f"- omitted (budget): {clip(', '.join(om8), 300)}"])
            om.append(("analogous function", len(om8), clip(", ".join(om8), 300)))
        if not blocks8c:
            pb.force(["(no analogous source function above the threshold)"])
        return pa, pb, om, n_h, n_p, n_a

    pa, pb, om8_all, n_s8_helpers, n_s8_parallel, n_s8_analogs = emit8(UNLIMITED, UNLIMITED)
    if b.cap < UNLIMITED:
        a8a, a8bc = water([pa.used, pb.used], b.left - b.reserve)
        pa, pb, om8_all, n_s8_helpers, n_s8_parallel, n_s8_analogs = emit8(a8a, a8bc)
    b.absorb(pa)
    b.absorb(pb)
    for typ, n, det in om8_all:
        omit("s8", typ, n, det)
    done("s8", b)

    # ---------------------------------------------------------- sections 10-14 (v4)
    # dedupe registry: numbered code lines sections 2-9 show (S1's 3-line token windows are not counted); sections
    # 10-14 skip an item an earlier section already shows (>= 80% of its lines) and elide shown runs of >= 3 lines
    reg: dict[str, dict[int, str]] = defaultdict(dict)
    for k_ in OLD_SECTIONS:
        if k_ != "s1":
            register_shown(sec[k_].out, tree.files, k_, reg)
    fdefs_cache: dict[str, tuple] = {}

    def file_defs(path):
        """(base parse or None, base lines, def_index) of any base file (parse objects shared with the Resolver)."""
        if path not in fdefs_cache:
            t_, lines_ = rs.parsed(path) if path.endswith((".py", ".pyi")) else (None, split_lines(tree.read(path)))
            fdefs_cache[path] = (t_, lines_, def_index(t_) if t_ is not None else [])
        return fdefs_cache[path]

    def bget(path):
        return file_defs(path)[0]

    def qual_of(path, node):
        defs_ = file_defs(path)[2]
        e_ = next((x for x in defs_ if x["node"] is node), None) or \
            next((x for x in defs_ if x["name"] == getattr(node, "name", None) and x["node"].lineno == node.lineno), None)
        return e_["qual"] if e_ else getattr(node, "name", "?")

    def shown_share(path, s, e):
        sh = reg.get(path, {})
        return sum(1 for i in range(s, e + 1) if i in sh) / max(1, e - s + 1), \
            sorted({sh[i] for i in range(s, e + 1) if i in sh}, key=lambda x: int(x[1:]))

    def make_item(sec_, path, s, e, symbol, why, cap, focus=frozenset(), marks=None, rows_fn=None, node=None,
                  doc_cap=None):
        lines_ = file_defs(path)[1]
        if rows_fn is None and not (1 <= s <= e <= len(lines_)):
            return None  # a range outside the base file (defensive: a post-patch node)
        doc = node_doc(node) if node is not None else None
        n = len(head_rows(lines_, s, e, 10 ** 6, frozenset(), doc, doc_cap)) if doc is not None else e - s + 1

        def render(L):
            rows = rows_fn(L) if rows_fn else head_rows(lines_, s, e, min(L, cap), focus, doc, doc_cap)
            rows = drop_shown(rows, reg.get(path, {}))
            note = "" if n <= min(L, cap) else \
                f"; elided to ~{min(L, cap)} of {n} lines ({'content cap' if L >= cap else cut_word.get(sec_, 'budget')})"
            return [f"### {path}:{s}-{e} `{symbol}` ({why}{note})"] + code_block(rows, marks or {})
        return {"name": f"{path}::{symbol}", "fixed": None, "render": render, "n": min(n, cap), "sec": sec_,
                "path": path, "s": s, "e": e}

    def accept(cands, limit):
        """Drop candidates an earlier section (or an earlier candidate) already shows; return (items, pointers)."""
        items_, ptrs, taken = [], [], []
        for it in (c_ for c_ in cands if c_ is not None):
            share, secs = shown_share(it["path"], it["s"], it["e"])
            if share >= 0.8:
                ptrs.append(f"`{it['name'].split('::', 1)[1]}` {it['path']}:{it['s']} (section "
                            f"{', '.join(x[1:] for x in secs)}{'; ' + it['ptr_note'] if it.get('ptr_note') else ''})")
                continue
            if any(p_ == it["path"] and a_ <= it["s"] and it["e"] <= z_ for p_, a_, z_ in taken):
                continue
            if len(items_) >= limit:
                continue
            taken.append((it["path"], it["s"], it["e"]))
            items_.append(it)
        return items_, ptrs

    new_counts = {}
    ceiling_cuts: list[dict] = []
    cut_word = {k_: ("content ceiling" if budgets[k_] >= NEW_CEILING[k_] else "budget") for k_ in NEW_SECTIONS}

    def emit_new(key, title, purpose, items_, ptrs, floor, what, extra=()):
        """Render a v4 section: title, purpose, items (per-item degradation, then omission), pointer line. An empty
        section is left out of the pack."""
        b_ = take(key)
        by_ceiling = budgets[key] >= NEW_CEILING[key]
        why_cut = "content ceiling" if by_ceiling else "budget"

        def omit_(sec_, typ, n, detail=""):
            if by_ceiling:
                if n > 0:
                    ceiling_cuts.append({"section": sec_, "type": "omitted " + typ, "n": n})
            else:
                omit(sec_, typ, n, detail)

        def trunc_(sec_, typ, n, detail=""):
            if by_ceiling:
                if n > 0:
                    ceiling_cuts.append({"section": sec_, "type": typ, "n": n})
            else:
                trunc(sec_, typ, n, detail)
        if not items_ and not ptrs and not extra:
            done(key, b_)
            new_counts[key] = 0
            return
        b_.force([f"## {key[1:]}. {title}", f"Purpose: {purpose}"])
        for x in extra:
            b_.force(x)
        tail = [f"- already shown in an earlier section: {clip('; '.join(ptrs), 700)}"] if ptrs else []
        tail_cost = cost(tail) if tail else 0
        shown_n = 0
        if items_:
            def render_all(L):
                return [u["render"](L) for u in items_]
            avail = b_.left - b_.reserve - tail_cost
            hi = max([floor] + [u["n"] for u in items_])
            L = (fill_cap(render_all, floor, hi, avail) if b_.cap < UNLIMITED else hi)
            om = []
            if L is not None:
                for blk in render_all(L):
                    b_.force(blk)
                shown_n = len(items_)
                trunc_(key, f"{what} elided", sum(1 for u in items_ if u["n"] > L), f"to ~{L} lines")
            else:
                used_ = 0
                for u, blk in zip(items_, render_all(floor)):
                    c = cost(blk)
                    if used_ + c <= avail:
                        b_.force(blk)
                        used_ += c
                        shown_n += 1
                    else:
                        om.append(u["name"])
                trunc_(key, f"{what} elided", sum(1 for u in items_ if u["n"] > floor and u["name"] not in om),
                       f"to ~{floor} lines")
            if om:
                plural = what[:-1] + "ies" if what.endswith("y") and not what.endswith("ey") else what + "s"
                b_.force([f"- {len(om)} {plural if len(om) > 1 else what} omitted ({why_cut}): "
                          f"{clip(', '.join(om), 400)}"])
                omit_(key, what, len(om), clip(", ".join(om), 300))
        if tail:
            b_.force(tail)
        done(key, b_)
        register_shown(b_.out, tree.files, key, reg)
        new_counts[key] = shown_n

    # gold-changed lines: (path, side, parse, line numbers); side "removed" = base lines, "added" = post-patch lines
    gold_sides = []
    for path, (f, ap) in sorted(gold_ap.items()):
        if not path.endswith(".py"):
            continue
        bt_ = module_info.get(path, {}).get("tree")
        if bt_ is not None and ap["removed"]:
            gold_sides.append((path, "removed", bt_, set(ap["removed"]), ap["base"]))
        if ap["added"]:
            pt_ = parse_py("\n".join(ap["post"]) + "\n")
            if pt_ is not None:
                gold_sides.append((path, "added", pt_, set(ap["added"]), ap["post"]))

    # ---- S10 callee bodies: base definitions the gold-changed lines call (hop 1) and that those call (hop 2)
    def method_in_mro(path, cnode, nm, include_self):
        queue = [(path, cnode, 0)] if include_self else [(p2, c2, 1) for p2, c2 in class_bases(path, cnode, bget, rs)]
        seen_c = set()
        while queue:
            p_, c_, dep = queue.pop(0)
            if (p_, c_.lineno) in seen_c or dep > 6:
                continue
            seen_c.add((p_, c_.lineno))
            m_ = next((x for x in c_.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == nm),
                      None)
            if m_ is not None:
                base_m = same_node(bget(p_), m_) if p_.endswith(".py") else None
                if base_m is None:
                    return None  # a method the gold patch adds (absent at base)
                return p_, base_m, f"method of {c_.name}" + (" (base class)" if dep else "")
            queue.extend((p2, c2, dep + 1) for p2, c2 in class_bases(p_, c_, bget, rs))
        return None

    def_line_cache: dict[str, list] = {}

    def only_definition(nm, exclude):
        """(path, node) of the single source definition of a function / method name, the calling function excluded."""
        if nm not in def_line_cache:
            rx = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+" + re.escape(nm) + r"[ \t]*\(")
            hits = []
            for off in corpus.find(f"def {nm}", word=True, cap=400):
                fi = corpus.file_of(off)
                if corpus.cats[fi] != "src" or not corpus.paths[fi].endswith(".py") or not rx.match(corpus.line_text(off)):
                    continue
                hits.append((corpus.paths[fi], corpus.lineno(fi, off)))
            def_line_cache[nm] = hits
        hits = [h for h in def_line_cache[nm] if h != exclude]
        if len(hits) != 1:
            return None
        p_, ln = hits[0]
        node = next((x["node"] for x in file_defs(p_)[2] if x["kind"] == "func" and x["node"].lineno == ln), None)
        return (p_, node) if node is not None else None

    edited_starts = {(p_, s_) for (p_, _q, _n, _d, s_, _e) in edited_funcs}

    def receiver_fits(recv, p_, node):
        """v4.1: the single-definition fallback holds only if a word of the receiver's name (`self.tokenizer` ->
        tokenizer) occurs in the name of the definition's class (module-level function: its module's name)."""
        ch = attr_chain(recv.func if isinstance(recv, ast.Call) else recv) or []
        words = set().union(*[name_tokens(x) for x in ch if x not in ("self", "cls")] or [set()])
        q = qual_of(p_, node)
        if "." in q:
            owner = q.rsplit(".", 1)[0].split(".")[-1]
        else:
            stem = os.path.splitext(os.path.basename(p_))[0]
            owner = os.path.basename(os.path.dirname(p_)) if stem == "__init__" else stem
        return bool(words & name_tokens(owner))

    def empty_class(node):
        """v4.1: a class whose body is only a docstring / pass / `...` (S10 drops it: nothing to read)."""
        return isinstance(node, ast.ClassDef) and all(
            isinstance(st, ast.Pass) or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                                         and (st.value.value is Ellipsis or isinstance(st.value.value, str)))
            for st in node.body)

    def call_targets(call, path, t_, cls_node, enc, precise):
        """[(path, node, how)] that one Call resolves to at base."""
        fnode = call.func
        if isinstance(fnode, ast.Name):
            nm = fnode.id
            if nm in SKIP_NAMES or len(nm) <= 2:
                return []
            bt0 = bget(path) if path.endswith(".py") else None
            top = next((x for x in (bt0.body if bt0 is not None else [])
                        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and x.name == nm), None)
            if top is not None:
                return [(path, top, "same module")]
            imps = file_imports(t_, path)
            if nm in imps and imps[nm][1]:
                r = resolve_chain(rs, imps[nm][0], [imps[nm][1]])
                if r and isinstance(r[1], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    return [(r[0], r[1], f"imported from {imps[nm][0]}")]
            return []
        if not isinstance(fnode, ast.Attribute):
            return []
        nm = fnode.attr
        if len(nm) <= 2 or (nm.startswith("__") and nm.endswith("__")):
            return []
        recv = fnode.value
        if isinstance(recv, ast.Name) and recv.id in ("self", "cls") and cls_node is not None:
            r = method_in_mro(path, cls_node, nm, True)
            return [r] if r else []
        if isinstance(recv, ast.Call) and isinstance(recv.func, ast.Name) and recv.func.id == "super" \
                and cls_node is not None:
            r = method_in_mro(path, cls_node, nm, False)
            return [r] if r else []
        ch = attr_chain(fnode)
        if ch:
            imps = file_imports(t_, path)
            if ch[0] in imps:
                m_, a_ = imps[ch[0]]
                r = resolve_chain(rs, m_, ([a_] if a_ else []) + ch[1:])
                if r and isinstance(r[1], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    return [(r[0], r[1], f"via {'.'.join(ch[:-1])}")]
                return []
        if precise or nm in BUILTIN_METHODS:
            return []
        r = only_definition(nm, enc)
        if r is None or not receiver_fits(recv, r[0], r[1]):
            return []
        return [(r[0], r[1], "the only source definition of this name; receiver type not resolved")]

    def init_of(node):
        if isinstance(node, ast.ClassDef):
            init = next((x for x in node.body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and x.name == "__init__"), None)
            return init or node
        return node

    def class_ctx(defs_, line, path, side):
        """Base ClassDef enclosing a line (for self / super calls); post-side classes mapped to their base version."""
        best = None
        for d_ in defs_:
            if d_["kind"] == "class" and d_["start"] <= line <= d_["end"] and "func" not in d_["parents"]:
                if best is None or d_["start"] >= best["start"]:
                    best = d_
        if best is None:
            return None
        if side == "added":
            bt0 = module_info.get(path, {}).get("tree")
            bq = next((x["node"] for x in def_index(bt0) if x["kind"] == "class" and x["qual"] == best["qual"]),
                      None) if bt0 is not None else None
            return bq or best["node"]
        return best["node"]

    s10_c, seen10 = [], set()

    def add10(p_, node, why, hop, parent=None):
        node = init_of(node)
        if node is None or not p_.endswith((".py", ".pyi")) or category(p_) != "src" or empty_class(node):
            return
        s_, e_ = node_start(node), node.end_lineno
        if (p_, s_) in seen10 or (p_, s_) in edited_starts or (p_, node.lineno) in edited_starts:
            return
        seen10.add((p_, s_))
        q = qual_of(p_, node)
        s10_c.append({"p": p_, "node": node, "why": why, "hop": hop, "q": q, "parent": parent})

    for side_order in ("added", "removed"):
        for path, side, t_, lnset, src_lines in gold_sides:
            if side != side_order:
                continue
            defs_ = def_index(t_)
            calls = sorted((n for n in ast.walk(t_) if isinstance(n, ast.Call) and n.lineno in lnset),
                           key=lambda n: (n.lineno, n.col_offset))
            bdefs_ = def_index(module_info[path]["tree"]) if module_info.get(path, {}).get("tree") is not None else []
            for n in calls:
                fn_, _c = unit_of(defs_, n.lineno)
                cnode = class_ctx(defs_, n.lineno, path, side)
                enc = None
                if fn_ is not None:
                    bfn = next((x for x in bdefs_ if x["qual"] == fn_["qual"]), None)
                    enc = (path, bfn["node"].lineno) if bfn else None
                where = f"{path}:{n.lineno}, a base line the gold patch removes" if side == "removed" else \
                    f"a gold-added line ({path}, post-patch line {n.lineno})"
                for p_, node, how in call_targets(n, path, t_, cnode, enc, False):
                    add10(p_, node, f"called at {where}; {how}", 1)
    hop1 = [c for c in s10_c if c["hop"] == 1][:S10_HOP1]
    for c in hop1[:5]:
        if not isinstance(c["node"], (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        p_ = c["p"]
        t_, _l, defs_ = file_defs(p_)
        if t_ is None:
            continue
        cnode = class_ctx(defs_, c["node"].lineno, p_, "removed")
        k2 = 0
        for n in sorted((n for n in ast.walk(c["node"]) if isinstance(n, ast.Call)),
                        key=lambda n: (n.lineno, n.col_offset)):
            if k2 >= 2 or sum(1 for x in s10_c if x["hop"] == 2) >= S10_HOP2:
                break
            for p2, node2, how in call_targets(n, p_, t_, cnode, None, True):
                if p2 != p_ or how != "same module":
                    continue  # hop 2 = module-level functions of the hop-1 callee's own module (other hop-2 kinds: 1 hit
                    # in 86 blocks on the 120 calibration / fresh tasks)
                before = len(s10_c)
                add10(p2, node2, f"called by `{c['q']}` at {p_}:{n.lineno}; {how}", 2, c["q"])
                k2 += len(s10_c) - before
    cands10 = []
    for c in [x for x in s10_c if x["hop"] == 1][:S10_HOP1] + [x for x in s10_c if x["hop"] == 2][:S10_HOP2]:
        node = c["node"]
        cands10.append(make_item("s10", c["p"], node_start(node), node.end_lineno, c["q"],
                                 ("hop 1: " if c["hop"] == 1 else "hop 2: ") + c["why"], S10_BODY_LINES,
                                 node=node, doc_cap=4))
    items10, ptr10 = accept(cands10, S10_HOP1 + S10_HOP2)
    emit_new("s10", "Callee bodies: base code that the gold-changed lines call (1-2 hops)",
             "what the functions the fix relies on actually do (hop 1 = called on a gold-added or gold-removed line; "
             "hop 2 = a function of the same module that a hop-1 body calls).", items10, ptr10, S10_FLOOR,
             "callee body")

    # ---- S11 full bodies of members the gold-changed lines or the F2P tests touch but other sections show only as a
    # signature / head / member list; docstrings of the edited classes
    gold_names, gold_attrs = defaultdict(set), set()  # identifiers on the gold-changed lines, per file
    for path, side, t_, lnset, src_lines in gold_sides:
        for ln in lnset:
            txt = src_lines[ln - 1]
            gold_names[path] |= {v for k_, v in regex_tokens(txt) if k_ == "ident"}
            gold_attrs |= set(re.findall(r"\b(?:self|cls)\.(\w+)", txt))
    f2p_attrs = set()
    for rec in seen_tests.values():
        if rec.get("kind") == "py" and rec.get("node") is not None:
            f2p_attrs |= {n.attr for n in ast.walk(rec["node"]) if isinstance(n, ast.Attribute)}
    f2p_names = (f2p_idents | f2p_attrs) - SKIP_NAMES
    cands11 = []
    for (path, cq), cd in sorted(classes.items(), key=lambda kv: (kv[0][0], kv[1]["start"])):
        node = cd["node"]
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None),
                                                                           ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            if node.body[0].end_lineno - node_start(node) + 1 <= S11_SHORT_CLASS_DOC and \
                    ("class", cq) not in module_info[path]["info"]:
                continue  # v4.1: a short docstring of a class whose methods (not its body) the gold edits: 0 hits
            cands11.append(make_item("s11", path, node_start(node), node.body[0].end_lineno, f"{cq} (class docstring)",
                                     "docstring of a class the gold patch edits", S11_BODY_LINES))
    for (path, cq), cd in sorted(classes.items(), key=lambda kv: (kv[0][0], kv[1]["start"])):
        edited_here = {q_ for (p_, q_, *_r) in edited_funcs if p_ == path}
        for m in cd["node"].body:
            if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) or f"{cq}.{m.name}" in edited_here:
                continue
            why = []
            if m.name in gold_attrs:
                why.append("the gold-changed lines use self." + m.name)
            if m.name in f2p_names and (sidx.informative(m.name) or m.name in gold_attrs):
                why.append("named in an F2P test")
            if why:
                cands11.append(make_item("s11", path, node_start(m), m.end_lineno, f"{cq}.{m.name}",
                                         "member of an edited class; " + "; ".join(why), S11_BODY_LINES,
                                         node=m, doc_cap=12))
    for (dp, dl), e in order[:16]:
        kind, _p, node, dlines = e["r"]
        if not (e["assert"] or e["feed"]) or in_s2(dp, node.lineno) or (dp, node.lineno) in s7_class_lines:
            continue
        if category(dp) == "test" or TEST_UTIL_PATH.search(dp):
            continue  # v4.1: test-utility module (assert_frame_equal, TestClient, ...): section 9's head only, 0 hits
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            cands11.append(make_item("s11", dp, node_start(node), node.end_lineno, qual_of(dp, node),
                                     "repository function an F2P assertion depends on; section 9 shows its head",
                                     S11_BODY_LINES, node=node, doc_cap=0))
        elif isinstance(node, ast.ClassDef):
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name in f2p_attrs \
                        and not (m.name.startswith("__") and m.name.endswith("__")):
                    cands11.append(make_item("s11", dp, node_start(m), m.end_lineno, qual_of(dp, m),
                                             f"method of `{e['name']}` that an F2P test calls; section 9 lists it as a "
                                             "signature", S11_BODY_LINES, node=m, doc_cap=12))
    for path, info in sorted(module_info.items()):
        t_ = info.get("tree")
        if t_ is None:
            continue
        for st in t_.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and st.name in gold_names[path] \
                    and sidx.informative(st.name) and not in_s2(path, st.lineno) and (path, st.lineno) not in s7_class_lines:
                cands11.append(make_item("s11", path, node_start(st), st.end_lineno, st.name,
                                         "definition in an edited module that the gold-changed lines name",
                                         S11_BODY_LINES, node=st, doc_cap=12))
    items11, ptr11 = accept(cands11, S11_ITEMS)
    emit_new("s11", "Full bodies of members the gold patch or the F2P tests touch",
             "full code of definitions that the gold-changed lines or the F2P tests use and that sections 2, 7 and 9 "
             "show only as a signature, a head or a member list; docstrings of the edited classes.",
             items11, ptr11, S11_FLOOR, "member body")

    # ---- S12 traceback frames of the issue (section A only), innermost first
    frames = issue_frames(issue)
    by_base = Counter(os.path.basename(p_) for p_ in tree.files)
    cands12, seen12, tb_done = [], set(), set()
    nfr = len(frames)
    for k, (raw, ln, func, code, tbi) in reversed(list(enumerate(frames, 1))):
        if tbi in tb_done:
            continue
        comps = [c_ for c_ in raw.replace("\\", "/").split("/") if c_]
        hit = None
        for i in range(len(comps)):
            suf = "/".join(comps[i:])
            if suf in tree.files:
                hit = suf
                break
        if hit is None or (hit.count("/") == 0 and by_base[hit] > 1):
            continue
        fname = func.split(".")[-1]
        if fname in ("<module>", "<lambda>", "<listcomp>", "<genexpr>", "<dictcomp>"):
            continue
        t_, flines, defs_ = file_defs(hit)
        if defs_:
            cs = [d_ for d_ in defs_ if d_["name"] == fname and d_["kind"] == "func"]
            d_ = min(cs, key=lambda d_: (not (d_["start"] <= ln <= d_["end"]), abs(d_["start"] - ln))) if cs else None
            span = (d_["start"], d_["end"], d_["qual"]) if d_ else None
        else:
            starts = [i for i, l_ in enumerate(flines, 1) if (m_ := DEF_RE.match(l_)) and m_.group(2) == fname]
            span = None
            if starts:
                u = regex_unit(flines, min(starts, key=lambda i: abs(i - ln)))
                span = (u[1], u[2], u[0]) if u else None
        if span is None:
            continue
        s_, e_, q = span
        if (hit, s_) in seen12:
            continue
        seen12.add((hit, s_))
        tb_done.add(tbi)
        focus, found = set(), ""
        if code:
            qm = quoted_line(flines, s_, e_, code, ln)
            if qm:
                focus = {qm[0]}
                found = f"; the quoted line is base L{qm[0]}" if qm[1] else f"; closest base line L{qm[0]}"
            else:
                found = "; the quoted line is not in the base body"
        node12 = next((d_["node"] for d_ in defs_ if d_["start"] == s_), None) if defs_ else None
        it12 = make_item("s12", hit, s_, e_, q, f"innermost repository frame of the issue's traceback, frame "
                         f"{k} of {nfr}: \"line {ln}, in {func}\"{found}",
                         S12_BODY_LINES, focus, {f_: SHOWN_MARK for f_ in focus}, node=node12, doc_cap=0)
        if it12 is not None:  # v4.1: a pointer (item shown in an earlier section) still names the quoted base line
            it12["ptr_note"] = f"traceback frame \"line {ln}, in {func}\"{found}"
        cands12.append(it12)
    items12, ptr12 = accept(cands12, S12_FRAMES)
    emit_new("s12", "Traceback of the issue: innermost repository frame, at base",
             "the base code where each traceback the issue quotes leaves the repository (its innermost repository "
             "frame); `!` marks the quoted line.", items12, ptr12, 8, "traceback frame")

    # ---- S13 repository docs that mention identifiers / messages absent from the issue (changelogs excluded)
    doc_files = [i for i, p_ in enumerate(corpus.paths) if p_.endswith((".rst", ".md")) and not CHANGELOG_PATH.search(p_)
                 and "versioned_docs" not in p_]
    terms = {}

    def string_terms(v, min_len, in_issue_ok=False):
        """A message and its literal fragments (placeholders / quotes split off) with at least three words."""
        out_ = ([v.strip()] if "{" not in v and "%" not in v else []) + fragments(v)
        out_ = [x.strip(" :;,.-'\"") for x in out_]
        return [x for x in dict.fromkeys(out_) if len(x) >= min_len and len(re.findall(r"(?:^|\s)[A-Za-z]{2,}", x)) >= 3
                and (in_issue_ok or x not in issue)]

    for it in kept:  # messages of the gold's added lines (identifiers and F2P strings: 1 hit in 73 paragraphs)
        if it["kind"] == "string" and "gold+" in it["roles"]:
            for x in string_terms(it["value"], 16):
                terms.setdefault(x, "string")
    for path, side, t_, lnset, src_lines in gold_sides:  # messages the gold patch removes (even if the issue quotes them)
        if side != "removed":
            continue
        for ln in lnset:
            for k_, v in regex_tokens(src_lines[ln - 1]):
                if k_ == "string":
                    for x in string_terms(v, 16, True):
                        terms.setdefault(x, "string")

    hits13 = defaultdict(lambda: defaultdict(set))  # doc fi -> line -> terms
    dfreq = {}
    doc_set = set(doc_files)
    for v, kind in terms.items():
        fis = defaultdict(list)
        for off in corpus.find(v, word=kind == "ident", cap=3000):
            fi = corpus.file_of(off)
            if fi in doc_set:
                fis[fi].append(off)
        if not fis or len(fis) > S13_MAX_DF:
            continue
        dfreq[v] = len(fis)
        for fi, offs in fis.items():
            for off in offs[:20]:
                hits13[fi][corpus.lineno(fi, off)].add(v)
    nd = max(1, len(doc_files))
    paras = []
    for fi, by_line in hits13.items():
        flines = corpus.text(fi).split("\n")
        spans = []
        for ln in sorted(by_line):
            a_ = ln
            while a_ > 1 and ln - a_ < S13_RADIUS and flines[a_ - 2].strip():
                a_ -= 1
            z_ = ln
            while z_ < len(flines) and z_ - ln < S13_RADIUS and flines[z_].strip():
                z_ += 1
            if spans and a_ <= spans[-1][1] + 1:
                spans[-1] = (spans[-1][0], max(spans[-1][1], z_))
            else:
                spans.append((a_, z_))
        for a_, z_ in spans:
            para = flines[a_ - 1:z_]
            words = [len(re.findall(r"[A-Za-z]{2,}", l_)) for l_ in para]
            if not any(w_ >= 6 for w_ in words):
                continue  # not prose: API listings, tables, code only
            tset = set().union(*[by_line[l_] for l_ in by_line if a_ <= l_ <= z_])
            score = sum(math.log((nd + 1) / (dfreq[v] + 0.5)) * (1.5 if terms[v] == "string" else 1.0) for v in tset)
            strong = any(terms[v] == "string" for v in tset) or any(dfreq[v] <= 2 for v in tset) or \
                sum(1 for v in tset if dfreq[v] <= 4) >= 2
            if strong:
                paras.append((-score, corpus.paths[fi], a_, z_, fi, sorted(tset, key=lambda v: (dfreq[v], v))))
    paras.sort(key=lambda r: (r[0], r[1], r[2]))
    cands13, per_file = [], Counter()
    for negs, p_, a_, z_, fi, tset in paras:
        if per_file[p_] >= S13_PER_FILE:
            continue
        per_file[p_] += 1
        flines = corpus.text(fi).split("\n")
        hd = doc_heading(flines, a_)
        shown_terms = ", ".join(json.dumps(v, ensure_ascii=False) if terms[v] == "string" else v for v in tset[:5])
        cands13.append(make_item("s13", p_, a_, min(z_, len(flines)), clip(hd, 70) or os.path.basename(p_),
                                 f"mentions {clip(shown_terms, 240)}", 2 * S13_RADIUS + 1))
    items13, ptr13 = accept(cands13, S13_PARAS)
    emit_new("s13", "Repository documentation of the messages the gold patch changes",
             "prose paragraphs of the repository docs (rst / md, changelogs excluded) that mention a message the gold "
             "patch removes, or adds without the issue quoting it (documented behaviour the fix changes or extends).",
             items13, ptr13, 6, "doc paragraph")

    # ---- S14 test-file context: in each file the test patch edits, the units the F2P tests use (setUp, same-file
    # helpers, module constants) and the other tests as signature + one assertion line
    f2p_by_file = defaultdict(list)  # base path -> [(start, end, node or None, name)]
    for gk, g in groups.items():
        rec = g["recs"][0]
        if rec["kind"] == "py":
            d0, dp0, _n = locate_py(rec["path"], rec["parts"], btree_fn, rs) if tree.read(rec["path"]) \
                else (None, None, "")
            if d0 is not None:
                f2p_by_file[dp0].append((d0["start"], d0["end"], d0["node"], d0["name"]))
        elif rec["kind"] == "case" and tree.read(rec["path"]):
            fc = find_case(tree.read(rec["path"]), rec["parts"][-1])
            if fc:
                f2p_by_file[rec["path"]].append((fc[0], fc[1], None, re.sub(r"-\w+$", "", fc[2])))
    f2p_test_names = {x[3] for v in f2p_by_file.values() for x in v} | \
        {rec["parts"][-1] for rec in seen_tests.values() if rec["parts"]}
    f2p_tokens = (set().union(*[name_tokens(n_) for n_ in f2p_test_names]) if f2p_test_names else set()) - \
        {"test", "tests", "case"}
    cands14_ctx, blocks14 = [], []
    for path, (f, ap) in sorted(test_ap.items()):
        if f["is_new"] or f["is_deleted"] or f["old"] is None or tree.read(f["old"]) is None:
            continue
        bpath = f["old"]
        t_, blines, defs_ = file_defs(bpath)
        edited_ranges = []
        if t_ is not None:
            _pt, info_, *_r = edited_units(base_texts.get(bpath), ap, bpath)
            edited_ranges = [(v[0], v[1]) for k_, v in info_.items() if v is not None and k_[0] == "func"]
        f2p_here = f2p_by_file.get(bpath, [])
        busy = [(a_, z_) for a_, z_, *_r in f2p_here] + edited_ranges
        units = []  # (line, end, name, kind)
        if t_ is not None:
            f2p_nodes = [x[2] for x in f2p_here if x[2] is not None]
            used = {n.id for fn_ in f2p_nodes for n in ast.walk(fn_) if isinstance(n, ast.Name)}
            used |= {n.func.id for fn_ in f2p_nodes for n in ast.walk(fn_)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            f2p_keys = {(a_, nm_) for a_, z_, n_, nm_ in f2p_here}
            f2p_cls = {d_["qual"].rsplit(".", 1)[0] for d_ in defs_ if (d_["start"], d_["name"]) in f2p_keys
                       and "." in d_["qual"]}
            for d_ in defs_:
                if any(a_ <= d_["start"] and d_["end"] <= z_ for a_, z_ in busy) or "func" in d_["parents"]:
                    continue
                owner = d_["qual"].rsplit(".", 1)[0] if "." in d_["qual"] else ""
                if d_["kind"] == "func" and owner in f2p_cls and d_["name"] in (
                        "setUp", "setUpClass", "setup_method", "setup_class", "tearDown", "asyncSetUp"):
                    cands14_ctx.append(make_item("s14", bpath, d_["start"], d_["end"], d_["qual"],
                                                 "set-up of the class of an F2P test", S14_CONTEXT_LINES,
                                                 node=d_["node"], doc_cap=0))
                elif d_["kind"] == "func" and not owner and d_["name"] in used and not d_["name"].startswith("test"):
                    cands14_ctx.append(make_item("s14", bpath, d_["start"], d_["end"], d_["qual"],
                                                 "same-file helper an F2P test calls", S14_CONTEXT_LINES,
                                                 node=d_["node"], doc_cap=0))
                elif d_["kind"] == "func" and d_["name"].startswith("test"):
                    fn_ = d_["node"]
                    al = sorted(assert_lines(fn_))
                    body = [x for x in fn_.body if not (isinstance(x, ast.Expr) and isinstance(
                        getattr(x, "value", None), ast.Constant))]
                    one = al[0] if al else (body[0].lineno if body else None)
                    units.append((fn_.lineno, one, d_["qual"], d_["start"], d_["end"]))
            for st in t_.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                    nms = {x.id for x in tg if isinstance(x, ast.Name)}
                    if nms & used and not any(a_ <= st.lineno <= z_ for a_, z_ in busy) and \
                            not any(st.lineno <= r_ <= st.end_lineno for r_ in ap["removed"]):
                        cands14_ctx.append(make_item("s14", bpath, st.lineno, st.end_lineno, ", ".join(sorted(nms)),
                                                     "module constant an F2P test reads", S14_CONTEXT_LINES))
        elif bpath.endswith(".test"):
            hdrs = [(i + 1, m.group(1)) for i, l in enumerate(blines) for m in [CASE_HDR.match(l)] if m]
            for k, (ln, nm) in enumerate(hdrs):
                end = hdrs[k + 1][0] - 1 if k + 1 < len(hdrs) else len(blines)
                if any(a_ <= ln <= z_ for a_, z_ in busy) or any(ln <= r_ <= end for r_ in ap["removed"]) or \
                        any(ln <= r_ < end for r_ in ap["inserts"]):
                    continue
                one = next((i for i in range(ln + 1, end + 1) if re.search(r"#\s*[ENW]:", blines[i - 1])), None) or \
                    next((i for i in range(ln + 1, end + 1) if blines[i - 1].strip()), None)
                units.append((ln, one, f"[case {nm}]", ln, end))
        if not units:
            continue
        near = [((a_ + z_) // 2) for a_, z_ in busy] + sorted(ap["inserts"]) + sorted(ap["removed"]) or [0]
        ranked = sorted(units, key=lambda u: (-len(name_tokens(u[2].split(".")[-1].replace("[case ", "")) & f2p_tokens),
                                              min(abs(u[0] - x) for x in near), u[0]))
        blocks14.append((bpath, ranked))

    def tests_item(bpath, ranked):
        lines_ = file_defs(bpath)[1]
        n_all = len(ranked)

        def render(L):
            k = max(1, min(S14_TESTS_PER_FILE, L // 2, n_all))
            pick = sorted(ranked[:k], key=lambda u: u[0])
            rows = []
            for ln, one, q, s_, e_ in pick:
                if ln in reg.get(bpath, {}):
                    continue
                rows.append((ln, lines_[ln - 1]))
                if one and one != ln:
                    rows.append((one, lines_[one - 1]))
            hdr = f"### {bpath}: {len(pick)} of {n_all} other tests in this test-patch file (signature + first " \
                  f"assertion line; nearest to the F2P tests first{'' if len(pick) == n_all else ', rest omitted'})"
            return [hdr] + code_block(rows)
        return {"name": f"{bpath} other tests", "fixed": None, "render": render,
                "n": 2 * min(S14_TESTS_PER_FILE, n_all), "path": bpath, "s": 0, "e": -1}

    items14, ptr14 = accept(cands14_ctx, 12)
    items14 += [tests_item(p_, r_) for p_, r_ in blocks14]
    emit_new("s14", "Test-file context: the rest of each file the test patch edits",
             "the set-up, same-file helpers and constants the F2P tests use (in full up to "
             f"{S14_CONTEXT_LINES} lines), and the file's other tests (signature + first assertion line) as a "
             "convention reference.", items14, ptr14, 4, "test-file context item")

    # ---------------------------------------------------------- render
    if omissions or truncations:
        by_sec = defaultdict(Counter)
        for o in omissions:
            by_sec[o["section"]][f"omitted {o['type']}"] += o["n"]
        for t_ in truncations:
            by_sec[t_["section"]][t_["type"]] += t_["n"]
        deg = "; ".join(f"{k}: " + ", ".join(f"{t_} x{n}" for t_, n in v.items()) for k, v in sorted(by_sec.items()))
        deg_line = f"- budget degradations in this pack (each also stated in its section): {clip(deg, 700)}"
    else:
        deg_line = "- budget degradations in this pack: none (every section shown at its content rules)"
    hdr = [f"# Code-evidence pack: {iid}", "",
           f"- repo {repo}, base commit {sha}, version {task.get('version')}; read from {tree.source}",
           f"- issue {len(issue)} chars; hints_text (discussion) {len(hints)} chars (not reproduced here; section 1 "
           f"flags tokens that occur in it)",
           f"- gold patch: {len(gold)} files ({clip(', '.join(f['path'] for f in gold), 800)}); test patch: "
           f"{len(tests)} files ({clip(', '.join(f['path'] for f in tests), 800)})",
           f"- mechanical pack ({BUILDER_VERSION}): lexical search over {stats['corpus_files']} text files of the base "
           f"tree ({', '.join(SEARCH_EXT)}); skipped tokens: keywords, builtins, self/cls, 1-char names, numbers "
           f"0/1/2/-1, strings shorter than 2 chars; docstrings and comments of the gold patch are not tokenised.",
           "- every section starts with a one-line purpose; sections 10-14 appear only when they have content and never "
           "repeat code an earlier section shows (they point to it). Code-block markers: `*` base line the gold patch "
           "(section 5: the test patch) removes or rewrites, `>` the gold patch (section 5: the test patch) inserts "
           "after this line, `!` focus line (e.g. a traceback line).",
           f"- budget: at most {budgets.get('_total', TOKEN_BUDGET)} o200k tokens, applied per section and per item "
           "(never to the whole pack; sections 10-14 are cut first); an item left out is stated where it would appear.",
           deg_line, ""]
    body = hdr[:]
    for k in SECTIONS:
        if sec[k].out:
            body.extend(sec[k].out)
            body.append("")
    text = "\n".join(body).rstrip() + "\n"
    stats.update({
        "n_f2p": len(f2p), "n_f2p_tests": len(seen_tests),
        "n_f2p_located": sum(1 for r in seen_tests.values() if r["kind"]),
        "n_tokens_total": len(items), "n_in_issue": n_in_issue, "n_listed": len(kept), "n_absent": len(absent),
        "n_present": len(present), "n_common": len(common), "n_in_patch_only": len(in_patch_only),
        "n_local_names_dropped": len(n_local),
        "n_absent_f2p": sum(1 for i in absent if i["rank"] < 3),
        "n_absent_f2p_assert": sum(1 for i in absent if i["rank"] == 0),
        "n_absent_test_defined": sum(1 for i in absent if i["kind"] == "ident" and i["value"] in test_defined),
        "n_edited_units": len(units), "n_edited_funcs": len(edited_funcs), "n_new_defs": len(new_defs),
        "n_callers_shown": n_callers, "n_raises_shown": n_raises,
        "n_s7_classes": len(classes), "n_s7_full": n_s7_full, "n_s7_sigs": n_s7_sig,
        "n_s8_helpers": n_s8_helpers, "n_s8_parallel": n_s8_parallel, "n_s8_analogs": n_s8_analogs,
        "n_s9_symbols": n_s9, "n_s9_named": len(named_only),
        "n_f2p_groups": n_groups, "n_f2p_inherited": n_inherited, "s5_region_labels": dict(sorted(s5_labels.items())),
        **{f"n_{k}_items": new_counts.get(k, 0) for k in NEW_SECTIONS},
    })
    return text, {"stats": stats, "missing": missing, "omissions": omissions, "truncations": truncations,
                  "ceiling_cuts": ceiling_cuts,
                  "source": tree.source, "header_tokens": cost(hdr),
                  "section_used": {k: sec[k].used for k in SECTIONS},
                  "section_tokens": {k: ntokens("\n".join(sec[k].out)) for k in SECTIONS}}


# ------------------------------------------------------------------ driver

_ENC = None


def ntokens(text: str) -> int:
    global _ENC
    if _ENC is None:
        import tiktoken
        _ENC = tiktoken.get_encoding("o200k_base")
    return len(_ENC.encode(text, disallowed_special=()))


def load_harness() -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return runpy.run_path(str(HARNESS))["MAP_REPO_VERSION_TO_SPECS"]


def allocate(need: dict, base: dict, avail: int) -> dict:
    """Section budgets: each section gets min(need, base); the rest of avail is water-filled to the sections that need
    more, in proportion to base, capped at their need. Sum <= avail."""
    alloc = {k: min(need[k], base[k]) for k in OLD_SECTIONS}
    pool = avail - sum(alloc.values())
    needy = [k for k in OLD_SECTIONS if need[k] > alloc[k]]
    while pool > 0 and needy:
        w = sum(base[k] for k in needy)
        nxt, given = [], 0
        for k in needy:
            g = min(need[k] - alloc[k], pool * base[k] // w)
            alloc[k] += g
            given += g
            if alloc[k] < need[k]:
                nxt.append(k)
        pool -= given
        if given == 0 or len(nxt) == len(needy):
            break
        needy = nxt
    return alloc


def allocate_v4(need: dict, base: dict, avail: int) -> dict:
    """Sections 10-14 are cut first, whole, in CUT_ORDER; if the pack still does not fit, sections 1-9 get the v3
    allocation (allocate()) of what is left."""
    alloc = dict(need)
    over = sum(need[k] for k in SECTIONS) - avail
    for k in CUT_ORDER:
        if over <= 0:
            return alloc
        c = min(over, alloc[k])
        alloc[k] -= c
        over -= c
    if over > 0:
        alloc.update(allocate(need, base, avail - sum(alloc[k] for k in NEW_SECTIONS)))
    return alloc


def work(task: dict, out_dir: str, total: int, write: bool = True, force_fallback: bool = False) -> dict:
    """Pass 1 builds every section at its content rules (no budget). If the pack exceeds `total`, pass 2 rebuilds
    with per-section budgets (allocate()); the whole pack is never cut. A rare token-accounting overshoot shrinks the
    fullest section and rebuilds."""
    t0 = time.time()
    harness = load_harness()
    ctx: dict = {}
    try:
        unl = {k: UNLIMITED for k in SECTIONS}
        unl["_total"] = total
        text, meta = build(task, harness, unl, ctx, force_fallback)
        demand_tokens = ntokens(text)
        demand = dict(meta["section_used"])
        demand_sec_tokens = dict(meta["section_tokens"])
        nt, passes, alloc = demand_tokens, 1, None
        if nt > total:
            avail = total - meta["header_tokens"] - 250
            base = {k: SECTION_BUDGET[k] * total / 64000 for k in OLD_SECTIONS}
            sc = min(1.0, avail / sum(base.values()))
            base = {k: int(v * sc) for k, v in base.items()}
            alloc = allocate_v4(demand, base, avail)

            def run(al):
                bud = dict(al)
                bud["_total"] = total
                bud["_reserve"] = {k: (NOTICE_RESERVE if al[k] < demand[k] else 0) for k in SECTIONS}
                t_, m_ = build(task, harness, bud, ctx, force_fallback)
                return t_, m_, ntokens(t_)

            for _ in range(4):
                text, meta, nt = run(alloc)
                passes += 1
                if nt <= total:
                    break
                k = next((x for x in CUT_ORDER if alloc[x] > 0), None) or \
                    max(OLD_SECTIONS, key=lambda x: (meta["section_used"][x], x))
                alloc[k] = max(0, alloc[k] - (nt - total) - 200)
            if nt > total:
                raise RuntimeError(f"pack still {nt} tokens > {total} after per-section rebuilds")
            # top-up: the measured slack (accounting is an upper bound) goes to the sections still short of demand
            for _ in range(2):
                short = {k for k in SECTIONS if alloc[k] < demand[k]}
                slack = total - nt - 150
                if not short or slack < 300:
                    break
                al2 = dict(alloc)
                old_short = [k for k in OLD_SECTIONS if k in short]
                if old_short:  # sections 1-9 first (v3 rule), then sections 10-14 in reverse cut order
                    w = sum(base[k] for k in old_short)
                    for k in old_short:
                        al2[k] = min(demand[k], alloc[k] + slack * base[k] // w)
                    slack -= sum(al2[k] - alloc[k] for k in old_short)
                for k in reversed(CUT_ORDER):
                    if k in short and slack > 0:
                        g = min(demand[k] - alloc[k], slack)
                        al2[k] += g
                        slack -= g
                t2, m2, n2 = run(al2)
                passes += 1
                if n2 > total:
                    break
                text, meta, nt, alloc = t2, m2, n2, al2
    finally:
        if "tree" in ctx:
            ctx["tree"].close()
    iid = task["instance_id"]
    if write:
        out = Path(out_dir)
        tmp = out / f".{iid}.md.tmp"
        tmp.write_text(text)
        os.replace(tmp, out / f"{iid}.md")
    return {"id": iid, "repo": task["repo"], "base_commit": task["base_commit"], "source": meta["source"],
            "chars": len(text), "tokens_o200k": nt, "token_budget": total, "demand_tokens_o200k": demand_tokens,
            "header_tokens": meta["header_tokens"], "demand_section_tokens": demand_sec_tokens,
            "demand_section_used": demand,
            "section_tokens": meta["section_tokens"], "section_budget": alloc, "n_builds": passes,
            "sha256": hashlib.sha256(text.encode()).hexdigest(), **meta["stats"],
            "omissions": meta["omissions"], "truncations": meta["truncations"], "ceiling_cuts": meta["ceiling_cuts"],
            "missing": meta["missing"], "elapsed_s": round(time.time() - t0, 2), "builder": BUILDER_VERSION}


def read_ids(spec: str) -> tuple[list[str], str]:
    path, _, label = spec.partition("=")
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    label = label or p.parent.name
    if p.suffix == ".jsonl":
        ids = [json.loads(l).get("task") or json.loads(l).get("instance_id") for l in p.open() if l.strip()]
    else:
        obj = json.loads(p.read_text())
        if isinstance(obj, dict):
            obj = obj.get("tasks") or obj.get("ids") or obj.get("instance_ids")
        ids = [x if isinstance(x, str) else (x.get("task") or x.get("instance_id")) for x in obj]
    return ids, label


def scope_ids() -> list[str]:
    """SWE-Gym-Clean tasks after the environment checks (S1, S2) and exact-duplicate removal (S4)."""
    cfg = json.loads(CLEAN_CONFIG.read_text())
    return sorted(k for k, t in cfg["tasks"].items() if t.get("drop_step") in (None, "S3_quality"))


def main():
    if os.environ.get("PYTHONHASHSEED") != "0":
        # set iteration order (and so float sums over sets, tie order) must not vary between runs
        os.environ["PYTHONHASHSEED"] = "0"
        os.execv(sys.executable, sys.orig_argv)
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", action="append", default=[], help="PATH[=LABEL] (.jsonl with 'task' or .json list)")
    ap.add_argument("--scope", action="store_true", help="add the 2,266 in-scope tasks (label scope2266)")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--budget", type=int, default=TOKEN_BUDGET)
    ap.add_argument("--no-packs", action="store_true", help="index only")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--force-fallback", action="store_true", help="test the /testbed path")
    a = ap.parse_args()
    import pandas as pd

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    wanted, labels = [], {}
    specs = [read_ids(x) for x in a.ids] + ([(scope_ids(), "scope2266")] if a.scope else [])
    for ids, label in specs:
        for i in ids:
            labels.setdefault(i, []).append(label)
            if i not in wanted:
                wanted.append(i)
    if a.only:
        wanted = [i for i in wanted if i in set(a.only)]
    df = pd.read_parquet(PARQUET)
    df = df[df.instance_id.isin(wanted)]
    tasks = {}
    for r in df.to_dict("records"):
        r["FAIL_TO_PASS"], r["PASS_TO_PASS"] = list(r["FAIL_TO_PASS"]), list(r["PASS_TO_PASS"])
        tasks[r["instance_id"]] = r
    rows, failures = {}, {}
    for i in wanted:
        if i not in tasks:
            failures[i] = "not in the SWE-Gym parquet"
    for repo in sorted({t["repo"] for t in tasks.values()}):
        d = ensure_repo(repo)
        for t in tasks.values():
            if t["repo"] == repo and not ensure_commit(d, t["base_commit"]):
                print(f"[warn] {t['instance_id']}: base commit missing from clone; /testbed fallback", flush=True)
    t_start = time.time()
    # largest repos first so that the pool drains evenly
    order = sorted((i for i in wanted if i in tasks), key=lambda i: (tasks[i]["repo"] != "pandas-dev/pandas", i))
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        futs = {ex.submit(work, tasks[i], str(out), a.budget, not a.no_packs, a.force_fallback): i for i in order}
        for fu in as_completed(futs):
            i = futs[fu]
            try:
                row = fu.result()
                row["sets"] = labels[i]
                rows[i] = row
                print(f"[ok] {i} {row['tokens_o200k']} tok (demand {row['demand_tokens_o200k']}), "
                      f"omissions {len(row['omissions'])}, truncations {len(row['truncations'])}, "
                      f"{row['elapsed_s']}s", flush=True)
            except Exception as e:  # noqa: BLE001
                import traceback
                failures[i] = f"{type(e).__name__}: {e}"
                print(f"[FAIL] {i}: {failures[i]}\n{traceback.format_exc()}", flush=True)
    wall = time.time() - t_start
    idx = out / "index.jsonl"
    old = {}
    if idx.exists():
        for l in idx.open():
            if l.strip():
                r = json.loads(l)
                old[r["id"]] = r
    for i, r in rows.items():
        if i in old:
            r["sets"] = sorted(set(old[i].get("sets", [])) | set(r["sets"]))
        old[i] = r
    for i, err in failures.items():
        old[i] = {"id": i, "failed": err, "sets": labels.get(i, []), "builder": BUILDER_VERSION}
    tmp = out / ".index.jsonl.tmp"
    with tmp.open("w") as f:
        for i in sorted(old):
            f.write(json.dumps(old[i], sort_keys=True) + "\n")
    os.replace(tmp, idx)
    print(json.dumps({"built": len(rows), "failed": failures, "wall_s": round(wall, 1), "jobs": a.jobs,
                      "cpu_s": round(sum(r["elapsed_s"] for r in rows.values()), 1)}, indent=1))


if __name__ == "__main__":
    main()
