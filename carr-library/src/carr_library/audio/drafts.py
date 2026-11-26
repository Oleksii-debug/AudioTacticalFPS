"""Private per-editor recovery drafts, separate from committed audio projects."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
import re
import time
import uuid

from .contracts import Role, digest
from .project import (MAX_PROJECT_BYTES, _check_cancel, _fsync_directory, _local_path,
                      _read_document, atomic_bytes, contained_file, json_bytes,
                      local_directory, project_lock)

_SESSION = re.compile(r"[0-9a-f]{32}\Z")
_CHECKSUM = re.compile(r"[0-9a-f]{64}\Z")
DIRECTORY = "editor-drafts"
MAX_DRAFTS = 128
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024


def draft_path(relative):
    return isinstance(relative, str) and bool(re.fullmatch(r"editor-drafts/[0-9a-f]{32}\.json", relative))


def _identity(value):
    if not isinstance(value, str) or not _SESSION.fullmatch(value):
        raise ValueError("Invalid recovery-draft session identity.")
    return value


def _settings(value):
    allowed = {"interpret_speakers", "stems", "export_format", "music_mood", "music_changed",
               "ffmpeg", "ffprobe", "speaker_name", "voice_provider", "voice_id", "pan", "rate", "volume"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unsupported recovery-draft settings.")
    result = dict(value)
    for name in ("interpret_speakers", "stems", "music_changed"):
        if name in result and type(result[name]) is not bool:
            raise ValueError("Invalid recovery-draft checkbox setting.")
    if "export_format" in result and result["export_format"] not in ("wav", "flac", "mp3", "ogg", "m4a"):
        raise ValueError("Invalid recovery-draft export format.")
    if "music_mood" in result and result["music_mood"] not in (None, "calm", "tense", "bright"):
        raise ValueError("Invalid recovery-draft music choice.")
    for name in ("ffmpeg", "ffprobe", "speaker_name", "voice_provider", "voice_id"):
        if name in result and (not isinstance(result[name], str) or len(result[name]) > 8192):
            raise ValueError("Invalid recovery-draft text setting.")
    for name, minimum, maximum in (("pan", -1, 1), ("rate", .5, 2), ("volume", 0, 1)):
        if name in result and (type(result[name]) not in (int, float) or not math.isfinite(result[name]) or not minimum <= result[name] <= maximum):
            raise ValueError("Invalid recovery-draft numeric setting.")
    return result


@dataclass(frozen=True)
class EditorDraft:
    session_id: str
    revision: int
    base_project_sha256: str | None
    title: str
    text: str
    settings: dict
    roles: tuple[Role, ...]
    saved_at_ns: int

    def payload(self):
        _identity(self.session_id)
        if type(self.revision) is not int or not 1 <= self.revision < 2**63 or type(self.saved_at_ns) is not int or not 1 <= self.saved_at_ns < 2**63:
            raise ValueError("Invalid recovery-draft version or timestamp.")
        if self.base_project_sha256 is not None and (not isinstance(self.base_project_sha256, str) or not _CHECKSUM.fullmatch(self.base_project_sha256)):
            raise ValueError("Invalid recovery-draft base revision.")
        # Blank text, NUL and invalid *script syntax* must survive failed Save.
        # Unicode encoding and serialized size remain bounded storage contracts.
        if not isinstance(self.text, str) or not isinstance(self.title, str) or len(self.title) > 8192:
            raise ValueError("Invalid recovery-draft editor text.")
        self.text.encode("utf-8")
        self.title.encode("utf-8")
        if len(self.text.encode("utf-8")) > MAX_PROJECT_BYTES:
            raise ValueError("Recovery draft exceeds the configured text budget.")
        names = set()
        for role in self.roles:
            role.validate()
            if role.name.casefold() in names:
                raise ValueError("Duplicate recovery-draft speaker setting.")
            names.add(role.name.casefold())
        return {**asdict(self), "settings": _settings(self.settings),
                "roles": [asdict(role) for role in self.roles]}


def decode_draft(document, *, expected_session=None):
    if not isinstance(document, dict) or set(document) != {"schema_version", "kind", "payload", "sha256"} or type(document["schema_version"]) is not int or document["schema_version"] != 1 or document["kind"] != "carr-audio-editor-draft":
        raise ValueError("Invalid recovery-draft envelope.")
    payload = document["payload"]
    fields = set(EditorDraft.__dataclass_fields__)
    try:
        if not isinstance(payload, dict) or set(payload) != fields or document["sha256"] != digest(payload):
            raise ValueError("Recovery-draft checksum or fields are invalid.")
        data = dict(payload)
        if not isinstance(data["roles"], list):
            raise ValueError("Invalid recovery-draft speaker list.")
        data["roles"] = tuple(Role(**role) for role in data["roles"])
        draft = EditorDraft(**data)
        draft.payload()
    except (TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("Invalid recovery-draft content.") from exc
    if expected_session is not None and draft.session_id != _identity(expected_session):
        raise ValueError("Recovery draft belongs to a different session filename.")
    return draft


def load_draft_file(root, relative):
    if not draft_path(relative):
        raise ValueError("Recovery-draft path must use its exact session identity.")
    document, raw = _read_document(contained_file(root, relative))
    return decode_draft(document, expected_session=Path(relative).stem), raw


class DraftStore:
    def __init__(self, root):
        self.root = local_directory(root)
        self.directory = self.root / DIRECTORY

    @classmethod
    def reserve(cls, root):
        """Reserve a user-chosen NEW folder, without inventing a valid project."""
        root = _local_path(root)
        local_directory(root.parent, create=True)
        root.mkdir(mode=0o700, exist_ok=False)
        return cls(root)

    def save(self, session_id, *, text, title, settings, roles=(), base_project_sha256=None):
        session_id = _identity(session_id)
        with project_lock(self.root, "editor-drafts"):
            directory = local_directory(self.directory, create=True)
            path = directory / f"{session_id}.json"
            revision = 1
            if path.exists() or path.is_symlink():
                previous, _ = load_draft_file(self.root, f"{DIRECTORY}/{session_id}.json")
                revision = previous.revision + 1
            draft = EditorDraft(session_id, revision, base_project_sha256, title, text,
                                dict(settings), tuple(roles), time.time_ns())
            payload = draft.payload()
            data = json_bytes({"schema_version": 1, "kind": "carr-audio-editor-draft",
                               "payload": payload, "sha256": digest(payload)})
            if len(data) > MAX_PROJECT_BYTES:
                raise ValueError("Recovery draft exceeds the configured serialized-size budget.")
            atomic_bytes(path, data, overwrite=path.exists())
            return draft

    def read(self, session_id):
        session_id = _identity(session_id)
        with project_lock(self.root, "editor-drafts"):
            return load_draft_file(self.root, f"{DIRECTORY}/{session_id}.json")[0]

    def snapshots(self, *, cancel=None):
        """A coherent, validated collection for backup and explicit recovery."""
        _check_cancel(cancel)
        with project_lock(self.root, "editor-drafts"):
            _local_path(self.directory)
            if not self.directory.exists():
                return ()
            directory = local_directory(self.directory)
            result = []
            total = 0
            for index, path in enumerate(directory.iterdir()):
                _check_cancel(cancel)
                if index >= MAX_DRAFTS * 2:
                    raise ValueError("Recovery draft directory exceeds the configured entry budget.")
                if path.name.startswith(".audio-save-"):
                    continue  # Uncommitted atomic-save staging from a crash.
                relative = f"{DIRECTORY}/{path.name}"
                draft, raw = load_draft_file(self.root, relative)
                total += len(raw)
                if len(result) >= MAX_DRAFTS or total > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Recovery draft collection exceeds the configured resource budget.")
                result.append((relative, draft, raw))
            return tuple(sorted(result, key=lambda row: row[0]))

    def list(self):
        return tuple(sorted((draft for _, draft, _ in self.snapshots()),
                            key=lambda draft: draft.saved_at_ns, reverse=True))

    def discard(self, session_id):
        session_id = _identity(session_id)
        with project_lock(self.root, "editor-drafts"):
            path = _local_path(self.directory / f"{session_id}.json")
            if not path.exists() and not path.is_symlink():
                return False
            load_draft_file(self.root, f"{DIRECTORY}/{session_id}.json")
            path.unlink()
            _fsync_directory(self.directory)
            return True


def new_session_id():
    return uuid.uuid4().hex
