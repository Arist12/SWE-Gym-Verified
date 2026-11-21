# Prompts

The final prompt of every step of the audit. `prompts.json` gives each file's step and model families; `SHA256SUMS` lists the sha256 of every file, and the workflow checks it on every read. A file name starts with its step's id; the workflow guide (`docs/WORKFLOW.md`, "Names") lists the ids with their stages and the repair checks G1-G5 (called gates here).

| file | step | models |
|---|---|---|
| `00_shared_definitions.md` | shared definition blocks (byte-identical inside every prompt that uses them) |  |
| `1a_statement_hygiene.txt` | step 1a: statement hygiene (images, links, title-only) | claude-opus-5.5 |
| `1b_solution_in_issue.txt` | step 1b: the full solution is in the issue; F2P groups (labels only) | claude-opus-5.5, gpt-6.1-sol |
| `1d_misleading_statement.txt` | step 1d: the statement misleads the solver (both families -> removed) | claude-opus-5.5, gpt-6.1-sol |
| `2_pai_router.txt` | step 2: problem/test misalignment router for tasks without a human label | claude-opus-5.5 |
| `3a_fn_rater.txt` | step 3a: per-assertion FN rater (two families; decision rule in taskaudit/stages/fn_rate.py) | claude-opus-5.5, gpt-6.1-sol |
| `3b_witness.md` | step 3b: witness - write and execute an alternative patch (agent) | claude-opus-5.5, gpt-6.1-sol |
| `3b_witness.schema.json` | step 3b reply schema |  |
| `3c_review.md` | step 3c: review of the executed witnesses (agent) | claude-opus-5.5, gpt-6.1-sol |
| `3c_review.schema.json` | step 3c reply schema |  |
| `3d_debate.md` | step 3d: one debate round on disagreement (agent) | claude-opus-5.5, gpt-6.1-sol |
| `3d_debate.schema.json` | step 3d reply schema |  |
| `4a_p2p_judge.txt` | step 4a: PASS_TO_PASS audit (keep / relax / flaky drop / delete) | claude-opus-5.5, gpt-6.1-sol |
| `4b_p2p_gold_variant.txt` | step 4b: write a gold variant to test whether a P2P test pins old behaviour | claude-opus-5.5, gpt-6.1-sol |
| `5a_spec_writer.txt` | step 5a: statement repair - write the appended specification | claude-opus-5.5 |
| `5b_g2_traceability.txt` | gate G2: every requirement traces to a tested assertion | claude-opus-5.5, gpt-6.1-sol |
| `5c_g3_leak_judge.txt` | gate G3: the specification does not leak the solution | claude-opus-5.5, gpt-5.6-sol |
| `5d_flip_judge.txt` | flip judge: strict gate for test-side changes (not called in the reported pipeline: the one test relaxation had no flip to judge) | claude-opus-5.5, gpt-6.1-sol |
| `5f_fp_judge.txt` | Clean+FP: three-way FP judge of a passing patch (no gold, no tests) | claude-opus-5.5, gpt-6.1-sol |
| `5g_g5_jump_read.txt` | gate G5: leak re-read of a repaired task whose base-model pass count rose from 0/8 to at least 6/8 (the prompt states this jump; both flag -> removed) | claude-opus-5.5, gpt-5.6-sol |
| `6a_blind_solver.md` | gate G4, blind-solve part: Claude Code solves the repaired statement (agent); the base-model part of G4 escalates with 3c / 3d (harness texts in the workflow's escalation stage) | claude-opus-5.5 |
| `6a_blind_solver.schema.json` | gate G4 reply schema |  |
| `7a_triage_reader.md` | FN triage reader (agent): repair route or delete; the read card the pipeline builds starts with the task's base-model band | claude-opus-5.5, gpt-6.1-sol |
| `7a_triage_reader.schema.json` | triage reader reply schema |  |
