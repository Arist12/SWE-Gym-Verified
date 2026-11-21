"""Call patterns of the pipeline on top of LLM.chat.

judged()      one judge decision = up to ``attempts`` independent calls (tag "<stage>:a<k>"); an attempt is accepted
              when the reply parses and passes the stage's consistency rules; after the last failing attempt the last
              parsed reply is kept with its errors (accepted false); no parsed reply -> undecided (the screens, the
              FP judge and the flip judge).
reasked()     the FN rater's re-ask: one call, and if the reply breaks the rules, one follow-up turn that lists the
              broken rules and asks for the corrected object.
tool_loop()   the offline repository access of the P2P judge and the gold variant: the model may answer with
              {"requests": [...]} (at most 8 per round, 3 rounds) before its final JSON.

``stage`` starts with the step id (1a, 3a, ...; "Names" in docs/WORKFLOW.md); it tags the request and names its
dry-run directory.
"""
from __future__ import annotations

import json

from ..util import clip_mid, parse_json
from .client import DryRun


def send(llm, who: str, system: str, turns: list[dict], tag: str, stage: str, style: str) -> dict:
    """One request of a conversation. A model entry with ``single_turn: true`` (the GPT family as run) gets the whole
    conversation rendered into one user message: style "reask" appends the rejected reply and the reasons, style
    "tool" appends each earlier reply and the results it got."""
    if not llm.entry(who).get("single_turn") or len(turns) == 1:
        return llm.chat(who, system, turns if len(turns) > 1 else turns[0]["content"], tag=tag, stage=stage)
    if style == "reask":
        user = (turns[0]["content"] + "\n\n=== YOUR EARLIER REPLY (rejected) ===\n" + turns[1]["content"] +
                "\n\n=== WHY IT WAS REJECTED ===\n" + turns[2]["content"])
    else:
        parts, k = [turns[0]["content"]], 0
        for t in turns[1:]:
            if t["role"] == "assistant":
                k += 1
                parts.append(f"\n\n=== YOUR REPLY {k} (earlier in this conversation) ===\n{t['content']}")
            else:
                parts.append(f"\n\n=== RESULTS FOR YOUR REPLY {k} ===\n{t['content']}")
        user = "".join(parts)
    return llm.chat(who, system, user, tag=tag, stage=stage)


def judged(llm, who: str, stage: str, system: str, user: str, parse, attempts: int = 3) -> dict:
    """``parse(j) -> (parsed | None, errors)``. Returns {accepted, parsed, errors, attempts, dry_run, calls}."""
    calls, last = [], {"parsed": None, "errors": ["no_call"]}
    for a in range(attempts):
        try:
            res = llm.chat(who, system, user, tag=f"{stage}:a{a}", stage=stage)
        except DryRun:
            return {"accepted": False, "parsed": None, "errors": ["dry_run"], "attempts": a, "dry_run": True,
                    "calls": calls, "model": llm.model(who)}
        j = parse_json(res.get("text")) if res.get("model_ok") else None
        p, errs = parse(j) if j is not None else (None, ["no_json" if res.get("model_ok") else "model"])
        calls.append({"attempt": a, "cached": res.get("cached"), "http_status": res.get("http_status"),
                      "returned_model": res.get("returned_model"), "usage": res.get("usage"), "errors": errs,
                      "error": res.get("error")})
        if p is not None:
            last = {"parsed": p, "errors": errs}
        if p is not None and not errs:
            return {"accepted": True, "parsed": p, "errors": [], "attempts": a + 1, "calls": calls,
                    "model": llm.model(who), "reply": res.get("text")}
    return {"accepted": False, "parsed": last["parsed"], "errors": last["errors"], "attempts": attempts,
            "calls": calls, "model": llm.model(who)}


def reasked(llm, who: str, stage: str, system: str, user: str, validate, reasks: int = 1) -> dict:
    """``validate(obj) -> errors``. Returns {valid, parsed, parsed_invalid, final_errors, attempts, dry_run}."""
    turns = [{"role": "user", "content": user}]
    attempts, parsed, errs, obj = [], None, None, None
    for k in range(1 + reasks):
        try:
            res = send(llm, who, system, turns, f"{stage}:t{k}", stage, "reask")
        except DryRun:
            return {"valid": False, "parsed": None, "dry_run": True, "attempts": attempts, "final_errors": ["dry_run"]}
        text = res.get("text") if res.get("model_ok") else None
        obj = parse_json(text)
        errs = validate(obj) if obj is not None else ["no parseable JSON object in the reply"]
        attempts.append({"http_status": res.get("http_status"), "returned_model": res.get("returned_model"),
                         "model_ok": res.get("model_ok"), "cached": res.get("cached"), "errors": errs})
        if not errs:
            parsed = obj
            break
        if text:
            turns = [{"role": "user", "content": user}, {"role": "assistant", "content": text},
                     {"role": "user", "content": "Your reply broke these rules:\n- " + "\n- ".join(errs) +
                      "\nReply again with the complete corrected JSON object only."}]
        else:
            turns = [{"role": "user", "content": user}]
    return {"valid": parsed is not None, "parsed": parsed, "parsed_invalid": None if parsed else obj,
            "final_errors": [] if parsed else errs, "attempts": attempts, "reasked": len(attempts) > 1,
            "model": llm.model(who)}


TOOLS_DOC = """REPOSITORY ACCESS. You may inspect the repository offline. Instead of a verdict, reply with ONE JSON object
{"requests": [...]} holding at most 8 requests; you get the results and can then answer or ask again (at most 3 rounds
of requests in total). Request forms:
  {"op": "show", "path": "pkg/mod.py", "start": 1, "end": 200}     file at the BASE commit (before any fix), <= 400 lines
  {"op": "grep", "pattern": "regex", "path": "pkg/"}                 git grep -n -E at the base commit (first 80 hits)
  {"op": "test_file", "path": "tests/x.py", "start": 1, "end": 200} test file AFTER the upstream test patch
  {"op": "ls", "path": "pkg/sub"}                                    directory listing at the base commit
  {"op": "gold_file", "path": "pkg/mod.py", "start": 1, "end": 200} a file AFTER the reference fix (gold post-image)
The base commit is the repository the candidate patch was written against."""


def tool_loop(llm, who: str, stage: str, system: str, user: str, validate, answer, rounds: int = 3) -> dict:
    """``answer(requests) -> text`` serves one round of repository requests (repo.RepoAccess.answer)."""
    turns = [{"role": "user", "content": user}]
    calls, parsed = [], None
    for k in range(rounds + 2):
        try:
            res = send(llm, who, system, turns, f"{stage}:r{k}", stage, "tool")
        except DryRun:
            return {"parsed": None, "dry_run": True, "calls": calls}
        reply = res.get("text") if res.get("model_ok") else None
        calls.append({x: res.get(x) for x in ("http_status", "returned_model", "model_ok", "cached", "error")})
        if not reply:
            break
        obj = parse_json(reply)
        if obj and isinstance(obj.get("requests"), list) and k < rounds:
            results = answer(obj["requests"])
            turns += [{"role": "assistant", "content": reply}, {"role": "user", "content": results + (
                "\n\nThat was your last round of repository requests: reply with the final JSON now."
                if k == rounds - 1 else "")}]
            continue
        v = validate(obj) if obj else None
        if v is not None:
            parsed = v
            break
        if k >= rounds + 1:
            break
        turns += [{"role": "assistant", "content": reply},
                  {"role": "user", "content": "Your reply was not a valid final JSON object of the required schema (or "
                   "you asked for more requests after the limit). Reply with the final JSON object only."}]
    return {"parsed": parsed, "calls": calls, "n_rounds": len(calls),
            "transcript_chars": sum(len(json.dumps(t)) for t in turns), "model": llm.model(who),
            "last_turns": [dict(t, content=clip_mid(t["content"], 4000)) for t in turns[-2:]]}
