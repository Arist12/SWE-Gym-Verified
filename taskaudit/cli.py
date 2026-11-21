"""Command line.

    taskaudit run    --config CFG --stage STAGE[,STAGE...]|all [--ids ID,ID | --ids-file FILE] [--force]
    taskaudit status --config CFG [--ids ...]   # decisions per stage, and the tasks kept / repaired / removed / pending
    taskaudit export --config CFG [--out DIR]   # tasks, audit_records, witness_patches, fp_labels, fp_tests (.jsonl)
    taskaudit prompts --config CFG              # each step's prompt file and whether it matches SHA256SUMS

Stages (in order): prescreen, environment, screen, fn_rate, escalate, p2p, measure, triage, repair, reaudit, fp,
export. --stage all runs them in order; every stage is resumable (a finished task is skipped). LLM and agent steps
make no call while llm.dry_run is true: requests are written to <work_dir>/llm/requests and the stage stays pending
for those tasks.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from . import NAME


def _ids(a) -> list[str] | None:
    ids = []
    if a.ids:
        ids += [x.strip() for x in a.ids.split(",") if x.strip()]
    if a.ids_file:
        txt = Path(a.ids_file).read_text()
        try:
            ids += list(json.loads(txt))
        except ValueError:
            ids += [x.strip() for x in txt.splitlines() if x.strip() and not x.startswith("#")]
    return ids or None


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("PYTHONHASHSEED") != "0":  # the pack builder's determinism requirement
        os.environ["PYTHONHASHSEED"] = "0"
        os.execv(sys.executable, [sys.executable, "-m", f"{NAME}.cli", *(argv if argv is not None else sys.argv[1:])])
    from .config import load
    from .stages import ORDER, Ctx, run_stage

    ap = argparse.ArgumentParser(prog=NAME, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "status", "export", "prompts"):
        p = sub.add_parser(name)
        p.add_argument("--config", help="YAML config file")
        p.add_argument("--ids", help="comma-separated instance ids")
        p.add_argument("--ids-file", help="file of instance ids (a JSON list, or one id per line)")
        if name == "run":
            p.add_argument("--stage", required=True, help="a stage name, a comma list, or all")
            p.add_argument("--force", action="store_true", help="recompute rows whose key is unchanged")
        if name == "export":
            p.add_argument("--out", help="export directory (default: <work_dir>/export)")
    a = ap.parse_args(argv)
    if not (a.config or os.environ.get(f"{NAME.upper()}_CONFIG")):
        ap.error(f"--config FILE (or ${NAME.upper()}_CONFIG) is required")
    over = {"stages": {"export": {"out": a.out}}} if getattr(a, "out", None) else None
    cfg = load(a.config, over)
    ctx = Ctx(cfg, _ids(a))
    if a.cmd == "prompts":
        from .prompts import STEPS
        for step in STEPS:
            m = ctx.prompts.meta(step)
            print(f"{step:20s} {m['file']:32s} {m['sha256'][:12]} {'ok' if m['matches_sums'] else 'DIFFERS FROM SHA256SUMS'}")
        return 0
    if a.cmd == "run":
        stages = ORDER if a.stage == "all" else [s.strip() for s in a.stage.split(",")]
        bad = [s for s in stages if s not in ORDER]
        if bad:
            raise SystemExit(f"unknown stage {bad}; stages: {', '.join(ORDER)}")
        for s in stages:
            c = run_stage(ctx, s, _ids(a), force=a.force)
            print(f"== {s}: {json.dumps(c, sort_keys=True)}", flush=True)
        return 0
    if a.cmd == "export":
        run_stage(ctx, "export", _ids(a))
        return 0
    from .stages.export import status
    tab = {}
    for s in ORDER[:-1]:
        rows = ctx.store.rows(s)
        tab[s] = dict(Counter((r.get("decision") if r.get("complete") else f"pending:{(r.get('reasons') or ['?'])[0]}")
                              for i, r in rows.items() if not _ids(a) or i in _ids(a)))
    for s, c in tab.items():
        print(f"{s:12s} {json.dumps(c, sort_keys=True)}")
    print("final       ", json.dumps(dict(Counter(status(ctx, i)["decision"] for i in ctx.tasks)), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
