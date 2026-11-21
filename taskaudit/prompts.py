"""Prompts as data files, loaded by step name.

The prompt directory (default: the packaged ``data/prompts``; config ``prompts.dir`` swaps it without code changes)
holds the prompt files, ``prompts.json`` and ``SHA256SUMS``. SHA256SUMS lists the sha256 of every prompt and schema
file. Every read compares the file with it and records the result in the stage row (``prompt_sha256``,
``prompt_matches_sums``); ``prompts.check: strict`` stops instead. A step maps to a file through STEPS, overridable
per step (``prompts.files.<step>: <file>``).
"""
from __future__ import annotations

import functools
import hashlib
import warnings
from pathlib import Path

PACKAGED = Path(__file__).resolve().parent / "data" / "prompts"

# step name -> (prompt file, reply schema file or None)
STEPS: dict[str, tuple[str, str | None]] = {
    "hygiene": ("1a_statement_hygiene.txt", None),
    "solution_in_issue": ("1b_solution_in_issue.txt", None),
    "misleading": ("1d_misleading_statement.txt", None),
    "pai_router": ("2_pai_router.txt", None),
    "fn_rater": ("3a_fn_rater.txt", None),
    "witness": ("3b_witness.md", "3b_witness.schema.json"),
    "review": ("3c_review.md", "3c_review.schema.json"),
    "debate": ("3d_debate.md", "3d_debate.schema.json"),
    "p2p_judge": ("4a_p2p_judge.txt", None),
    "p2p_gold_variant": ("4b_p2p_gold_variant.txt", None),
    "spec_writer": ("5a_spec_writer.txt", None),
    "g2_traceability": ("5b_g2_traceability.txt", None),
    "g3_leak": ("5c_g3_leak_judge.txt", None),
    "flip_judge": ("5d_flip_judge.txt", None),
    "fp_judge": ("5f_fp_judge.txt", None),
    "g5_jump_read": ("5g_g5_jump_read.txt", None),
    "blind_solver": ("6a_blind_solver.md", "6a_blind_solver.schema.json"),
    "triage_reader": ("7a_triage_reader.md", "7a_triage_reader.schema.json"),
}


class PromptMismatch(RuntimeError):
    pass


class Prompts:
    def __init__(self, cfg: dict):
        pc = cfg.get("prompts") or {}
        self.dir = Path(pc.get("dir") or PACKAGED)
        self.check = pc.get("check", "warn")
        self.files = {k: (pc.get("files") or {}).get(k, v[0]) for k, v in STEPS.items()}
        self.schemas = {k: v[1] for k, v in STEPS.items()}

    @functools.cached_property
    def sums(self) -> dict[str, str]:
        p = self.dir / "SHA256SUMS"
        out = {}
        if p.exists():
            for line in p.read_text().splitlines():
                if line.strip():
                    h, name = line.split(None, 1)
                    out[name.strip()] = h
        return out

    def _read(self, name: str) -> tuple[str, dict]:
        b = (self.dir / name).read_bytes()
        h = hashlib.sha256(b).hexdigest()
        matches_sums = self.sums.get(name) == h
        if not matches_sums and self.check != "off":
            msg = f"prompt {name}: sha256 {h[:12]} differs from SHA256SUMS ({(self.sums.get(name) or 'absent')[:12]})"
            if self.check == "strict":
                raise PromptMismatch(msg)
            warnings.warn(msg, stacklevel=3)
        return b.decode(), {"file": name, "sha256": h, "matches_sums": matches_sums}

    def text(self, step: str) -> str:
        return self._read(self.files[step])[0]

    def meta(self, step: str) -> dict:
        return self._read(self.files[step])[1]

    def schema_path(self, step: str) -> Path | None:
        name = self.schemas.get(step)
        return self.dir / name if name else None

    def shas(self, *steps: str) -> dict[str, str]:
        out = {}
        for s in steps:
            out[self.files[s]] = self.meta(s)["sha256"]
            if self.schemas.get(s):
                out[self.schemas[s]] = self._read(self.schemas[s])[1]["sha256"]
        return out
