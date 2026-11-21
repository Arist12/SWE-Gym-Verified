"""Task input adapters: any source -> SWE-bench-style task dicts.

A task dict has the SWE-bench field names:

    instance_id, repo, base_commit, version, created_at, problem_statement, hints_text,
    patch (the gold patch), test_patch, FAIL_TO_PASS (list), PASS_TO_PASS (list),
    image (the task's environment image)

and, optionally, ``environment_setup_commit``, ``test_data_mounts``, and for the ``script`` harness backend
``test_cmd`` / ``install_cmd`` / ``log_parser`` (collections that ship their own test command, once their records
carry the fields above). ``dataset.field_map`` renames source fields to these names; ``dataset.image_template`` builds ``image``.
"""
from __future__ import annotations

from .base import load_tasks, normalize  # noqa: F401
