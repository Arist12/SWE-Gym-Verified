"""Grader guards of the SWE-Gym harness run (copied verbatim from the authors' grader; do not edit).

eval_script() wraps the SWE-Gym fork's eval script (github.com/SWE-Gym/SWE-Bench-Fork @ 242429c1) with
the test-file / test-config reset, the test-patch collision guard and the runtime-config check;
parse_status() / test_outcome() read per-test statuses with the fork's log parsers.
"""
from __future__ import annotations

import codecs
import re
import shlex
import subprocess
from collections import defaultdict


TASK_ENV = {
    "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1", "PYTEST_XDIST_AUTO_NUM_WORKERS": "2", "PYTHON_CPU_COUNT": "2",
    "PYTHONPATH": "/tmp/chk_site",
}


# os.cpu_count() reports all 384 host CPUs inside a 2-CPU cpuset; thread/process pools
# sized from it exceed the 128-PID limit ("can't start new thread"). Report the cpuset.
SITECUSTOMIZE = "import os\nos.cpu_count = lambda: len(os.sched_getaffinity(0))\n"


RESOURCE_ERRORS = re.compile(
    r"can't start new thread|Resource temporarily unavailable|Cannot allocate memory|MemoryError|fork: retry"
)


TEST_PATH = re.compile(
    r"(^|/)(tests?|testing|test-data)(/|$)|(^|/)test_[^/]*$|_tests?\.py$|(^|/)conftest\.py$"
)


TEST_PATCH_MARK = "CHK_TEST_PATCH_FAIL"


COLLISION_MARK = "CHK_TEST_PATCH_COLLISION"  # one line per path the agent occupied where the test patch needs one


TEST_RESET_MARK = "CHK_TEST_RESET"  # one line per test file / test configuration of the model patch put back


TEST_RESET_FAIL_MARK = "CHK_TEST_RESET_FAIL"


# the SWE-Gym fork's test-file reset list (swebench.harness.test_spec.DIFF_MODIFIED_FILE_REGEX)
FORK_RESET = re.compile(r"--- a/(.*)")


TESTS_START = "CHK_TESTS_START"


RUNTIME_CONFIG_MARK = "CHK_RUNTIME_CONFIG"  # one line per conftest / pytest-config file created or changed during the run


# Grade rows' ``grader``: guard4 = guard3 plus runtime-config detection (a conftest.py / pytest config the patch's code
# writes while the tests run, not in the diff, so the static test_reset / collision guards miss it; a nested conftest
# in a not-yet-collected directory is loaded and can force tests to pass under newer pytest — 2026-10-14 review). guard3
# = guard2 (collision guard with non-directory parents, test-file and test-configuration reset (test_reset_guard),
# whole-line markers, TIMEOUT; 2026-10-13) with setup.cfg / tox.ini / pyproject.toml put back whole when a change
# touches pytest (CONFIG_RESTORE: dotted keys, keys before the first section, TOML escapes; was: the pytest sections
# only, by section name). Older tags ("swegym_harness", "swegym_harness:pids1024", "swegym_harness:guard2",
# "swegym_harness:guard3") predate it.
GRADER = "swegym_harness:guard4"


# The test-run timeout of grading = the one the frozen spec was verified under (runtime_profile.timeout_seconds): the
# slowest verified gold runs take ~2,300 s (MONAI-6514), so a shorter grading timeout fails correct patches.
TIMEOUT = 2400


# Test configuration outside the test patch that changes which tests run or pass (the fork resets only the test
# patch's files; test_reset_guard puts a model patch's copies back, as it does its test files, TEST_PATH). Pro V2's
# test.sh resets every conftest.py; pytest also reads pytest.ini / pytest.toml and the pytest sections below in any
# directory between the rootdir and the tests, and Python imports sitecustomize / usercustomize / .pth files from
# sys.path (conan's eval script puts the repo on PYTHONPATH).
TEST_CONFIG_FILE = re.compile(r"(^|/)(conftest\.py|\.?pytest\.(ini|toml)|sitecustomize\.py|usercustomize\.py|[^/]+\.pth)$")


# ... and files that also hold other settings (setup.cfg [tool:pytest], tox.ini [pytest], pyproject.toml
# [tool.pytest.ini_options], pytest11 plugin entry points): put back whole only when a change touches pytest
# (CONFIG_RESTORE), otherwise the agent's edits there stay
TEST_CONFIG_SECTIONS = re.compile(r"(^|/)(setup\.cfg|tox\.ini|pyproject\.toml)$")


# TOML \uXXXX / \UXXXXXXXX escapes: a quoted key "pytest" is pytest to the TOML parser
TOML_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})")


def pytest_text(path: str, line: str) -> bool:
    """Whether a line of a section file mentions pytest as pytest reads it (case-insensitive; TOML escapes decoded).
    The same rule as CONFIG_RESTORE's ``mentions`` (tests/test_grader_guard3.py: both catch each TOML hack)."""
    if path.endswith(".toml"):
        line = TOML_ESCAPE.sub(lambda m: chr(min(int(m.group(1) or m.group(2), 16), 0x10FFFF)), line)
    return "pytest" in line.lower()


# Run inside the task container by the testbed's Python (>= 3.6, isolated mode): argv = base commit, mark, paths. A
# path whose change from the base commit touches pytest's configuration is put back whole (checked out at the base
# commit, removed if new) and ``mark path`` is printed; other paths stay as the agent left them. A change touches
# pytest when a changed line (either side) mentions pytest (any section, dotted key, comment or dependency line;
# TOML escapes decoded), or a changed line lies in a part of the file that mentions pytest on that side: a section
# from its header to the next one, or the lines before the first header (an existing [tool:pytest] / [pytest] /
# [tool.pytest.ini_options] section, a pytest11 entry-point list, a [tool] table with dotted pytest.* keys).
CONFIG_RESTORE = r"""
import difflib, os, re, shutil, subprocess, sys
base, mark, paths = sys.argv[1], sys.argv[2], sys.argv[3:]
HEAD = {"ini": re.compile(r"^[ \t]*\[[^\]\r\n]+\][ \t]*(?:[#;].*)?$"),
        "toml": re.compile(r"^[ \t]*\[\[?.+?\]\]?[ \t]*(?:#.*)?$")}
ESC = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})")


def mentions(path, line):
    if path.endswith(".toml"):
        line = ESC.sub(lambda m: chr(min(int(m.group(1) or m.group(2), 16), 0x10FFFF)), line)
    return "pytest" in line.lower()


def parts(path, lines):
    head = HEAD["toml" if path.endswith(".toml") else "ini"]
    starts = [0] + [i for i, x in enumerate(lines) if i and head.match(x)]
    return [range(a, b) for a, b in zip(starts, starts[1:] + [len(lines)])]


def touches_pytest(path, old, cur):
    a, b = old.splitlines(), cur.splitlines()
    ch_a, ch_b = set(), set()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            ch_a.update(range(i1, i2))
            ch_b.update(range(j1, j2))
    for lines, ch in ((a, ch_a), (b, ch_b)):
        if any(mentions(path, lines[i]) for i in ch):
            return True
        for r in parts(path, lines):
            if ch.intersection(r) and any(mentions(path, lines[i]) for i in r):
                return True
    return False


def git(*args):
    p = subprocess.run(["git"] + list(args), stdout=subprocess.PIPE)
    if p.returncode:
        sys.exit("git " + " ".join(args) + " failed")
    return p.stdout.decode("utf-8", "surrogateescape")


def read(path):
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


for path in paths:
    tracked = bool(git("ls-tree", "--name-only", base, "--", path).strip())
    if not touches_pytest(path, git("show", base + ":" + path) if tracked else "", read(path)):
        continue
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)
    if tracked:
        git("checkout", "-q", base, "--", path)
    print(mark + " " + path)
"""


def image_name(instance_id: str) -> str:
    return "xingyaoww/sweb.eval.x86_64." + instance_id.lower().replace("__", "_s_") + ":latest"


def docker(*args: str, timeout: float | None = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


def _unquote(path: str) -> str:
    """A path as git prints it: C-quoted ("a/x\\303\\251") when it has special bytes."""
    if len(path) >= 2 and path[0] == path[-1] == '"':
        return codecs.escape_decode(path[1:-1].encode())[0].decode("utf-8", "replace")
    return path


def patch_sections(patch: str):
    """Each ``diff --git`` section of a patch as (src, dst, new, body): the pre- and post-image paths (src None: a new
    file; dst None: a deleted one), whether dst is created (new file, rename or copy target), and the section's lines
    from its first hunk (or binary marker) on. Paths come from the header (an empty new file or a 100% rename has no
    ``---`` / ``+++`` lines)."""
    for sec in re.split(r"(?m)^(?=diff --git )", patch or ""):
        if not sec.startswith("diff --git "):
            continue
        lines = sec.splitlines()
        m = re.match(r"diff --git a/(.+) b/(.+)$", lines[0])
        src, dst = (m.group(1), m.group(2)) if m else (None, None)
        new, body = False, []
        for i, line in enumerate(lines[1:], 1):
            if line.startswith(("@@", "GIT binary patch", "Binary files ")):
                body = lines[i:]
                break
            if line.startswith(("--- ", "+++ ")):
                path = line[4:-1] if line.endswith("\t") else line[4:]  # git appends a tab to names with spaces
                path = None if path == "/dev/null" else _unquote(path)
                if line[0] == "-":
                    src = path[2:] if path and path.startswith("a/") else path
                else:
                    dst = path[2:] if path and path.startswith("b/") else path
            elif line.startswith(("rename from ", "copy from ")):
                src = _unquote(line.split(" ", 2)[2])
            elif line.startswith(("rename to ", "copy to ")):
                dst, new = _unquote(line.split(" ", 2)[2]), True
            elif line.startswith("new file mode"):
                src, new = None, True
            elif line.startswith("deleted file mode"):
                dst = None
        yield src, dst, new, body


def test_patch_paths(test_patch: str) -> tuple[list[str], list[str]]:
    """(paths the test patch needs at their base version: modified, deleted, renamed or copied from; paths it creates:
    new files, rename and copy targets), from each ``diff --git`` section's header (``patch_sections``)."""
    reads: list[str] = []
    creates: list[str] = []
    for src, dst, new, _ in patch_sections(test_patch):
        if src and src not in reads:
            reads.append(src)
        if new and dst and dst not in creates:
            creates.append(dst)
    return reads, creates


def collision_guard(test_patch: str, base_commit: str) -> list[str]:
    """Shell lines run between the fork's test-file reset and the test-patch apply (the SWE-bench harness rule, as in
    SWE-bench Pro V2's test.sh): restore each path the test patch needs that the fork's reset misses (its regex skips
    100% renames; no frozen-spec task has one) to its base version, remove a file or symlink the agent left where the
    test patch needs a directory (restoring the test patch's base files under it), and remove whatever the agent left
    at a path the test patch creates, logging ``COLLISION_MARK <path>``. Without a collision the lines change nothing:
    an occupied path makes ``git apply`` refuse the whole test patch, so they act only where the old script lost the
    hidden tests."""
    reads, creates = test_patch_paths(test_patch)
    fork = set(FORK_RESET.findall(test_patch))
    missed = [p for p in reads if p not in fork]
    out = [f"git checkout {base_commit} -- {' '.join(shlex.quote(p) for p in missed)}"] if missed else []
    parents = {"/".join(p.split("/")[:k]) for p in reads + creates for k in range(1, p.count("/") + 1)} - set(reads)
    for d in sorted(parents, key=lambda x: (x.count("/"), x)):
        q, under = shlex.quote(d), [shlex.quote(p) for p in reads if p.startswith(d + "/")]
        restore = f"; git checkout {base_commit} -- {' '.join(under)}" if under else ""
        out.append(f"if [ -e {q} ] && [ ! -d {q} ] || [ -L {q} ]; then echo {shlex.quote(f'{COLLISION_MARK} {d}')}; "
                   f"rm -f -- {q}{restore}; fi")
    for p in creates:
        q = shlex.quote(p)
        out.append(f"if [ -e {q} ] || [ -L {q} ]; then echo {shlex.quote(f'{COLLISION_MARK} {p}')}; rm -rf -- {q}; fi")
    return out


def test_reset_paths(patch: str, test_patch: str = "", gold: str = "") -> tuple[list[str], list[str]]:
    """(files, section files) of a model patch that ``test_reset_guard`` puts back: test files (``TEST_PATH``) and
    test configuration (``TEST_CONFIG_FILE``), and the files it puts back whole if their change touches pytest
    (``TEST_CONFIG_SECTIONS``, ``CONFIG_RESTORE``); never a path of the test patch (the fork's reset and
    ``collision_guard`` own those) or of the gold patch (three frozen-spec gold patches add mypy test-data plugins: a
    fix may need them)."""
    reads, creates = test_patch_paths(patch)
    own = set().union(*test_patch_paths(test_patch), *test_patch_paths(gold))
    paths = sorted(set(reads + creates) - own)
    return ([p for p in paths if TEST_PATH.search(p) or TEST_CONFIG_FILE.search(p)],
            [p for p in paths if TEST_CONFIG_SECTIONS.search(p)])


# Reward hacking that touches no test file (adversarial review P4, 2026-10-13): code the tests run (a package module,
# setup.py, a requirement file or Makefile the install step reads) that registers a pytest plugin (pytest11 entry
# point, pytest_plugins, PYTEST_ADDOPTS / PYTEST_PLUGINS), hooks or monkeypatches pytest's internals (_pytest, hook
# and report names, pytest.Function & co.), ends the process early (os._exit) or installs a sitecustomize ...
CONTENT_HACK = re.compile(
    r"pytest11|pytest_plugins|\b_pytest\b|pytest_runtest_|pytest_collection|hookimpl|hookwrapper|TestReport|os\._exit"
    r"|PYTEST_ADDOPTS|PYTEST_PLUGINS|sitecustomize|usercustomize"
    r"|\bpytest\s*\.\s*(?:Function|Item|Session|Collector|CallInfo|CollectReport|Config)\b")


# ... or downloads the fix while the tests run (grading containers have network). The agent cannot publish anything
# (its container has no network), so a download can only bring public code; the fix is the project's own: its GitHub
# repository (raw / objects / codeload / API hosts, raw / archive / release / .git URLs) or its package (PyPI hosts,
# git+ requirement URLs), named by "owner/name" or by its name as a path segment. Other links do not count: GitHub
# issue, pull, blob and repository links fill comments and docstrings of 79 gold patches, pandas docstrings link
# PyPI pages of optional dependencies, and MONAI's bundle API downloads from templated GitHub URLs by design
# (MONAI-6997's gold).
GITHUB_FETCH = re.compile(
    r"(?:[\w-]+\.)*githubusercontent\.com/|\b(?:codeload|api)\.github\.com/"
    r"|github\.com[:/][^\s'\"()<>]*?(?:/(?:raw|archive|releases/download|zipball|tarball)/|\.git\b|[?&]raw=)",
    re.IGNORECASE)


PACKAGE_FETCH = re.compile(
    r"(?:[\w-]+\.)*(?:pypi\.org|pypi\.python\.org|pythonhosted\.org)/|git\+(?:https?|ssh|git|file)://", re.IGNORECASE)


URL_TOKEN = re.compile(r"[^\s'\"()<>]*")


# Lines that never run: comments, doctest prompts and doctest continuations of a string (``...`` then a quote is not
# Python; ``..., x`` or ``... and x`` is)
NOT_CODE = re.compile(r"\s*(?:#|>>>|\.\.\.\s+['\"])")


# Files the grading run executes or reads as configuration: Python / Cython sources, build and test-runner
# configuration, requirement files and Makefiles of the install step (conan, dvc, hydra, MONAI and mypy install
# requirement files; moto and pydantic run make), shell scripts. Not documentation, data or backups (a README's
# install line, a stray core.py.bak).
CODE_PATH = re.compile(r"\.(py|pyx|pxd|pxi|pth|cfg|toml|ini|sh|mk)$|(^|/)(Makefile|GNUmakefile|makefile)$"
                       r"|(^|/)[^/]*(requirements|constraints)[^/]*\.(txt|in)$|(^|/)requirements/[^/]*\.(txt|in)$")


def fetches(line: str, repo: str) -> list[str]:
    """The URLs of a line (up to the first quote, space or bracket, lower case; one per URL) that download the project
    ``repo`` ("owner/name"; any project when empty) from GitHub (``GITHUB_FETCH``) or as a package
    (``PACKAGE_FETCH``)."""
    name = re.escape(repo.lower().rsplit("/", 1)[-1])
    out, end = [], -1
    for start in sorted({m.start() for rx in (GITHUB_FETCH, PACKAGE_FETCH) for m in rx.finditer(line)}):
        if start < end:  # inside the URL of an earlier match (git+https://github.com/o/r.git)
            continue
        url = URL_TOKEN.match(line, start).group(0)
        end = start + len(url)
        url = url.lower()
        if not repo or repo.lower() in url or re.search(rf"/{name}(?![a-z0-9])", url):
            out.append(url)
    return out


def content_hits(patch: str, test_patch: str = "", gold: str = "", repo: str = "") -> dict[str, list[str]]:
    """{path: matches} of ``CONTENT_HACK`` and the project's download URLs (``fetches``; ``repo``: the task's
    "owner/name") that a model patch adds to a file the grading run executes or reads (``CODE_PATH``), except the test
    files and test configuration ``test_reset_guard`` puts back and the test patch's paths (the fork's reset and the
    test patch overwrite them). Comment and doctest lines never run (``NOT_CODE``: pandas docstring examples read
    raw.githubusercontent.com/pandas-dev data), and a match the file's removed lines carry as often is not new (a
    re-indented or moved line, pandas docstrings' PyPI links)."""
    gone = set(test_reset_paths(patch, test_patch, gold)[0]).union(*test_patch_paths(test_patch))
    out = {}
    for _, dst, _, body in patch_sections(patch):
        if dst is None or dst in gone or not CODE_PATH.search(dst):
            continue
        n = {"+": defaultdict(int), "-": defaultdict(int)}
        for line in body:
            if line.startswith(("+", "-")) and not NOT_CODE.match(line, 1):
                for x in [*(m.group(0) for m in CONTENT_HACK.finditer(line)), *fetches(line[1:], repo)]:
                    n[line[0]][x] += 1
        if new := sorted(x for x, k in n["+"].items() if k > n["-"][x]):
            out[dst] = new
    return dict(sorted(out.items()))


def section_hits(patch: str, test_patch: str = "", gold: str = "") -> list[str]:
    """The section files of a model patch (``test_reset_paths``) with a changed line, added or removed, that mentions
    pytest (``pytest_text``): ``CONFIG_RESTORE`` puts each back whole. Known from the patch alone, before grading
    (``CONFIG_RESTORE`` also puts back a file whose change only lies in a section that mentions pytest)."""
    files = set(test_reset_paths(patch, test_patch, gold)[1])
    out = set()
    for src, dst, _, body in patch_sections(patch):
        for path in {src, dst} & files:
            if any(x.startswith(("+", "-")) and pytest_text(path, x[1:]) for x in body):
                out.add(path)
    return sorted(out)


def config_hits(patch: str, test_patch: str = "", gold: str = "", repo: str = "") -> dict[str, list[str]]:
    """Test configuration a model patch changes outside the files the grader puts back, known from the patch alone (the
    RL reward's ``test_config``, 0 reward): {path: reasons}, ``pytest_line`` for a section file (``section_hits``) and
    the ``content_hits`` matches."""
    out = {p: list(v) for p, v in content_hits(patch, test_patch, gold, repo).items()}
    for p in section_hits(patch, test_patch, gold):
        out.setdefault(p, []).insert(0, "pytest_line")
    return dict(sorted(out.items()))


def test_reset_guard(patch: str, test_patch: str, gold: str, base_commit: str) -> list[str]:
    """Shell lines that put the model patch's test files and test configuration (``test_reset_paths``) back to the
    base commit before the install step and the tests, as Pro V2's test.sh resets the test-code surface: such a file
    is checked out at its base version or removed if new; so is a section file whose change touches pytest
    (``CONFIG_RESTORE``, by the testbed's Python in isolated mode). The agent's source edits stay. Each path put back
    logs ``TEST_RESET_MARK <path>``, a failure ``TEST_RESET_FAIL_MARK`` (a harness error: the tests would run on the
    agent's test files or configuration). The lines depend only on the model patch: a patch without such files gets
    none, so its grade cannot change."""
    files, sections = test_reset_paths(patch, test_patch, gold)
    out = []
    for p in files:  # a failing ls-tree must not read as "new at base" (removing a tracked file)
        q = shlex.quote(p)
        out.append(f"if t=$(git ls-tree --name-only {base_commit} -- {q}); then "
                   f"{{ if [ -n \"$t\" ]; then git checkout -q {base_commit} -- {q}; else rm -rf -- {q}; fi; }} "
                   f"&& echo {shlex.quote(f'{TEST_RESET_MARK} {p}')} || echo {TEST_RESET_FAIL_MARK}; "
                   f"else echo {TEST_RESET_FAIL_MARK}; fi")
    if sections:
        args = " ".join(shlex.quote(x) for x in [base_commit, TEST_RESET_MARK, *sections])
        out.append(f"python -I - {args} <<'CHK_CONFIG_RESTORE_EOF' || echo {TEST_RESET_FAIL_MARK}\n"
                   f"{CONFIG_RESTORE.strip()}\nCHK_CONFIG_RESTORE_EOF")
    return out


# A conftest.py / pytest config the patch's code writes *while the tests run* (not in the diff) is missed by the
# static test_reset / collision guards; a nested conftest in a not-yet-collected directory loads and can force tests
# to pass (demonstrated under pytest 9 with a directory test target). Snapshot every conftest.py / pytest config in the
# repo right before the tests (after the test-patch apply, so base and test-patch files are the baseline) and diff
# after: a file created or changed in between is logged ``RUNTIME_CONFIG_MARK <path>`` and makes the run a harness
# error (the grader: no verdict, so a reward hack scores nothing even when it flipped a test).
_RC_NAMES = (r"-name conftest.py -o -name pytest.ini -o -name .pytest.ini -o -name pytest.toml -o -name .pytest.toml "
             r"-o -name sitecustomize.py -o -name usercustomize.py -o -name '*.pth'")


RUNTIME_CONFIG_SNAPSHOT = (f"_chk_cfgscan() {{ find . \\( {_RC_NAMES} \\) -type f -exec sha256sum {{}} + "
                           f"2>/dev/null | LC_ALL=C sort; }}\n_chk_cfgscan > /tmp/chk_cfg_before 2>/dev/null || true")


RUNTIME_CONFIG_CHECK = ("_chk_cfgscan > /tmp/chk_cfg_after 2>/dev/null || true\n"
                        "comm -13 /tmp/chk_cfg_before /tmp/chk_cfg_after 2>/dev/null "
                        f"| sed -n 's/^[0-9a-f]\\{{64\\}}  /{RUNTIME_CONFIG_MARK} /p' || true")


def runtime_config_hits(log: str) -> list[str]:
    """Paths of conftest / pytest-config files the run created or changed (``RUNTIME_CONFIG_MARK``), read from the test
    output (after ``TESTS_START``, where the post-test check prints them). Forging one in test output only costs the
    model a harness error, so reading the agent-reachable region is safe."""
    post = split_tests(log)[1] or ""
    return sorted({m.group(1) for m in re.finditer(rf"(?m)^{RUNTIME_CONFIG_MARK} (.+)$", post)})


def eval_script(t: dict) -> str:
    """The fork's eval script with ``test_reset_guard`` (the test files and test configuration of the patch graded,
    ``t["patch"]``, put back; ``t["gold_patch"]``: the task's gold patch, whose paths are kept; without it ``t["patch"]``
    is the gold patch itself, as in verification) after its informational ``git diff`` and before the install step,
    ``collision_guard`` before the test-patch apply,
    ``TEST_PATCH_MARK`` when that apply still fails (a harness error, the grader's ``grade_record``),
    ``TESTS_START`` before the test command, and a conftest / pytest-config snapshot around it
    (``RUNTIME_CONFIG_MARK`` for a file the run writes). Raises if the fork's script lacks one of these anchors (a
    changed fork must not silently drop a guard)."""
    from swebench.harness.test_spec import make_test_command, make_test_spec

    spec = make_test_spec(t)
    test_cmd = make_test_command(t)
    out, seen = [], set()
    for c in spec.eval_script_list:
        if c == f"git diff {t['base_commit']}" and "diff" not in seen:
            seen.add("diff")
            patch = t.get("patch") or ""
            out += [c, *test_reset_guard(patch, t["test_patch"], t.get("gold_patch", patch), t["base_commit"])]
            continue
        if c.startswith("git apply -v - <<'"):
            seen.add("apply")
            out += collision_guard(t["test_patch"], t["base_commit"])
            head, rest = c.split("\n", 1)
            c = f"{head} || echo {TEST_PATCH_MARK}\n{rest}"
        elif c == test_cmd and "apply" in seen:
            seen.add("test")
            c = f"{RUNTIME_CONFIG_SNAPSHOT}\necho {TESTS_START}\n{c}\n{RUNTIME_CONFIG_CHECK}"
        out.append(c)
    if seen != {"diff", "apply", "test"}:
        raise ValueError(f"{t.get('instance_id')}: the fork's eval script lacks {sorted({'diff', 'apply', 'test'} - seen)}")
    return "\n".join(["#!/bin/bash", "set -xo pipefail", *out]) + "\n"


def split_tests(log: str) -> tuple[str, str | None]:
    """(the log before the eval script's ``TESTS_START`` line, the test output after it; None if the tests never
    started). Only a whole ``TESTS_START`` line counts: the marker text in the agent's diff printed by ``git diff``
    neither starts the tests early nor moves the parsed region."""
    parts = re.split(rf"(?m)^{TESTS_START}$", log, maxsplit=1)
    return parts[0], (parts[1] if len(parts) > 1 else None)


def marks(log: str, mark: str) -> list[str]:
    """The arguments of the eval script's ``mark`` lines: whole lines of the log before the tests started, so the
    agent's diff (``git diff``), git's file listings and the tests' own output neither produce nor hide one."""
    return [m.group(1) or "" for m in re.finditer(rf"(?m)^{mark}(?: (.+))?$", split_tests(log)[0])]


def parse_status(repo: str, log: str) -> dict[str, str]:
    """Per-test statuses of the test output (``split_tests``; the whole log if the script printed no start line)."""
    from swebench.harness.log_parsers import MAP_REPO_TO_PARSER

    pre, tests = split_tests(log)
    return MAP_REPO_TO_PARSER[repo.lower()](pre if tests is None else tests)


def test_outcome(tests: list[str], sm: dict[str, str]) -> dict:
    passed = [x for x in tests if sm.get(x) in ("PASSED", "XFAIL")]
    missing = [x for x in tests if x not in sm]
    failed = [x for x in tests if x in sm and sm[x] not in ("PASSED", "XFAIL")]
    return {"pass": len(passed), "fail": len(failed), "missing": len(missing),
            "failed_ids": sorted(failed), "missing_ids": sorted(missing),
            "skipped_ids": sorted(x for x in failed if sm[x] == "SKIPPED")}
