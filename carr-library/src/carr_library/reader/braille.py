"""Explicit retained-source derivative export through optional local Liblouis.

A reviewed selection binds original source positions. It is not quotation,
execution, tactile-quality or embosser-layout authority.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from ..application import LibraryApp
from ..durable_state import local_path, regular
from ..research.export_file import ExportDestination, capture_export_destination, write_binary_export
from ..integrations import liblouis
from ..operations.broker import ResourceBroker
from ..operations.models import ResourceRequest
from ..speech._io import check_cancelled, local_file
from ..speech.source import read_source_range
from .reading_desk import ReadingDesk


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_hash(source):
    return sha256(_canonical(asdict(source)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BrailleSelection:
    workspace: str
    edition: str
    revision: str
    topic: str | None
    first: int
    last: int
    table: str
    source_sha256: str
    text_sha256: str
    characters: int
    title: str
    warnings: tuple[str, ...]


class BrailleSourceExport:
    """One original LibraryApp object/root, no queue, job store or new reader."""
    def __init__(self, library, *, guard=lambda: None):
        if not isinstance(library, LibraryApp) or not callable(guard):
            raise TypeError("BRF export needs the original library and its current-view guard.")
        self.library, self.guard = library, guard
        self.root = local_path(library.root, exists=True)
        info = regular(self.root, directory=True)
        self.root_identity = info.st_dev, info.st_ino

    def _current(self, cancelled):
        check_cancelled(cancelled)
        self.guard()
        current = local_path(self.library.root, exists=True)
        info = regular(current, directory=True)
        if current != self.root or (info.st_dev, info.st_ino) != self.root_identity:
            raise PermissionError("Reopen BRF export in its original library.")
        check_cancelled(cancelled)

    def review(self, workspace, edition, first, last, table, *, topic=None,
               revision=None, cancelled=lambda: False):
        self._current(cancelled)
        if type(table) is not str or table not in liblouis.TABLES:
            raise ValueError("Choose English UEB grade-1 or Ukrainian for this plain BRF derivative.")
        desk = ReadingDesk(self.library, workspace, edition, topic=topic,
            revision=revision, cancelled=cancelled)
        source = read_source_range(desk, first, last, guard=lambda: self._current(cancelled),
            cancelled=cancelled, max_characters=liblouis.MAX_TEXT_CHARACTERS)
        liblouis.selected_text(source.text)
        self._current(cancelled)
        return BrailleSelection(source.workspace, source.edition, source.revision, topic,
            first, last, table, _source_hash(source), sha256(source.text.encode("utf-8")).hexdigest(),
            len(source.text), source.title, source.warnings)

    def _source(self, selection, cancelled):
        if type(selection) is not BrailleSelection:
            raise TypeError("Review an exact retained source range before BRF export.")
        self._current(cancelled)
        desk = ReadingDesk(self.library, selection.workspace, selection.edition,
            topic=selection.topic, revision=selection.revision, cancelled=cancelled)
        source = read_source_range(desk, selection.first, selection.last,
            guard=lambda: self._current(cancelled), cancelled=cancelled,
            max_characters=liblouis.MAX_TEXT_CHARACTERS)
        current = BrailleSelection(source.workspace, source.edition, source.revision, selection.topic,
            selection.first, selection.last, selection.table, _source_hash(source),
            sha256(source.text.encode("utf-8")).hexdigest(), len(source.text), source.title, source.warnings)
        if current != selection:
            raise ValueError("The selected original source changed; review its exact range again.")
        liblouis.selected_text(source.text)
        return source

    def export(self, selection, runtime_archive, destination, *, approval=None,
               cancelled=lambda: False):
        """Publish one new BRF, or replace exactly one explicitly approved file.

        The native stage is cleaned before publication. A post-commit export
        cleanup warning is retained in the successful receipt, never retried.
        """
        self._current(cancelled)
        source = self._source(selection, cancelled)
        if selection.table not in liblouis.TABLES:
            raise ValueError("The reviewed BRF table is unavailable.")
        chosen = Path(destination)
        if chosen.suffix.lower() != ".brf":
            raise ValueError("Choose an explicit .brf filename for the derivative.")
        captured = capture_export_destination(chosen, private_root=self.root,
                                              cancelled=lambda: self._cancelled(cancelled))
        if approval is not None:
            if type(approval) is not ExportDestination or approval != captured:
                raise ValueError("The approved BRF destination changed; choose and confirm it again.")
        elif not captured.require_absent:
            raise FileExistsError("Choose a new BRF destination or explicitly confirm the existing file.")
        self._current(cancelled)
        runtime_root = self.root / "runtime"
        if not runtime_root.exists():
            runtime_root.mkdir(mode=0o700)
        runtime_root = local_path(runtime_root, exists=True)
        regular(runtime_root, directory=True)
        broker = ResourceBroker.for_app(self.root, cancelled=lambda: self._cancelled(cancelled))
        request = ResourceRequest(cpu_slots=1, memory_bytes=512 * 1024 * 1024,
            disk_bytes=liblouis.MAX_STAGE_BYTES, disk_path=runtime_root,
            additional_disks=((captured.target.parent, liblouis.MAX_BRF_BYTES),))
        # The temporary context encloses admission: ResourceBroker settles the
        # existing CleanupOwner before its finally can remove native inputs.
        with TemporaryDirectory(prefix="braille-", dir=runtime_root) as directory:
            stage = Path(directory)
            with broker.reserve(request, operation_id="braille:" + uuid4().hex,
                    cancelled=lambda: self._cancelled(cancelled), timeout=30, run_timeout=120) as lease:
                self._source(selection, cancelled)
                lease.checkpoint()
                payload, runtime = liblouis.render_selected(source.text, selection.table,
                    runtime_archive, stage, cancelled=lambda: self._cancelled(lease.cancelled))
                lease.checkpoint()
                self._current(cancelled)
        self._source(selection, cancelled)
        def publication_check():
            # Re-attest the exact retained selection at every pre-commit writer
            # checkpoint, including after its private file has been flushed.
            self._source(selection, cancelled)
            return False
        receipt = write_binary_export(captured.requested, payload, private_root=self.root,
            cancelled=publication_check, require_absent=captured.require_absent,
            approval=captured)
        # No cancellation, changed-view callback or optional observer follows
        # a committed destination. The original source is never rewritten.
        return {"schema": "carr-brf-export/1", "status": "complete", "path": str(receipt.path),
            "size": len(payload), "sha256": sha256(payload).hexdigest(),
            "source": asdict(selection), "runtime": runtime,
            "cleanup_warning": receipt.cleanup_warning, "quotation_authority": False,
            "layout": "logical lines only; no pagination, navigation, images or embosser settings",
            "tactile_quality": "NOT_RUN", "nvda": "NOT_RUN"}

    def _cancelled(self, cancelled):
        self._current(cancelled)
        return False


def main(argv=None):
    """Called by the existing managed application entrypoint braille route."""
    parser = argparse.ArgumentParser(description="Explicit local plain BRF derivative export")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Verify the selected official archive without loading it")
    inspect.add_argument("--runtime-archive", required=True)
    export = commands.add_parser("export", help="Read an exact original range and publish a new .brf file")
    for name in ("root", "workspace", "edition", "runtime-archive", "output"):
        export.add_argument("--" + name, required=True)
    export.add_argument("--first", required=True, type=int)
    export.add_argument("--last", required=True, type=int)
    export.add_argument("--table", required=True, choices=tuple(liblouis.TABLES))
    export.add_argument("--topic")
    export.add_argument("--revision")
    args = parser.parse_args(argv)
    if args.command == "inspect":
        report = liblouis.inspect_archive(args.runtime_archive)
    else:
        root = local_path(args.root, exists=True)
        regular(root, directory=True)
        # A typo must not create a new empty library as a side effect.
        local_file(root / "library.sqlite")
        service = BrailleSourceExport(LibraryApp(root))
        selection = service.review(args.workspace, args.edition, args.first, args.last,
            args.table, topic=args.topic, revision=args.revision)
        report = service.export(selection, args.runtime_archive, args.output)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0
