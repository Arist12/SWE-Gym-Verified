# Shared definitions: SWE-Gym-Verified prompt set (2026-10-14)

Each block is the text between its `=== BEGIN <NAME> ===` and `=== END <NAME> ===` lines (both excluded). A prompt that uses a definition includes the whole block byte-identically.

Blocks:
- `V`: what the solver sees; used by every judge, writer and gate prompt except the PAI router.
- `FN_DEFINITIONS`: graded F2P assertion (row) and its `passes_at_base` field, DETERMINED / UNDERDETERMINED, kinds (3a-3d).
- `REGRESSION_GUARD_RULE`: the one regression-guard rule (3a-3d, 4a, 4b, 5d).
- `EXPLICIT_CHECKS`: checks a-i before a row status; g applies the guard rule (3a-3d).
- `REASONABLE_ALTERNATIVE`: what a reasonable / natural alternative is (3a-3d; its "fully handles exactly the cases the issue describes" clause is also quoted in 5d).
- `COUNTING_RULE`: condition (iii) per test, regression guards and PASS_TO_PASS failures for executed patches (3b-3d).
- `PACK_V41`: description of the v4.1 code-evidence pack (3a, 3b).
- `DISCUSSION_QUOTE`: the one form of a discussion quote (1d resolving_quote, 3a-3d discussion_quote, G1, IS route).
- `SPEC_FORMAT`: SWE-bench Pro specification layout (5a, 5b, 5c, G1).
- `QUOTE_FRAME`: fixed frame of a discussion-quote requirement (5a, 5b, 5c, G1).
- `SPEC_CONTENT`: what a specification may / must never state; "in-scope assertions" is defined by each prompt (5a, 5b, 5c, G1).
- `TOKEN_RULES`: the deterministic G1 token rules, word for word the same in the writer and in G1 (5a, G1).

=== BEGIN V ===
SOLVER-VISIBLE CONTEXT V (apply exactly). The solver (the engineer who resolves the task) sees (1) the task statement: the issue title and body exactly as given in this message, followed by the specification appended to it when the task has one (a "Requirements:" / "New interfaces introduced:" block; the issue text itself is never edited), (2) the full repository at the base commit (neither the fix nor the test patch applied), and (3) the documentation of the pinned dependencies. The solver does NOT see the issue discussion, the pull request, the hidden tests (the test patch), the gold patch (the reference solution), or the content behind links, images and attachments, and it has no network. A passage of the issue discussion reaches the solver only if the specification quotes it.
=== END V ===

=== BEGIN FN_DEFINITIONS ===
DEFINITIONS
"The issue" means the task statement of V throughout: the issue title and body, followed by the appended specification when the task has one.
Graded F2P assertion (a row): a check executed by an F2P test (assert, expected value, pytest.raises type and match=, warns, expected output, mypy `# E:` / `# N:` lines and [out] blocks) that passes with the gold. Group checks of the same requirement into one row (at most 12 rows) and list the tests each row covers (short names are fine); never group a check that fails at base with one that already passes at base. Each row records passes_at_base: "no" if its check fails at base, "yes" if it already passes at base (when the test stops at an earlier check at base, evaluate this check's expression at base). A row with passes_at_base "yes" is a regression-guard row (check g).
DETERMINED: V fixes the checked value. Evidence: an issue quote, a base-repository location (file:line) shown in the pack or verified in the repository, or a named standard. "The gold does it" is not evidence.
UNDERDETERMINED: there is a concrete alternative that a competent maintainer would accept as resolving the issue, consistent with V, which the assertion rejects. You must exhibit that alternative; if you cannot, the row is DETERMINED.
Kinds. DETERMINED kinds: stated_in_issue, derivable_from_codebase, standard_convention. UNDERDETERMINED kinds: guess_interface (a new public name, signature, parameter or exception class), guess_literal (message text, format or exact value), guess_behaviour_choice (one of several reasonable behaviours), beyond_issue (behaviour the issue does not ask for). The status follows from the kind.
=== END FN_DEFINITIONS ===

=== BEGIN REGRESSION_GUARD_RULE ===
REGRESSION-GUARD RULE (one rule for FAIL_TO_PASS checks and PASS_TO_PASS tests)
A regression guard is a check that already passes on the original (base) code: it holds behaviour the base already has. A patch that changes that behaviour fails the guard. The failure counts against the test, not against the patch, only if
(a) the task statement as shown to the solver (V) explicitly asks for the change the check forbids (quote its words), or
(b) the check pins the defect the task statement reports, in a sibling code path or input: the same defect, not merely related behaviour, which a careful fix of the reported defect would naturally also fix (name the sibling path and quote the words that report the defect).
Otherwise the guard protects behaviour that a correct fix must keep, even if the patch's change looks like an improvement, and the failure counts against the patch. Support that exists only in the issue discussion satisfies neither (a) nor (b). A failure that does not depend on the patch (the same test also fails without the patch or with the gold, or the failure comes from the environment: network, randomness, timing, test order, warning-registry state, resource limits, a missing optional dependency) is not a regression-guard failure: it counts neither against the test nor against the patch.
=== END REGRESSION_GUARD_RULE ===

=== BEGIN EXPLICIT_CHECKS ===
EXPLICIT CHECKS (apply each before deciding a row's status)
a. Existing is not new. If the pack shows that a name, helper, parameter, message, format or mapping already exists at base (section 1 hits, section 2 bodies, section 4 messages) and the test uses it in the role it already has, that is derivable_from_codebase, not a guess. A token that is ABSENT AT BASE and not in the issue is a candidate guess (you still need an alternative).
b. Pre-existing tests count as specified. The expected output of a test that exists at base (EXISTS AT BASE, an xfail being re-enabled, an expected value already written in the base test) is visible to the solver.
c. Shared code path. An "extra" assertion that runs through the function the natural fix must change (sections 2-3), so that the natural fix of the issue also makes it pass, is derivable_from_codebase. The converse also holds: if the natural fix of the cases the issue describes gives an extra, undescribed case a DIFFERENT result than the test expects, that row is beyond_issue (or a guess) and that fix is its rejected alternative.
d. Exact values produced by the mechanism. An exact hash, format, shape or ordering is UNDERDETERMINED only if you can name a natural fix that produces a different value. If every natural fix of the root cause yields this value, it is DETERMINED.
e. Standards count. Language, library, framework, protocol and cloud-API semantics (NumPy semantics, the Python data model, a PEP, an RFC, an AWS API as documented in the pinned botocore service models, ...) count as specified: name the standard.
f. Quoted real output counts as stated. Text the issue quotes from real output (tracebacks, service error messages, shown expected output) is stated_in_issue.
g. Regression guards. A row with passes_at_base "yes" is a regression guard: apply the REGRESSION-GUARD RULE. If neither (a) nor (b) holds, mark it DETERMINED (derivable_from_codebase; the evidence is the base code that already produces the checked behaviour). If (a) or (b) holds, mark it UNDERDETERMINED (guess_behaviour_choice), write the basis ("(a)" or "(b)") and the quoted words in its evidence, and give the fix that changes the guarded behaviour as its rejected alternative.
h. A convention settles a choice only if it is unambiguous. A repository pattern makes a NEW name, signature or behaviour DETERMINED only if it is the one established way of doing this kind of thing in the touched area, so that nearly every engineer would copy it. If the repository shows two or more ways of doing the same kind of thing, or the pattern comes from an unrelated area, or the issue does not ask for the option or interface at all, then a solver who picks another reasonable way (or adds no option) is a valid alternative.
i. Weak tests are not false negatives. If a concrete wrong or incomplete solution that a solver might plausibly submit would pass, describe it in `too_weak` (null if you cannot name one); it never raises fn.
=== END EXPLICIT_CHECKS ===

=== BEGIN REASONABLE_ALTERNATIVE ===
WHAT COUNTS AS A REASONABLE ALTERNATIVE (both directions matter equally)
Natural = what a competent engineer who sees only V would write first: the simplest change that makes the behaviour the issue describes correct, at the place V points to (the traceback, the function the issue names, the code that produces the behaviour, the helper the pack shows).
- NOT reasonable: narrow workarounds (a blanket try/except, special-casing the reproducer, silencing an error or declining the input instead of fixing its cause); ignoring a helper, sibling pattern or convention that check h says is unambiguous; a solution more elaborate than the gold that adds machinery, options or layers the issue does not ask for; moving the fix to a different layer than the one that produces the behaviour; reading a phrase of the issue more literally than its evident intent when that reading needs extra special-casing.
- Reasonable, even if the gold is more general: a fix that fully handles exactly the cases the issue describes (its examples and stated expectations), in the natural place. "Less complete than the gold" is not a reason to dismiss it when the extra behaviour the tests demand is not described in V. Likewise, another reasonable name or interface for something new that V does not fix (check h).
=== END REASONABLE_ALTERNATIVE ===

=== BEGIN COUNTING_RULE ===
WHEN A FAILING TEST COUNTS (condition (iii), judged per test). A patch fails the graded tests when at least one F2P test fails with it and passes with the gold. Look at the check where that test fails with the patch: if the check fails at base (its row has passes_at_base "no"), the failure counts; if it already passes at base (passes_at_base "yes"; when the test stops at an earlier check at base, evaluate this check's expression at base directly), it is a regression guard and the failure counts only if (a) or (b) of the REGRESSION-GUARD RULE holds (quote the issue; for (b) also name the sibling path). Otherwise the patch breaks existing behaviour and is not acceptable (condition (ii)): breaking a regression guard alone is never an FN. A patch that fails a PASS_TO_PASS test that the gold passes also breaks existing behaviour and is not acceptable, unless (a) or (b) holds for that test or the failure does not depend on the patch (the last sentence of the rule).
=== END COUNTING_RULE ===

=== BEGIN PACK_V41 ===
a mechanical excerpt of the base repository (the solver has the whole repository; the pack is what was extracted for you). Section 1 = tokens of the F2P tests and of the gold's added lines that are not in the issue, with where they occur at base ("ABSENT AT BASE" = nowhere in the base tree's text files; "only inside the files the test patch edits" = pre-existing test content); 2 = base bodies of the functions/classes the gold edits, plus public signatures of those modules; 3 = callers at base; 4 = raise/warn statements of the edited modules; 5 = the F2P tests at base (EXISTS AT BASE / ABSENT AT BASE, every F2P id and the same tests' PASS_TO_PASS ids, xfail markers, base bodies; other tests the test patch edits are labelled "in PASS_TO_PASS" or "not graded by the harness"); 6 = pinned dependency versions; 7 = the unedited members of the edited classes; 8 = code elsewhere that does the same kind of thing; 9 = contracts of the repository symbols and fixtures the F2P tests use; 10 = bodies of the functions the gold-changed lines call; 11 = full bodies of members the gold or the F2P tests use that earlier sections show only in part, and docstrings of the edited classes; 12 = base code at the innermost repository frame of each traceback the issue quotes; 13 = repository doc paragraphs that mention a message the gold removes or adds; 14 = set-up, helpers and constants the F2P tests use from their own files, and those files' other tests (first lines). Sections 10-14 appear only when they have content; instead of repeating code an earlier section shows, they point to it. Code markers: `*` base line the gold removes or rewrites, `>` the gold inserts lines after it (in section 5 both mean the test patch), `!` the quoted traceback line.
=== END PACK_V41 ===

=== BEGIN DISCUSSION_QUOTE ===
DISCUSSION QUOTE (the one form for every quote of the issue discussion): the SHORTEST contiguous span of the issue discussion that still states the detail, copied verbatim from a single comment: prose only (no code block), cut at a sentence or clause boundary, with no ellipsis ("..." or "…") anywhere in it, at most 300 characters. Line breaks inside it are joined with single spaces when it is quoted.
=== END DISCUSSION_QUOTE ===

=== BEGIN SPEC_FORMAT ===
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
=== END SPEC_FORMAT ===

=== BEGIN QUOTE_FRAME ===
DISCUSSION-QUOTE REQUIREMENT. A requirement of the form `A later comment on the issue says: "<quote>"` presents a DISCUSSION QUOTE; the solver sees the quote only through this requirement. Its attribution prefix ("A later comment on the issue says:") and the quotation marks are a fixed frame that only says where the quoted text comes from: the frame carries no requirement and is never extra content or a leak. The quoted text itself is judged like any other specification text: every part of it must be allowed by the MAY list, so a clause that no in-scope assertion checks is not allowed.
=== END QUOTE_FRAME ===

=== BEGIN SPEC_CONTENT ===
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
=== END SPEC_CONTENT ===

=== BEGIN TOKEN_RULES ===
TOKEN RULES (G1, deterministic; checked on every requirement, on the quoted text of a discussion-quote requirement, and on every interface field except Type). A text passes only if ALL hold:
1. Identifiers (backticked spans and code-shaped prose words: dotted, snake_case, camelCase, call syntax, --flag) have provenance: they appear, spelled the same way, in the issue, or in a routed assertion's entry point or checked expression (its `api` entries with their keyword names and dotted suffixes, its `code` line, an identifier-shaped pinned `literal`). Names that the hidden test code defines itself (def/class names, function parameters, assignment targets, loop/with targets: fixtures, locals, test helpers) have no provenance from the tests unless the issue or an `api` entry also has them.
2. Literals (quoted strings and numbers, also inside code spans) have provenance: they appear in the issue (a number as a standalone number token), or are a contract value that a routed assertion checks (a string inside a pinned `literal`, including a message, format or order string; a number equal to a number in a pinned `literal`). A value that occurs only as an argument or data in the test code has no provenance.
3. A code span (backticked text) appears verbatim in the issue or in a routed assertion's `api` / `code` text, and its identifiers and literals pass 1 and 2 one by one (so `seed=123` fails when 123 is a test input).
4. No identifier has a private segment (a segment that starts with "_" and is not a dunder: `__all__` and `__init__` are public) or an internal segment (core, _libs, internals), even if the issue or the tests have it.
5. No path-like token (a file name with a source or config extension, an a/b/c path, a URL, a "Path:" line), even if the issue has it.
6. No instruction phrasing: no sentence, list item, or text after a colon or semicolon that starts with a code-change verb (add, modify, update, replace, change, implement, fix, remove, rename, use, call, create, define, ensure, make sure, ...); no code-change verb after a modal or a person (should, must, need(s) to, has/have to, will, can, could, would, let's, we, you, I: "should add", "can change", "we remove"); no "should/must/needs to be added/changed/... to/in/from ..."; no "replace ... with `...`". A verb in a declarative description of the result is allowed ("the value no longer changes", "`X` returns `Y`").
7. No test words: test, tests, tested, testing, pytest, unittest, assert*, fixture(s), parametrize, xfail, doctest, conftest, test_x, TestX, F2P, P2P, FAIL_TO_PASS, PASS_TO_PASS.
=== END TOKEN_RULES ===
