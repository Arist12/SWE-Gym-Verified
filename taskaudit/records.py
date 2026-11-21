"""Append-only JSONL records, one file per stage, and the task status derived from them.

<work_dir>/stages/<namespace>/<stage>.jsonl holds one row per (task, run); the last row of a task is its record. A
stage skips a task whose last row is ``complete`` and carries the stage's current input ``key`` (prompt sha256s,
inputs, settings), so every stage is resumable and idempotent: re-running it after a crash, or with a changed prompt,
recomputes exactly the rows whose inputs changed. Common row fields (every stage):

    instance_id   task id
    stage         stage name
    key           sha256 of the stage's inputs for this task
    complete      false: the stage could not decide (dry run, an infrastructure error); a later run retries it
    decision      keep | drop | flag | repaired | pending | ... (stage-specific values are listed per stage)
    reasons       list of reason codes (a drop's codes are the removal reasons)
    utc           time the row was written
"""
from __future__ import annotations

import threading
from pathlib import Path

from .util import append_jsonl, read_jsonl, utc

DROP = "drop"


class Store:
    def __init__(self, work_dir: str | Path, namespace: str = "main"):
        self.root = Path(work_dir)
        self.ns = namespace
        self.dir = self.root / "stages" / namespace
        self.dir.mkdir(parents=True, exist_ok=True)
        self._rows: dict[str, dict[str, dict]] = {}
        self._lock = threading.Lock()

    def path(self, stage: str) -> Path:
        return self.dir / f"{stage}.jsonl"

    def rows(self, stage: str) -> dict[str, dict]:
        with self._lock:
            if stage not in self._rows:
                out = {}
                for r in read_jsonl(self.path(stage)):
                    out[r["instance_id"]] = r
                self._rows[stage] = out
            return self._rows[stage]

    def get(self, stage: str, iid: str) -> dict | None:
        return self.rows(stage).get(iid)

    def put(self, stage: str, row: dict) -> dict:
        row = {"instance_id": row["instance_id"], "stage": stage, **{k: v for k, v in row.items()
                                                                       if k not in ("instance_id", "stage")}}
        row.setdefault("complete", True)
        row.setdefault("reasons", [])
        row["utc"] = utc()
        self.rows(stage)
        append_jsonl(self.path(stage), row)
        with self._lock:
            self._rows[stage][row["instance_id"]] = row
        return row

    def done(self, stage: str, iid: str, key: str) -> bool:
        r = self.get(stage, iid)
        return bool(r and r.get("complete") and r.get("key") == key)

    def work(self, stage: str, iid: str) -> Path:
        d = self.root / "work" / self.ns / stage / iid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def dropped(self, iid: str, stages: list[str]) -> dict | None:
        """The first stage row (in pipeline order) that removed the task, or None."""
        for s in stages:
            r = self.get(s, iid)
            if r and r.get("decision") == DROP:
                return r
        return None
