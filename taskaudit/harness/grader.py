"""Grade one patch on one task in a fresh container, with the paper's resolution rule and guards.

One run: start the task image (network off, 2 CPUs, 8 GB, 1024 PIDs), run PREP (single-commit history at the base;
``harden`` >= 1 also removes copies of the project's source), apply the patch (git apply, else patch --fuzz=5), run
the guarded eval script (evalscript.py) under the timeout, parse per-test statuses.

Resolved = the patch applied, the tests ran (no timeout, no harness error), every FAIL_TO_PASS test passed and every
graded PASS_TO_PASS test passed; an empty patch never resolves (the empty control reports what the tests do without a
fix). A patch's test files and test configuration are put back before the tests (``_guards.test_reset_guard``), so a
patch that edits test files cannot pass by them; a test patch that fails to apply, a test reset that fails, or a
conftest / pytest configuration written while the tests run is a harness error: no verdict.
"""
from __future__ import annotations

import gzip
import json
import tempfile
import time
import uuid
from pathlib import Path

from . import _guards as G
from .evalscript import backend_of, eval_script
from .parsers import parse_status
from .runtime import COPIES, Docker, parse_prep, prep_script

TEST_PATCH_FAILED, TEST_RESET_FAILED, RUNTIME_CONFIG = "test_patch_apply_failed", "test_reset_failed", "runtime_config"
APPLY = ("cd /testbed && (git apply -v /tmp/chk.patch >/dev/null 2>&1 && echo git) || "
         "(patch --batch --fuzz=5 -p1 -i /tmp/chk.patch >/dev/null 2>&1 && echo fuzz) || echo fail")


def harness_error(rec: dict) -> str | None:
    if rec.get("test_patch_failed"):
        return TEST_PATCH_FAILED
    if rec.get("test_reset_failed"):
        return TEST_RESET_FAILED
    if rec.get("runtime_config"):
        return RUNTIME_CONFIG
    return None


def verdict(rec: dict, patch: str) -> dict:
    """``resolved`` of one run (PASS_TO_PASS is already the graded list); a harness error has no verdict."""
    err = harness_error(rec)
    ran = ("f2p" in rec and "p2p" in rec and not rec.get("timeout") and not rec.get("error")
           and rec.get("apply") != "fail" and err is None)
    ok = ran and all(rec[k]["fail"] == 0 and rec[k]["missing"] == 0 for k in ("f2p", "p2p"))
    out = {"resolved": bool(ok and (patch.strip() or rec.get("kind") == "empty"))}
    if err:
        out |= {"resolved": False, "harness_error": err}
    return out


def infra_reason(r: dict) -> str | None:
    """Why a run says nothing about the task (retried by the environment stage)."""
    if r.get("error"):
        return "error"
    if r.get("prep_error"):
        return "prep"
    if r.get("timeout"):
        return "timeout"
    if r.get("oom"):
        return "oom"
    if r.get("test_reset_failed"):
        return "test_reset"
    if r.get("runtime_config"):
        return "runtime_config"
    bad = any((r.get(p) or {}).get(k) for p in ("f2p", "p2p") for k in ("failed_ids", "missing_ids"))
    if r.get("resource_errors") and bad and "gold" in r.get("kind", ""):
        return "resource"
    return None


def test_data_mounts(cfg: dict, t: dict) -> list:
    """The read-only mounts of a task's ``test_data_mounts``: [host path, container path] pairs as they are, and the
    dataset's entries ({container_path, ...}) from <harness.testdata_dir>/<instance_id>/<container path>."""
    root = (cfg.get("harness") or {}).get("testdata_dir")
    entries = t.get("test_data_mounts") or []
    out = []
    for m in json.loads(entries) if isinstance(entries, str) else entries:
        if not isinstance(m, dict):
            out.append(list(m))
        elif root:
            out.append([str(Path(root).expanduser() / t["instance_id"] / m["container_path"].lstrip("/")),
                        m["container_path"]])
    return out


class Grader:
    def __init__(self, cfg: dict, stage: str, work_dir: str | Path):
        self.cfg = cfg
        self.docker = Docker(cfg, stage)
        try:
            usable = self.docker.docker("version", timeout=60).returncode == 0
        except OSError:
            usable = False
        if not usable:  # a host problem must stop the run instead of being recorded as a task failure
            raise SystemExit("docker is not usable: `docker version` failed (is the daemon running?)")
        self.logdir = Path(work_dir) / "logs" / stage
        self.timeout = int((cfg.get("harness") or {}).get("timeout_s") or G.TIMEOUT)
        self.env = {**G.TASK_ENV, **((cfg.get("harness") or {}).get("env") or {})}

    def mounts(self, t: dict) -> list[tuple[str, str]]:
        return [tuple(m) for m in (t.get("mounts") or [])]

    def run(self, t: dict, patch: str, kind: str = "model", *, harden: int = 0, network: str | None = None,
            keep_statuses: bool = False) -> dict:
        """Grade ``patch`` ("" for the empty control) on task ``t`` (the effective task: graded F2P / P2P lists)."""
        backend = backend_of(self.cfg, t)
        rec: dict = {"instance_id": t["instance_id"], "kind": kind, "grader": G.GRADER, "backend": backend,
                     "image": t.get("image"), "network": network or (self.cfg.get("harness") or {}).get("network")}
        start = time.time()
        name = None
        try:
            script = eval_script(t, patch, t["patch"], backend)
            why = self.docker.ensure_image(t["image"])
            if why:
                rec["error"] = why
                return rec
            name, err = self.docker.start(t["image"], t["instance_id"], kind, network=network,
                                          mounts=self.mounts(t), env=self.env)
            if not name:
                rec["error"] = err
                return rec
            with tempfile.TemporaryDirectory() as d:
                Path(d, "eval.sh").write_text(script)
                Path(d, "model.patch").write_text(patch or "")
                Path(d, "prep.sh").write_text(prep_script(t["base_commit"], 1, harden))
                Path(d, "copies.py").write_text(COPIES)
                Path(d, "site").mkdir()
                Path(d, "site", "sitecustomize.py").write_text(G.SITECUSTOMIZE)
                for src, dst in (("eval.sh", "/tmp/chk_eval.sh"), ("model.patch", "/tmp/chk.patch"),
                                 ("prep.sh", "/root/.prep.sh"), ("copies.py", "/root/.prep_copies.py"),
                                 ("site", "/tmp/chk_site")):
                    if not self.docker.cp_in(name, f"{d}/{src}", dst):
                        rec["error"] = f"docker_cp {src}"
                        return rec
            rc, out, errs, to = self.docker.exec(name, "bash /root/.prep.sh 2>&1; echo __PREP_RC__$?; "
                                                       "rm -f /root/.prep.sh /root/.prep_copies.py", timeout=1200)
            if "__PREP_OK__" not in out:
                rec["prep_error"] = (out + errs)[-800:]
                return rec
            rec["prep"] = parse_prep(out)
            if (patch or "").strip():
                rc, out, _, _ = self.docker.exec(name, APPLY, timeout=300)
                rec["apply"] = (out.strip().splitlines() or ["fail"])[-1]
                if rec["apply"] == "fail":
                    return rec
            rc, out, errs, to = self.docker.exec(name, "bash /tmp/chk_eval.sh", timeout=self.timeout)
            log = out + "\n" + errs
            rec["exit"] = rc
            if to:
                rec["timeout"] = True
            if self.docker.oom(name):
                rec["oom"] = True
            lp = self.logdir / t["instance_id"] / f"{kind}-{uuid.uuid4().hex[:6]}.log.gz"
            lp.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(lp, "wt") as f:
                f.write(log)
            rec["log"] = str(lp)
            rec["test_patch_failed"] = bool(G.marks(log, G.TEST_PATCH_MARK))
            if cols := G.marks(log, G.COLLISION_MARK):
                rec["test_patch_collisions"] = sorted(set(cols))
            rec["test_reset"] = G.marks(log, G.TEST_RESET_MARK)
            rec["test_reset_failed"] = bool(G.marks(log, G.TEST_RESET_FAIL_MARK))
            rec["runtime_config"] = G.runtime_config_hits(log)
            rec["tests_started"] = G.split_tests(log)[1] is not None
            rec["resource_errors"] = len(G.RESOURCE_ERRORS.findall(log))
            sm = parse_status(t, log)
            rec["n_parsed"] = len(sm)
            if keep_statuses:
                rec["statuses"] = sm
            rec["f2p"] = G.test_outcome(t["FAIL_TO_PASS"], sm)
            rec["p2p"] = G.test_outcome(t["PASS_TO_PASS"], sm)
            return rec
        except Exception as exc:  # one run's failure is a record, never a crash of the stage
            rec["error"] = f"{type(exc).__name__}: {str(exc)[-300:]}"
            return rec
        finally:
            if name:
                self.docker.rm(name)
            rec["seconds"] = round(time.time() - start, 1)
            rec.update(verdict(rec, patch if kind != "empty" else ""))


def bad_ids(rec: dict) -> tuple[list[str], list[str]]:
    f2p, p2p = rec.get("f2p") or {}, rec.get("p2p") or {}
    return (sorted(set(f2p.get("failed_ids") or []) | set(f2p.get("missing_ids") or [])),
            sorted(set(p2p.get("failed_ids") or []) | set(p2p.get("missing_ids") or [])))


def read_log(path: str | None) -> str:
    if not path:
        return ""
    try:
        with gzip.open(path, "rt", errors="replace") as f:
            return f.read()
    except OSError:
        return ""
