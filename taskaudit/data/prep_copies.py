"""Copies of the task's own project source outside the working repository, for PREP (``data/prep.sh``).

Why: SWE-Gym pandas and dask images keep newer released versions of the project in the conda package cache
(``/opt/miniconda3/pkgs/pandas-2.2.2-*/.../pandas``, ``dask-core-2024.8.1-*`` and their ``.conda`` / ``.tar.bz2``
archives), where the fix can be read; SWE-bench Pro images also ship ``/build.sh`` and ``/preprocess.sh``.

Self-contained (standard library, Python >= 3.7): PREP writes this file into the task container and runs it with the
image's ``python3``::

    python3 prep_copies.py scan  REPO   # JSON report, changes nothing
    python3 prep_copies.py clean REPO   # remove, then re-scan; exit 3 (fail closed) if any copy remains

Mount points (read-only test data, tmpfs) are not part of the image and are skipped.

Detection is generic (no per-repo names): every tracked source file of ``REPO`` is indexed by its path; a file
outside ``REPO`` whose path ends with at least the last two components of a tracked path is a match, and the matches
of one directory ``D`` against one repo directory ``S`` (the outermost common directory) form a group. ``D`` is a
copy of ``S`` when

- ``S`` is a project root directory: a top-level Python package (``pandas``, ``src/flask``, ``lib/ansible``), a
  direct child of the repository root or of a directory with ``go.mod`` / ``package.json``, or such a directory
  itself; never inside a vendored tree (``extern``, ``externals``, ``vendor``, ``_vendor``, ``third_party``, ...), so
  a third-party library the project vendors is not taken for the project;
- at least ``MIN_MATCH`` tracked files of ``S`` are present in ``D`` and they are at least ``MIN_FRAC`` of the
  tracked source files under ``S`` or directly in ``S``.

Archives (``.whl``, ``.zip``, ``.egg``, Go module zips, tarballs up to ``TAR_MAX``, pip's HTTP cache bodies) are
matched on their member names the same way. Conda archives (``.conda``, ``.tar.bz2``) are matched by package name:
the conda names of the package directories found to be copies, plus every conda package whose recorded files
(``conda-meta`` of any environment, ``info/paths.json`` / ``info/files`` of the package cache) include a project
top-level package's ``__init__.py``.

Kept: anything inside ``REPO``, symlinks (an editable install, egg-link or ``.pth`` points into ``REPO`` and is never
a copy), and a copy whose matched files are byte-identical to the working tree (an environment's non-editable
install of the base commit leaks nothing; version files written at install time are ignored). Every other copy
(``plan``): a conda package-cache entry is deleted together with its archives, as are archives, copies in caches or
outside any environment and the build scripts at the filesystem root (``BUILD_SCRIPTS``: Pro's ``/build.sh`` and
``/preprocess.sh``); a copy inside an environment's site-packages (a dependency such as pip's vendored requests, or
an install of another version) becomes sourceless bytecode, so whatever imports it keeps working; a copy among
installed Go modules or node packages is left alone and fails the check (a build may need it). A copy that would
become bytecode stays (and fails the check) when ``compileall`` returns non-zero: its sources are never deleted
without their bytecode.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile

SOURCE_EXT = (".py", ".pyx", ".pxd", ".pyi", ".c", ".cc", ".cpp", ".h", ".hpp", ".go", ".js", ".jsx", ".ts", ".tsx",
              ".mjs", ".cjs", ".vue", ".svelte", ".rs", ".java", ".rb", ".php")
MIN_MATCH = 5
MIN_FRAC = 0.25
# path components that mark a vendored third-party tree inside the repository
VENDORED = {"extern", "externs", "external", "externals", "vendor", "vendored", "_vendor", "_vendored", "third_party",
            "thirdparty", "third-party", "node_modules", "_externals", "bundled", "_bundled"}
# written at install time, so they differ between the working tree and an install of it
VERSION_FILES = {"_version.py", "version.py", "__version__.py", "_version_meson.py", "__about__.py", "_build_info.py"}
MODULE_MARKERS = ("go.mod", "package.json")
# top-level package names too generic to identify the project's conda package by its files
GENERIC_PACKAGES = {"tests", "test", "testing", "scripts", "script", "benchmarks", "benchmark", "asv_bench", "docs",
                    "doc", "examples", "example", "tools", "tool", "ci", "bench", "utils", "util", "conftest", "setup",
                    "build", "dev", "contrib", "misc", "demo", "demos", "samples", "sample", "integration", "e2e",
                    "fixtures", "data", "src", "lib"}
TAR_EXT = (".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")
TAR_MAX = 200 << 20
ZIP_EXT = (".whl", ".zip", ".egg")
CONDA_ARCHIVE = re.compile(r"^(?P<name>.+)-(?P<version>[^-]+)-(?P<build>[^-]+)\.(?P<ext>conda|tar\.bz2)$")
# pip's HTTP caches hold downloaded wheels / sdists under hashed names
HTTP_CACHE = re.compile(r"/(?:http|http-v2)/")
# Pro images: /build.sh starts a pypi-timemachine at a date that can equal the fix date; /preprocess.sh names the base
# (plus any other ``/*.sh`` or ``/root/*.sh`` mentioning a timemachine)
BUILD_SCRIPTS = ("/build.sh", "/preprocess.sh")
SKIP_TOP = ("/proc", "/sys", "/dev")


# ------------------------------------------------------------------ repository index


def tracked_files(repo):
    """Tracked files of ``repo`` (paths relative to it)."""
    out = subprocess.run(["git", "-c", "core.quotepath=off", "-C", repo, "ls-files", "-z"], capture_output=True,
                         check=True).stdout
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def python_top_packages(files):
    """Top-level Python packages: directories with a tracked ``__init__.py`` whose parent has none."""
    inits = {p[: -len("/__init__.py")] for p in files if p.endswith("/__init__.py")}
    return sorted(d for d in inits if os.path.dirname(d) not in inits)


def project_roots(files):
    """Repo directories a copy may correspond to (module docstring): top-level Python packages, children of the root
    and of ``go.mod`` / ``package.json`` directories, and those directories; nothing inside a vendored tree."""
    dirs = set()
    for p in files:
        d = os.path.dirname(p)
        while d:
            dirs.add(d)
            d = os.path.dirname(d)
    modules = {""} | {os.path.dirname(p) for p in files if os.path.basename(p) in MODULE_MARKERS}
    roots = set(python_top_packages(files))
    for d in dirs:
        if os.path.dirname(d) in modules or d in modules:
            roots.add(d)
    return {r for r in roots if r and not (set(r.split("/")) & VENDORED)}


def import_packages(files):
    """Names of the project's importable top-level packages (at the root, ``src/``, ``lib/`` or ``python/``), minus
    generic names (``tests``, ``scripts``, ...): used to recognise the project's conda package by its files."""
    out = set()
    for d in python_top_packages(files):
        name, parent = os.path.basename(d), os.path.dirname(d)
        if parent in ("", "src", "lib", "python") and name.isidentifier() and name not in GENERIC_PACKAGES | VENDORED:
            out.add(name)
    return sorted(out)


class Index:
    def __init__(self, repo, files=None):
        self.repo = repo.rstrip("/") or "/"
        files = tracked_files(repo) if files is None else files
        self.files = [p for p in files if p.endswith(SOURCE_EXT) and "/" in p]
        self.packages = import_packages(files)
        self.roots = project_roots(files)
        self.by_key = {}
        self.under = {}  # repo dir -> number of indexed source files below it
        self.direct = {}  # repo dir -> number of indexed source files directly in it
        for p in self.files:
            parts = tuple(p.split("/"))
            self.by_key.setdefault(parts[-2:], []).append(parts)
            d = os.path.dirname(p)
            self.direct[d] = self.direct.get(d, 0) + 1
            while d:
                self.under[d] = self.under.get(d, 0) + 1
                d = os.path.dirname(d)

    def match(self, parts):
        """[(copy dir, repo dir, repo path)] for the longest path-suffix matches of an outside file (``parts``: its
        path components), or []."""
        cands = self.by_key.get(tuple(parts[-2:]))
        if not cands:
            return []
        best, out = 0, []
        for r in cands:
            k = 2
            while k < len(r) and k < len(parts) - 1 and r[-k - 1] == parts[-k - 1]:
                k += 1
            if k > best:
                best, out = k, [r]
            elif k == best:
                out.append(r)
        return [("/".join(parts[: len(parts) - best + 1]), "/".join(r[: len(r) - best + 1]), "/".join(r)) for r in out]


# ------------------------------------------------------------------ filesystem walk


def mount_points():
    """Mount points other than ``/`` (bind-mounted runtime, tmpfs): not part of the image."""
    out = set()
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                fields = line.split()
                if len(fields) > 1:
                    mp = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[1])
                    if mp != "/":
                        out.add(mp)
    except OSError:
        pass
    return out


def walk(root, prune):
    """Every non-symlink file below ``root`` (no descent into symlinked directories, pruned paths or mounts)."""
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if os.path.join(dp, d) not in prune]
        for fn in fns:
            yield dp, fn


def is_copy(n, den, n_top, den_top, root):
    """At least ``MIN_MATCH`` files and ``MIN_FRAC`` of the repo directory's files, either below it or directly in it
    (a vendored subtree inside the repo directory, e.g. old requests' ``requests/packages/urllib3``, is not in other
    copies, so it must not dilute the fraction)."""
    return bool(root) and n >= MIN_MATCH and (n >= MIN_FRAC * den or (den_top > 0 and n_top >= MIN_FRAC * den_top
                                                                         and n_top >= MIN_MATCH))


def stem_name(stem):
    """Conda package name of a ``name-version-build`` stem, or None."""
    m = CONDA_ARCHIVE.match(stem + ".conda")
    return m.group("name") if m else None


def conda_entry(path):
    """``(<pkgs>/<stem>, name)`` when ``path`` lies in an extracted conda package-cache entry (a directory with
    ``info/index.json`` that is not an environment), else None."""
    parts = path.split("/")
    for i in range(len(parts) - 1, 1, -1):
        base = "/".join(parts[: i + 1])
        is_env = os.path.isdir(os.path.join(base, "conda-meta"))
        if os.path.isfile(os.path.join(base, "info", "index.json")) and not is_env:
            return base, stem_name(parts[i])
    return None


def conda_names_shipping(packages, meta_files, info_files):
    """Conda package names whose recorded files include ``site-packages/<pkg>/__init__.py`` for a project package:
    ``conda-meta/*.json`` of every environment (``meta_files``) and ``info/paths.json`` / ``info/files`` of every
    package-cache entry (``info_files``: entry -> file)."""
    if not packages:
        return set()
    needles = [f"site-packages/{p}/__init__.py" for p in packages]
    pattern = re.compile(r'(?:^|[\s"/])(?:' + "|".join(re.escape(n) for n in needles) + r')(?:["\s]|$)', re.MULTILINE)
    names = set()
    for path, name in [(f, None) for f in meta_files] + [(f, e) for e, f in info_files.items()]:
        try:
            with open(path, errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if not any(n in text for n in needles):
            continue
        if not pattern.search(text):
            continue
        name = stem_name(os.path.basename(path)[: -len(".json")] if name is None else os.path.basename(name))
        if name:
            names.add(name)
    return names


def archive_members(path):
    """Member file names of a zip or (at most ``TAR_MAX`` bytes) tar archive; [] when unreadable."""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                return [n for n in z.namelist() if not n.endswith("/")]
        if os.path.getsize(path) <= TAR_MAX:
            with tarfile.open(path) as t:
                return [m.name for m in t if m.isfile()]
    except (zipfile.BadZipFile, tarfile.TarError, OSError, ValueError, RuntimeError, NotImplementedError, EOFError):
        pass
    return []


def _looks_archive(path):
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head == b"PK\x03\x04" or head[:2] == b"\x1f\x8b"


def identical(copy_path, repo_path):
    try:
        if os.path.getsize(copy_path) != os.path.getsize(repo_path):
            return False
        with open(copy_path, "rb") as f, open(repo_path, "rb") as g:
            return f.read() == g.read()
    except OSError:
        return False


def timemachine_scripts(root):
    """Other shell scripts at the root or in ``/root`` (under 1 MB) that pin a package index to a date."""
    out = []
    for d in (root, os.path.join(root, "root")):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            p = os.path.join(d, fn)
            if fn.endswith(".sh") and os.path.isfile(p) and os.path.getsize(p) < 1 << 20:
                with open(p, "rb") as f:
                    if b"timemachine" in f.read().lower():
                        out.append(p)
    return out


def scan(repo, root="/", files=None, extra_prune=()):
    """Report of the copies of ``repo``'s source below ``root`` (module docstring); changes nothing."""
    t0 = time.time()
    idx = Index(repo, files)
    base = root.rstrip("/") or "/"
    prune = {os.path.realpath(idx.repo), idx.repo} | set(extra_prune)
    if base == "/":
        prune |= set(SKIP_TOP)
    prune |= {m for m in mount_points() if m != base and (base == "/" or m.startswith(base + "/"))}
    groups = {}  # (copy dir, repo dir) -> {repo path: copy path (None inside an archive)}
    archives, conda_archives, meta_files, info_files = [], [], [], {}
    n_files = 0
    for dp, fn in walk(base, prune):
        n_files += 1
        path = os.path.join(dp, fn)
        ca = CONDA_ARCHIVE.match(fn)
        if ca:
            conda_archives.append((path, ca.group("name")))
        elif fn.endswith(ZIP_EXT + TAR_EXT) or (HTTP_CACHE.search(dp + "/") and _looks_archive(path)):
            archives.append(path)
        if dp.endswith("/conda-meta") and fn.endswith(".json"):
            meta_files.append(path)
        elif dp.endswith("/info") and fn in ("paths.json", "files"):
            entry = dp[: -len("/info")]
            if fn == "paths.json" or entry not in info_files:
                info_files[entry] = path
        if os.path.islink(path):
            continue
        for d, s, r in idx.match(path.split("/")):
            groups.setdefault((d, s), {})[r] = path
    for a in archives:  # members as files below "<archive>!"
        for m in archive_members(a):
            for d, s, r in idx.match((a + "!/" + m.lstrip("./")).split("/")):
                groups.setdefault((d, s), {})[r] = None
    copies = []
    for (d, s), hits in sorted(groups.items()):
        den = idx.under.get(s, 0)
        n_top = sum(1 for r in hits if os.path.dirname(r) == s)
        if not is_copy(len(hits), den, n_top, idx.direct.get(s, 0), s in idx.roots):
            continue
        rec = {"copy": d, "repo_dir": s, "matched": len(hits), "repo_files": den}
        if "!/" in d:
            rec["archive"] = d.split("!/", 1)[0]
        else:
            same = {r for r, p in hits.items() if identical(p, os.path.join(idx.repo, r))}
            rec["identical"] = len(same)
            rec["base_copy"] = all(r in same or os.path.basename(r) in VERSION_FILES for r in hits)
        ce = conda_entry(rec.get("archive") or d)
        if ce:
            rec["conda_entry"], rec["conda_name"] = ce
        copies.append(rec)
    names = {c["conda_name"] for c in copies if c.get("conda_name") and not c.get("base_copy")}
    names |= conda_names_shipping(idx.packages, meta_files, info_files)
    conda_items = sorted({p for p, n in conda_archives if n in names}
                         | {e for e in info_files if stem_name(os.path.basename(e)) in names})
    scripts = [q for q in (os.path.join(root, p.lstrip("/")) for p in BUILD_SCRIPTS) if os.path.lexists(q)]
    scripts += [q for q in timemachine_scripts(root) if q not in scripts]
    return {"repo": idx.repo, "packages": idx.packages, "files_indexed": len(idx.files), "files_walked": n_files,
            "archives_checked": len(archives), "copies": copies, "conda_names": sorted(names),
            "conda_items": conda_items, "build_scripts": scripts,
            "seconds": round(time.time() - t0, 2)}


# package caches (a conda cache entry without its info/ also counts: ``name-version-build`` below ``pkgs``)
CACHE_DIRS = re.compile(r"/(?:\.cache|\.npm|\.yarn|\.pnpm-store|pkg/mod/cache)/|/pkgs/[^/]+-[^/-]+-[^/-]+/")
# installed dependencies of other ecosystems (Go modules, node packages outside the repository): no safe removal
DEPENDENCY_DIRS = re.compile(r"/pkg/mod/[^!]*@[^/]+/|/node_modules/")
SITE = re.compile(r"^(?P<prefix>.*)/lib/python(?P<ver>\d+\.\d+)/(?:site|dist)-packages(?:/|$)"
                  r"|^/usr/lib/python3/dist-packages(?:/|$)")


def interpreter(path):
    """The Python that imports from the site-packages tree holding ``path`` (its bytecode must match), or None."""
    m = SITE.match(path)
    if not m:
        return None
    if m.group("prefix") is None:  # Debian's dist-packages
        cands, ver = ["/usr/bin/python3"], None
    else:
        ver = m.group("ver")
        cands = [f"{m.group('prefix')}/bin/python{ver}", f"{m.group('prefix')}/bin/python3"]
    for py in cands:
        try:
            out = subprocess.run([py, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], capture_output=True,
                                 text=True, timeout=60).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if out and (ver is None or out == ver):
            return py
    return None


def plan(report):
    """[(action, path)] for a ``scan`` report. ``delete``: conda cache entries with their archives, archives, copies
    in caches or outside any environment, build scripts. ``bytecode``: a copy inside an environment's site-packages
    (a dependency such as pip's vendored requests or conda's own requests, or a non-editable install of another
    version) is replaced by sourceless bytecode, so whatever imports it keeps working while no source is left. A
    copy among a Go module cache's or a node_modules' installed packages gets no action: a build may need it, so it
    stays and the check fails closed."""
    out = []
    for c in report["copies"]:
        if c.get("base_copy"):
            continue
        if c.get("conda_entry"):
            e = c["conda_entry"]
            out += [("delete", e), ("delete", e + ".conda"), ("delete", e + ".tar.bz2")]
        elif c.get("archive"):
            out.append(("delete", c["archive"]))
        elif CACHE_DIRS.search(c["copy"] + "/"):
            out.append(("delete", c["copy"]))
        elif SITE.match(c["copy"]):
            out.append(("bytecode", c["copy"]))
        elif not DEPENDENCY_DIRS.search(c["copy"] + "/"):
            out.append(("delete", c["copy"]))
        # else: a Go module or node package a build may require; left in place, so the check fails closed
    out += [("delete", p) for p in report["conda_items"] + report["build_scripts"]]
    seen, uniq = set(), []
    for a, p in out:
        if p not in seen and os.path.lexists(p):
            seen.add(p)
            uniq.append((a, p))
    # a path below a deleted one goes with it
    dels = [p for a, p in uniq if a == "delete"]
    return [(a, p) for a, p in uniq if not any(p != q and p.startswith(q.rstrip("/") + "/") for q in dels)]


def remaining(report):
    """What a fail-closed check refuses: a non-base copy, a conda item of the project or a build script."""
    return ([c["copy"] for c in report["copies"] if not c.get("base_copy")] + report["conda_items"]
            + report["build_scripts"])


def _rm(path):
    if stat.S_ISDIR(os.lstat(path).st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def to_bytecode(path):
    """Compile ``path``'s modules to legacy ``.pyc`` files beside them (``compileall -b``, the importing
    interpreter), then delete every source file (``SOURCE_EXT``) and ``__pycache__``. A non-zero return code from
    compileall raises before anything is deleted (the copy stays, so the check fails closed)."""
    py = interpreter(path)
    if py is None:
        raise OSError("no interpreter for " + path)
    # Python 2's compileall writes the legacy foo.pyc beside foo.py already and has no -b (it exits 2 on it): a py2.7
    # environment's pip vendors requests (Verified psf__requests-1724)
    major = subprocess.run([py, "-c", "import sys; print(sys.version_info[0])"], capture_output=True, text=True,
                           timeout=60).stdout.strip()
    r = subprocess.run([py, "-m", "compileall", "-q"] + (["-b"] if major != "2" else []) + ["-f", path],
                       capture_output=True, timeout=1800)
    if r.returncode != 0:
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        raise OSError("compileall rc %d: %s" % (r.returncode, out[-400:]))
    for dp, dns, fns in os.walk(path):
        for fn in fns:
            if fn.endswith(SOURCE_EXT):
                os.unlink(os.path.join(dp, fn))
        for d in [d for d in dns if d == "__pycache__"]:
            shutil.rmtree(os.path.join(dp, d))
            dns.remove(d)


def clean(repo, root="/", files=None, extra_prune=()):
    """Remove the copies (``plan``), re-scan, and report what remains (fail closed on it)."""
    before = scan(repo, root, files, extra_prune)
    actions = plan(before)
    errors = []
    for action, p in actions:
        try:
            if action == "bytecode":
                to_bytecode(p)
            else:
                _rm(p)
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"{action} {p}: {e}")
    after = scan(repo, root, files, extra_prune) if actions else before
    return {"removed": [p for a, p in actions if a == "delete"], "bytecode": [p for a, p in actions if a == "bytecode"],
            "errors": errors, "copies": before["copies"], "conda_names": before["conda_names"],
            "remaining": remaining(after),
            "base_copies": [c["copy"] for c in after["copies"] if c.get("base_copy")],
            "files_walked": before["files_walked"],
            "seconds": round(before["seconds"] + after["seconds"], 2)}


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(prog="prep_copies.py")
    ap.add_argument("cmd", choices=("scan", "clean"))
    ap.add_argument("repo")
    try:
        args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        return 2
    if args.cmd == "scan":
        print(json.dumps(scan(args.repo)))
        return 0
    if not (os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")):
        sys.stderr.write("prep_copies: clean runs only inside a task container\n")
        return 4
    rep = clean(args.repo)
    print(json.dumps(rep))
    return 3 if rep["remaining"] or rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
