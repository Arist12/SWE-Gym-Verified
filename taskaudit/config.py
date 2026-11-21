"""Run configuration: a YAML file merged over DEFAULTS. ``${VAR}`` and ``${VAR:-default}`` read the environment.

Nothing machine-specific lives in code or in the shipped configs: paths, endpoints and keys come from the environment.
API keys are never stored in the config: a model entry names the environment variable that holds its key.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

from . import ENV_PREFIX, NAME

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# The paper's settings are the defaults (its reasoning settings are provider options: see configs/swegym.yaml);
# dataset-specific values are configurable instead of changing the method.
DEFAULTS: dict = {
    "work_dir": f"./{NAME}-work",
    "workers": 4,
    "dataset": {
        "adapter": "swebench",          # swebench | swegym | jsonl | hf (see adapters/)
        "path": None,                   # .jsonl / .json / .parquet file of SWE-bench-style records
        "hf_name": None, "hf_split": "train", "hf_revision": None,
        "image_template": None,         # e.g. "xingyaoww/sweb.eval.x86_64.{iid_swegym}:latest"
        "image_registry": "",           # prefix put in front of every image name (a pull-through mirror)
        "image_digests": None,          # JSON / JSONL instance_id -> image digest (named in the escalation packets)
        "human_labels": None,           # PAIChecker-style labels: CSV / JSONL with instance_id, label
        "pai_records": None,            # PAIChecker evidence records (JSONL) for the router on unlabelled tasks
        "repos_dir": None,              # bare clones <owner>__<name>.git (default: <work_dir>/repos)
    },
    "harness": {
        "backend": "auto",              # auto | swegym_fork | swebench | script
        "timeout_s": 2400,              # test-run cap (the slowest verified gold runs take ~2,300 s)
        "network": "none",              # every stage runs offline; set "bridge" only for a network probe
        "cpus": 2, "memory": "8g", "pids_limit": 1024, "cpuset": None,
        "container_prefix": f"{NAME}-",
        "labels": {},
        "pull": "missing",              # missing | never | always
        "remove_pulled_images": False,  # remove images this process pulled once a stage finishes
        "testdata_dir": None,           # <dir>/<instance_id>/<container path>: read-only test-data mounts
        "env": {"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    },
    "llm": {
        "dry_run": True,                # no call: each request is written to <work_dir>/llm/requests/
        "cache": True,
        "retries": 4, "backoff_s": 30, "timeout_s": 1800, "concurrency": 8,
        "families": {                   # the two model families of the paper; any compatible endpoint works
            "claude": {"provider": "anthropic", "model": "claude-opus-5-5",
                       "base_url": "${ANTHROPIC_BASE_URL:-https://api.anthropic.com}",
                       "api_key_env": "ANTHROPIC_API_KEY", "max_tokens": 32000, "returned_model": None,
                       "extra_body": {}},
            "gpt": {"provider": "openai", "model": "gpt-6.1-sol",
                    "base_url": "${OPENAI_BASE_URL:-https://api.openai.com/v1}",
                    "api_key_env": "OPENAI_API_KEY", "max_tokens": 32000, "returned_model": None,
                    "extra_body": {}, "single_turn": True},
        },
        # extra model roles; each is a model entry like the families above, or the name of a family
        "roles": {
            "writer": "claude",             # repair writer (one family, as in the paper)
            "leak_judge_a": "claude",       # leak judges of G3 / G5 (the paper: Claude Opus 5.5 + GPT-5.6 Sol)
            "leak_judge_b": {"provider": "openai", "model": "gpt-5.6-sol",
                             "base_url": "${OPENAI_BASE_URL:-https://api.openai.com/v1}",
                             "api_key_env": "OPENAI_API_KEY", "max_tokens": 32000},
            "hygiene": "claude",            # statement hygiene (one family)
            "pai_router": "claude",         # PAIChecker router for tasks without a human label
        },
    },
    "agents": {                         # container-side coding agents (witness / review / debate / read / blind solve)
        "claude": {"command": [
            "claude", "-p", "--model", "{model}", "--effort", "high", "--output-format", "stream-json", "--verbose",
            "--no-session-persistence", "--safe-mode", "--permission-mode", "dontAsk", "--settings", "{settings_json}",
            "--tools", "Bash", "--allowedTools", "Bash(tbx:*)", "--disable-slash-commands", "--strict-mcp-config",
            "--mcp-config", "{\"mcpServers\":{}}", "--json-schema", "{schema_json}"],
            "model": "claude-opus-5-5", "output": "claude_stream",
            # a tbx call may run 900 s (+ slot wait): Bash calls must not move to the background
            "settings": {"env": {"BASH_DEFAULT_TIMEOUT_MS": "1200000", "BASH_MAX_TIMEOUT_MS": "1200000",
                                 "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK": "1",
                                 "CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK": "1"}},
            "env": {}, "drop_env": ["CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID"],
            # blind solve (stages.repair.blind_solve: true): the only tool is the MCP shell of data/box_mcp.py
            "blind_command": [
                "claude", "-p", "--model", "{model}", "--effort", "high", "--output-format", "stream-json", "--verbose",
                "--no-session-persistence", "--permission-mode", "dontAsk", "--setting-sources", "", "--tools", "",
                "--allowedTools", "mcp__box__shell", "--strict-mcp-config", "--mcp-config", "{mcp_config}",
                "--max-turns", "150", "--json-schema", "{schema_json}"]},
        "gpt": {"command": [
            "codex", "exec", "-m", "{model}", "-c", "model_reasoning_effort=\"high\"", "-c",
            "project_doc_max_bytes=0", "--skip-git-repo-check", "--ephemeral", "--json", "--output-schema",
            "{schema_path}", "-o", "{final_path}", "-s", "danger-full-access", "-C", "{workdir}", "-"],
            "model": "gpt-6.1-sol", "output": "codex_events", "env": {}},
        # per agent step: W witness, R review, D debate, T triage read, B blind solve
        "timeouts_s": {"W": 5400, "R": 2700, "D": 2700, "T": 3600, "B": 7200},
        "task_cap_s": 7200,              # per-task wall-time cap of one escalation
        "tbx_timeout_s": 900, "tbx_slots": 8, "tbx_cpu_base": None,
        "void_retries": 1,               # an attempt with an effective audit flag is void and re-run once
    },
    "prompts": {
        "dir": None,                    # default: the packaged prompts (taskaudit/data/prompts)
        "check": "warn",                # strict: a prompt whose sha256 differs from SHA256SUMS stops the run
        "files": {},                    # step -> file name override (see prompts.STEPS)
    },
    "stages": {
        "prescreen": {"min_statement_chars": 1, "dedupe": True},
        "environment": {"reps": 3, "max_infra_retries": 2, "prep_harden": 1, "probe_network": False},
        "screen": {"attempts": 3, "router": True},
        "fn_rate": {"pack_budget": 64000, "pack_budget_degraded": 128000, "reask": 1},
        "escalate": {},
        "p2p": {"enabled": False, "candidates": None, "max_groups": 8},
        "triage": {"read": True},
        "repair": {"blind_solve": False, "blind_attempts": 3, "writer_attempts": 3},
        "measure": {"enabled": False, "samples": 8, "policy": None, "rollouts": None},
        "fp": {"candidates": None, "max_patches_per_task": None, "split_seed": "fp-split",
               "script_timeout_s": 60, "max_base_runtime_s": 24},
        "export": {},
    },
}


def _expand(o):
    if isinstance(o, dict):
        return {k: _expand(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_expand(v) for v in o]
    if isinstance(o, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), o)
    return o


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*:\s*['\"]?[A-Za-z0-9_\-]{16,}")


def load(path: str | Path | None = None, overrides: dict | None = None) -> dict:
    """The config at ``path`` (or $<PREFIX>_CONFIG) over DEFAULTS, environment references expanded."""
    import yaml

    p = path or os.environ.get(f"{ENV_PREFIX}_CONFIG")
    user = {}
    if p:
        text = Path(p).read_text()
        if SECRET.search(text):
            raise SystemExit(f"{p}: looks like it contains a secret; name the environment variable instead "
                             "(api_key_env)")
        user = yaml.safe_load(text) or {}
    cfg = _expand(merge(merge(DEFAULTS, user), overrides or {}))
    work = Path(cfg["work_dir"]).expanduser()
    cfg["work_dir"] = str(work.resolve())
    return cfg

