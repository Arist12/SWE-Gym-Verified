"""Run one coding agent on one rendered prompt, in the task's environment, through ``tbx``.

``agents.<family>`` configures the agent CLI: ``command`` (a list; placeholders {model} {schema_path} {schema_json}
{final_path} {workdir} {settings_json}), ``model``, ``output`` (how the final JSON is read: ``claude_stream`` = the
Claude Code stream-json result's structured_output, ``codex_events`` = the file Codex writes with -o, ``stdout_json`` =
the last JSON object on stdout, ``file`` = {final_path}), ``env``. The prompt goes to stdin. The agent's PATH starts
with the directory of ``tbx``; ``tbx`` runs every command in a fresh ``--network none`` container of the task's final
environment image (``final_image``: the task image after PREP with single-commit history and the project's copies
removed), with /in (read-only inputs) and /out (the agent's scratch).

The reply is validated against the stage's released JSON schema; an attempt with a hard check flag (e.g. a claimed
witness without a patch file) or an effective audit flag is retried once (``agents.void_retries``); a last attempt
that is still void leaves the family UNSURE for the stage. Dry run (``llm.dry_run``): no agent starts; the rendered
prompt is written to the stage directory and the stage stays pending.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .. import NAME
from ..harness.runtime import COPIES, Docker, prep_script
from ..util import sha, slug
from . import audit as A

TBX = Path(__file__).resolve().parents[1] / "data" / "tbx"


def validate_schema(obj, schema: dict | None) -> list[str]:
    if schema is None:
        return []
    import jsonschema

    v = jsonschema.Draft7Validator(schema)
    return [f"{'/'.join(map(str, e.path))}: {e.message}"[:300] for e in v.iter_errors(obj)][:20]


class AgentRunner:
    def __init__(self, cfg: dict, stage: str):
        self.cfg = cfg
        self.ac = cfg.get("agents") or {}
        self.dry = bool((cfg.get("llm") or {}).get("dry_run", True))
        self.docker = Docker(cfg, stage)
        self.work_dir = cfg["work_dir"]
        self.label_ref = A.label_ref_re(self.work_dir)
        self.slots = Path(self.work_dir) / "tbx_slots"

    # ------------------------------------------------------------------ final environment image
    def final_image(self, t: dict) -> dict:
        """The task image after PREP (sanitize 1, harden 1), committed once per task; ``drop_image`` removes it."""
        img = f"{self.docker.prefix}env/{slug(t['instance_id'], 60)}:{sha(t['image'] + t['base_commit'])[:12]}"
        if self.docker.docker("image", "inspect", img).returncode == 0:
            return {"image": img, "reused": True}
        why = self.docker.ensure_image(t["image"])
        if why:
            raise RuntimeError(f"{t['instance_id']}: {why}")
        name, err = self.docker.start(t["image"], t["instance_id"], "envbuild")
        if not name:
            raise RuntimeError(f"{t['instance_id']}: final env: {err}")
        try:
            with tempfile.TemporaryDirectory() as d:
                Path(d, "prep.sh").write_text(prep_script(t["base_commit"], 1, 1))
                Path(d, "copies.py").write_text(COPIES)
                self.docker.cp_in(name, f"{d}/prep.sh", "/root/.prep.sh")
                self.docker.cp_in(name, f"{d}/copies.py", "/root/.prep_copies.py")
            rc, out, err, _ = self.docker.exec(name, "bash /root/.prep.sh < /dev/null; rc=$?; "
                                                     "rm -f /root/.prep.sh /root/.prep_copies.py; exit $rc", timeout=1800)
            hist = next((x for x in out.splitlines() if x.startswith("__PREP_HISTORY__")), "")
            if rc or "__PREP_OK__" not in out or "depth1_at_base" not in hist:
                raise RuntimeError(f"{t['instance_id']}: final env PREP failed: {(out + err)[-400:]}")
            cfg = json.loads(self.docker.docker("image", "inspect", t["image"], "--format",
                                                "{{json .Config}}").stdout or "{}")
            changes = [f"CMD {json.dumps(cfg.get('Cmd') or ['/bin/bash'])}",
                       f"WORKDIR {cfg.get('WorkingDir') or '/testbed'}",
                       f"ENTRYPOINT {json.dumps(cfg.get('Entrypoint') or [])}",
                       f"LABEL {NAME}.env={slug(t['instance_id'], 60)}"]
            if not self.docker.commit(name, img, changes):
                raise RuntimeError(f"{t['instance_id']}: final env: docker commit failed")
        finally:
            self.docker.rm(name)
        return {"image": img, "history": hist}

    def drop_image(self, image: str) -> None:
        if image and image.startswith(self.docker.prefix):
            self.docker.docker("rmi", "-f", image, timeout=600)

    # ------------------------------------------------------------------ one agent run
    def run(self, t: dict, family: str, step: str, prompt: str, in_dir: Path, schema_path: Path | None, wd: Path,
            image: str, timeout_s: float, attempt: int) -> tuple[dict | None, dict]:
        ac = self.ac[family]
        out_dir = wd / "out"
        if out_dir.exists() and attempt > 0:
            shutil.move(str(out_dir), str(wd / f"out.a{attempt - 1}.{int(time.time())}"))
        out_dir.mkdir(parents=True, exist_ok=True)
        (wd / "prompt.md").write_text(prompt)
        final = wd / f"final.a{attempt}.json"
        final.unlink(missing_ok=True)
        tbxlog = wd / f"tbx.a{attempt}.jsonl"
        schema_json = schema_path.read_text() if schema_path else "{}"
        subs = {"model": ac.get("model") or "", "schema_path": str(schema_path or ""), "schema_json": schema_json,
                "final_path": str(final), "workdir": str(wd), "settings_json": json.dumps(ac.get("settings") or {})}
        cmd = []
        for x in ac["command"]:
            x = str(x)
            for k, v in subs.items():
                x = x.replace("{" + k + "}", v)
            cmd.append(x)
        tag = f"{family[:2]}{step.lower()[:3]}-{slug(t['instance_id'], 24)}"
        env_extra = {"TBX_IMAGE": image, "TBX_IN": str(in_dir), "TBX_OUT": str(out_dir), "TBX_TAG": tag,
                     "TBX_LOG": str(tbxlog), "TBX_SLOTS_DIR": str(self.slots), "TBX_NSLOTS": str(self.ac.get("tbx_slots", 8)),
                     "TBX_TIMEOUT": str(self.ac.get("tbx_timeout_s", 900)), "TBX_PREFIX": self.docker.prefix,
                     "TBX_LABELS": " ".join(f"{k}={v}" for k, v in {**self.docker.labels,
                                                                    f"{NAME}.agent": f"{family}-{step}"}.items()),
                     "TBX_MOUNTS": ";".join(f"{a}|{b}" for a, b in (t.get("mounts") or []))}
        if self.ac.get("tbx_cpu_base") is not None:
            env_extra["TBX_CPU_BASE"] = str(self.ac["tbx_cpu_base"])
        bindir = wd / ".bin"
        bindir.mkdir(exist_ok=True)
        shutil.copy(TBX, bindir / "tbx")
        os.chmod(bindir / "tbx", 0o755)
        drop = set(ac.get("drop_env") or [])
        env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith(("TBX_",))}
        env |= {"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}", "TERM": "dumb",
                **(ac.get("settings") or {}).get("env", {}), **(ac.get("env") or {}), **env_extra}
        out_f, err_f = wd / f"stream.a{attempt}.jsonl", wd / f"stderr.a{attempt}.log"
        t0 = time.time()
        timed_out = False
        with open(out_f, "w") as fo, open(err_f, "w") as fe:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=fo, stderr=fe, env=env, cwd=wd,
                                 start_new_session=True)
            p.stdin.write(prompt.encode())
            p.stdin.close()
            try:
                rc = p.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    rc = p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    rc = p.wait()
        self._remove_leftovers(tag)
        meta = {"step": step, "family": family, "attempt": attempt, "exit_code": rc, "timed_out": timed_out,
                "wall_s": round(time.time() - t0, 1),
                "n_tbx_logged": sum(1 for _ in open(tbxlog)) if tbxlog.exists() else 0}
        out, per_call = None, []
        mode = ac.get("output", "stdout_json")
        if mode == "claude_stream":
            res, calls = A.claude_calls(out_f)
            per_call = [(A.audit_command(c["input"].get("command") or "", self.label_ref) if c["name"] == "Bash" else
                         [f"non_shell_tool: {c['name']}: {json.dumps(c['input'])[:200]}"], c["denied"])
                        for c in calls if c["name"] != "StructuredOutput"]
            out = res.get("structured_output")
            if out is None and res.get("result"):
                try:
                    out = json.loads(res["result"])
                except ValueError:
                    out = None
            used = sorted(res.get("modelUsage") or {})
            meta |= {"num_turns": res.get("num_turns"), "usage": res.get("usage"), "models_used": used,
                     "model_ok": (not ac.get("model")) or used == [ac["model"]], "n_commands": len(calls)}
        else:
            if mode == "codex_events":
                usage, commands, turns, errors, other = A.codex_events(out_f, wd)
                per_call = [(A.audit_command(c, self.label_ref), False) for c in commands] + [([o], False) for o in other]
                meta |= {"turns": turns, "usage": usage, "n_commands": len(commands), "event_errors": errors[:3]}
            src = final if mode in ("codex_events", "file") else out_f
            if src.exists():
                from ..util import parse_json
                txt = src.read_text(errors="replace")
                try:
                    out = json.loads(txt)
                except ValueError:
                    out = parse_json(txt[-200000:])
            meta["model_ok"] = True
        meta["audit_effective"], meta["audit_denied"] = A.split(per_call)
        meta["schema_errors"] = validate_schema(out, json.loads(schema_json) if schema_path else None) if out else []
        return out, meta

    def _remove_leftovers(self, tag: str) -> None:
        ids = self.docker.docker("ps", "-aq", "--filter", f"label={NAME}.run={self.docker.run_id}",
                                 "--filter", f"name={self.docker.prefix}tbx-{tag}-").stdout.split()
        if ids:
            self.docker.docker("rm", "-f", *ids)

    def stage(self, t: dict, family: str, step: str, prompt: str, in_dir: Path, schema_path: Path | None, wd: Path,
              image: str, check, deadline: float | None = None) -> dict:
        """The cached stage result for a byte-identical prompt, else up to 1 + void_retries attempts. ``check(out, wd)``
        -> (hard flags, soft flags)."""
        cached = wd / "stage.json"
        psha = sha(prompt)
        if cached.exists():
            row = json.loads(cached.read_text())
            if row.get("prompt_sha256") == psha and not row.get("dry_run"):
                return row
        wd.mkdir(parents=True, exist_ok=True)
        if self.dry:
            (wd / "prompt.md").write_text(prompt)
            return {"family": family, "step": step, "output": None, "dry_run": True, "prompt_sha256": psha,
                    "flags": ["dry_run"], "soft_flags": [], "unsure": False, "attempts": []}
        tries, out = [], None
        limit = float((self.ac.get("timeouts_s") or {}).get(step, 2700))
        for attempt in range(1 + int(self.ac.get("void_retries", 1))):
            budget = limit if deadline is None else max(60.0, min(limit, deadline - time.time()))
            out, meta = self.run(t, family, step, prompt, in_dir, schema_path, wd, image, budget, attempt)
            hard, soft = check(out, wd) if out is not None else (["no_valid_output"], [])
            if meta.get("schema_errors"):
                hard = hard + ["schema: " + "; ".join(meta["schema_errors"][:3])]
            meta["flags"], meta["soft_flags"] = hard, soft
            meta["void"] = bool(meta.get("audit_effective"))
            tries.append(meta)
            if not meta["void"] and not hard and meta.get("model_ok", True):
                break
            if deadline is not None and deadline - time.time() < 120:
                break
        unsure = tries[-1]["void"]
        row = {"family": family, "step": step, "output": None if unsure else out, "flags": tries[-1]["flags"],
               "soft_flags": tries[-1]["soft_flags"], "attempts": tries, "prompt_sha256": psha,
               "n_void": sum(1 for a in tries if a["void"]), "unsure": unsure,
               "n_denied_attempts": sum(1 for a in tries if a.get("audit_denied"))}
        cached.write_text(json.dumps(row, indent=1, default=str))
        return row


BOX = Path(__file__).resolve().parents[1] / "data" / "box_mcp.py"


def blind_attempt(self, t: dict, family: str, prompt: str, wd: Path) -> dict:
    """One blind-solve attempt (step 6a): a persistent network-less container of the final environment, the agent's only
    tool the MCP ``shell`` of data/box_mcp.py; the patch is ``git add -A; git diff --cached`` of /testbed.
    ``agents.<family>.blind_command`` is the agent command (placeholders as ``command`` plus {mcp_config})."""
    import sys
    ac = self.ac[family]
    if not ac.get("blind_command"):
        return {"error": f"agents.{family}.blind_command is not set"}
    wd.mkdir(parents=True, exist_ok=True)
    img = self.final_image(t)
    name, err = self.docker.start(img["image"], t["instance_id"], "blind")
    if not name:
        return {"error": err}
    try:
        mcp = json.dumps({"mcpServers": {"box": {"command": sys.executable, "args": [str(BOX), name]}}})
        saved = ac["command"]
        ac["command"] = [str(x).replace("{mcp_config}", mcp) for x in ac["blind_command"]]
        try:
            out, meta = self.run(t, family, "B", prompt, wd, self.prompts_schema_6a(), wd, img["image"],
                                 float((self.ac.get("timeouts_s") or {}).get("B", 7200)), 0)
        finally:
            ac["command"] = saved
        rc, patch, _, _ = self.docker.exec(name, "cd /testbed && git add -A >/dev/null 2>&1; git diff --cached", 600)
        return {"notes": out, "patch": patch if rc == 0 else "", "meta": meta}
    finally:
        self.docker.rm(name)
        if not img.get("reused"):
            self.drop_image(img["image"])


def _schema_6a(self) -> Path:
    from ..prompts import Prompts
    return Prompts(self.cfg).schema_path("blind_solver")


AgentRunner.blind_attempt = blind_attempt
AgentRunner.prompts_schema_6a = _schema_6a


def run_tests_script(eval_script: str, env: dict) -> str:
    """/in/run_tests.sh: the grader's own eval script with the grader's environment."""
    import shlex

    from ..harness import _guards as G
    head = ("#!/bin/bash\n# The grader's eval script with the grader's environment.\n"
            f"mkdir -p /tmp/chk_site && printf '%s' {shlex.quote(G.SITECUSTOMIZE)} > /tmp/chk_site/sitecustomize.py\n"
            f"export {' '.join(f'{k}={v}' for k, v in env.items())}\n")
    return head + eval_script.split("\n", 1)[1]

