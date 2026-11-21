"""Eval scripts for three backends, each wrapped by the same grader guards (``_guards.py``, carried over unchanged).

- ``swegym_fork``: the SWE-Gym fork of the SWE-bench harness (github.com/SWE-Gym/SWE-Bench-Fork @ 242429c1, MIT),
  installed as ``swebench``; ``_guards.eval_script`` is used as is. This is the paper's grader.
- ``swebench``: the public SWE-bench harness (``uv sync --extra swebench``); its eval-script list is wrapped by the same
  guard transformation (``wrap``), with the anchors found by shape instead of by the fork's exact strings.
- ``script``: tasks that bring their own ``test_cmd`` (and optional ``install_cmd`` / ``env_setup``): a minimal
  SWE-bench-shaped script is built and wrapped the same way.

In every backend the informational ``git show`` is cut to ``git show -s`` (after the single-commit truncation HEAD is
a root commit and a full ``git show`` prints the whole tree into every log).
"""
from __future__ import annotations

import re

from . import _guards as G

HEREDOC = "EOF_114329324912"
DEFAULT_ENV_SETUP = ["source /opt/miniconda3/bin/activate 2>/dev/null || true",
                     "conda activate testbed 2>/dev/null || true"]


def fork_available() -> bool:
    try:
        from swebench.harness.test_spec import make_test_command  # noqa: F401
        return True
    except Exception:
        return False


def backend_of(cfg: dict, task: dict) -> str:
    b = ((cfg.get("harness") or {}).get("backend") or "auto")
    if b != "auto":
        return b
    if task.get("test_cmd"):
        return "script"
    return "swegym_fork" if fork_available() else "swebench"


def _upstream_spec(t: dict):
    try:
        from swebench.harness.test_spec.test_spec import make_test_spec  # swebench >= 3
    except ImportError:
        from swebench.harness.test_spec import make_test_spec  # swebench 2.x
    return make_test_spec(t)


def _test_index(lines: list[str]) -> int:
    """The test command of an upstream eval-script list: the first command after the test-patch apply that is not a
    ``: '>>>>> ...'`` marker."""
    seen = False
    for i, c in enumerate(lines):
        if c.startswith("git apply") and "<<'" in c.split("\n", 1)[0]:
            seen = True
            continue
        if seen and not c.startswith((": '", "echo ")):
            return i
    raise ValueError("no test command after the test-patch apply")


def wrap(lines: list[str], test_idx: int, t: dict) -> str:
    """``_guards.eval_script``'s transformation over any SWE-bench-shaped list: test_reset_guard after the
    informational ``git diff <base>``, collision_guard + TEST_PATCH_MARK at the test-patch apply, TESTS_START and the
    runtime-config snapshot around the test command. Raises if an anchor is missing (no guard is silently dropped)."""
    base = t["base_commit"]
    diff_rx = re.compile(rf"git(?: -c \S+)* diff {re.escape(base)}")
    out, seen = [], set()
    for i, c in enumerate(lines):
        if diff_rx.fullmatch(c) and "diff" not in seen:
            seen.add("diff")
            patch = t.get("patch") or ""
            out += [c, *G.test_reset_guard(patch, t["test_patch"], t.get("gold_patch", patch), base)]
            continue
        if c.startswith("git apply") and "<<'" in c.split("\n", 1)[0] and "apply" not in seen:
            seen.add("apply")
            out += G.collision_guard(t["test_patch"], base)
            head, rest = c.split("\n", 1)
            c = f"{head} || echo {G.TEST_PATCH_MARK}\n{rest}"
        elif i == test_idx and "apply" in seen:
            seen.add("test")
            c = f"{G.RUNTIME_CONFIG_SNAPSHOT}\necho {G.TESTS_START}\n{c}\n{G.RUNTIME_CONFIG_CHECK}"
        out.append(c)
    if seen != {"diff", "apply", "test"}:
        raise ValueError(f"{t.get('instance_id')}: the eval script lacks {sorted({'diff', 'apply', 'test'} - seen)}")
    return "\n".join(["#!/bin/bash", "set -xo pipefail", *out]) + "\n"


def script_lines(t: dict) -> tuple[list[str], int]:
    base, tp = t["base_commit"], t["test_patch"]
    files = re.findall(G.FORK_RESET, tp)
    reset = f"git checkout {base} {' '.join(files)}"
    lines = [*(t.get("env_setup") or DEFAULT_ENV_SETUP), "cd /testbed",
             "git config --global --add safe.directory /testbed", "git status", "git show", f"git diff {base}"]
    if t.get("install_cmd"):
        lines.append(t["install_cmd"])
    lines += [reset, f"git apply -v - <<'{HEREDOC}'\n{tp}\n{HEREDOC}", t["test_cmd"], reset]
    return lines, len(lines) - 2


def eval_script(task: dict, patch: str, gold_patch: str, backend: str) -> str:
    """The guarded eval script grading ``patch`` (the gold patch's paths are kept by the test-file reset)."""
    t = dict(task, patch=patch or "", gold_patch=gold_patch)
    try:
        if backend == "swegym_fork":
            s = G.eval_script(t)
        elif backend == "swebench":
            lines = list(_upstream_spec(t).eval_script_list)
            s = wrap(lines, _test_index(lines), t)
        elif backend == "script":
            lines, i = script_lines(t)
            s = wrap(lines, i, t)
        else:
            raise ValueError(f"harness.backend {backend!r}: swegym_fork | swebench | script")
    except ImportError as exc:  # the harness extra is missing or the other one is installed: not a task failure
        extra = "swegym" if backend == "swegym_fork" else "swebench"
        raise SystemExit(f"harness.backend {backend}: {exc} (run `uv sync --extra {extra}`)") from None
    return s.replace("\ngit show\n", "\ngit show -s\n", 1)
