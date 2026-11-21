#!/usr/bin/env python3
"""Rebuild the full task rows of the paper's four training sets (Clean, Clean+FP, Orig, Orig+Filter) from the public
SWE-Gym dataset.

`training_sets/*.jsonl` hold what the audit added to each task (appended requirements, dropped and added tests,
labels, flags, difficulty) and the sha256 of every field taken from SWE-Gym. This script joins every row with SWE-Gym
(revision bb94ed9e39bbeb96a7fcbfb533b80f25a7fd59cb) by instance_id, rebuilds problem_statement, patch, test_patch,
hints_text, FAIL_TO_PASS and the graded PASS_TO_PASS, checks every sha256, and writes the full rows of each set to
<out>/data/<set>/train.parquet (train.jsonl without pyarrow); the column `training_set` names the set. The exit status
is 1 if a sha256 differs.

Run in `experiments/`:

    uv run --with datasets python rebuild_training_sets.py --hf                   # download with `datasets`
    uv run python rebuild_training_sets.py --swe-gym train-00000-of-00001.parquet # a local copy of SWE-Gym's parquet
    uv run --with datasets python rebuild_training_sets.py --hf --out /tmp/rebuilt # write elsewhere
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REVISION = "bb94ed9e39bbeb96a7fcbfb533b80f25a7fd59cb"
SETS = {"clean": "training_sets/clean.jsonl", "clean_fp": "training_sets/clean_fp.jsonl",  # Clean, Clean+FP
        "orig": "training_sets/orig.jsonl", "orig_filter": "training_sets/orig_filter.jsonl"}  # Orig, Orig+Filter
HELPERS = {"statement_appendix", "test_patch_override", "fp_test_patch_added", "sha256",
           "fp_test_patch_added_sha256_as_frozen"}


def sha(v):
    if not isinstance(v, str):
        v = json.dumps(v, ensure_ascii=False)
    return hashlib.sha256(v.encode()).hexdigest()


def jl(p):
    return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]


def as_list(v):
    return json.loads(v) if isinstance(v, str) else list(v)


def load_upstream(a):
    if a.hf:
        from datasets import load_dataset
        rows = load_dataset("SWE-Gym/SWE-Gym", revision=REVISION, split="train")
    else:
        import pyarrow.parquet as pq
        rows = pq.read_table(a.swe_gym).to_pylist()
    return {r["instance_id"]: dict(r) for r in rows}


def rebuild(row, u, training_set):
    po = u["problem_statement"]
    ps = po + (row.get("statement_appendix") or "")
    tp = row.get("test_patch_override") or u["test_patch"]
    if row.get("fp_test_patch_added"):
        tp += row["fp_test_patch_added"]
    dropped = row.get("p2p_dropped") or []
    graded = [t for t in as_list(u["PASS_TO_PASS"]) if t not in dropped] + list(row.get("fp_added_tests") or [])
    f2p = as_list(u["FAIL_TO_PASS"])
    got = {"problem_statement_original": sha(po), "problem_statement": sha(ps), "patch": sha(u["patch"]),
           "hints_text": sha(u["hints_text"]), "test_patch_upstream": sha(u["test_patch"]), "test_patch": sha(tp),
           "FAIL_TO_PASS": sha(f2p), "PASS_TO_PASS": sha(graded)}
    bad = sorted(k for k, v in (row.get("sha256") or {}).items() if got.get(k) != v)
    full = {k: v for k, v in row.items() if k not in HELPERS}
    full.update({"patch": u["patch"], "hints_text": u["hints_text"], "problem_statement_original": po,
                 "problem_statement": ps, "test_patch": tp, "FAIL_TO_PASS": f2p, "PASS_TO_PASS": graded,
                 "version": u["version"], "created_at": u["created_at"], "training_set": training_set})
    if isinstance(full.get("test_data_mounts"), (list, dict)):
        full["test_data_mounts"] = json.dumps(full["test_data_mounts"])
    return full, bad


def write(rows, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.Table.from_pylist(rows), path.with_suffix(".parquet"))
        return path.with_suffix(".parquet")
    except ImportError:
        p = path.with_suffix(".jsonl")
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        return p


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--swe-gym", type=Path, help="SWE-Gym train parquet at revision " + REVISION)
    g.add_argument("--hf", action="store_true", help="download SWE-Gym/SWE-Gym with the datasets library")
    ap.add_argument("--out", type=Path, default=ROOT)
    a = ap.parse_args()
    up = load_upstream(a)
    status = 0
    for name, rel in SETS.items():
        rows, bad_rows = [], []
        for row in jl(ROOT / rel):
            full, bad = rebuild(row, up[row["instance_id"]], name)
            rows.append(full)
            if bad:
                bad_rows.append((row["instance_id"], bad))
        p = write(rows, a.out / "data" / name / "train")
        print(f"{name}: {len(rows)} rows -> {p}; sha256 mismatches {len(bad_rows)}")
        for t, b in bad_rows[:10]:
            print(f"  MISMATCH {t}: {b}")
        status |= bool(bad_rows)
    return status


if __name__ == "__main__":
    sys.exit(main())
