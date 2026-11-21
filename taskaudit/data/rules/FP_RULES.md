# False positives: labels, counterexamples, regression tests

Goal: add PASS_TO_PASS tests that make observed false-positive (FP) patches fail while every non-FP patch still
passes. Adding a false negative is the red line: when in doubt, a test is not added.

## 1. Population
- The distinct passing patches of the task that differ from the gold patch (distinct by normalised diff): the
  resolved samples of the difficulty measurement, or a file of candidate patches (`stages.fp.candidates`).

## 2. Labels
- FP judge: `prompts/5f_fp_judge.txt`; each family labels a patch correct / incorrect / uncertain, once per patch.
- Judge input: the issue, the appended specification when the task has one, a code-evidence pack and the candidate
  patch. No gold patch, no tests, no test ids. The pack is the code-evidence pack builder run with the candidate patch
  in the gold patch's place and no test patch, ids or hints; sections 1, 5, 9, 11, 14 are dropped; "gold" wording is
  renamed "candidate"; code markers are removed; a line naming an F2P test that the candidate does not name, or quoting
  a gold-added line (>= 20 characters, absent at base, not a candidate line), is withheld.
- Attempts: a reply without a parsable answer is an unparsed attempt; 3 unparsed attempts = unusable (label
  `undetermined`).
- Counterexample confirmation: every counterexample script of a both-incorrect patch runs offline on the original
  code, the gold patch and the candidate, in its behaviour form (prints inside except handlers become raised-or-not
  markers; except clauses are broadened, and an exception at base that a broadened clause catches although it is not
  an instance of the script's own except classes counts as an uncaught raise at base). Comparator: stdout value lines,
  equal up to formatting; an exception compared only as raised-or-not. Confirmed = base == gold != candidate.
- Label: `false_positive` = both families incorrect AND >= 1 confirmed counterexample. `correct` = both families
  correct. `undetermined` = everything else (disagreement, uncertain, unusable, both incorrect without a confirmed
  counterexample).

## 3. Candidate tests
- Each confirmed counterexample of a false positive becomes one candidate PASS_TO_PASS test (one per distinct
  behaviour-form script): it runs the script and compares its stdout with the base output (up to formatting) and
  asserts exit code 0. A script that raises uncaught at base gives no test; nor does one that measures time or memory
  or prints a warning as a value.
- One added test file per task, with a name no patch touches.

## 4. Checks (a candidate test is kept iff it passes all three)
- `check1`: the test passes at base and with the gold, and fails on >= 1 of its own false-positive patches.
- `check2`: the test passes on every patch of the task that is not a false positive (an unusable run counts as not
  passed) AND >= 1 such patch whose normalised diff differs from the gold's passes it. A task without such a patch
  gets no test.
- `check3`: the task's full test patch with the added tests is graded twice with the gold; any disagreement drops the
  test; so does a script whose base runtime exceeds `max_base_runtime_s` (24 s).

## 5. Check on an extra pool of passing patches (tests are only deleted)
- Split 50 / 50 per task by sha256("<split_seed>:<task>:<normalised diff sha>") (twins in the same half).
- Construction half (tests may only be deleted): every kept test runs on every patch of the half; each rejected
  patch is judged by the same two-family judge; a rejection stands only if both families say incorrect, otherwise the
  rejecting test is deleted. Then the test set is frozen.
- Analysis half: measurement only (rejection counts). No test changes.
