"""Per-repository log parsers.

The SWE-bench harness parsers are used whenever ``swebench`` is installed (the SWE-Gym fork for SWE-Gym's repositories,
the public harness for SWE-bench's): ``MAP_REPO_TO_PARSER[repo]``. A task may name a parser (``log_parser``) instead;
names that the installed harness does not provide fall back to the small built-in parsers below (a re-implementation
of the pytest status-line format, no code copied).
"""
from __future__ import annotations

import re

from . import _guards as G

_LINE_A = re.compile(r"^(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\s+(\S+)")
_LINE_B = re.compile(r"^(\S+::\S*)\s+(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)\b")


def parse_pytest(log: str) -> dict[str, str]:
    """``pytest -rA`` short summary lines (``STATUS id`` or ``id STATUS``); the last status of an id wins."""
    out: dict[str, str] = {}
    for line in log.splitlines():
        line = line.strip()
        m = _LINE_A.match(line)
        if m:
            tid = m.group(2)
            out[tid.split(" - ")[0]] = m.group(1)
            continue
        m = _LINE_B.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


BUILTIN = {"pytest": parse_pytest}


def _harness_map() -> dict:
    try:
        from swebench.harness.log_parsers import MAP_REPO_TO_PARSER
        return dict(MAP_REPO_TO_PARSER)
    except Exception:
        pass
    try:  # swebench >= 3 splits the parsers by language
        from swebench.harness.log_parsers.python import MAP_REPO_TO_PARSER_PY
        return dict(MAP_REPO_TO_PARSER_PY)
    except Exception:
        return {}


def parser_for(repo: str, name: str | None = None):
    if name and name in BUILTIN:
        return BUILTIN[name]
    m = _harness_map()
    if name:
        try:
            import swebench.harness.log_parsers as LP
            fn = getattr(LP, name if name.startswith("parse_log_") else f"parse_log_{name}", None)
            if fn:
                return fn
        except Exception:
            pass
    return m.get(repo.lower()) or m.get(repo) or parse_pytest


def parse_status(task: dict, log: str) -> dict[str, str]:
    """Per-test statuses of the test output (after TESTS_START; the whole log if the script printed no start line)."""
    pre, tests = G.split_tests(log)
    text = pre if tests is None else tests
    fn = parser_for(task["repo"], task.get("log_parser"))
    try:
        return fn(text)
    except TypeError:  # swebench >= 3 parsers take (log, test_spec)
        from .evalscript import _upstream_spec
        return fn(text, _upstream_spec(task))
