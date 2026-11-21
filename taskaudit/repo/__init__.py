"""Offline repository access at the base commit: bare clones, post-images, F2P test sources, word presence.

Bare clones live in ``<repos_dir>/<owner>__<name>.git`` (default ``<work_dir>/repos``; cloned from GitHub on first
use, or provided by the user for repositories that are not on GitHub). The pack builder reads the same directory.
"""
from __future__ import annotations

import ast
import functools
import os
import re
import subprocess
import tempfile
from pathlib import Path

from ..util import clip_mid


class Repos:
    def __init__(self, cfg: dict):
        d = (cfg.get("dataset") or {}).get("repos_dir") or str(Path(cfg["work_dir"]) / "repos")
        self.dir = Path(d).expanduser().resolve()

    def bare(self, repo: str) -> Path:
        return self.dir / (repo.replace("/", "__") + ".git")

    def ensure(self, repo: str, commit: str | None = None) -> Path:
        d = self.bare(repo)
        if not d.is_dir():
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = d.with_name(d.name + ".tmp")
            subprocess.run(["rm", "-rf", str(tmp)])
            subprocess.run(["git", "clone", "-q", "--bare", f"https://github.com/{repo}", str(tmp)], check=True,
                           timeout=3600)
            os.rename(tmp, d)
        if commit and not self.has(repo, commit):
            subprocess.run(["git", "--git-dir", str(d), "fetch", "-q", "origin", commit], capture_output=True, timeout=900)
        return d

    def has(self, repo: str, commit: str) -> bool:
        return subprocess.run(["git", "--git-dir", str(self.bare(repo)), "cat-file", "-e", commit + "^{commit}"],
                              capture_output=True).returncode == 0

    def git(self, repo: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "--git-dir", str(self.bare(repo)), *args], capture_output=True, timeout=timeout)

    def base_file(self, repo: str, base: str, path: str) -> str | None:
        p = self.git(repo, "show", f"{base}:{path}")
        return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else None

    def present_at_base(self, repo: str, commit: str, tokens, globs=("*.py", "*.pyi", "*.pyx", "*.pxd")) -> set[str]:
        """Whole-word presence of tokens in the base tree (git grep, offline)."""
        toks = sorted({x for x in tokens if x and re.fullmatch(r"[\w.\-]+", x)})
        if not toks or not self.bare(repo).is_dir():
            return set()
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("\n".join(toks) + "\n")
            fn = f.name
        try:
            out = self.git(repo, "grep", "-h", "-o", "-w", "-F", "-I", "-f", fn, commit, "--", *globs).stdout.decode(
                "utf-8", "replace")
        finally:
            os.unlink(fn)
        want = set(toks)
        return {x for x in out.splitlines() if x in want} | {x.split(":", 1)[-1] for x in out.splitlines()
                                                             if x.split(":", 1)[-1] in want}

    # ------------------------------------------------------------------ post-images
    def apply_to_base(self, t: dict, patch: str, extra_paths=()) -> dict:
        """{files: {path: post-image}, apply: status}: ``patch`` applied to the base versions of the files it touches."""
        repo, base = t["repo"], t["base_commit"]
        paths = sorted({b for _, b in patch_files(patch)} | {a for a, _ in patch_files(patch)} | set(extra_paths))
        with tempfile.TemporaryDirectory(prefix="ta-post-") as d:
            for p in paths:
                txt = self.base_file(repo, base, p)
                if txt is not None:
                    fp = Path(d) / p
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_text(txt)
            (Path(d) / ".tp.patch").write_text(patch)
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            ap = subprocess.run(["git", "apply", "--whitespace=nowarn", "--unsafe-paths", ".tp.patch"], cwd=d,
                                capture_output=True, env=env)
            status = "git_apply" if ap.returncode == 0 else None
            if status is None:
                pp = subprocess.run(["patch", "-p1", "-f", "--no-backup-if-mismatch", "-i", ".tp.patch"], cwd=d,
                                    capture_output=True)
                status = "patch_fuzz" if pp.returncode == 0 else "apply_failed: " + ap.stderr.decode()[:300]
            files = {}
            for p in paths:
                fp = Path(d) / p
                if fp.is_file():
                    files[p] = fp.read_text(errors="replace")
        return {"files": files, "apply": status}

    def post_images(self, t: dict) -> dict:
        """The test-patch files and the F2P files after the test patch."""
        return self.apply_to_base(t, t["test_patch"], {fid.split("::")[0] for fid in t["FAIL_TO_PASS"]})


def patch_files(patch: str) -> list[tuple[str, str]]:
    return re.findall(r"^diff --git a/(\S+) b/(\S+)", patch or "", re.M)


def render_numbered(patch: str) -> str:
    """Unified diff with post-image line numbers on context and added lines."""
    outl, new = [], None
    for line in (patch or "").splitlines():
        if line.startswith("diff --git"):
            outl.append(f"### {line[11:]}")
            new = None
            continue
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", line)
        if m:
            new = int(m.group(1))
            outl.append(f"@@ (post-image line {new}){m.group(2)}")
            continue
        if new is None:
            if line.startswith(("new file", "deleted file", "rename ")):
                outl.append(f"    [{line}]")
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("-"):
            outl.append(f"{'':>6} -{line[1:]}")
        elif line.startswith("+"):
            outl.append(f"{new:>6} +{line[1:]}")
            new += 1
        else:
            outl.append(f"{new:>6}  {line[1:] if line.startswith(' ') else line}")
            new += 1
    return "\n".join(outl)


def find_py_func(src: str, quals: list[str]) -> tuple[int, int] | None:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    *classes, func = quals

    def cands(name: str) -> list[str]:
        base = re.sub(r"\[.*$", "", name)
        out_ = [base] + [base[:m.start()] for m in re.finditer(r"_\d+", base)][::-1]
        return list(dict.fromkeys(out_))

    def find_class(dotted: list[str]):
        node, scope_ = None, tree.body
        for part in dotted:
            nxt_ = [n for n in scope_ if isinstance(n, ast.ClassDef) and n.name == part]
            if not nxt_:
                return None
            node, scope_ = nxt_[0], nxt_[0].body
        return node

    queue, seen = ([find_class(classes)] if classes else [tree]), set()
    while queue:
        c = queue.pop(0)
        if c is None or id(c) in seen:
            continue
        seen.add(id(c))
        for name in cands(func):
            for n in c.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                    return min([d.lineno for d in n.decorator_list] + [n.lineno]), n.end_lineno
        for b in getattr(c, "bases", []):
            parts, x = [], b
            while isinstance(x, ast.Attribute):
                parts.insert(0, x.attr)
                x = x.value
            if isinstance(x, ast.Name):
                queue.append(find_class([x.id] + parts))
    for name in cands(func):  # inherited from another class of the same module
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                return min([d.lineno for d in n.decorator_list] + [n.lineno]), n.end_lineno
    return None


def find_case(files: dict[str, str], name: str, data_file: str | None = None) -> tuple[str, int, int] | None:
    """A data-driven ``[case NAME]`` block (mypy-style ``.test`` files); the id's data file is preferred."""
    names = [name] + ([name[: -len("_cached")]] if name.endswith("_cached") else [])
    order = sorted(files, key=lambda p: (not (data_file and p.endswith("/" + data_file)), p))
    for nm in names:
        pat = re.compile(rf"^\[case {re.escape(nm)}(\]|-)", re.M)
        for p in order:
            if not p.endswith(".test"):
                continue
            m = pat.search(files[p])
            if m:
                start = files[p][: m.start()].count("\n") + 1
                lines = files[p].splitlines()
                end = start
                while end < len(lines) and not lines[end].startswith("[case "):
                    end += 1
                return p, start, end
    return None


def test_units(t: dict, files: dict[str, str], ids: list[str], repos: Repos | None = None) -> list[dict]:
    """Source location (post-image) of graded test ids, grouped by the definition they resolve to."""
    units: dict[tuple, dict] = {}
    files = dict(files)
    for fid in ids:
        parts = fid.split("::")
        path, quals = parts[0], parts[1:]
        if path not in files and path.endswith(".py") and repos is not None:
            txt = repos.base_file(t["repo"], t["base_commit"], path)
            if txt is not None:
                files[path] = txt
        hit = None
        data_driven = any(q.endswith(".test") for q in quals)
        if not data_driven and files.get(path) and quals:
            hit = find_py_func(files[path], quals)
            if hit:
                u = units.setdefault((path, hit), {"file": path, "start": hit[0], "end": hit[1], "ids": []})
        if not hit:
            mh = find_case(files, parts[-1], next((q for q in quals if q.endswith(".test")), None))
            if mh:
                hit = mh
                u = units.setdefault(("case", mh[0], mh[1]), {"file": mh[0], "start": mh[1], "end": mh[2], "ids": []})
        if not hit:
            u = units.setdefault(("missing", fid), {"file": None, "start": None, "end": None, "ids": []})
        u["ids"].append(fid)
    return list(units.values()), files


def render_units(units: list[dict], files: dict[str, str], budget: int = 45000) -> str:
    out_, used = [], 0
    for u in units:
        head = f"### {u['ids'][0]}" + (f"  (+{len(u['ids']) - 1} more ids of this test)" if len(u["ids"]) > 1 else "")
        if not u["file"]:
            out_.append(head + "\n[source not located]")
            continue
        lines = files[u["file"]].splitlines()
        sel = list(range(u["start"], min(u["end"], len(lines)) + 1))
        if len(sel) > 220:
            sel = sel[:150] + [None] + sel[-60:]
        body = "\n".join(f"{i:>6}  {lines[i - 1]}" if i else "   ...  [lines omitted]" for i in sel)
        block = f"{head}\n{u['file']} lines {u['start']}-{u['end']}:\n{body}"
        if used + len(block) > budget:
            out_.append(head + f"\n{u['file']} lines {u['start']}-{u['end']} [omitted: input budget]")
            continue
        used += len(block)
        out_.append(block)
    return "\n\n".join(out_)


class TaskSources:
    """Per-task cached test-side views: post-images, numbered test patch, F2P / any-id test sources."""

    def __init__(self, repos: Repos, t: dict):
        self.repos, self.t = repos, t

    @functools.cached_property
    def post(self) -> dict:
        return self.repos.post_images(self.t)

    @functools.cached_property
    def gold(self) -> dict:
        return self.repos.apply_to_base(self.t, self.t["patch"])

    def numbered_patch(self) -> str:
        return render_numbered(self.t["test_patch"])

    def f2p_sources(self) -> str:
        units, files = test_units(self.t, self.post["files"], self.t["FAIL_TO_PASS"], self.repos)
        return render_units(units, files)

    def sources_of(self, ids: list[str]) -> str:
        units, files = test_units(self.t, self.post["files"], ids, self.repos)
        return render_units(units, files)

    def test_line(self, file: str, line: int) -> str:
        for p, txt in self.post["files"].items():
            if p.endswith(file) or file.endswith(p):
                ls = txt.splitlines()
                return ls[line - 1].strip() if 0 < line <= len(ls) else ""
        return ""

    # ---------------------------------------------------------------- REPOSITORY ACCESS (4a / 4b)
    def _numbered(self, txt: str, start, end, cap: int = 400) -> str:
        lines = txt.splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), int(end or start + 199), start + cap - 1)
        return "\n".join(f"{i:>6}  {lines[i - 1]}" for i in range(start, end + 1)) + (
            f"\n[file has {len(lines)} lines]" if end < len(lines) else "")

    def run_request(self, q: dict) -> str:
        repo, base = self.t["repo"], self.t["base_commit"]
        op = q.get("op")
        try:
            if op == "show":
                txt = self.repos.base_file(repo, base, str(q.get("path", "")).lstrip("/"))
                return "[no such file at the base commit]" if txt is None else self._numbered(txt, q.get("start"), q.get("end"))
            if op in ("test_file", "gold_file"):
                p = str(q.get("path", "")).lstrip("/")
                src = self.post if op == "test_file" else self.gold
                txt = src["files"].get(p) or self.repos.base_file(repo, base, p)
                return "[no such file]" if txt is None else self._numbered(txt, q.get("start"), q.get("end"))
            if op == "grep":
                args = ["grep", "-n", "-I", "-E", "-e", str(q.get("pattern", ""))[:300], base]
                if q.get("path"):
                    args += ["--", str(q["path"]).lstrip("/")]
                lines = [x.split(":", 1)[1] if x.startswith(base + ":") else x
                         for x in self.repos.git(repo, *args).stdout.decode("utf-8", "replace").splitlines()]
                out = "\n".join(x[:300] for x in lines[:80])
                return (out or "[no match]") + (f"\n[{len(lines)} hits, first 80 shown]" if len(lines) > 80 else "")
            if op == "ls":
                p = self.repos.git(repo, "ls-tree", "--name-only", f"{base}:{str(q.get('path', '')).strip('/')}")
                return p.stdout.decode("utf-8", "replace")[:6000] or "[no such directory]"
        except Exception as exc:
            return f"[request failed: {type(exc).__name__}]"
        return "[unknown op]"

    def answer(self, reqs: list) -> str:
        import json

        out = []
        for i, q in enumerate(reqs[:8], 1):
            q = q if isinstance(q, dict) else {}
            out.append(f"--- request {i}: {json.dumps(q)[:300]}\n{clip_mid(self.run_request(q), 24000)}")
        return "\n\n".join(out)
