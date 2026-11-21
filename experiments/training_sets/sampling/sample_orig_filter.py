#!/usr/bin/env python3
"""Redraw Orig+Filter (default) or Orig (--orig) from orig_frame.jsonl, with the Python standard library only.

Run in `experiments/`:

    uv run python training_sets/sampling/sample_orig_filter.py           # Orig+Filter
    uv run python training_sets/sampling/sample_orig_filter.py --orig    # Orig

The exit status is 1 unless the draw equals training_sets/orig_filter.jsonl (with --orig: training_sets/orig.jsonl).

Frame (`in_frame: true`): every SWE-Gym task with a complete base-model measurement (eight samples on the original
statement and tests) that solves 0 to 7 of the 8 (`base_resolved_of_8`; the band k/8), without status filtering,
minus one task whose fix is recoverable from the visible git history (`in_frame: false`). Tasks solved 8 of 8 carry
no GRPO signal and are not in the frame.

Sizes: N_ZERO tasks at 0/8 (Orig 77, Orig+Filter none), and the other 512 - N_ZERO split over the bands 1/8..7/8 in
proportion to the measured counts of every complete task, the excluded one included (largest remainder; ties go to
the lower band). Inside each band, tasks are ranked by sha256("20261014:<instance_id>") (hex, ascending; the
`rank_key` column) and the first ones are taken; a task's rank does not depend on the rest of the frame.
"""
import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20261014
N = 512
N_ZERO = {"orig_filter": 0, "orig": 77}
BANDS = list(range(1, 8))  # base_resolved_of_8 of the bands 1/8..7/8


def quotas(counts, n):
    exact = {b: n * counts[b] / sum(counts[x] for x in BANDS) for b in BANDS}
    q = {b: int(exact[b]) for b in BANDS}
    for b in sorted(BANDS, key=lambda b: (q[b] - exact[b], BANDS.index(b)))[:n - sum(q.values())]:
        q[b] += 1
    return q


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orig", action="store_true", help="draw Orig (77 tasks at 0/8) instead of Orig+Filter")
    args = ap.parse_args()
    name = "orig" if args.orig else "orig_filter"
    rows = [json.loads(x) for x in (HERE / "orig_frame.jsonl").read_text().splitlines() if x.strip()]
    for r in rows:
        assert r["rank_key"] == hashlib.sha256(f"{SEED}:{r['instance_id']}".encode()).hexdigest(), r["instance_id"]
    frame = [r for r in rows if r["in_frame"]]
    q = {0: N_ZERO[name], **quotas(Counter(r["base_resolved_of_8"] for r in rows), N - N_ZERO[name])}
    picked = []
    for band, k in q.items():
        ranked = sorted((r for r in frame if r["base_resolved_of_8"] == band), key=lambda r: r["rank_key"])
        assert len(ranked) >= k, band
        picked += [r["instance_id"] for r in ranked[:k]]
    picked.sort()
    print(json.dumps({"set": name, "frame": len(frame), "per_band": {f"{b}/8": k for b, k in q.items()},
                      "n": len(picked)}))
    data = HERE.parent / f"{name}.jsonl"
    if data.exists():
        same = sorted(json.loads(x)["instance_id"] for x in data.read_text().splitlines() if x.strip()) == picked
        print(f"equals training_sets/{name}.jsonl: " + ("yes" if same else "NO"), file=sys.stderr)
        return 0 if same else 1
    print("\n".join(picked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
