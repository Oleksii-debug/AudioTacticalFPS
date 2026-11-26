"""Versioned, data-only production plans shared by the CLI and native studio."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import math
import re
import uuid

SCHEMA = 1
SAMPLE_RATES = (16000, 22050, 24000, 44100, 48000)
EXPORT_FORMATS = ("wav", "flac", "mp3", "ogg", "m4a")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(value, label, *, empty=False):
    if not isinstance(value, str) or "\0" in value or (not empty and not value.strip()):
        raise ValueError(f"{label} must be readable text without NUL characters.")
    # Refuse lone surrogates before they can fail in a worker or a checkpoint.
    value.encode("utf-8")
    return value


def _identity(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} must be a simple stable identifier.")
    return value


def _number(value, low, high, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return value


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError(f"Invalid or unsupported fields in {label}.")
    return value


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    provider: str
    voice: str
    pan: float = 0.0
    rate: float = 1.0
    volume: float = 1.0

    def validate(self):
        _identity(self.id, "Role ID")
        _text(self.name, "Role name")
        _identity(self.provider, "Local provider ID")
        _text(self.voice, "Voice ID")
        _number(self.pan, -1, 1, "Pan")
        _number(self.rate, 0.5, 2, "Speech rate")
        _number(self.volume, 0, 1, "Voice volume")


@dataclass(frozen=True)
class Line:
    id: str
    role: str
    text: str
    pause_after: float = 0.3
    source_start: int | None = None
    source_end: int | None = None

    def validate(self, source):
        _identity(self.id, "Line ID")
        _identity(self.role, "Line role")
        _text(self.text, "Spoken text")
        # A pause is a bounded operation. A production can contain arbitrarily
        # many such operations and has no ten-minute or one-hour final ceiling.
        _number(self.pause_after, 0, 86400, "Pause after line (seconds)")
        if (self.source_start is None) != (self.source_end is None):
            raise ValueError("Both source offsets must be supplied together.")
        if self.source_start is not None:
            if type(self.source_start) is not int or type(self.source_end) is not int or not (
                0 <= self.source_start < self.source_end <= len(source)):
                raise ValueError("Invalid original-script offsets.")
            if source[self.source_start:self.source_end] != self.text:
                raise ValueError("Spoken text differs from the recorded original-script span.")


@dataclass(frozen=True)
class MusicCue:
    mode: str = "procedural"
    mood: str = "calm"
    tempo: int = 90
    seed: int = 0
    gain: float = 0.10

    def validate(self):
        if self.mode != "procedural":
            raise ValueError("This renderer supports explicitly labelled local procedural music only. "
                             "MusicGen and ACE-Step need separately verified local engines and weights.")
        if self.mood not in ("calm", "tense", "bright"):
            raise ValueError("Choose calm, tense, or bright procedural music.")
        if type(self.tempo) is not int or not 40 <= self.tempo <= 240:
            raise ValueError("Music tempo must be an integer between 40 and 240 BPM.")
        if type(self.seed) is not int or not 0 <= self.seed <= 2**31 - 1:
            raise ValueError("Invalid music seed.")
        _number(self.gain, 0, 0.5, "Music gain")


@dataclass(frozen=True)
class Scene:
    id: str
    title: str
    lines: tuple[Line, ...]
    pause_before: float = 0.0
    music: MusicCue | None = None

    def validate(self, source, roles):
        _identity(self.id, "Scene ID")
        _text(self.title, "Scene title")
        _number(self.pause_before, 0, 86400, "Scene opening pause (seconds)")
        if not self.lines:
            raise ValueError("Every scene needs at least one spoken line.")
        seen = set()
        for line in self.lines:
            line.validate(source)
            if line.id in seen or line.role not in roles:
                raise ValueError("Duplicate line ID or unknown speaker.")
            seen.add(line.id)
        if self.music is not None:
            self.music.validate()


@dataclass(frozen=True)
class AudioProject:
    id: str
    title: str
    source_text: str
    roles: tuple[Role, ...]
    scenes: tuple[Scene, ...]
    sample_rate: int = 24000
    export_format: str = "wav"
    stems: bool = True
    local_only: bool = True
    schema_version: int = SCHEMA
    interpret_speaker_syntax: bool = True

    def validate(self):
        if type(self.schema_version) is not int or self.schema_version != SCHEMA:
            raise ValueError("Unsupported audio project schema.")
        _identity(self.id, "Project ID")
        _text(self.title, "Project title")
        _text(self.source_text, "Original script")
        if type(self.sample_rate) is not int or self.sample_rate not in SAMPLE_RATES:
            raise ValueError("Unsupported production sample rate.")
        if self.export_format not in EXPORT_FORMATS:
            raise ValueError("Unsupported audio export format.")
        if self.local_only is not True or type(self.stems) is not bool or type(self.interpret_speaker_syntax) is not bool:
            raise ValueError("This version only executes local-only plans with explicit stem settings.")
        if not self.roles or not self.scenes:
            raise ValueError("A project needs voices and at least one scene.")
        names = set()
        for role in self.roles:
            role.validate()
            if role.id in names:
                raise ValueError("Duplicate role ID.")
            names.add(role.id)
        scenes = set()
        for scene in self.scenes:
            scene.validate(self.source_text, names)
            if scene.id in scenes:
                raise ValueError("Duplicate scene ID.")
            scenes.add(scene.id)
        return self

    def to_dict(self):
        self.validate()
        # The interchange contract uses JSON arrays, including in-memory callers.
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @property
    def sha256(self):
        return digest(self.to_dict())

    @property
    def source_sha256(self):
        return text_digest(self.source_text)

    @classmethod
    def from_dict(cls, value):
        try:
            data = dict(_object(value, cls.__dataclass_fields__, "project"))
            roles = data.get("roles")
            scenes = data.get("scenes")
            if not isinstance(roles, list) or not isinstance(scenes, list):
                raise ValueError("Project roles and scenes must be lists.")
            data["roles"] = tuple(Role(**_object(v, Role.__dataclass_fields__, "role")) for v in roles)
            converted = []
            for item in scenes:
                item = dict(_object(item, Scene.__dataclass_fields__, "scene"))
                if not isinstance(item.get("lines"), list):
                    raise ValueError("Scene lines must be a list.")
                item["lines"] = tuple(Line(**_object(v, Line.__dataclass_fields__, "line")) for v in item["lines"])
                if item.get("music") is not None:
                    item["music"] = MusicCue(**_object(item["music"], MusicCue.__dataclass_fields__, "music"))
                converted.append(Scene(**item))
            data["scenes"] = tuple(converted)
            return cls(**data).validate()
        except (TypeError, KeyError) as exc:
            raise ValueError("Incomplete or invalid audio project.") from exc


def parse_script(script: str, voices, *, title="Audio project", project_id=None,
                 music=None, sample_rate=24000, export_format="wav", stems=True):
    """Prepare a faithful plan from explicit speaker and chapter notation.

    '# Chapter', 'Speaker: exact spoken words', and '[pause 2]' are the entire
    deterministic grammar. Other text is narration, never tool instructions.
    Voices are actual discovered Voice objects, not names inferred by a model.
    """
    _text(script, "Script")
    voices = tuple(voices)
    if not voices:
        raise ValueError("No supported local voice is available. Configure a local engine first.")
    roles, role_by_name, scenes = [], {}, []
    scene_title, lines, pause_before, offset = "Chapter 1", [], 0.0, 0

    def role_for(name):
        key = name.casefold()
        if key not in role_by_name:
            identity = f"role-{len(roles) + 1}"
            voice = voices[len(roles) % len(voices)]
            roles.append(Role(identity, name, voice.provider, voice.id))
            role_by_name[key] = identity
        return role_by_name[key]

    def finish():
        nonlocal lines, pause_before
        if lines:
            scenes.append(Scene(f"scene-{len(scenes) + 1}", scene_title,
                                tuple(lines), pause_before, music))
        lines, pause_before = [], 0.0

    for raw in script.splitlines(keepends=True):
        text = raw.rstrip("\r\n")
        trimmed = text.strip()
        if trimmed.startswith("# "):
            finish()
            scene_title = trimmed[2:].strip() or f"Chapter {len(scenes) + 1}"
        elif match := re.fullmatch(r"\[pause ([0-9]+(?:\.[0-9]+)?)\]", trimmed, re.I):
            pause = _number(float(match.group(1)), 0, 86400, "Pause")
            if lines:
                lines[-1] = replace(lines[-1], pause_after=pause)
            else:
                pause_before += pause
        elif trimmed:
            role, begin = "Narrator", len(text) - len(text.lstrip())
            speaker = re.match(r"([^:\r\n]{1,60}):[ \t]*", text[begin:])
            if speaker and not speaker.group(1).strip().startswith(("http", "file", "#", "[")):
                role = speaker.group(1).strip()
                begin += speaker.end()
            end = len(text.rstrip())
            if begin < end:
                lines.append(Line(f"line-{len(lines) + 1}", role_for(role), text[begin:end],
                                  source_start=offset + begin, source_end=offset + end))
        offset += len(raw)
    finish()
    return AudioProject(project_id or uuid.uuid4().hex, title, script, tuple(roles),
                        tuple(scenes), sample_rate, export_format, stems).validate()


def split_speech(text: str, *, maximum=1800):
    """Bound each engine call while retaining every original Unicode character."""
    if type(maximum) is not int or maximum < 128:
        raise ValueError("Speech chunk size must be at least 128 characters.")
    chunks = []
    while len(text) > maximum:
        window = text[:maximum]
        candidates = [m.end() for m in re.finditer(r"[.!?。！？][ \t\n]+|\n|[ \t]+", window)]
        boundary = next((p for p in reversed(candidates) if p >= maximum // 2), maximum)
        chunks.append(text[:boundary])
        text = text[boundary:]
    if text:
        chunks.append(text)
    return tuple(chunks)
