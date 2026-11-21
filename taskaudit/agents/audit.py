"""Command audit of an agent run and the void rule.

The only allowed shape of a host command is one ``tbx`` call with one argument and nothing the host shell expands
outside single quotes; anything else is flagged ``non_tbx``, and a path to this tool's records or outside the agent's
sandbox in the host part of a command is flagged ``label_ref``. A run with an EFFECTIVE flag is void: its output is
not used and the stage is retried once; void again -> that family has no verdict for the stage (UNSURE; the task's
escalation then resolves CONTESTED). Effective: any label_ref (denied or not), and any other flagged command or tool
call that EXECUTED. A flagged Claude Code call the CLI DENIED (tool_result is_error AND the CLI's own refusal text)
executed nothing and is not effective. Codex runs with no host-side denial, so every flagged Codex command counts.
Not flags: a tbx call whose output is truncated on the host by ``| head`` / ``| tail``, Claude Code reading its own
backgrounded tbx output, and Codex writing a file into its own work dir.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

DENIAL = re.compile(r"^\s*(?:<tool_use_error>\s*)?(?:Error:\s*)?"
                    r"(?:Permission to use [\w.:-]+ has been denied|No such tool available)")
NOTES = ("bg_output_read", "file_change_in_wd", "host_truncation")  # recorded, never void an attempt
TRUNC = re.compile(r"\s*(?:2>&1\s*)?(?:\|\s*(?:head|tail)(?:\s+(?:-n\s*)?[-+]?\d+)?\s*)+$")


def label_ref_re(work_dir: str | None) -> re.Pattern:
    """Paths an agent must not read on the host: this tool's records and caches, relative escapes, home dirs."""
    parts = [r"stages/", r"llm/cache", r"llm/requests", r"\.jsonl\b", r"stage\.json", r"exec\.json", r"gold\.json",
             r"\.\./", r"/home/", r"~/"]
    if work_dir:
        parts.append(re.escape(str(work_dir)))
    return re.compile("|".join(parts))


def host_text(s: str) -> str:
    """The parts of a shell command outside single quotes (what the host shell expands or interprets)."""
    out, i, st = [], 0, None
    while i < len(s):
        c = s[i]
        if st == "'":
            if c == "'":
                st = None
        elif c == "\\":
            out.append(s[i:i + 2])
            i += 1
        elif st == '"':
            if c == '"':
                st = None
            else:
                out.append(c)
        elif c in "'\"":
            st = c
        else:
            out.append(c)
        i += 1
    return "".join(out)


def base_audit(cmd: str, label_ref: re.Pattern) -> list[str]:
    inner = cmd
    try:
        p = shlex.split(cmd)
        if len(p) == 3 and os.path.basename(p[0]) in ("bash", "sh", "zsh") and p[1] in ("-lc", "-c"):
            inner = p[2]
    except ValueError:
        pass
    host = host_text(inner).strip()
    ok = False
    try:
        lx = shlex.shlex(inner, posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        toks = list(lx)
        ok = len(toks) == 2 and toks[0] == "tbx" and not re.search(r"[$`;&|<>(){}\n]", host[3:])
    except ValueError:
        pass
    flags = []
    if not ok:
        if re.fullmatch(r"(sleep \d+\s*;\s*)?(cat|tail -\S+|head -\S+) /tmp/claude-\d+/\S+/tasks/\S+\.output", host):
            return ["bg_output_read: " + cmd[:300]]
        flags.append("non_tbx: " + cmd[:300])
        if label_ref.search(host):
            flags.append("label_ref: " + cmd[:300])
    return flags


def strip_host_truncation(cmd: str) -> str | None:
    inner = cmd
    try:
        p = shlex.split(cmd)
        if len(p) == 3 and p[0].rsplit("/", 1)[-1] in ("bash", "sh", "zsh") and p[1] in ("-lc", "-c"):
            inner = p[2]
    except ValueError:
        pass
    m = TRUNC.search(inner)
    if not m or m.start() == 0:
        return None
    return inner[:m.start()].rstrip()


def audit_command(cmd: str, label_ref: re.Pattern) -> list[str]:
    flags = base_audit(cmd, label_ref)
    if flags:
        bare = strip_host_truncation(cmd)
        if bare is not None and not base_audit(bare, label_ref):
            return ["host_truncation: " + cmd[:300]]
    return flags


def split(flags_by_call: list[tuple[list[str], bool]]) -> tuple[list[str], list[str]]:
    """[(flags of one call, denied)] -> (effective, denied_only)."""
    eff, den = [], []
    for flags, denied in flags_by_call:
        for f in flags:
            kind = f.split(":", 1)[0]
            if kind in NOTES:
                continue
            (den if denied and kind != "label_ref" else eff).append(f)
    return eff, den


# ------------------------------------------------------------------ stream parsers
def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text") or "" for x in content if isinstance(x, dict))
    return ""


def claude_calls(path: Path) -> tuple[dict, list[dict]]:
    """(the result event, every tool_use in stream order with its tool_result: is_error, result text, denied)."""
    res, calls, by_id = {}, [], {}
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    x = {"id": c.get("id"), "name": c.get("name"), "input": c.get("input") or {}, "is_error": None,
                         "result": None}
                    calls.append(x)
                    by_id[x["id"]] = x
        elif e.get("type") == "user":
            for c in (e.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in by_id:
                    x = by_id[c["tool_use_id"]]
                    x["is_error"], x["result"] = c.get("is_error"), _text(c.get("content"))
        elif e.get("type") == "result":
            res = e
    for x in calls:
        x["denied"] = x["is_error"] is True and bool(DENIAL.match(x["result"] or ""))
    return res, calls


def codex_events(path: Path, wd: Path | None = None):
    """(usage, commands, turns, errors, other items) of a Codex ``exec --json`` event stream."""
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    commands, turns, errors, other = [], 0, [], []
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "turn.completed":
            turns += 1
            for k, v in (e.get("usage") or {}).items():
                if isinstance(v, int):
                    usage[k] = usage.get(k, 0) + v
        elif e.get("type") in ("turn.failed", "error"):
            errors.append(json.dumps(e)[:400])
        elif e.get("type") == "item.completed":
            it = e.get("item") or {}
            if it.get("type") == "command_execution":
                commands.append(it.get("command") or "")
            elif it.get("type") == "file_change" and wd is not None and all(
                    os.path.abspath(c.get("path") or "/").startswith(str(wd) + "/") for c in it.get("changes") or []):
                other.append("file_change_in_wd: " + ", ".join(Path(c.get("path")).name for c in it.get("changes") or []))
            elif it.get("type") == "error":  # a notice of the CLI (e.g. unknown model metadata), not a tool call
                errors.append(json.dumps(it)[:400])
            elif it.get("type") not in ("agent_message", "reasoning", "todo_list"):
                other.append(f"non_shell_tool: {it.get('type')}: {json.dumps(it)[:200]}")
    return usage, commands, turns, errors, other
