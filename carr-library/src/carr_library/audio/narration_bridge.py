"""Explicit snapshots of saved reading copies for literal audio production.

The existing reading-copy producer owns validation, storage and revision history.
This adapter never saves it, applies its pronunciation rules, reads a recovery
draft, or grants quotation authority. Audio editing starts a separate history.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from importlib import import_module
import re
from typing import TYPE_CHECKING

from ..reader.narration_text import NarrationDraft, text_digest

if TYPE_CHECKING:
    from ..reader.narration import NarrationProject
    from ..reader.reading_state import ReadingLocation


class ReadingCopyUnavailable(RuntimeError):
    """The optional integrated reading-copy producer is not installed."""


def _producer():
    try:
        narration = import_module('carr_library.reader.narration')
    except ModuleNotFoundError as exc:
        if exc.name not in ('carr_library.reader.narration', 'carr_library.reader.reading_state'):
            raise
        raise ReadingCopyUnavailable(
            'Saved reading-copy support is unavailable. Install the integrated reading editor first.') from None
    return narration


def _check(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Reading-copy transfer cancelled; no project was changed.')


def _workspace(workspace):
    if not isinstance(workspace, str) or not workspace.strip() or len(workspace) > 512:
        raise ValueError('Choose the active library before selecting a saved reading copy.')


def _in_scope(project, workspace):
    location = project.source_location
    if location is not None and location.workspace != workspace:
        raise ValueError('This reading copy belongs to another library.')


@dataclass(frozen=True)
class ReadingSnapshot:
    """Detached text and historical provenance; never a fresh source attestation."""
    project_id: str
    title: str
    revision: int
    project_sha256: str
    original_text: str = field(repr=False)
    original_sha256: str
    reading_text: str = field(repr=False)
    reading_sha256: str
    source_location: ReadingLocation | None

    @property
    def source_id(self):
        return f'reading-copy:{self.project_id}:revision:{self.revision}:sha256:{self.project_sha256}'

    def new_draft(self):
        # Make a fresh editable object each time; no mutable producer state is
        # shared with Audio Studio and later producer revisions cannot retarget it.
        if (text_digest(self.original_text) != self.original_sha256
                or text_digest(self.reading_text) != self.reading_sha256):
            raise ValueError('The reading snapshot failed its content check.')
        draft = NarrationDraft(self.original_text, source_id=self.source_id)
        draft.edit_text(self.reading_text, reason=f'Snapshot of saved reading revision {self.revision}')
        return draft

    def source_reference(self):
        return {
            'kind': 'saved-reading-copy-snapshot', 'version': 1,
            'reading_project_id': self.project_id, 'reading_revision': self.revision,
            'reading_project_sha256': self.project_sha256,
            'original_sha256': self.original_sha256, 'reading_sha256': self.reading_sha256,
            'source_location': asdict(self.source_location) if self.source_location is not None else None,
            'quotation_authority': False,
            'provenance': 'Captured reading-copy provenance; not a fresh verification of the original source.',
        }

    def production_plan(self, *, voice='sapi5:default', language='en'):
        from .production import plan_narration
        # Source-looking directives and speaker labels remain literal speech.
        self.new_draft()
        return plan_narration(self.reading_text, title=self.title, voice=voice, language=language)


def snapshot_reading_project(project: NarrationProject) -> ReadingSnapshot:
    """Validate the actual producer type and copy exactly its selected revision.

    project_sha256 identifies the producer's canonical complete project content,
    not an arbitrary on-disk JSON layout or an assertion of publisher identity.
    The audio snapshot contains only the selected revision and original, not a
    second editable copy of the reading editor's history or pronunciation rules.
    """
    producer = _producer()
    raw = producer.encode_project(project)
    captured = producer.decode_project(raw)
    revision = captured.revisions[captured.cursor]
    return ReadingSnapshot(captured.id, captured.title, revision.number, sha256(raw).hexdigest(),
                           captured.original_text, captured.original_sha256,
                           revision.text, text_digest(revision.text), captured.source_location)


@dataclass(frozen=True)
class SavedReadingCopy:
    project_id: str
    title: str
    revision: int
    project_sha256: str
    independent: bool

    @property
    def label(self):
        scope = 'independent text' if self.independent else 'current library'
        return f'{self.title} — revision {self.revision} — {scope} — {self.project_id[:8]}'


def saved_reading_copies(root, *, workspace, cancelled=lambda: False):
    """List committed projects in one library, plus clearly labelled authored text."""
    _workspace(workspace)
    _check(cancelled)
    producer = _producer()
    rows = producer.NarrationStore(root).list_projects(workspace=workspace)
    choices = []
    for row in rows:
        _check(cancelled)
        if not row['saved']:
            continue
        project = row['project']
        _in_scope(project, workspace)
        snapshot = snapshot_reading_project(project)
        choices.append(SavedReadingCopy(snapshot.project_id, snapshot.title, snapshot.revision,
            snapshot.project_sha256, snapshot.source_location is None))
    _check(cancelled)
    return tuple(choices)


def load_saved_reading_snapshot(root, project_id, *, workspace, expected_project_sha256,
                                cancelled=lambda: False):
    """Reload and check the exact selection after a chooser or other delay."""
    _workspace(workspace)
    if (not isinstance(expected_project_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_project_sha256)):
        raise ValueError('Select a saved reading copy with its content digest.')
    _check(cancelled)
    project, _file_digest = _producer().NarrationStore(root).load(project_id, draft=False)
    _check(cancelled)
    _in_scope(project, workspace)
    snapshot = snapshot_reading_project(project)
    if snapshot.project_sha256 != expected_project_sha256:
        raise ValueError('The reading copy changed after selection. Reopen the chooser to review its new revision.')
    _check(cancelled)
    return snapshot
