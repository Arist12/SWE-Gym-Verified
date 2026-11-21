"""Stages of the audit, in pipeline order. Each stage module defines

    NAME          the stage name (its JSONL file: <work_dir>/stages/main/<NAME>.jsonl)
    DEPENDS       stages whose row must be complete before this stage looks at a task
    wants(ctx, iid) whether the task needs this stage at all (default: every task not removed earlier)
    key(ctx, iid)   sha256 of the stage's inputs for the task (prompt sha256s, task view, settings)
    run(ctx, iid)   -> the stage row (see records.py for the common fields)

and is resumable: a task whose last row is complete under the current key is skipped.
"""
from __future__ import annotations

import functools
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..adapters import load_tasks
from ..harness.grader import test_data_mounts
from ..llm import LLM
from ..prompts import Prompts
from ..records import Store
from ..repo import Repos, TaskSources
from ..util import sha_json, statement_with_spec

ORDER = ["prescreen", "environment", "screen", "fn_rate", "escalate", "p2p", "measure", "triage", "repair", "reaudit",
         "fp", "export"]
REMOVING = ["prescreen", "environment", "screen", "fn_rate", "escalate", "p2p", "triage", "repair", "reaudit"]


def module(name: str):
    import importlib
    return importlib.import_module(f".{name}", __name__)


class Ctx:
    """Everything a stage needs: config, records, prompts, LLM, repositories, tasks and their effective views."""

    def __init__(self, cfg: dict, ids: list[str] | None = None, store: Store | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg["work_dir"])
        self.prompts = Prompts(cfg)
        self.llm = LLM(cfg)
        self.repos = Repos(cfg)
        self.workers = int(cfg.get("workers") or 4)
        self._tasks = None
        self._ids = ids
        self._lock = threading.Lock()
        self._packs: dict[str, dict] = {}
        self._pack_locks: dict[str, threading.Lock] = {}
        self._sources: dict[str, TaskSources] = {}

    @property
    def tasks(self) -> dict[str, dict]:
        if self._tasks is None:
            self._tasks = {t["instance_id"]: t for t in load_tasks(self.cfg, self._ids)}
        return self._tasks

    def scfg(self, stage: str) -> dict:
        return (self.cfg.get("stages") or {}).get(stage) or {}

    # ------------------------------------------------------------------ task views
    def effective(self, iid: str, with_repair: bool = True, with_p2p: bool = True) -> dict:
        """The task as later stages see it: the environment stage's P2P drops and test-data mounts, and (after a repair)
        the appended specification or the relaxed test side. ``statement`` = the issue followed by the specification;
        ``problem_statement`` is set to it, as every judge and agent reads the statement under that name."""
        t = dict(self.tasks[iid])
        env = self.store.get("environment", iid) or {}
        drop = set((env.get("p2p_dropped") or {}).keys()) | set(t.get("p2p_dropped") or [])
        if with_p2p:  # P2P ids the P2P audit confirmed as flaky
            drop |= set(((self.store.get("p2p", iid) or {}).get("p2p_dropped") or {}).keys())
        rep = self.store.get("repair", iid) or {}
        spec = None
        if with_repair and rep.get("overlay") and rep.get("decision") == "repaired":
            o = rep["overlay"]
            for k in ("test_patch", "FAIL_TO_PASS", "PASS_TO_PASS"):
                if o.get(k) is not None:
                    t[k] = o[k]
            spec = o.get("spec_block")
            drop |= set(o.get("p2p_dropped") or [])
        t["PASS_TO_PASS"] = [x for x in t["PASS_TO_PASS"] if x not in drop]
        t["p2p_dropped"] = sorted(drop)
        t["mounts"] = env.get("mounts") or test_data_mounts(self.cfg, t)
        t["statement_original"] = self.tasks[iid]["problem_statement"] or ""
        t["spec_block"] = spec
        t["statement"] = statement_with_spec(t["statement_original"], spec)
        t["problem_statement"] = t["statement"]
        return t

    def view_sha(self, t: dict) -> str:
        return sha_json([t["statement"], t["test_patch"], t["FAIL_TO_PASS"], t["PASS_TO_PASS"], t["patch"],
                         t.get("image"), t["base_commit"]])

    def sources(self, t: dict) -> TaskSources:
        k = self.view_sha(t)
        with self._lock:
            if k not in self._sources:
                self.repos.ensure(t["repo"], t["base_commit"])
                self._sources[k] = TaskSources(self.repos, t)
            return self._sources[k]

    # ------------------------------------------------------------------ packs
    @functools.cached_property
    def builder(self):
        from ..packs import builder
        return builder(self.cfg["work_dir"], str(self.repos.dir))

    def fn_pack(self, t: dict) -> dict:
        from ..packs import fn_pack
        sc = self.scfg("fn_rate")
        k = self.view_sha(t)
        with self._lock:
            hit = self._packs.get(k)
            build = self._pack_locks.setdefault(k, threading.Lock())
        if hit:
            return hit
        with build:                                   # the two families' threads ask for the same pack: build it once
            with self._lock:
                hit = self._packs.get(k)
            if hit:
                return hit
            out = Path(self.cfg["work_dir"]) / "packs" / "fn" / k[:16]
            p = fn_pack(self.builder, t, out, int(sc.get("pack_budget", 64000)), int(sc.get("pack_budget_degraded", 128000)))
            with self._lock:
                self._packs[k] = p
        return p

    def fp_pack(self, t: dict, patch: str) -> str:
        from ..packs import fp_pack
        out = Path(self.cfg["work_dir"]) / "packs" / "fp" / self.view_sha(t)[:16]
        return fp_pack(self.builder, t, patch, out)

    # ------------------------------------------------------------------ status
    def removed(self, iid: str, before: str | None = None) -> dict | None:
        stages = REMOVING if before is None else [s for s in REMOVING if ORDER.index(s) < ORDER.index(before)]
        return self.store.dropped(iid, stages)


def run_stage(ctx: Ctx, name: str, ids: list[str] | None = None, force: bool = False, log=print) -> dict:
    """Run one stage over the selected tasks; returns counts by decision."""
    mod = module(name)
    if hasattr(mod, "run_all"):
        return mod.run_all(ctx, ids, force=force, log=log)
    todo, counts = [], {}
    for iid in (ids or list(ctx.tasks)):
        if ctx.removed(iid, before=name):
            counts["skipped_removed"] = counts.get("skipped_removed", 0) + 1
            continue
        if any(not (ctx.store.get(d, iid) or {}).get("complete") for d in getattr(mod, "DEPENDS", [])):
            counts["waiting"] = counts.get("waiting", 0) + 1
            continue
        if hasattr(mod, "wants") and not mod.wants(ctx, iid):
            counts["not_applicable"] = counts.get("not_applicable", 0) + 1
            continue
        k = mod.key(ctx, iid)
        if not force and ctx.store.done(name, iid, k):
            counts["up_to_date"] = counts.get("up_to_date", 0) + 1
            continue
        todo.append((iid, k))

    def one(job):
        iid, k = job
        try:
            row = mod.run(ctx, iid)
        except Exception as exc:  # one task's failure must not stop the stage
            row = {"instance_id": iid, "complete": False, "decision": "error",
                   "reasons": [f"{type(exc).__name__}: {str(exc)[:300]}"], "traceback": traceback.format_exc()[-2000:]}
        row["instance_id"], row["key"] = iid, k
        return ctx.store.put(name, row)

    with ThreadPoolExecutor(max(1, min(ctx.workers, len(todo) or 1))) as ex:
        futs = [ex.submit(one, j) for j in todo]
        for f in as_completed(futs):
            r = f.result()
            d = r.get("decision") if r.get("complete") else f"incomplete:{r.get('decision')}"
            counts[d] = counts.get(d, 0) + 1
            log(f"[{name}] {r['instance_id']}: {d} {','.join(map(str, r.get('reasons') or []))[:160]}")
    cleanup = getattr(mod, "cleanup", None)
    if cleanup:
        cleanup(ctx)
    return counts


def stage_key(ctx: Ctx, stage: str, t: dict, *steps: str, extra=None) -> str:
    return sha_json({"stage": stage, "view": ctx.view_sha(t), "prompts": ctx.prompts.shas(*steps),
                     "settings": ctx.scfg(stage), "extra": extra})

