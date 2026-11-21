# Third-party code and data

taskaudit itself is released under the Apache License 2.0. It contains no code copied from third-party projects; it depends on:

| dependency | used for | license | how |
|---|---|---|---|
| [SWE-bench harness](https://github.com/SWE-bench/SWE-bench) (`uv sync --extra swebench`) | eval scripts and per-repository log parsers of SWE-bench collections | MIT | dependency (`harness.backend: swebench`) |
| [SWE-Gym fork of the SWE-bench harness](https://github.com/SWE-Gym/SWE-Bench-Fork) @ 242429c1 | eval scripts, install specs and log parsers of SWE-Gym's repositories | MIT | dependency (`uv sync --extra swegym`; both provide the `swebench` package, install one) |
| tiktoken | token counts of the code-evidence packs (o200k_base) | MIT | dependency |
| PyYAML, pandas, pyarrow | config, parquet datasets | MIT / BSD-3-Clause / Apache-2.0 | dependencies |
| datasets | Hugging Face datasets | Apache-2.0 | installed with either harness extra; without it the parquet files are fetched over HTTPS |
| jsonschema | validating agent replies against the released schemas | MIT | dependency |
| Claude Code, Codex CLI | the default agent command lines | their own terms | external programs on PATH, configurable |

In `taskaudit/`, `harness/_guards.py`, `packs/build_pack.py`, `gates/g1.py`, `gates/stats.py`, `data/prep.sh`,
`data/prep_copies.py` and `stages/fp_compare.py` are the paper's own code (Apache-2.0). The PAIChecker router prompt
(`data/prompts/2_pai_router.txt`) follows the label taxonomy of PAIChecker (MIT); its human annotations are not
included (`dataset.human_labels`).

Task data (statements, repositories, tests, images) keeps the licenses of its sources; SWE-Gym is MIT-licensed.

## Experiments

- `experiments/configs/sweagent/tools/edit_anthropic` is the editor tool bundle of SWE-agent
  ([SWE-agent](https://github.com/SWE-agent/SWE-agent), MIT; its license is in that folder) with `undo_edit` removed and the edit history
  written to its state file explicitly.
- `experiments/configs/eval_splits.json` names tasks of SWE-bench Verified (MIT) and of the public set of SWE-bench Pro
  (its own license) by id only.
- `experiments/training_sets/` holds what the audit added to each task. Issue text, gold patches and tests are not
  copied; `experiments/rebuild_training_sets.py` fetches them from SWE-Gym (MIT).
