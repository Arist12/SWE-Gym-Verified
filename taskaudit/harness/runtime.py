"""Docker runtime: one fresh container per run, network off, resource limits, timeouts, this run's labels.

Only containers this process started are ever removed (by name); images are pulled per ``harness.pull`` and, with
``harness.remove_pulled_images``, the images this process pulled are removed when ``cleanup()`` runs. Nothing is
pruned.
"""
from __future__ import annotations

import subprocess
import threading
import uuid
from pathlib import Path

from .. import NAME
from ..util import slug

DATA = Path(__file__).resolve().parents[1] / "data"
PREP_TEMPLATE = (DATA / "prep.sh").read_text()
COPIES = (DATA / "prep_copies.py").read_text()


def prep_script(base: str, sanitize: int = 1, harden: int = 0) -> str:
    """The environment PREP: single-commit history at the base (nearest tag re-pointed), files the image ships modified
    hidden, and (harden >= 1) copies of the project's source outside /testbed removed (data/prep_copies.py)."""
    return PREP_TEMPLATE.format(repo="/testbed", base=base, sanitize=sanitize, tok=uuid.uuid4().hex[:8], harden=harden)


def parse_prep(out: str) -> dict:
    info: dict = {}
    for line in out.splitlines():
        if line.startswith("__PREP_COMMITS__"):
            info["commits_kept"], info["setup_commits"], info["tags_kept"] = map(int, line.split()[1:4])
        elif line.startswith("__PREP_HISTORY__ "):
            mode, base, commits, tag = line.split()[1:5]
            info["history"] = {"mode": mode, "base": base, "commits": int(commits), "tag": None if tag == "none" else tag}
        elif line.startswith("__PREP_HIDDEN__"):
            info["hidden_modified"], info["hidden_untracked"] = map(int, line.split()[1:3])
        elif line.startswith("__PREP_COPIES__ "):
            import json
            try:
                rep = json.loads(line.split(" ", 1)[1])
                info["copies"] = {k: rep.get(k) for k in ("removed", "remaining", "errors", "seconds")}
            except ValueError:
                info["copies"] = {"unparsed": line[:300]}
    return info


class Docker:
    def __init__(self, cfg: dict, stage: str):
        h = cfg.get("harness") or {}
        self.h, self.stage = h, stage
        self.prefix = h.get("container_prefix") or f"{NAME}-"
        self.run_id = uuid.uuid4().hex[:8]
        self.labels = {f"{NAME}.stage": stage, f"{NAME}.run": self.run_id, **(h.get("labels") or {})}
        self.pulled: set[str] = set()
        self._pull_lock = threading.Lock()
        self._mine: set[str] = set()

    # ------------------------------------------------------------------ plumbing
    def docker(self, *args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", *args], capture_output=True, text=True, errors="replace", timeout=timeout)

    def name(self, iid: str, kind: str) -> str:
        return f"{self.prefix}{self.stage[:10]}-{slug(iid, 36)}-{slug(kind, 12)}-{uuid.uuid4().hex[:6]}"

    def label_args(self) -> list[str]:
        return [x for k, v in self.labels.items() for x in ("--label", f"{k}={v}")]

    def limit_args(self, network: str | None = None) -> list[str]:
        h = self.h
        out = [f"--network={network or h.get('network') or 'none'}", f"--cpus={h.get('cpus', 2)}",
               f"--memory={h.get('memory', '8g')}", f"--pids-limit={h.get('pids_limit', 1024)}", "--ulimit", "core=0"]
        if h.get("cpuset"):
            out.append(f"--cpuset-cpus={h['cpuset']}")
        return out

    # ------------------------------------------------------------------ images
    def ensure_image(self, image: str) -> str | None:
        """None when the image is usable, else the reason."""
        with self._pull_lock:
            have = self.docker("image", "inspect", image).returncode == 0
            pol = self.h.get("pull", "missing")
            if have and pol != "always":
                return None
            if pol == "never" and not have:
                return f"image_missing: {image} (harness.pull is never)"
            p = self.docker("pull", image, timeout=7200)
            if p.returncode:
                return "image_pull_failed: " + p.stderr.strip()[-300:]
            if not have:
                self.pulled.add(image)
            return None

    def cleanup(self) -> None:
        for n in list(self._mine):
            self.rm(n)
        if self.h.get("remove_pulled_images"):
            for img in sorted(self.pulled):
                self.docker("rmi", img, timeout=600)
            self.pulled.clear()

    # ------------------------------------------------------------------ containers
    def start(self, image: str, iid: str, kind: str, *, network: str | None = None, mounts=(),
              env: dict | None = None) -> tuple[str | None, str]:
        name = self.name(iid, kind)
        mnt = [x for host, cont in mounts for x in ("-v", f"{host}:{cont}:ro")]
        ev = [x for k, v in (env or {}).items() for x in ("-e", f"{k}={v}")]
        p = self.docker("run", "-d", "--init", "--name", name, *self.label_args(), *self.limit_args(network),
                        *mnt, *ev, "--entrypoint", "sleep", image, "infinity", timeout=600)
        if p.returncode:
            self.docker("rm", "-f", name)  # a failed start can leave the container in state Created
            return None, "container_start: " + p.stderr.strip()[-300:]
        self._mine.add(name)
        return name, ""

    def exec(self, name: str, script: str, timeout: float | None = None, workdir: str | None = None):
        """(rc, stdout, stderr, timed_out) of ``bash -c script`` in the container."""
        args = ["exec", *(["-w", workdir] if workdir else []), name, "bash", "-c", script]
        try:
            p = self.docker(*args, timeout=timeout)
            return p.returncode, p.stdout, p.stderr, False
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return None, out, "", True

    def cp_in(self, name: str, src: str | Path, dst: str) -> bool:
        return self.docker("cp", str(src), f"{name}:{dst}", timeout=600).returncode == 0

    def oom(self, name: str) -> bool:
        return self.docker("inspect", "-f", "{{.State.OOMKilled}}", name).stdout.strip() == "true"

    def rm(self, name: str) -> None:
        for _ in range(3):
            try:
                if self.docker("rm", "-f", name, timeout=300).returncode == 0:
                    break
            except subprocess.TimeoutExpired:
                pass
        self._mine.discard(name)

    def commit(self, name: str, image: str, changes: list[str]) -> bool:
        ch = [x for c in changes for x in ("--change", c)]
        return self.docker("commit", *ch, name, image, timeout=1800).returncode == 0
