"""Provider-agnostic chat calls with request-hash caching, retries and a dry-run mode.

A *model entry* is ``{provider: anthropic | openai, model, base_url, api_key_env, headers, max_tokens, extra_body,
returned_model}``; ``openai`` is any OpenAI-compatible ``/chat/completions`` endpoint (vLLM, SGLang, a gateway) and
``headers`` adds HTTP headers for a gateway that needs its own. ``returned_model``: a substring that the model name
reported by the endpoint must contain; a reply from another model is not used and not cached.
``llm.families`` holds the two judge families of the paper (``claude``, ``gpt``); ``llm.roles`` names further models
(the second leak judge, the repair writer, ...) by entry or by family name.

Caching: one file per request under ``<work_dir>/llm/cache``, keyed by sha256 of (model, system, messages, tag); the
tag carries the stage and attempt, so a re-ask is a new request and a re-run of a finished stage makes no call.
Dry run (``llm.dry_run: true``, the default): each request is written to ``<work_dir>/llm/requests/<stage>/`` and
``DryRun`` is returned; no network call is made and nothing is spent. Keys are read from the environment variable an
entry names, never from the config, and never logged.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..util import sha, write_json


class DryRun(Exception):
    """Raised instead of a reply in dry-run mode (the request is on disk)."""


class LLM:
    def __init__(self, cfg: dict):
        lc = cfg.get("llm") or {}
        self.cfg = lc
        self.dry = bool(lc.get("dry_run", True))
        self.use_cache = bool(lc.get("cache", True))
        self.work = Path(cfg["work_dir"]) / "llm"
        self.sem = threading.Semaphore(int(lc.get("concurrency") or 8))

    def entry(self, who: str) -> dict:
        fams, roles = self.cfg.get("families") or {}, self.cfg.get("roles") or {}
        if who in fams:
            return dict(fams[who], name=who)
        if who in roles:
            r = roles[who]
            return self.entry(r) if isinstance(r, str) else dict(r, name=who)
        raise KeyError(f"no model family or role {who!r} in llm.families / llm.roles")

    def model(self, who: str) -> str:
        return self.entry(who)["model"]

    # ------------------------------------------------------------------ one request
    def chat(self, who: str, system: str, messages: list[dict] | str, tag: str, stage: str = "misc") -> dict:
        """Returns {text, model, returned_model, model_ok, cached, http_status, usage}; raises DryRun in dry-run mode."""
        m = self.entry(who)
        msgs = [{"role": "user", "content": messages}] if isinstance(messages, str) else list(messages)
        req = {"model": m["model"], "system": system, "messages": msgs, "max_tokens": m.get("max_tokens", 32000)}
        key = sha("\0".join([m["model"], system, json.dumps(msgs, ensure_ascii=False), tag]))
        cache = self.work / "cache" / key[:2] / f"{key}.json"
        if self.use_cache and cache.exists():
            hit = json.loads(cache.read_text())
            if hit.get("model_ok") and hit.get("text"):
                return dict(hit, cached=True)
        if self.dry:
            write_json(self.work / "requests" / stage / f"{tag}.{m['name']}.{key[:12]}.json",
                       {"provider": m.get("provider"), "base_url": m.get("base_url"), "tag": tag, **req})
            raise DryRun(tag)
        with self.sem:
            res = self._post(m, req)
        res |= {"model": m["model"], "cached": False, "request_sha256": key, "tag": tag}
        want = m.get("returned_model")
        res["model_ok"] = bool(res.get("text")) and (not want or want in str(res.get("returned_model") or ""))
        if self.use_cache and res["model_ok"]:
            write_json(cache, res)
        return res

    def _post(self, m: dict, req: dict) -> dict:
        api_key = os.environ.get(m.get("api_key_env") or "", "")
        if m.get("provider", "openai") == "anthropic":
            url = m["base_url"].rstrip("/") + "/v1/messages"
            body = {"model": req["model"], "max_tokens": req["max_tokens"], "system": req["system"],
                    "messages": req["messages"]}
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        else:
            url = m["base_url"].rstrip("/") + "/chat/completions"
            body = {"model": req["model"], "max_completion_tokens": req["max_tokens"],
                    "messages": [{"role": "system", "content": req["system"]}, *req["messages"]]}
            headers = {"Authorization": f"Bearer {api_key}"}
        body |= m.get("extra_body") or {}
        headers |= {str(k): str(v) for k, v in (m.get("headers") or {}).items()}
        retries, backoff = int(self.cfg.get("retries") or 4), float(self.cfg.get("backoff_s") or 30)
        last = None
        for attempt in range(retries):
            r = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
            t0 = time.time()
            try:
                with urllib.request.urlopen(r, timeout=float(self.cfg.get("timeout_s") or 1800)) as resp:
                    out = json.loads(resp.read())
                if "content" in out:
                    text = "".join(b.get("text", "") for b in out["content"] if b.get("type") == "text")
                else:
                    text = out["choices"][0]["message"].get("content") or ""
                return {"text": text, "http_status": 200, "returned_model": out.get("model"),
                        "usage": out.get("usage"), "latency_s": round(time.time() - t0, 1)}
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code in (400, 401, 403, 404):
                    break
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
                last = type(exc).__name__
            time.sleep(backoff * (attempt + 1))
        return {"text": None, "http_status": None, "error": last, "returned_model": None}
