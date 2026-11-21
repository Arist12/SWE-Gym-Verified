# Experiments

The files behind the paper's RL experiments: the four training sets (512 tasks each), how they were drawn, the
training and evaluation settings, and the outcome of every held-out attempt. Training code and model weights are not
released.

The [dataset](https://huggingface.co/datasets/Arist12/SWE-Gym-Verified) alone is enough to rebuild all four training
sets (its data card, "The paper's training sets"). This folder adds the sets as files with a sha256 of every field,
the sampling frames and samplers that show how the sets were drawn, the configurations, and the per-attempt results.

| path | contents |
|---|---|
| `training_sets/` | `clean.jsonl` (Clean), `clean_fp.jsonl` (Clean+FP, with the new tests), `orig.jsonl` (Orig), `orig_filter.jsonl` (Orig+Filter) |
| `training_sets/sampling/` | the sampling frames (`clean_frame.jsonl`, `orig_frame.jsonl`), the samplers that redraw the sets from them, and `statement_quality_prompt.txt`, the prompt of the statement-quality filter that builds Orig's sampling frame (gpt-5.6-sol) |
| `rebuild_training_sets.py` | rebuilds the full rows of each training set from public SWE-Gym and checks every sha256 |
| `configs/` | the GRPO hyperparameters, the SWE-agent configs of the training rollouts and of both evaluations (with the editor tool bundle), and the evaluation task lists; see [`configs/README.md`](configs/README.md) |
| `results/heldout_attempts.jsonl` | every held-out attempt of every run |

## Redraw and rebuild the training sets

Run in this folder. The samplers need only the Python standard library; each exits with status 0 if its draw equals
the released set.

```bash
uv run python training_sets/sampling/sample_clean.py > /dev/null     # Clean (and Clean+FP)
uv run python training_sets/sampling/sample_orig_filter.py --orig    # Orig
uv run python training_sets/sampling/sample_orig_filter.py           # Orig+Filter

# full rows -> data/<set>/train.parquet; reports sha256 mismatches (0)
uv run --with datasets --with pyarrow python rebuild_training_sets.py --hf
```

`rebuild_training_sets.py` joins every row with SWE-Gym by `instance_id` and writes rows in SWE-Gym's format
(`problem_statement`, `patch`, `test_patch`, `FAIL_TO_PASS`, the graded `PASS_TO_PASS`, ...). `--swe-gym FILE` reads a
local copy of SWE-Gym's train parquet instead of downloading it, and `--out DIR` writes elsewhere.

## Rows of the training sets

The rows of `training_sets/*.jsonl` use the column names of the dataset's `tasks` table (`statement_repaired`,
`statement_appendix`, `p2p_dropped`, `test_patch_override`, `test_data_mounts`, `sha256`, and `flags` in Clean and
Clean+FP; see
[FIELDS.md](https://huggingface.co/datasets/Arist12/SWE-Gym-Verified/blob/main/FIELDS.md)), with these differences:

- `audit_decision` is the dataset's `decision` (`kept`, `repaired` or `removed`).
- `base_resolved_of_8` is the difficulty on the statement and tests that the set trains on: the final ones in Clean and
  Clean+FP (measured without the new tests of Clean+FP), the original ones in Orig and Orig+Filter (the dataset's `base_resolved_of_8_original`) and in
  `sampling/orig_frame.jsonl`.
- `clean_fp.jsonl` carries the new tests: `fp_hardened`, `fp_test_file`, `fp_added_tests` and `fp_test_patch_added`
  (the dataset's `fp_tests` table: `test_file`, `tests`, `test_patch`). `fp_test_patch_added_sha256_as_frozen` is the
  sha256 of that diff as used in training; the released diff differs from it only in names (a prefix in the helper
  code, and the names of the test file and class).

## Held-out results

`results/heldout_attempts.jsonl` has one row per attempt (76,032 rows):

| column | meaning |
|---|---|
| `run` | `base` (the base model), or the training set and seed, for example `clean_fp_s2` |
| `benchmark` | `dev100` (dev-100), `v400` (Verified-397) or `pro` (SWE-bench Pro) |
| `step` | the checkpoint: 0 for the base model; 10, 20, ..., 80 on dev-100; the selected checkpoint on the other two |
| `task_id`, `attempt`, `resolved` | the task, the attempt number, and whether the attempt resolved the task |
| `language` | the task's language on SWE-bench Pro (`python`, `go`, `js`, `ts`); empty elsewhere |

`attempt` is empty where only each task's pass count was archived (27,128 rows: some dev-100 checkpoints, the
Verified-397 evaluations of two Clean+FP runs and one Orig run, and every dev-100 and Verified-397 row of
Orig+Filter). Such rows spell the pass count out as resolved and unresolved attempts, so pass@1 is unaffected.

The paper's held-out table follows from this file:

```python
import pandas as pd

rows = pd.read_json("results/heldout_attempts.jsonl", lines=True)
held = rows[rows.benchmark != "dev100"]
pass1 = held.groupby(["run", "benchmark"]).resolved.mean().mul(100).reset_index()
pass1["set"] = pass1.run.str.replace(r"_s\d$", "", regex=True)
print(pass1.groupby(["set", "benchmark"]).resolved.agg(["mean", "std"]).round(1).unstack())
# clean: 62.6 ± 1.1 on v400 and 56.2 ± 1.3 on pro; orig: 60.2 ± 2.1 and 53.5 ± 2.1
```
