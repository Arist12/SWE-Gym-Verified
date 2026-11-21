#!/usr/bin/env python3
"""Draw Clean from its sampling frame: a band-stratified random sample of the tasks whose audit had completed at
sampling time.

Run in `experiments/`:

    uv run python training_sets/sampling/sample_clean.py                # prints the 512 ids
    uv run python training_sets/sampling/sample_clean.py --out ids.txt  # writes them to a file

The exit status is 1 unless the ids equal those of training_sets/clean.jsonl.

Frame (`clean_frame.jsonl`, rows with in_frame true): the tasks kept or repaired by the audit with a base band of 0/8 to
7/8 (8/8 tasks are never sampled). Rank key: sha256("20261014:<instance_id>"), ascending (a fixed pseudo-random order;
Orig uses the same key). Quotas (those of Orig): 0/8 = 77; 1-2/8 = 101; 3-5/8 = 145; 6-7/8 = 189. Each group takes its
first quota tasks in rank order. A short 1-7/8 group is filled with the next-ranked tasks of another 1-7/8 group:
1-2/8 from 3-5/8, then 6-7/8; 6-7/8 from 3-5/8, then 1-2/8; 3-5/8 from both neighbours in equal halves (the odd one
from 6-7/8). 0/8 neither gives nor receives.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20261014
ZERO_Q = 77
SUB = {"1-2/8": ((1, 2), 101), "3-5/8": ((3, 4, 5), 145), "6-7/8": ((6, 7), 189)}
FILL_FROM = {"1-2/8": ["3-5/8", "6-7/8"], "6-7/8": ["3-5/8", "1-2/8"], "3-5/8": ["1-2/8", "6-7/8"]}


def rank_key(iid: str) -> str:
    return hashlib.sha256(f"{SEED}:{iid}".encode()).hexdigest()


def fill_plan(g: str, need: int, spare: dict[str, list]) -> list[tuple[str, int]]:
    if g == "3-5/8":
        want = {"1-2/8": need // 2, "6-7/8": need - need // 2}
        for a, b in (("1-2/8", "6-7/8"), ("6-7/8", "1-2/8")):
            lack = max(0, want[a] - len(spare[a]))
            want[a] -= lack
            want[b] += lack
        return [(d, want[d]) for d in FILL_FROM[g]]
    plan, rest = [], need
    for d in FILL_FROM[g]:
        k = min(rest, len(spare[d]))
        plan.append((d, k))
        rest -= k
    return plan


def sample(frame: dict[str, int]) -> tuple[list[str], list[dict]]:
    """frame: instance_id -> base resolved of 8 (0-7). Returns (picked ids, fills)."""
    zero = sorted((t for t, r in frame.items() if r == 0), key=rank_key)
    if len(zero) < ZERO_Q:
        raise SystemExit(f"0/8 short: {len(zero)} < {ZERO_Q}")
    picked = zero[:ZERO_Q]
    pools = {g: sorted((t for t, r in frame.items() if r in bands), key=rank_key) for g, (bands, _) in SUB.items()}
    own = {g: pools[g][:q] for g, (_, q) in SUB.items()}
    spare = {g: pools[g][q:] for g, (_, q) in SUB.items()}
    fills = []
    for g, (_, q) in SUB.items():
        need = q - len(own[g])
        if need <= 0:
            continue
        for d, k in fill_plan(g, need, spare):
            k = min(k, len(spare[d]))
            if k > 0:
                took, spare[d] = spare[d][:k], spare[d][k:]
                fills.append({"to": g, "from": d, "n": k, "tasks": took})
    for g in SUB:
        picked += own[g] + [t for f in fills if f["to"] == g for t in f["tasks"]]
    return sorted(picked), fills


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", type=Path, default=HERE / "clean_frame.jsonl")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    rows = [json.loads(x) for x in a.frame.read_text().splitlines() if x.strip()]
    frame = {r["instance_id"]: r["base_resolved_of_8"] for r in rows if r["in_frame"]}
    picked, fills = sample(frame)
    print(f"frame {len(frame)}; picked {len(picked)}; fills " + ", ".join(f"{f['n']} {f['from']} -> {f['to']}"
                                                                         for f in fills), file=sys.stderr)
    if a.out:
        a.out.write_text("\n".join(picked) + "\n")
    else:
        print("\n".join(picked))
    data = HERE.parent / "clean.jsonl"
    if data.exists():
        same = sorted(json.loads(x)["instance_id"] for x in data.read_text().splitlines() if x.strip()) == picked
        print("equals training_sets/clean.jsonl: " + ("yes" if same else "NO"), file=sys.stderr)
        return 0 if same else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
