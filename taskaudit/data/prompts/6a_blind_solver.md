<uploaded_files>
@@WORKING_DIR@@
</uploaded_files>
I've uploaded a python code repository in the directory @@WORKING_DIR@@. Consider the following PR description:

<pr_description>
@@PROBLEM_STATEMENT@@
</pr_description>

Can you help me implement the necessary changes to the repository so that the requirements specified in the <pr_description> are met?
I've already taken care of all changes to any of the test files described in the <pr_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!
Your task is to make the minimal changes to non-tests files in the @@WORKING_DIR@@ directory to ensure the <pr_description> is satisfied.
Follow these steps to resolve the issue:
1. As a first step, it might be a good idea to find and read code relevant to the <pr_description>
2. Create a script to reproduce the error and execute it with `python <filename.py>` using the bash tool, to confirm the error
3. Edit the sourcecode of the repo to resolve the issue
4. Rerun your reproduce script and confirm that the error is fixed!
5. Think about edgecases and make sure your fix handles them as well
Your thinking should be thorough and so it's fine if it's very long.

Working environment: run every command inside @@WORKING_DIR@@ with your shell tool. There is no network access. Leave your change as uncommitted edits in the working tree (do not commit, stash or reset); the change is read with `git diff` when you finish. Delete any scratch scripts you created before you finish.

SOLVER NOTES. After your change is complete, report how you read the specification part of the <pr_description> (the "Requirements:" and "New interfaces introduced:" block at its end):
- "spec_notes": one entry for every requirement and for every interface entry of that block, in order:
  - "sentence": the requirement copied exactly; for an interface entry, its Name line copied exactly;
  - "reading": "clear" if the sentence left you exactly one way to write the behaviour it describes; "ambiguous" if you could read it in two or more ways that lead to different code (a different name, signature, default, return value or type, exception or warning type, message, output format or order, or behaviour);
  - "assumptions": for "ambiguous", the readings you saw, the one you implemented and why; for "clear", any assumption you still had to make ("" if none);
  - "how_chosen": every name, signature, value, message or format in your change that this sentence concerns and that the <pr_description> does not give word for word, and how you chose it ("" if none).
- "other_assumptions": assumptions you made about the rest of the <pr_description> ("" if none).
- "summary": two sentences on what you changed.
Reply at the end with JSON only:
{"spec_notes": [{"sentence": "...", "reading": "clear"|"ambiguous", "assumptions": "...", "how_chosen": "..."}], "other_assumptions": "...", "summary": "..."}
