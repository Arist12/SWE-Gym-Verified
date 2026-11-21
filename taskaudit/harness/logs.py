"""pytest failure blocks of a grading log: excerpts for the review / debate packets and failure sites."""
from __future__ import annotations

import re

from . import _guards as G


def test_blocks(test_out: str) -> dict[str, list[str]]:
    """pytest failure / error sections: header -> lines."""
    blocks, cur = {}, None
    for line in test_out.splitlines():
        m = re.match(r"^_{3,} (.+?) _{3,}$", line)
        if m:
            name = m.group(1).strip()
            if name.startswith("ERROR at setup of "):
                name = name[len("ERROR at setup of "):]
            cur = blocks.setdefault(name, [])
            continue
        if re.match(r"^={5,}", line):
            cur = None
            continue
        if cur is not None:
            cur.append(line)
    return blocks


def _cands(test_id: str, blocks: dict) -> list[str]:
    last = test_id.split("::")[-1]
    cands = [k for k in blocks if k == last or k.endswith("." + last) or k.split(".")[-1] == last]
    if not cands:  # parametrized ids sometimes differ in the bracket; match on the function name
        base = last.split("[")[0]
        cands = [k for k in blocks if k.split(".")[-1].split("[")[0] == base]
    return cands


def excerpt_for(test_id: str, blocks: dict, summary: list[str]) -> str:
    """The failure excerpt shown to the review and debate agents (at most 50 lines of the block)."""
    last = test_id.split("::")[-1]
    cands = _cands(test_id, blocks)
    lines = blocks[cands[0]] if cands else []
    keep = []
    if len(lines) <= 50:
        keep = lines
    else:
        for l in lines:
            if l.startswith(("E ", ">")) or re.match(r"^\S+\.py:\d+:", l) or l.startswith(("Expected:", "Actual:")):
                keep.append(l)
            elif keep and keep[-1].startswith(("Expected:", "Actual:", "  ")) and l.startswith("  "):
                keep.append(l)
        keep = keep[:50]
    summ = [s for s in summary if test_id in s or last in s][:2]
    return "\n".join(summ + (["    " + l for l in keep] if keep else ["    (no failure block found in the log)"]))


def failure_excerpt(log_text: str, test_id: str, max_lines: int = 60) -> str:
    tests = G.split_tests(log_text)[1] or ""
    blocks = test_blocks(tests)
    last = test_id.split("::")[-1]
    cands = _cands(test_id, blocks)
    lines = blocks[cands[0]] if cands else []
    if len(lines) > max_lines:
        keep = [l for l in lines if l.startswith(("E ", ">")) or re.match(r"^\S+\.py:\d+:", l)]
        lines = keep[:max_lines]
    summ = [l for l in tests.splitlines() if l.startswith(("FAILED ", "ERROR ")) and last in l][:2]
    return "\n".join(summ + lines)


def failure_lines(log_text: str, test_id: str) -> list[dict]:
    """The location lines pytest prints for a failing test: [{file, line, text}] in order; the LAST one inside a
    test file is where the test failed."""
    out = []
    for l in failure_excerpt(log_text, test_id, max_lines=400).splitlines():
        m = re.match(r"^(\S+\.py):(\d+):\s*(.*)$", l.strip())
        if m:
            out.append({"file": m.group(1), "line": int(m.group(2)), "text": m.group(3)})
    return out


MASK = [(re.compile(r"0x[0-9a-fA-F]+"), "0x?"), (re.compile(r"/tmp/[\w./-]+"), "/tmp/?"),
        (re.compile(r"/private/var/folders/[\w./-]+"), "/tmp/?"), (re.compile(r"\bat 0x\?"), "at 0x?")]


def norm_fail(excerpt: str) -> str:
    """A failure's signature: its pytest E lines with addresses and temporary paths masked."""
    txt = "\n".join(x.rstrip() for x in (excerpt or "").splitlines() if x.startswith("E "))
    for rx, sub in MASK:
        txt = rx.sub(sub, txt)
    return txt
