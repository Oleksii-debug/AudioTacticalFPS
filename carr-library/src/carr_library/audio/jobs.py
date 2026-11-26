"""Durable content-addressed local render checkpoints, independent of the GUI."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

from .project import (atomic_bytes, contained_file, file_sha256, json_bytes,
                      local_directory, read_json, _check_cancel, _fsync_directory)


@dataclass(frozen=True)
class Artifact:
    path: Path
    sha256: str
    reused: bool
    bytes: int


class RenderJournal:
    """Caller holds the project render lock; backup may read atomic snapshots."""
    def __init__(self, root, *, cancel=None, progress=None):
        self.root = local_directory(root)
        self.cache = local_directory(self.root / "cache", create=True)
        self.path = self.root / "render-state.json"
        self.cancel, self.progress = cancel, progress or (lambda value: None)
        self.created, self.reused = 0, 0
        if self.path.exists():
            self.state = read_json(self.path)
            if not isinstance(self.state, dict) or self.state.get("schema_version") != 1 or not isinstance(self.state.get("entries"), dict):
                raise ValueError("Invalid audio render journal; completed files were preserved.")
        else:
            self.state = {"schema_version": 1, "entries": {}, "status": "ready", "current": None}

    def save(self):
        atomic_bytes(self.path, json_bytes(self.state), overwrite=self.path.exists())

    def start(self, project):
        self.state.update(status="running", project_sha256=project.sha256,
                          source_sha256=project.source_sha256, current=None,
                          started_at=time.time(), message="Rendering local audio.")
        self.save()

    def report(self, message, **values):
        self.progress({"message": message, "created": self.created, "reused": self.reused, **values})

    def run(self, key, kind, producer, *, extension="wav"):
        _check_cancel(self.cancel)
        if not re.fullmatch(r"[0-9a-f]{64}", key) or extension not in ("wav", "flac", "mid"):
            raise ValueError("Invalid checkpoint identity or format.")
        relative = f"cache/{key}.{extension}"
        target = self.cache / f"{key}.{extension}"
        old = self.state["entries"].get(key)
        if isinstance(old, dict) and old.get("status") == "complete" and old.get("path") == relative:
            try:
                existing = contained_file(self.root, relative)
                actual = file_sha256(existing)
            except FileNotFoundError:
                actual = None
            if actual is not None and actual == old.get("sha256"):
                self.reused += 1
                self.report(f"Reused verified {kind}.", operation=key)
                return Artifact(existing, actual, True, existing.stat().st_size)
        # A failed checksum is never accepted just because a previous job said
        # complete. Keep the damaged internal cache object for diagnosis.
        if target.exists() or target.is_symlink():
            contained_file(self.root, relative)
            quarantine = local_directory(self.root / "damaged-checkpoints", create=True)
            os.rename(target, quarantine / f"{key}-{uuid.uuid4().hex}.{extension}")
        self.state.update(status="running", current=key, message=f"Rendering {kind}.")
        self.state["entries"][key] = {"status": "running", "path": relative, "kind": kind}
        self.save()
        self.report(f"Rendering {kind}.", operation=key)
        stage = Path(tempfile.mkdtemp(prefix=".render-", dir=self.cache))
        begin = time.monotonic()
        try:
            output = stage / f"output.{extension}"
            producer(output)
            _check_cancel(self.cancel)
            contained_file(stage, output.name)
            if output.stat().st_size == 0:
                raise ValueError("The engine produced an empty artifact.")
            checksum = file_sha256(output)
            with output.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.link(output, target)
            _fsync_directory(self.cache)
            size = target.stat().st_size
            self.state["entries"][key] = {"status": "complete", "path": relative, "kind": kind,
                "sha256": checksum, "bytes": size, "elapsed_seconds": round(time.monotonic() - begin, 6)}
            self.state["current"] = None
            self.save()
            self.created += 1
            return Artifact(target, checksum, False, size)
        except BaseException as exc:
            self.state["entries"][key]["status"] = "interrupted" if isinstance(exc, (InterruptedError, KeyboardInterrupt)) or "Cancel" in type(exc).__name__ else "failed"
            self.state.update(status=self.state["entries"][key]["status"], current=None,
                              message=f"{kind} stopped ({type(exc).__name__}); completed checkpoints remain available.")
            self.save()
            raise
        finally:
            shutil.rmtree(stage)

    def finish(self, manifest):
        self.state.update(status="complete", current=None, completed_at=time.time(),
                          manifest=str(Path(manifest).relative_to(self.root)),
                          message="Local audio artifacts verified and published.")
        self.save()
        self.report(self.state["message"], manifest=str(manifest))

    def fail(self, exc):
        self.state.update(status="interrupted" if isinstance(exc, (InterruptedError, KeyboardInterrupt)) or "Cancel" in type(exc).__name__ else "failed",
                          current=None, message=f"Production stopped ({type(exc).__name__}); render again to resume verified work.")
        self.save()
