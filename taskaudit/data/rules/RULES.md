# Decision rules

Definitions (what the solver sees, the regression-guard rule, discussion quotes, the specification format and what
it may state) are the blocks of `prompts/00_shared_definitions.md`, included verbatim in every prompt that uses them.
The triage pre-checks, HARD removals and repair routes are quoted in `prompts/7a_triage_reader.md` (sections 3-5).
This file states the rules the code applies around the prompts; "claude" and "gpt" are the two model families
(`llm.families`). The module that implements each rule is named in brackets. A step's id, in parentheses at its first
mention, is the prefix of its prompt file ("Names" in `docs/WORKFLOW.md` lists them).

## 1. Pre-screen and environment [prescreen.py, environment.py]
- Removed before anything runs: no image, an empty or title-only statement, no FAIL_TO_PASS test, no gold or test
  patch, a duplicate (same repository, base commit and normalised gold diff, or same statement and test patch).
- The gold patch and the empty patch run 3 times each, offline, in fresh containers, after PREP: the repository is
  truncated to a single commit at the base (no later history to read the fix from), files the image ships modified
  are hidden, copies of the project's source outside the repository are removed.
- Kept iff the gold and test patches apply, the tests report results, every F2P test passes in every gold run and
  fails in at least one way in the empty runs (an F2P id that passes in only some gold or some empty runs is flaky:
  removed; an empty patch that passes every F2P test: removed). A gold run failing an id in every run removes the task
  (ENV_GOLD_F2P_FAIL / ENV_GOLD_P2P_FAIL; ENV_NEEDS_NETWORK when an online gold run passes, `probe_network`).
- A PASS_TO_PASS id that cannot pass under the grader (unreported, skipped, failing without any patch, or flaky across
  runs) is dropped from grading (`p2p_dropped`), not a removal.

## 2. Statement screens [screen.py]
- Statement hygiene (1a, one family): empty / title-only, external = 2 (what to do is only behind a link),
  incoherent -> removed.
- Defective pull request: a human label with DP (PAIChecker's annotations) -> removed; no human label -> the
  PAIChecker router (2) on the PR's evidence record, DP -> removed.
- Misleading statement (1d, both families): both misleading with a valid discussion quote from both -> kept with an
  IS repair item (flag `statement_misleading_both`); both misleading otherwise -> removed (MISLEADING_BOTH_FAMILIES);
  one family -> kept with a flag (`statement_misleading_claude` / `statement_misleading_gpt`).
- Solution in issue and F2P tests against the issue (1b): flags only, set when both families agree
  (`solution_in_issue` on "full", `f2p_unrelated` on "unrelated", `f2p_partly_unrelated` on "partly").

## 3. FN detection [fn_rate.py, escalate.py]
- FN rating (3a), both families, every graded F2P assertion: both fn >= 2 -> FN (witness mode); both fn = 0 and
  nothing unresolved -> no FN (no execution: `cleared_by_rating`); anything else -> full escalation. A failing row
  that already passes at base (a regression guard) always escalates; so does a task whose code-evidence pack is still
  degraded at 128K tokens.
- Full escalation (witness, 3b; execution; review, 3c; debate, 3d; coding agents with execution in the task
  container): CONFIRMED iff both families' final verdicts are FN_CONFIRMED on a common executed patch; if the
  confirmation rests on a regression guard, both name the same failed test with the same basis ((a) the statement
  asks for the change, (b) the same defect in a sibling path), else CONTESTED. NOT_FN iff both say NOT_FN. Anything
  else is CONTESTED. A family whose stage stays void under the command audit is UNSURE -> CONTESTED. Gold failing
  offline -> removed.
- Witness mode: the FN stands; both families' witnesses are written and executed (the evidence for the repair).
- Evidence behind an FN verdict (`fn_evidence`): `cross_reviewed` = CONFIRMED after full escalation; `two_witnesses` =
  witness mode with both witnesses executed and failing a common F2P test; `other_confirmed` = any other confirmed
  verdict; `contested` = a CONTESTED verdict.
- CONFIRMED and CONTESTED go to the triage (repair or removal); a CONTESTED item may take statement routes only.

## 4. PASS_TO_PASS audit [p2p.py]
- A candidate that passes every F2P test and fails a P2P test: the P2P judge (4a) by both families; both
  `pins_old_behaviour` and each family's gold variant (4b) runs "F2P pass, this P2P fails" -> a P2P item for repair;
  families disagree -> the test is kept. Both `flake_drop` -> dropped once 3 + 3 runs confirm the flake. Both
  `delete_task` -> removed.

## 5. Triage and repair [triage.py, repair.py]
- FN-flawed tasks the base model always solves (every sample) are removed without a repair attempt.
- Statement routes first (S-a public entry point, S-lit contract value, S-keep unchanged behaviour, S-scope natural
  extension, IS quoted discussion); T-b test relaxation only for values that cannot be stated; no test id is removed.
- Cases the mechanical rules leave open are read by both families (the triage read, 7a); the answers combine by fixed
  rules, and a disagreement removes the task.
- The repair writer (5a, one family) sees the issue, the route of each routed assertion, the routed assertions and
  the F2P test sources; never the gold patch or the rest of the discussion.
- Repair checks, all required. G1, the mechanical check: deterministic form and token rules (`G1_rules.txt`). G2,
  traceability (5b, both families): every routed assertion covered and traced, every item accurate and mapped to a
  routed assertion (extra content is recorded as the flag `repair_untraced_content`, not failed). G3, the leak judges
  (5c, two judges): a judge flags on any leak finding, leak level >= 1 or the solution given; a flag from either judge
  fails the repair.

## 6. Re-audit of a repaired task, once [reaudit.py]
- The misleading-statement screen again with the specification shown; FN rating and escalation again on the repaired
  statement (both fn >= 2 fails without a witness round); for a test repair, the environment check on the relaxed
  tests first.
- With a difficulty measurement: G4, the base-model check: if >= 2 samples violate the same routed assertion the same
  way, one violating sample is reviewed and debated by both families (CONFIRMED -> removed); G5, the jump read: 0
  samples resolved before the repair and >= 6 of 8 after triggers the leak re-read (5g; both judges flag -> removed).
- Any failed check removes the task; there is no second repair.

## 7. Difficulty [measure.py]
- Difficulty = how many samples of the policy model resolve the final statement and tests (the paper: 8 samples of
  its base model), written as the band resolved/samples; a resolved sample counts as unresolved when its patch fails
  the final grader on the final tests.
- A residual-FN audit draws tasks that the FN rating cleared without escalation (`stages.escalate.audit_n`, ranked by
  sha256("<seed>:<id>")) and runs the full escalation on them (`audit_path: residual_audit`); `gates/stats.py` has
  the Wilson bound of the stop rule.

## 8. False positives [fp.py]: `FP_RULES.md`.
