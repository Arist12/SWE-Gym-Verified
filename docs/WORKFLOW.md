# taskaudit: the audit workflow

An execution-backed audit of the hidden tests of SWE-bench-format coding tasks, as used to build
[SWE-Gym-Verified](https://huggingface.co/datasets/Arist12/SWE-Gym-Verified).
Hidden tests are reward labels; they can reject correct fixes (**false negatives**) and accept wrong ones (**false
positives**). taskaudit finds both, and no error verdict rests on judgment alone:

- **FN**: two LLM families (in the paper: Claude Opus 5.5 and GPT-6.1 Sol, each with its coding agent) rate every
  FAIL_TO_PASS assertion against the statement. Every flagged assertion goes to
  coding agents of both families, which write and *execute* a witness in the task container (a patch that follows
  the statement but differs from the gold patch in the flagged detail). Where the two ratings disagree or are unsure,
  the families also review each other's executed patches and debate once. A task with an FN verdict is repaired (a
  short *Requirements* block appended to the statement, checked for traceability and leaks, then audited again) or
  removed.
- **FP**: both families label passing patches without seeing the gold patch or the tests. A patch counts as a false
  positive only if both label it *incorrect* and an executed counterexample confirms it (same output on the original
  code and the gold patch, different on the candidate). Confirmed counterexamples become new PASS_TO_PASS tests, kept
  only if they pass the gold patch and every patch not labelled a false positive.

It runs on SWE-bench-format Python tasks with a Docker image per task. It has been run on SWE-Gym; other collections
in this format (SWE-bench and its Python variants, or your own) are supported by configuration and are untested.
[`FIDELITY.md`](FIDELITY.md) lists, stage by stage, what is identical to the paper's runs.

Paths and commands on this page are relative to the repository root.

## Quickstart: two SWE-Gym tasks

Requirements: Linux, Docker, git, [uv](https://docs.astral.sh/uv/), ~8 GB of disk per task image, network for the
dataset and the images.

```bash
# the SWE-Gym fork of the SWE-bench harness provides eval scripts and parsers
uv sync --extra swegym
# records, caches, logs, packs, exports
export TASKAUDIT_WORK=$PWD/work
IDS=pydantic__pydantic-8650,facebookresearch__hydra-2062

# 1. pre-screen and environment: gold and empty patch, 3 offline runs each
#    (pulls the two task images)
uv run taskaudit run --config configs/swegym.yaml --stage prescreen,environment --ids $IDS

# 2. statement screens and FN rating: llm.dry_run is true in the shipped config, so no call is
#    made; the screens' requests are written to $TASKAUDIT_WORK/llm/requests/<step id>/
#    (1a, 1b, ...; see "Names") and the tasks stay "pending"; the FN rating writes its requests
#    once the screens have replies
uv run taskaudit run --config configs/swegym.yaml --stage screen,fn_rate --ids $IDS
uv run taskaudit status --config configs/swegym.yaml --ids $IDS

# 3. real calls: copy the config, set llm.dry_run: false, export the keys it names,
#    run every stage
cp configs/swegym.yaml my.yaml && sed -i 's/dry_run: true/dry_run: false/' my.yaml
# or any compatible endpoint: *_BASE_URL in the config
export ANTHROPIC_API_KEY=... OPENAI_API_KEY=...
# resumable: re-running skips finished work
uv run taskaudit run --config my.yaml --stage all --ids $IDS
# writes $TASKAUDIT_WORK/export/: tasks.jsonl, audit_records.jsonl, ...
uv run taskaudit export --config my.yaml --ids $IDS
```

The escalation, the triage read and (optionally) the blind solve run coding-agent CLIs (`claude`, `codex` by default;
see "Agents") and need them on `PATH`. `taskaudit prompts --config ...` shows which prompt file each step reads and
whether it matches `SHA256SUMS`.

## Stages

`taskaudit run --stage <name>[,<name>...]|all`. Each stage writes `$TASKAUDIT_WORK/stages/main/<stage>.jsonl`, one row per task
and run; the last row of a task is its record. A row is skipped on re-run when it is `complete` and its `key` (the
sha256 of the stage's inputs: prompt hashes, the task view, settings) is unchanged, so every stage is resumable and
idempotent. Every row has `instance_id, stage, key, complete, decision, reasons, utc`; `decision: drop` removes the
task (its `reasons` are the removal codes) and later stages skip it. `flags` are notes that never remove a task.

| stage | what it does | decision | main fields |
|---|---|---|---|
| `prescreen` | unusable records, title-only statements, missing tests, duplicates | keep / drop | `duplicate_of` |
| `environment` | gold and empty patch x3 offline after PREP (single-commit history at the base, the project's copies removed); flaky F2P, gold failures, P2P drops | keep / drop | `runs`, `p2p_dropped` {id: why}, `history` |
| `screen` | statement hygiene; defective pull request (human labels, else the PAIChecker router); misleading statement; solution in issue (a flag) | keep / drop | `hygiene`, `pai`, `misleading`, `solution_in_issue`, `IS_route` |
| `fn_rate` | FN rating: both families rate every F2P assertion (statement, discussion, tests, gold, code-evidence pack) | OK / FN_BOTH / ESCALATE | `fn` {family: rating}, `families` (replies), `pack` |
| `escalate` | witness, execution, review, debate (agents in the task container): witness mode for FN_BOTH, full escalation for ESCALATE | OK / FN_CONFIRMED / CONTESTED / drop | `audit_path`, `fn_evidence`, `mode`, `escalation` (the record) |
| `p2p` | P2P judge and gold variant: does a P2P test that a candidate fails pin old behaviour? (`stages.p2p.enabled`) | keep / flag | `items`, `cases`, `p2p_dropped` |
| `measure` | difficulty: how many samples of a policy model resolve the task (`stages.measure.enabled`) | measured | `resolved`, `samples`, `band`, `rollouts` |
| `triage` | defect record, mechanical triage rules, two-family triage read | repair / drop / no_defect | `record`, `items`, `plan` |
| `repair` | repair writer, then the repair checks: mechanical check (G1), traceability (G2), leak judges (G3), optionally the blind solve; for a test repair, the relaxation checks | repaired / drop | `block`, `G1`-`G3`, `gates`, `failed`, `overlay` |
| `reaudit` | on the repaired task: misleading-statement screen, FN rating and escalation again, base-model check (G4), jump read (G5) | repaired / drop | `checks` |
| `fp` | FP judge by both families, executed counterexamples, new tests with three checks, construction / analysis split | keep / flag | `patches`, `candidate_tests`, `test_file`, `tests`, `test_patch` |
| `export` | writes the tables below | | |

Each stage module's docstring (`taskaudit/stages/*.py`) states its rules and row fields in full; the rules as prose
are in `taskaudit/data/rules/`. Stages that need an LLM or an agent stay `pending` (complete false) in a dry run and
are retried by the next run; replies are cached by request hash under `$TASKAUDIT_WORK/llm/cache`, so a step whose
replies could not be parsed is asked again only with `llm.cache: false` or after its cached replies are removed.

## Outputs

`taskaudit export` writes `$TASKAUDIT_WORK/export/` (or `--out DIR`). Files, fields and values follow the
[data card](https://huggingface.co/datasets/Arist12/SWE-Gym-Verified) of SWE-Gym-Verified wherever they describe the
same thing.

| file | rows | fields |
|---|---|---|
| `tasks.jsonl` | the released tasks (kept or repaired), as full SWE-bench rows | `problem_statement` (final: the issue followed by `statement_appendix`), `problem_statement_original`, `statement_repaired`, `statement_appendix`, `patch`, `test_patch` (final; `test_patch_relaxed`), `test_patch_with_fp_tests`, `FAIL_TO_PASS`, `PASS_TO_PASS` (graded), `p2p_dropped`, `difficulty` {`resolved`, `samples`, `band`}, `fn_verdict`, `fn_evidence`, `flags`, `decision`, `has_false_positive` |
| `audit_records.jsonl` | every audited task | `decision` (`kept` / `repaired` / `removed`; `pending` while a stage is unfinished, with `pending_stage`), `removal_stage`, `removal_reasons`, `rating_claude`, `rating_gpt`, `audit_path`, `fn_verdict`, `fn_evidence`, `flags`, `difficulty`, and under each stage's name the main fields of its row |
| `witness_patches.jsonl` | the executed escalation patches | `family`, `role` (`witness` / `alternative`), `patch`, `applied`, `f2p_failed_attributable`, `p2p_failed_new`, `resolved` |
| `fp_labels.jsonl` | the labelled passing patches | `patch_sha256`, `patch`, `n_samples`, `label` (`false_positive` / `correct` / `undetermined`), `label_claude`, `label_gpt`, `summary_claude`, `summary_gpt`, `counterexamples`, `n_counterexamples_confirmed` |
| `fp_tests.jsonl` | the tasks with candidate tests | `test_file`, `tests`, `test_patch` (the added file with the kept tests), `candidate_tests` (each with `check1`, `check2`, `check3`, `kept`) |
| `removals.json` | | removal counts by stage and reason, task counts by decision |

- `fn_verdict`: `no_fn`, `fn_confirmed`, `fn_contested` (the `escalate` decisions OK, FN_CONFIRMED, CONTESTED), or
  `not_reached` for a task removed before a verdict.
- `fn_evidence`: `cross_reviewed` (confirmed after full escalation), `two_witnesses` (witness mode, both witnesses
  fail a common F2P test), `other_confirmed`, `contested`.
- `audit_path`: `cleared_by_rating`, `witness_mode`, `full_escalation`, `residual_audit`.
- `flags`: see "Names".

What differs from the released dataset: `tasks.jsonl` holds full rows, not only what the audit added; difficulty is
`difficulty` (the dataset's `base_resolved_of_8` is `difficulty.resolved` with 8 samples); `removal_stage` is the
workflow stage that removed the task and `removal_reasons` are its reason codes (the dataset groups the stages and
words the reason); `audit_records.jsonl` carries the stage rows' fields instead of the dataset's `assertions`,
`family_verdicts` and `executed_patches`; `witness_patches.jsonl` carries each patch's execution outcome, which the dataset keeps in `executed_patches` of
`audit_records`, and has no `bytes` or `sha256`; `fp_labels.jsonl` has no `defects` or `rejected_by_fp_tests`;
`fp_tests.jsonl` also holds what the dataset puts in `fp_test_construction`. The set membership columns (`in_clean`, ...) come from the paper's sampling, not from this
workflow.

## Names

Records and prose use words. A few short ids stay, because the prompts use them and prompt files, request tags and
record keys are named by them:

**Steps.** A step is one prompt; its id is the prefix of its prompt file. An LLM request is tagged with its step's
id, or with the tag given in parentheses below.

| id | step | stage | step name (`taskaudit prompts`, `prompts.files`) |
|---|---|---|---|
| 1a | statement hygiene | screen | `hygiene` |
| 1b | solution in issue | screen | `solution_in_issue` |
| 1d | misleading statement | screen, reaudit | `misleading` |
| 2 | PAIChecker router (tag `pai`) | screen | `pai_router` |
| 3a | FN rating | fn_rate, reaudit | `fn_rater` |
| 3b, 3c, 3d | witness, review, debate (agents) | escalate, reaudit | `witness`, `review`, `debate` |
| 4a, 4b | P2P judge, gold variant | p2p | `p2p_judge`, `p2p_gold_variant` |
| 5a | repair writer | repair | `spec_writer` |
| 5b | traceability judge of check G2 (tag `G2`) | repair | `g2_traceability` |
| 5c | leak judge of check G3 (tag `G3`) | repair | `g3_leak` |
| 5d | flip judge (test repair) | repair | `flip_judge` |
| 5f | FP judge | fp | `fp_judge` |
| 5g | leak re-read after a jump in the pass count, check G5 (tag `g5`) | reaudit | `g5_jump_read` |
| 6a | blind solve (agent; optional part of check G4) | repair | `blind_solver` |
| 7a | triage read (agent) | triage | `triage_reader` |

The re-audit tags its runs of 1d and 3a `rr-1d` and `rr-3a`.

**Repair checks** (the prompts call them gates, hence G). G1-G4 are the four checks of the paper; G5 is a further
leak read in the re-audit.

| id | check |
|---|---|
| G1 | mechanical check: deterministic form and token rules, no paths, private names or test words, quotes verbatim (`taskaudit/gates/g1.py`, `taskaudit/data/rules/G1_rules.txt`) |
| G2 | traceability: both families map every requirement to a flagged assertion |
| G3 | leak judges: two judges, a flag from either fails the repair |
| G4 | base-model check, in the re-audit: a violation that the policy model's samples repeat is escalated (needs a difficulty measurement); `stages.repair.blind_solve: true` adds a blind solve of the repaired statement |
| G5 | jump read, in the re-audit: both leak judges read the repair again when the pass count rises from 0 to at least 6 of 8 |

**Escalation record** (`escalation` in the `escalate` row). Its keys, which are also the agents' work directories
(`$TASKAUDIT_WORK/work/main/escalate/<task>/<family>/<W|R|D>/`): `G` gold check, `W` witness round, `X` execution of
the patches, `R` review, `D` debate. `T` is the triage read and `B` the blind solve (`agents.timeouts_s`). The
prompts name the two families' patches `W-A` (claude) and `W-B` (gpt).

**Triage rules and repair routes.** The rule ids `P2` and `H1`-`H9` and the routes `S-a`, `S-lit`, `S-keep`,
`S-scope`, `IS`, `T-b`, `P2P-relax` are defined in the triage rules quoted in
`taskaudit/data/prompts/7a_triage_reader.md` (sections 3-5) and summarised in `taskaudit/data/rules/RULES.md`. A
triage reason code starts with its rule id (`H2_PRIVATE_OR_INTERNAL`); `READ:` marks a decision of the triage read.

**Flags** (lower case; the first eight are the data card's).

| flag | stage | meaning |
|---|---|---|
| `solution_in_issue` | screen | both families: the issue contains the full solution |
| `f2p_unrelated`, `f2p_partly_unrelated` | screen | both families: the F2P tests are unrelated, or partly unrelated, to the issue |
| `statement_misleading_claude`, `statement_misleading_gpt` | screen | one family rates the statement misleading relative to the tests |
| `statement_misleading_both` | screen | both do, each with a valid discussion quote: the task goes to repair |
| `repair_untraced_content` | repair | a traceability judge noted content beyond the flagged assertions (the repair passed the check) |
| `gold_patch_applies_with_fuzz` | environment | the gold patch applies to the base commit only with fuzz |
| `some_f2p_pass_without_patch` | environment | some (not all) F2P tests pass without any patch |
| `many_p2p_dropped` | environment | at least 10 and more than half of the P2P tests are left out of grading |
| `image_not_checked` | prescreen | the image was not looked up (`stages.prescreen.check_image`) |
| `defective_pr_not_screened` | screen | no human label and no PAIChecker record (or the router is off): the defective-PR screen did not run |

**Other terms.** *Family*: `claude` or `gpt` (`llm.families`). *Specification*: the *Requirements* block that a repair
appends (`block` and `spec_block` in the stage rows, `statement_appendix` in the export and the dataset). *Band*: the
difficulty written as resolved/samples, for example `3/8`. *Policy model*: the model whose samples measure difficulty
(the paper's base model; the prompts call it the student).

## Configuration

A YAML file over the defaults in `taskaudit/config.py` (`configs/swegym.yaml`: the paper's setting;
`configs/example.yaml`: a generic collection). `${VAR}` and `${VAR:-default}` read the environment. No key is ever
written to a config: a model entry names the environment variable of its key (`api_key_env`).

- `llm.families.claude` / `llm.families.gpt`: the two judge families (`provider: anthropic | openai`, `model`,
  `base_url`, `api_key_env`, optional `headers` for a gateway that needs its own; `openai` is any OpenAI-compatible
  endpoint). `llm.roles`: the repair writer, the two leak judges (the paper: Claude Opus 5.5 and GPT-5.6 Sol), the
  hygiene and router models. `llm.dry_run`, `llm.cache`, `llm.retries`, `llm.concurrency`.
- `harness`: `backend` (`swegym_fork`, `swebench`, `script`, `auto`), `timeout_s`, `network` (none), `cpus`, `memory`,
  `pids_limit`, `cpuset`, `container_prefix`, `pull`, `remove_pulled_images`.
- `prompts.dir`: another prompt directory (same file names, its own `SHA256SUMS`); `prompts.files.<step name>`: one
  file; `prompts.check: strict` stops when a prompt differs from `SHA256SUMS`.
- `stages.<stage>`: per-stage settings (see each module).

## Other collections

Records need the SWE-bench fields `instance_id, repo, base_commit, problem_statement, patch, test_patch,
FAIL_TO_PASS, PASS_TO_PASS` (+ `hints_text`, `version`, `created_at`) and an image: an `image` field, or
`dataset.image_template` (placeholders `{instance_id} {iid_lower} {iid_swegym} {iid_swebench} {repo} {version} {repo_slug} {id_short}`).
`dataset.field_map` renames fields (e.g. `{docker_image: image}`); `dataset.path` reads `.jsonl`, `.json` or
`.parquet`, `dataset.hf_name` a Hugging Face dataset.

- **SWE-bench / SWE-bench Verified and other Python sets that the public harness knows**: `uv sync --extra swebench`
  (instead of `--extra swegym`), `adapter: swebench`, `harness.backend: swebench`; the public harness builds the
  eval script and parses the logs. Tasks in other languages are not supported: the code pack, the grader guards and
  the counterexample runner are written for Python and pytest.
- **Collections with their own runner** (for example R2E-Gym or SWE-smith, once their records are converted to the
  fields above; their public records lack some of them): `harness.backend: script` and, per record,
  `test_cmd` (the command that runs the graded tests in `/testbed`, e.g. `pytest -rA tests/test_x.py`), optional
  `install_cmd`, `env_setup` (a list of shell lines that replaces the default conda activation) and `log_parser` (a
  SWE-bench parser name, or `pytest`). The grader guards wrap this script exactly as they wrap SWE-bench's.
- Repositories that are not on GitHub: put bare clones `<owner>__<name>.git` into `dataset.repos_dir`.
- Test data that a task reads and its image lacks: a record's `test_data_mounts` lists it, either as
  `[host path, container path]` pairs or as the entries of SWE-Gym-Verified (`container_path`, sizes, sha256), which
  are mounted read-only from `<harness.testdata_dir>/<instance_id>/<container path>`.

## Agents

`agents.claude` / `agents.gpt` configure the coding-agent CLIs of the two families: `command` (a list; placeholders
`{model} {schema_path} {schema_json} {final_path} {workdir} {settings_json}`), `model`, `output` (`claude_stream`,
`codex_events`, `stdout_json`, `file`). The defaults are the Claude Code and Codex CLI command lines of the paper's
runs; `agents.timeouts_s` is keyed by the agent step (`W`, `R`, `D`, `T`, `B`; see "Names"). The prompt goes to stdin.
Every command the agent runs must be `tbx '<bash command>'`: `tbx` (on the agent's PATH) runs it in a fresh
`--network none` container of the task's environment image, with `/in` (read-only inputs) and `/out` (the agent's
scratch). Runs with a command outside `tbx` that executed, or a reference to this tool's records, are void and re-run
once. The reply must validate against the step's released JSON schema. For the optional blind solve
(`stages.repair.blind_solve: true`), `blind_command` gets an MCP server (`taskaudit/data/box_mcp.py`) whose only tool, `shell`,
runs in the attempt's container.

## Difficulty

`stages.measure` (optional) grades `samples` (8) patches of a policy model per task: either precomputed rollouts
(`rollouts: <jsonl of {instance_id, patch, statement_sha256?}>`, e.g. from SWE-agent on the exported statements; every
row of a task is graded) or a
`policy.command` that writes one patch per call (`{statement_file} {image} {out_patch} ...`). The row holds `resolved`,
`samples` and the `band` (`3/8`). With a measurement, the triage removes the tasks with an FN
verdict that the policy model always solves (no repair is attempted), the re-audit runs the base-model check and the
jump read, and the FP stage labels the passing samples.

## License

Apache-2.0. Task data keeps the licenses of its sources; see [`THIRD_PARTY.md`](THIRD_PARTY.md) for the code this package
depends on or contains.
