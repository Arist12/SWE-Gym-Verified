"""Pre-screen (no LLM, no container): unusable records, title-only statements, missing tests, duplicates.

Row: decision keep | drop; reasons PRESCREEN_NO_IMAGE, PRESCREEN_EMPTY_STATEMENT, PRESCREEN_TITLE_ONLY,
PRESCREEN_NO_F2P, PRESCREEN_NO_PATCH, PRESCREEN_DUPLICATE (duplicate_of: the kept task); flag ``image_not_checked``
(unless ``check_image`` is set).
Duplicates: the same repository, base commit and normalised gold diff, or the same statement and test patch; the
first task in instance-id order is kept. Execution failures are the environment stage's job.
"""
from __future__ import annotations

import subprocess

from ..gates.stats import normalised
from ..util import sha_json

NAME = "prescreen"
DEPENDS: list[str] = []


def duplicates(ctx) -> dict[str, str]:
    if getattr(ctx, "_dupes", None) is None:
        seen, out = {}, {}
        for iid in sorted(ctx.tasks):
            t = ctx.tasks[iid]
            for k in (("p", t["repo"], t["base_commit"], sha_json(normalised(t["patch"]))),
                      ("s", sha_json([t["problem_statement"].strip(), t["test_patch"]]))):
                if k in seen:
                    out.setdefault(iid, seen[k])
                else:
                    seen[k] = iid
        ctx._dupes = out
    return ctx._dupes


def key(ctx, iid: str) -> str:
    t = ctx.tasks[iid]
    return sha_json({"t": [t["problem_statement"], t["patch"], t["test_patch"], t["FAIL_TO_PASS"], t.get("image")],
                     "settings": ctx.scfg(NAME), "dupe_of": duplicates(ctx).get(iid)})


def run(ctx, iid: str) -> dict:
    t, sc = ctx.tasks[iid], ctx.scfg(NAME)
    reasons, flags = [], []
    stmt = (t["problem_statement"] or "").strip()
    if not t.get("image"):
        reasons.append("PRESCREEN_NO_IMAGE")
    elif sc.get("check_image"):
        p = subprocess.run(["docker", "manifest", "inspect", t["image"]], capture_output=True, timeout=120)
        if p.returncode and subprocess.run(["docker", "image", "inspect", t["image"]], capture_output=True).returncode:
            reasons.append("PRESCREEN_NO_IMAGE")
    else:
        flags.append("image_not_checked")
    if len(stmt) < int(sc.get("min_statement_chars", 1)):
        reasons.append("PRESCREEN_EMPTY_STATEMENT")
    elif len([x for x in stmt.splitlines() if x.strip()]) <= 1 \
            and len(stmt) <= int(sc.get("title_only_max_chars", 200)):
        reasons.append("PRESCREEN_TITLE_ONLY")
    if not t["FAIL_TO_PASS"]:
        reasons.append("PRESCREEN_NO_F2P")
    if not t["patch"].strip() or not t["test_patch"].strip():
        reasons.append("PRESCREEN_NO_PATCH")
    dup = duplicates(ctx).get(iid) if sc.get("dedupe", True) else None
    if dup:
        reasons.append("PRESCREEN_DUPLICATE")
    return {"decision": "drop" if reasons else "keep", "reasons": reasons, "flags": flags, "duplicate_of": dup}
