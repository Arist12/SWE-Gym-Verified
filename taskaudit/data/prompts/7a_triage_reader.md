You are a reader in the repair triage of a coding-task dataset. A task is a real GitHub issue plus hidden graded tests. Earlier stages found graded checks of this task that a reasonable solution of the issue can fail; each such finding is an item of the READ CARD. A mechanical triage applied the triage rules below to these items and could not decide the task: the READ CARD names the open questions, the rule row each one comes from, and what the mechanical triage proposed. Answer the open questions from the rules and the evidence (the card, the tests and the base code), and give the task's triage decision: repair, with a route for every row to route, or delete, with the rule that requires it.

SOLVER-VISIBLE CONTEXT V (apply exactly). The solver (the engineer who resolves the task) sees (1) the task statement: the issue title and body exactly as given in this message, followed by the specification appended to it when the task has one (a "Requirements:" / "New interfaces introduced:" block; the issue text itself is never edited), (2) the full repository at the base commit (neither the fix nor the test patch applied), and (3) the documentation of the pinned dependencies. The solver does NOT see the issue discussion, the pull request, the hidden tests (the test patch), the gold patch (the reference solution), or the content behind links, images and attachments, and it has no network. A passage of the issue discussion reaches the solver only if the specification quotes it.

DEFINITIONS
"The issue" means the task statement of V throughout: the issue title and body, followed by the appended specification when the task has one.
Graded F2P assertion (a row): a check executed by an F2P test (assert, expected value, pytest.raises type and match=, warns, expected output, mypy `# E:` / `# N:` lines and [out] blocks) that passes with the gold. Group checks of the same requirement into one row (at most 12 rows) and list the tests each row covers (short names are fine); never group a check that fails at base with one that already passes at base. Each row records passes_at_base: "no" if its check fails at base, "yes" if it already passes at base (when the test stops at an earlier check at base, evaluate this check's expression at base). A row with passes_at_base "yes" is a regression-guard row (check g).
DETERMINED: V fixes the checked value. Evidence: an issue quote, a base-repository location (file:line) shown in the pack or verified in the repository, or a named standard. "The gold does it" is not evidence.
UNDERDETERMINED: there is a concrete alternative that a competent maintainer would accept as resolving the issue, consistent with V, which the assertion rejects. You must exhibit that alternative; if you cannot, the row is DETERMINED.
Kinds. DETERMINED kinds: stated_in_issue, derivable_from_codebase, standard_convention. UNDERDETERMINED kinds: guess_interface (a new public name, signature, parameter or exception class), guess_literal (message text, format or exact value), guess_behaviour_choice (one of several reasonable behaviours), beyond_issue (behaviour the issue does not ask for). The status follows from the kind.

REGRESSION-GUARD RULE (one rule for FAIL_TO_PASS checks and PASS_TO_PASS tests)
A regression guard is a check that already passes on the original (base) code: it holds behaviour the base already has. A patch that changes that behaviour fails the guard. The failure counts against the test, not against the patch, only if
(a) the task statement as shown to the solver (V) explicitly asks for the change the check forbids (quote its words), or
(b) the check pins the defect the task statement reports, in a sibling code path or input: the same defect, not merely related behaviour, which a careful fix of the reported defect would naturally also fix (name the sibling path and quote the words that report the defect).
Otherwise the guard protects behaviour that a correct fix must keep, even if the patch's change looks like an improvement, and the failure counts against the patch. Support that exists only in the issue discussion satisfies neither (a) nor (b). A failure that does not depend on the patch (the same test also fails without the patch or with the gold, or the failure comes from the environment: network, randomness, timing, test order, warning-registry state, resource limits, a missing optional dependency) is not a regression-guard failure: it counts neither against the test nor against the patch.

SPECIFICATION FORMAT (SWE-bench Pro layout, appended after the unedited issue):

Requirements:
- <requirement>
- <requirement>

New interfaces introduced:
- Name: <name>
- Type: <class | function | method | attribute | property | command | configuration key>
- Input: <parameters, or N/A>
- Output: <return value or type, or N/A>
- Description: <one sentence>

When there is no interface entry, the second section is the single line "No new interfaces are introduced."
- Requirements: no line breaks inside a requirement. When the route has a template requirement, it comes first and may have more than one sentence: route S-a, the given mechanical interface sentence, copied verbatim; route IS, the discussion-quote requirement framed as below, whose quote is the shortest span that states the needed requirement. Every other requirement is one sentence.
- New interfaces introduced: an entry only for a public name that does not exist at the base commit and that the hidden tests call directly (a new function, class, method, attribute, property, command or configuration key). A new keyword argument or option of an existing entry point is a requirement, not an interface entry. At most 40 words per entry. Input and Output give only what an in-scope assertion pins; N/A otherwise. There is no Path field.
- The headers "Requirements:" and "New interfaces introduced:", the line "No new interfaces are introduced." and the interface field labels (Name, Type, Input, Output, Description) are fixed layout: they carry no requirement and are never extra content or a leak.

WHAT THE SPECIFICATION MAY STATE (and nothing else):
(a) the observable requirement that an in-scope assertion checks, in general terms: the kind of input or situation and the result, including existing behaviour that the assertion requires to stay unchanged;
(b) the public entry point that the hidden tests call directly to exercise an in-scope assertion: the command and its options, or the public function, class, method, keyword argument, attribute or configuration key, existing or new, that the tests themselves invoke, import, construct, read or pass, named as the tests use it (a dotted module path only if the tests import the name from that public path), with the part of its signature that an in-scope assertion pins. Naming this entry point is part of WHAT even when the change is made inside it;
(c) the contract values that an in-scope assertion checks: a default, a return value or return type, an exception or warning type, an exact user-facing message, or the format or order of user-visible output;
(d) a public name or a value that the issue text itself states, where a requirement needs it (the specification does not otherwise restate the issue).

WHAT THE SPECIFICATION MUST NEVER STATE (each item holds even if the issue or the tests contain it, except where (d) is named):
- test inputs or examples: concrete arguments, data, parameter values or input/output pairs taken from, or equivalent to, the hidden tests' inputs (a contract value stated for a general kind of input is allowed; tied to the tests' concrete input it is a test input); a value that the issue text itself gives falls under (d);
- code or pseudo-code, beyond the name and signature of a tested entry point;
- the method or algorithm: the approach, mechanism, data flow or step-by-step logic (how the result is computed or cached, which helper or library facility to use, in which order, which condition to add), beyond the observable behaviour an in-scope assertion checks;
- file paths, or which file, module, class, function, method, line or branch to change, other than naming the tested entry point;
- private or internal names: a name with a segment that starts with an underscore (dunder names such as `__all__` or `__init__` are public protocol names, not private), a name with an internal module segment (`core`, `_libs`, `internals`), any other function, class, attribute, variable or constant that the hidden tests do not call directly and the issue text does not state, and any name that the tests only patch, mock or spy on, or a private attribute the tests read (describe the observable effect instead);
- any mention of tests, test files, test names, fixtures, parametrization ids, assertions or test code;
- anything the in-scope assertions do not check, whether or not it comes from the reference solution: an untested message, default, case or option, or a detail of an assertion that is not in scope.

TRIAGE RULES. The three sections below are quoted verbatim from the repair-triage rules. In them, "the tests" and "F2P" are the hidden graded tests and "the issue" is the task statement of V. A statement route (S-a, S-lit, S-keep, S-scope, IS) appends a specification to the issue; a test route (T-b, P2P-relax) relaxes the hidden tests.

## 3. Pre-checks (mechanical, every task)

| # | Check | Outcome |
|---|---|---|
| P1 | already deleted by an outcome of this pipeline: 1a delete; or 1d misleading in both families with no usable resolving quote (no quote from both families that passes the G1 quote check) | delete (no repair) |
| P2 | band 8/8. The band is the base model's eight-sample measurement, or its re-measurement when the freshness check fails | a task with any record item is deleted without a repair attempt: CONFIRMED or CONTESTED, a P2P item, or a 1d IS item. A task without items is kept unchanged (release set only, never in training). Other bands: priority 0/8 > 1-2/8 > 3-7/8 |
| P3 | gold ×3 with `--network none`; the base failure matches the issue's symptom | drop:env (fix the image first if one artefact is missing) |
| P4 | full pytest node ids; dedupe kind votes by assertion site (file:line); one literal or template at N sites = 1 defect; gold-mutant witness for a pure literal with no executed witness | input to the routing |

## 4. HARD: drop without attempting

| # | Condition | Mechanical check | Read needed when |
|---|---|---|---|
| H1 | environment needs network or is broken | P3 | never |
| H2 | private or internal pin: a private name (a segment with a leading `_` that is not a dunder; `__all__`, `__init__` are public), an internal module segment (`core`, `_libs`, `internals`), a mock call list on an internal collaborator, an internal data layout, or a hash / implementation artefact the tests do not call as an entry point | non-dunder leading `_`, `assert_called*` / `method_calls`, an internal global / attribute read by the test | the evidence is free text only (kind_unknown) |
| H3 | mixed routes: the items of one task need both a statement route (S-*, IS) and a test route (T-b, P2P-relax). There is no format limit on the statement items | routes after P4 dedupe | a second defect is suspected but not recorded (hydra-1404) |
| H5 | semantic choice that reverses the issue or a visible base test. "Reverses a visible base test" does not apply when the regression-guard rule (a) or (b) holds. "Reverses the issue" has no exception: (a) and (b) never excuse a choice that reverses the issue | none: a choice over any number of ids or P2P takes S-scope with a read | "reverses" needs a read (pandas-54002) |
| H6 | no route left: the content is out of scope and not a natural extension of the same entry point, so only a removal would apply; includes "every F2P carries the content" | out_of_scope kind + whole-id unit; every canonical F2P assertion is flagged and at least one flagged item has beyond_issue as the strict-majority (or only) kind in each family's own votes → delete without a read (H6_EVERY_F2P_FLAGGED; a tie, a split or a one-family vote never suffices) | deciding natural extension vs separate feature; every F2P flagged without such an item, at least one flagged item not a pure interface / literal pin (H6_EVERY_F2P_FLAGGED_READ) |
| H7 | arbitrary detail entangled with correctness inside one assertion (whole-frame equality with artefacts; an imprecise pin that protects soundness) | none | always |
| H8 | F2P flaky under candidate patches | 3 runs | never |
| H9 | 4a `delete_task` from both families: the P2P test cannot be repaired without losing a needed check, or the task is ill-posed | both 4a replies | never |

H7 blocks only test-side relaxation; a statement
can still state an exact message (dask-10796).

## 5. Routes: statement first, each attempted once

| Route | When (kind → route) | Statement content (SPEC_CONTENT) | Template requirement | Gates |
|---|---|---|---|---|
| **S-a** interface | `guess_interface`: a public entry point (new name, keyword, signature, exception class, attribute) that the F2P calls directly; dunders count as public | MAY (b); a second requirement may give the semantics of the same new parameter | mechanical interface sentence from the test call site | G1–G3 |
| **S-lit** contract value | `guess_literal` at one site or template whose value is a contract value: default, return value or type, exception or warning type, exact user-facing message (incl. warning text), or user-visible output format or order (incl. line position) | MAY (c) | none | G1–G3 |
| **S-keep** kept behaviour | the test pins the base value; a guard row with basis (b) (the same defect in a sibling path, which the tests require to stay as it is); a P2P `pins_old_behaviour` with scope `sibling` (4a `state_keep`; pandas-47387) | MAY (a), "existing behaviour that the assertion requires to stay unchanged"; G2 "traced" requires it to be stated | none | G1–G3 |
| **S-scope** natural extension | `beyond_issue` or `guess_behaviour_choice` where the tests check a natural extension on the same entry point (other operand order, sibling dtype or input form, an edge case) | MAY (a) + (c): one or more requirements (5a), each one WHAT sentence; they name the tested value | none | G1–G3 |
| **IS** quote plus requirement | (i) 1d misleading with a both-family `resolving_quote`; or (ii) an FN row whose required statement would contradict the issue text, and both families give a DISCUSSION QUOTE for that row (`specified_in_discussion` "yes") | QUOTE_FRAME + one or more requirements stating what the routed assertions pin | written by 5a: `A later comment on the issue says: "<span>"`, the span selected from both families' quotes (quote selection below), whitespace-normalised (G1 `is_quote_of` / `is_template`) | G1 (incl. `quote_check` with the candidates), G2 (every clause of the span), G3; ≥ 2 requirements |
| **T-b** relax | what the row pins cannot be stated as WHAT: a computed artefact (MONAI-4036 pixel offsets), or a guard row with basis (a) (old behaviour the issue asks to change). None of H5 / H7 applies and the item is not CONTESTED. Every relaxed site with `passes_at_base` "no" fails at base 3/3 (a relaxed guard row passes at base by definition: its F2P test must still fail at base 3/3); partial matches are possible (not mypy `.test` / pythoneval); the remaining tests cover every symptom the issue states | — | — | gold/empty ×3, site probe, positives (incl. the executed witness), negatives (incl. an accept-and-ignore patch), **strict flip gate** |
| **P2P-relax** | 4a `p2p_relax`: P2P `pins_old_behaviour` with scope `same_input` (the statement asks for the change, so keep-behaviour cannot be stated), both families, gold variants confirm | — | — | as T-b, **strict flip gate** |
| **T-d / T-e** remove ids / strip assertions | none by default | — | — | — |

Further rules:
- **Restriction.** A CONTESTED escalation item, or a P2P item that 4a marks `mixed`, may take only a
  statement route (S-*, IS). If none applies, or its gates fail, the task is deleted.
- **One repair pass per task** covers all its items. Each route is attempted once, and no second repair is made.
- **Several statement routes in one task** are written together, in one 5a call. The result is one specification:
  - order: the S-a template requirements first, then the IS discussion-quote requirement, then the other
    requirements;
  - it is gated once;
  - G1 `form(requirements, interfaces, templates, routes)` checks the whole specification against SPEC_FORMAT (its
    layout and sentence rules; no count or total-word limit);
  - a task that needs both statement and test routes is deleted (H3).
- **Quote selection (IS).**
  - Eligibility: both families gave a quote for the item (1d `resolving_quote`, or the row's `discussion_quote` with
    `specified_in_discussion` "yes"), and each passes the G1 quote check (verbatim after whitespace normalisation,
    cut at word boundaries, ≤ 300 characters, no code fence, no ellipsis). If either family gave none, or either
    quote fails, IS is unavailable: the item takes the S-route of its kind if one applies; otherwise it is deleted.
  - The writer (5a) gets both quotes as QUOTE CANDIDATES (not the rest of the discussion) and selects the SHORTEST
    contiguous span of one candidate that states the needed requirement, cut at a sentence or clause boundary, with no
    ellipsis, ≤ 300 characters (the shared DISCUSSION QUOTE form). The span, in the QUOTE_FRAME, is its first
    requirement.
  - G1 checks the span mechanically (`quote_check(span, hints_text, candidates)`: inside one candidate, verbatim,
    word-boundary cut, no ellipsis, ≤ 300 characters). G2's extra-content rule applies to every clause of the span.
- **Bare quote.** A quote alone is never a repair, and G1 rejects IS with
  fewer than 2 requirements.

**Strict flip gate**:
- A flip is a stored rollout patch that fails the original graded tests and passes the repaired ones.
- The repair fails if any flip has an execution-confirmed defect outside the relaxed or removed detail, or if both
  families judge the flip incorrect.
- For relaxations, a defect inside the relaxed detail also counts.
- A repair with 0 flips (no stored patch changes from fail to pass) still passes the flip gate when every other
  gate of the route passes:
  - the executed witness passes as a positive;
  - every negative, including the accept-and-ignore patch, still fails;
  - gold passes and the empty patch fails, 3/3.
  The relaxation is then accepted.
- Flips are judged with `5d_flip_judge.txt`:
  - a flip is correct if it fully handles exactly the cases the issue describes (the shared REASONABLE_ALTERNATIVE
    clause);
  - it may not break base behaviour unless (a) or (b) of the REGRESSION-GUARD RULE holds.

INPUTS
A. READ CARD (end of this message). It has:
   - OPEN QUESTIONS: each reason the mechanical triage gave, with the rule row it comes from (for a §4 row, its "Read needed when" cell, quoted);
   - MECHANICAL PROPOSAL: the route the triage would take for each row if the open questions were settled its way, the mechanical S-a templates, the IS QUOTE CANDIDATES (both families' discussion quotes that passed the quote check), and whether the restriction of §5 applies;
   - ROWS TO ROUTE: the row ids that a repair must route;
   - ITEMS: each defect item (type, kind votes per source, guard basis votes, tests, failure sites with the code line, literals, the public names the tests use), and the evidence of earlier independent readers for each of its rows (their row text, kind, status, passes_at_base, evidence, rejected alternative, discussion quote, route hint);
   - ESCALATION: the executed-patch round (claims and verdicts per family; the executed patches are in /in, see D), and other findings (misleading statement, PASS_TO_PASS pins).
   Earlier readers can be wrong: weigh their evidence and check it in the code where the decision turns on it.
B. Task packet (end of this message): the issue exactly as the solver sees it, the graded FAIL_TO_PASS test ids and the test patch. The gold patch (the upstream fix) is not shown: decide from V, the tests and the base code.
C. Issue discussion (end of this message): NOT visible to the solver. Use it only for the IS route (whether a needed requirement would contradict the issue text, and the QUOTE CANDIDATES); never as evidence that the issue asks for something.
D. The base repository in the task's environment, through the helper `tbx '<bash command>'` (run it with your shell tool; it is your only way to run anything). Every call is a fresh network-less container with the repository at /testbed at the base commit (single-commit history) and the task's Python environment active; each call is limited to 900 s. Nothing persists between calls EXCEPT /out, a directory that is yours (read-write). /in is read-only and holds:
   /in/test.diff (the test patch), /in/f2p.txt and /in/p2p.txt (the graded FAIL_TO_PASS and PASS_TO_PASS test ids), /in/run_tests.sh (the grader's own script: re-installs the package as the grader does, resets the test files to base, applies the test patch and runs the graded test files; it prints the test output), and /in/esc_<label>_<role>.diff (the patches of the ESCALATION, each run by the grader: role "witness" = its author claims it resolves the issue and fails a graded check; role "alternative" = its author's strongest alternative fix).
   Typical calls:
     tbx 'git apply /in/test.diff && sed -n "1,80p" <test file>'
     tbx 'git apply /in/test.diff && python -m pytest -q -x "<one test id>" 2>&1 | tail -40'                (does a check fail at base?)
     tbx 'git apply /in/esc_A_witness.diff && bash /in/run_tests.sh > /out/run.log 2>&1; grep -E "^(FAILED|ERROR|PASSED)" /out/run.log | head -80'
Rules: read no other file on this machine; do not call docker directly; no network; write nothing outside your working directory and /out. Every command you run is a single tbx call; run tbx calls one at a time.

PROCEDURE
1. Open questions. For each OPEN QUESTION, find its rule row in §4 or §5 and the items it concerns.
2. Evidence. For each item, open the test at its failure sites (apply /in/test.diff), see what the check pins and which public entry point the test calls and, where a question turns on it, whether the check passes at base and what the base code does. Note what decides each question: file:line with the line quoted, an issue quote, or a card line.
3. Route or rule, per item. Decide which route of §5 the item's rows meet (its "When" column, with SPEC_CONTENT for what a specification can state), or which rule of §4 or §5 leaves the item no route. Only the rules quoted above decide. The mechanical proposal is a starting point: give a row a different route when the evidence and the rules require it.
4. Task decision.
   - repair: every row in ROWS TO ROUTE gets exactly one route of §5, and the routes together meet §4 H3 and the further rules of §5 (the restriction; IS only for a row whose item lists QUOTE CANDIDATES).
   - delete: a rule of §4 applies, or an item has no route left. Name the rule in hard_rule: "H2", "H3", "H5", "H6" or "H7" for those rows of §4, "RESTRICTION" for the restriction of §5, "IS_UNAVAILABLE" for the deletion in the quote selection of §5.
5. S-a template (repair with an S-a row only). If the MECHANICAL PROPOSAL lists a template, it is used and you write none. Otherwise write the template requirement in templates: the mechanical interface sentence from the test call site, built only from these forms, with the names exactly as the tests use them (one sentence per new entry point, joined with a space):
   - "The `<method>` method of `<Class>` accepts the keyword argument `<name>`." (several: "accepts the keyword arguments `<a>`, `<b>` and `<c>`.")
   - "`<function or Class>` accepts the keyword argument `<name>`."
   - "`<module>` also provides the function `<name>`."
   - "`<module>` provides the class `<Name>`."
   - "`<module>` provides the exception class `<Name>`."
   - "The `<package>` package has the attribute `<name>`."
6. Relaxation (repair with a T-b or P2P-relax row only). Write the relaxed tests and the patches that check them into /out/relax/:
   - verified.diff: the whole test patch with only the routed checks relaxed, as narrowly as possible: the relaxed check accepts every value the issue leaves open and still rejects what the issue rules out, and every other check of the test patch stays as it is. Make it inside the container from /in/test.diff (apply it, edit the test files, then `git diff > /out/relax/verified.diff`; test files only).
   - Run the graded tests with verified.diff instead of the test patch (apply it, then run pytest on the ids of /in/f2p.txt): at base, every relaxed FAIL_TO_PASS test still fails; with an executed witness of /in whose failure was the relaxed detail, the relaxed tests pass.
   - negatives: source patches (no test files), each violating one requirement that the relaxed tests still hold, so each must still fail with verified.diff: /out/relax/neg_<key>.diff. One of them is an accept-and-ignore patch: it accepts the input, option or call the issue is about and ignores it. If no such patch can exist, give the reason in accept_and_ignore_na. Run each negative with verified.diff and confirm that it fails.
   - positives (optional): source patches that resolve the issue in another reasonable way and pass the relaxed tests: /out/relax/pos_<key>.diff.
   - probe (optional): /out/relax/probe.diff, the test patch plus test functions whose names start with probe_ids_prefix, each checking one kept requirement in isolation (each must fail at base).
   - description: a CHANGE NOTE that says what the hidden tests used to require and what they now also accept, without naming a fix.

OUTPUT: your final answer is one JSON object for task `@@TASK@@` and nothing else (the schema is enforced):
- task; steps_used (the number of tbx calls you made)
- decision: "repair" or "delete"
- row_routes: one {row, route} for every row in ROWS TO ROUTE when decision is "repair"; [] when "delete"
- hard_rule: when decision is "delete", one of "H2", "H3", "H5", "H6", "H7", "RESTRICTION", "IS_UNAVAILABLE"; "none" when "repair"
- templates: the S-a template requirements you wrote in step 5; [] otherwise
- basis: your answer to each open question and the reason for the decision, citing the card and the code (at most about 800 characters)
- code_evidence: the lines that decide it, each "file:line: quoted line" or "issue: quoted words" (at most 8)
- relax: null unless a row routes to T-b or P2P-relax; then {description, sites (the relaxed checks as file:line), verified_file ("relax/verified.diff"), probe_file (its path relative to /out, or ""), probe_ids_prefix (or ""), negatives: [{key, why, file, accept_and_ignore}], positives: [{key, why, file}], accept_and_ignore_na ("", or why no accept-and-ignore patch can exist)}; every file is a path relative to /out (for example "relax/neg_ignore.diff")

=== READ CARD ===
@@CARD@@

=== TASK PACKET ===
@@PACKET@@

=== ISSUE DISCUSSION (NOT visible to the solver; only for the IS route) ===
@@DISCUSSION@@
