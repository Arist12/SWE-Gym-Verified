"""Small helpers shared by every module (no third-party imports)."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


def sha(s: str | bytes) -> str:
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


def sha_json(o) -> str:
    return sha(json.dumps(o, sort_keys=True, default=str))


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_jsonl(p) -> list[dict]:
    p = Path(p)
    if not p.exists():
        return []
    op = gzip.open if p.suffix == ".gz" else open
    with op(p, "rt") as f:
        return [json.loads(x) for x in f if x.strip()]


def append_jsonl(p, row: dict) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, default=str, ensure_ascii=False) + "\n"
    with _LOCK, open(p, "a") as f:
        f.write(line)


def write_json(p, obj, indent: int | None = 1) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=indent, default=str, ensure_ascii=False)
    os.replace(tmp, p)


def fence(text: str, lang: str = "") -> str:
    runs = [len(m) for m in re.findall(r"`{3,}", text)]
    f = "`" * max(3, max(runs, default=0) + 1)
    return f"{f}{lang}\n{text.rstrip(chr(10))}\n{f}"


def clip_mid(s: str, n: int) -> str:
    """Head and tail halves around an omission note (the statement-screen and repair packets)."""
    s = s or ""
    return s if len(s) <= n else s[: n // 2] + f"\n\n[... {len(s) - n} characters omitted ...]\n\n" + s[-n // 2:]


def clip_tight(s: str, n: int) -> str:
    """The FP-judge clip (single newlines around the note)."""
    s = s or ""
    return s if len(s) <= n else s[: n // 2] + f"\n[... {len(s) - n} characters omitted ...]\n" + s[-n // 2:]


def parse_json(reply: str | None) -> dict | None:
    """The outermost ``{...}`` of a reply (greedy), else None."""
    m = re.search(r"\{.*\}", reply or "", re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return None
    return j if isinstance(j, dict) else None


def statement_with_spec(issue: str, spec_block: str | None) -> str:
    """The task statement the solver sees: the unedited issue followed by the appended specification, if any."""
    return issue if not spec_block else issue.rstrip("\n") + "\n\n" + spec_block.strip("\n") + "\n"


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[-n:].strip("-")
