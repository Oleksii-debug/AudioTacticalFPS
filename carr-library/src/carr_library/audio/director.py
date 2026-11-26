"""Execute a validated local production plan, with real cached engine outputs."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from .contracts import AudioProject, digest, split_speech, text_digest
from .jobs import RenderJournal
from .project import (ProjectStore, atomic_bytes, contained_file, file_sha256,
                      json_bytes, local_directory, project_lock, read_json,
                      _check_cancel, _fsync_directory, publish_directory)

_MUSIC_BUS = "__background_music__"  # not a valid Role.id; cannot collide with a speaker


@dataclass(frozen=True)
class RenderResult:
    manifest: Path
    master: Path
    frames: int
    sample_rate: int
    created: int
    reused: int

    @property
    def duration_seconds(self):
        return self.frames / self.sample_rate


def _stamp(frame, rate, *, vtt=False):
    milliseconds = round(frame * 1000 / rate)
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{'.' if vtt else ','}{ms:03d}"


def _subtitles(lines, rate, *, vtt=False):
    blocks = ["WEBVTT\n"] if vtt else []
    for number, row in enumerate(lines, 1):
        # A source line can contain subtitle syntax. Keep it plain text and
        # prevent it from creating a second timing record or markup cue.
        text = row["text"].replace("\r", " ").replace("\n", " ")
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        blocks.append(f"{number}\n{_stamp(row['start_frame'], rate, vtt=vtt)} --> "
                      f"{_stamp(row['end_frame'], rate, vtt=vtt)}\n{text}\n")
    return "\n".join(blocks)


def _check_space(root, required):
    # Capacity follows actual measured audio duration, not a fixed book limit.
    if shutil.disk_usage(root).free < required + 32 * 1024 * 1024:
        raise OSError("Insufficient free disk space for the next audio step. "
                      "Free space and render again; completed checkpoints are retained.")


def _copy_verified(source, destination, checksum):
    shutil.copyfile(source, destination)
    if file_sha256(destination) != checksum:
        raise ValueError("Audio changed while publishing; no completed production was published.")


def _matches_runtime(manifest, backend_identity, selected_voices):
    """Cheap admission check; full artifact verification still follows it."""
    if not isinstance(manifest, dict) or manifest.get("backend") != backend_identity:
        return False
    rows = manifest.get("voices")
    if not isinstance(rows, list) or len(rows) != len(selected_voices):
        return False
    actual = []
    for row in rows:
        if not isinstance(row, dict):
            return False
        actual.append(tuple(row.get(key) for key in ("role", "provider", "voice", "engine_identity")))
    return tuple(actual) == selected_voices


def verify_production(directory, *, expected_project_sha256=None, backend=None, cancel=None):
    """Verify a complete plan-bound production, including actual PCM timelines.

    A checksum manifest is not by itself evidence of a complete audio project.
    Required artifacts, source bytes, scene/line metadata and audio dimensions
    must agree with the exported validated plan before a result can be reused.
    """
    directory = local_directory(directory)
    _check_cancel(cancel)
    manifest_file = contained_file(directory, "production.json")
    manifest = read_json(manifest_file)
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1 or manifest.get("status") != "complete" or manifest.get("local_only") is not True:
        raise ValueError("Unsupported or incomplete production manifest.")
    if expected_project_sha256 is not None and manifest.get("project_sha256") != expected_project_sha256:
        raise ValueError("Production belongs to a different project revision.")
    files = manifest.get("files")
    if not isinstance(files, dict) or "project.json" not in files:
        raise ValueError("Production does not contain its validated plan.")
    project = AudioProject.from_dict(read_json(contained_file(directory, "project.json")))
    if manifest.get("project_sha256") != project.sha256 or manifest.get("source_sha256") != project.source_sha256:
        raise ValueError("Production plan or immutable script identity does not match its manifest.")
    rate, frames = project.sample_rate, manifest.get("frames")
    if type(frames) is not int or frames <= 0 or type(manifest.get("sample_rate")) is not int or manifest["sample_rate"] != rate or type(manifest.get("channels")) is not int or manifest["channels"] != 2:
        raise ValueError("Invalid production audio dimensions.")
    duration = manifest.get("duration_seconds")
    if type(duration) not in (int, float) or not math.isfinite(duration) or abs(duration - frames / rate) > 1 / rate:
        raise ValueError("Production duration differs from the declared frame count.")
    required = {"master.wav", "script.txt", "project.json", "chapters.json", "timing.json",
                "subtitles.srt", "subtitles.vtt"}
    chapter_names = [f"chapter-{number:04d}.wav" for number in range(1, len(project.scenes) + 1)]
    required.update(chapter_names)
    stem_names = []
    if project.stems:
        stem_names = [f"stem-{role.id}.wav" for role in project.roles]
        if any(scene.music for scene in project.scenes):
            stem_names.append(f"stem-{_MUSIC_BUS}.wav")
        required.update(stem_names)
    if project.export_format != "wav":
        required.add(f"master.{project.export_format}")
    if set(files) != required:
        raise ValueError("Production has missing or unexpected artifacts for its saved plan.")
    for relative, checksum in files.items():
        _check_cancel(cancel)
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Invalid production artifact checksum.")
        if file_sha256(contained_file(directory, relative), cancel=cancel) != checksum:
            raise ValueError("A published production artifact is missing or corrupt.")
    if files["script.txt"] != hashlib.sha256(project.source_text.encode("utf-8")).hexdigest():
        raise ValueError("Published original script differs from the saved plan.")
    voice_rows = manifest.get("voices")
    if not isinstance(voice_rows, list) or len(voice_rows) != len(project.roles):
        raise ValueError("Production voice inventory differs from its saved role map.")
    for role, row in zip(project.roles, voice_rows):
        if not isinstance(row, dict) or any(row.get(key) != expected for key, expected in (
            ("role", role.id), ("name", role.name), ("provider", role.provider), ("voice", role.voice))):
            raise ValueError("Production voice metadata differs from the selected role.")
        if not isinstance(row.get("engine_identity"), str) or not row["engine_identity"] or not isinstance(row.get("languages"), list) or not row["languages"] or any(not isinstance(language, str) or not language for language in row["languages"]):
            raise ValueError("Incomplete local voice capability metadata.")
    chapters = read_json(contained_file(directory, "chapters.json"))
    if not isinstance(chapters, dict) or set(chapters) != {"sample_rate", "chapters"} or type(chapters["sample_rate"]) is not int or chapters["sample_rate"] != rate:
        raise ValueError("Invalid chapter timeline document.")
    chapter_rows = chapters["chapters"]
    if not isinstance(chapter_rows, list) or len(chapter_rows) != len(project.scenes) or chapter_rows != manifest.get("chapters"):
        raise ValueError("Chapter inventory differs from the saved plan or manifest.")
    position = 0
    for scene, name, row in zip(project.scenes, chapter_names, chapter_rows):
        if not isinstance(row, dict) or set(row) != {"id", "title", "start_frame", "end_frame", "audio"}:
            raise ValueError("Invalid chapter timeline entry.")
        if row["id"] != scene.id or row["title"] != scene.title or row["audio"] != name or type(row["start_frame"]) is not int or type(row["end_frame"]) is not int or row["start_frame"] != position or not position < row["end_frame"] <= frames:
            raise ValueError("Chapter timeline has gaps, overlap or mismatched source identity.")
        position = row["end_frame"]
    if position != frames:
        raise ValueError("Chapter timeline does not cover the complete master audio.")
    timing = read_json(contained_file(directory, "timing.json"))
    if not isinstance(timing, dict) or set(timing) != {"sample_rate", "lines"} or type(timing["sample_rate"]) is not int or timing["sample_rate"] != rate or not isinstance(timing["lines"], list):
        raise ValueError("Invalid narration timing document.")
    rows = timing["lines"]
    if len(rows) != sum(len(scene.lines) for scene in project.scenes):
        raise ValueError("Narration timing is missing or duplicates a source line.")
    iterator = iter(rows)
    for scene, chapter in zip(project.scenes, chapter_rows):
        cursor = chapter["start_frame"] + round(scene.pause_before * rate)
        for line in scene.lines:
            row = next(iterator)
            if not isinstance(row, dict) or set(row) != {"scene", "line", "role", "text", "start_frame", "end_frame", "source_start", "source_end", "timing"}:
                raise ValueError("Invalid narration timing entry.")
            if any(row[key] != expected for key, expected in (
                ("scene", scene.id), ("line", line.id), ("role", line.role), ("text", line.text),
                ("source_start", line.source_start), ("source_end", line.source_end))):
                raise ValueError("Narration timing does not match its exact source line.")
            if type(row["source_start"]) is not type(line.source_start) or type(row["source_end"]) is not type(line.source_end) or row["timing"] != "measured synthesis-segment boundaries; not word alignment":
                raise ValueError("Narration provenance or timing capability is invalid.")
            if type(row["start_frame"]) is not int or type(row["end_frame"]) is not int or row["start_frame"] != cursor or not cursor < row["end_frame"] <= chapter["end_frame"]:
                raise ValueError("Narration timing is non-monotonic or outside its chapter.")
            cursor = row["end_frame"] + round(line.pause_after * rate)
        if cursor != chapter["end_frame"]:
            raise ValueError("Narration timing and planned pauses do not cover their chapter.")
    for name, vtt in (("subtitles.srt", False), ("subtitles.vtt", True)):
        if files[name] != hashlib.sha256(_subtitles(rows, rate, vtt=vtt).encode("utf-8")).hexdigest():
            raise ValueError("Synchronized text differs from its verified narration timing.")
    from .ffmpeg import AudioBackend, AudioCancelled, AudioError
    backend = backend or AudioBackend(sample_rate=rate)
    if backend.sample_rate != rate:
        raise ValueError("Verification runtime differs from the production sample rate.")

    def verify_audio(name, expected_frames, *, pcm=True):
        try:
            info = backend.probe(contained_file(directory, name), cancel=cancel)
        except AudioCancelled:
            raise
        except (AudioError, OSError) as exc:
            raise ValueError("A published audio artifact cannot be decoded or measured.") from exc
        if info.sample_rate != rate or info.channels != 2:
            raise ValueError("Published audio sample rate or channel count differs from its plan.")
        if pcm:
            if not info.exact_frames or not info.codec.startswith("pcm_") or info.frames != expected_frames:
                raise ValueError("Published PCM frame count differs from its measured timeline.")
        elif abs(info.frames - expected_frames) > max(round(rate * .25), 2048):
            # Lossy container padding is not represented as exact PCM timing.
            raise ValueError("Compressed output duration differs from the master audio.")

    verify_audio("master.wav", frames)
    for name, row in zip(chapter_names, chapter_rows):
        verify_audio(name, row["end_frame"] - row["start_frame"])
    for name in stem_names:
        verify_audio(name, frames)
    if project.export_format != "wav":
        verify_audio(f"master.{project.export_format}", frames, pcm=False)
    return manifest


def render_project(store, *, registry=None, backend=None, cancel=None, progress=None):
    """Render and verify speech, per-scene music/mixes, chapters, stems and text.

    No generated prose, command strings, remote fallback or model acquisition
    occurs here. A document cannot select a shell command or an output path.
    """
    from .ffmpeg import AudioBackend, Track
    from .voices import discover_local_providers
    from .music import render_procedural

    if not isinstance(store, ProjectStore):
        store = ProjectStore(store)
    with project_lock(store.root, "render"):
        loaded = store.load()
        project = loaded.project
        backend = backend or AudioBackend(sample_rate=project.sample_rate)
        if backend.sample_rate != project.sample_rate:
            raise ValueError("Audio backend sample rate differs from the saved production plan.")
        _check_cancel(cancel)
        # Resolve the current runtimes before considering a complete output.
        # Otherwise this shortcut would bypass the engine-bound speech cache.
        registry = registry or discover_local_providers(ffmpeg=backend.ffmpeg)
        available = {(v.provider, v.id): v for v in registry.list_voices()}
        voice_info = {}
        roles = {role.id: role for role in project.roles}
        for role in project.roles:
            if role.provider not in ("sapi5", "sapi5-x86", "flite"):
                raise ValueError(f"Local provider {role.provider!r} is not an implemented production adapter.")
            voice = available.get((role.provider, role.voice))
            if voice is None:
                raise ValueError(f"The local voice for {role.name} is unavailable. Choose an installed voice and save.")
            voice_info[role.id] = voice
        backend_identity = backend.identity
        selected_voices = tuple((role.id, role.provider, role.voice, voice_info[role.id].identity)
                                for role in project.roles)
        runtime_key = digest({"version": 1, "backend": backend_identity, "voices": selected_voices})
        exports = local_directory(store.root / "exports", create=True)
        original = exports / project.sha256
        publication_key = digest({"project": project.sha256, "runtime": runtime_key})
        current = exports / f"runtime-{publication_key}"
        for candidate in (original, current):
            _check_cancel(cancel)
            if not candidate.exists():
                continue
            try:
                folder = local_directory(candidate)
                preview = read_json(contained_file(folder, "production.json"))
                if not _matches_runtime(preview, backend_identity, selected_voices):
                    continue
                previous = verify_production(folder, expected_project_sha256=project.sha256,
                                             backend=backend, cancel=cancel)
            except (OSError, ValueError):
                continue  # Preserve altered/damaged output and recover separately.
            if progress:
                progress({"message": "Verified completed production for the selected local runtimes; nothing was rendered twice.",
                          "created": 0, "reused": len(previous["files"]), "manifest": str(folder / "production.json")})
            return RenderResult(folder / "production.json", folder / "master.wav", previous["frames"],
                                previous["sample_rate"], 0, len(previous["files"]))
        # The first publication retains the existing project-only layout.
        # A changed runtime gets a stable separate destination, so B can reuse
        # B and switching back to A can still reuse the preserved A output.
        final = original if not original.exists() and not original.is_symlink() else current
        if final.exists() or final.is_symlink():
            final = exports / f"{current.name}-recovered-{uuid.uuid4().hex[:8]}"
        journal = RenderJournal(store.root, cancel=cancel, progress=progress)
        journal.start(project)
        stage = None
        try:
            rate, position = project.sample_rate, 0
            scene_outputs, chapters, timings = [], [], []
            stem_outputs = {role.id: [] for role in project.roles} if project.stems else {}
            if project.stems and any(s.music for s in project.scenes):
                stem_outputs[_MUSIC_BUS] = []
            for scene_number, scene in enumerate(project.scenes, 1):
                _check_cancel(cancel)
                journal.report(f"Chapter {scene_number} of {len(project.scenes)}: {scene.title}",
                               scene=scene.id, completed_frames=position, sample_rate=rate)
                cursor = 0
                line_outputs = []
                for line_number, line in enumerate(scene.lines):
                    role, voice = roles[line.role], voice_info[line.role]
                    provider = registry.get(role.provider)
                    prefix = round(scene.pause_before * rate) if line_number == 0 else 0
                    start = cursor + prefix
                    utterances, utterance_frames, speech_frames = [], [], 0
                    for chunk in split_speech(line.text):
                        if not chunk.strip():
                            # Layout whitespace remains verbatim in the saved
                            # script/timing text but has no standalone speech.
                            continue
                        key = digest({"kind": "speech-v1", "text_sha256": text_digest(chunk),
                            "provider": role.provider, "voice": role.voice, "engine": voice.identity,
                            "rate": role.rate, "volume": role.volume, "sample_rate": rate})
                        artifact = journal.run(key, "speech", lambda output, text=chunk, provider=provider,
                            role=role: provider.synthesize(text, role.voice, output, rate=role.rate,
                                                         volume=role.volume, sample_rate=rate, cancel=cancel))
                        info = backend.probe(artifact.path)
                        if info.sample_rate != rate or info.frames <= 0:
                            raise ValueError("A voice returned invalid timing or the wrong sample rate.")
                        utterances.append(artifact)
                        utterance_frames.append(info.frames)
                        speech_frames += info.frames
                    long_line = len(utterances) > 1
                    if not long_line:
                        spoken = utterances[0]
                    else:
                        # Engine WAVs are mono; canonical assembly requires
                        # stereo. Apply the role pan once to each exact-frame
                        # chunk, then retain those channels when positioning.
                        positioned_chunks = []
                        for artifact, frames in zip(utterances, utterance_frames):
                            _check_space(store.root, frames * 4 * 3)
                            chunk_key = digest({"kind": "panned-speech-chunk-v1", "backend": backend.identity,
                                "speech": artifact.sha256, "frames": frames, "pan": role.pan, "sample_rate": rate})
                            positioned_chunks.append(journal.run(chunk_key, "positioned speech chunk",
                                lambda output, artifact=artifact, frames=frames:
                                    backend.mix([Track(artifact.path, pan=role.pan)], output,
                                                duration_frames=frames, duck_music=False, cancel=cancel)))
                        _check_space(store.root, speech_frames * 4 * 2)
                        utterance_key = digest({"kind": "joined-utterance-v2", "backend": backend.identity,
                            "chunks": [a.sha256 for a in positioned_chunks], "sample_rate": rate})
                        spoken = journal.run(utterance_key, "long spoken line", lambda output:
                            backend.concatenate([a.path for a in positioned_chunks], output, cancel=cancel))
                    line_frames = prefix + speech_frames + round(line.pause_after * rate)
                    _check_space(store.root, line_frames * 4 * 3)
                    line_key = digest({"kind": "positioned-line-v1", "backend": backend.identity,
                        "speech": spoken.sha256, "frames": line_frames, "prefix_frames": prefix,
                        "pan": role.pan, "sample_rate": rate})
                    positioned = journal.run(line_key, "timed speaker line", lambda output:
                        backend.mix([Track(spoken.path, start_frame=prefix,
                                           pan=0 if long_line else role.pan, preserve_stereo=long_line)], output,
                                    duration_frames=line_frames, duck_music=False, cancel=cancel))
                    line_outputs.append((role.id, positioned, line_frames))
                    timings.append({"scene": scene.id, "line": line.id, "role": role.id,
                        "text": line.text, "start_frame": position + start,
                        "end_frame": position + start + speech_frames,
                        "source_start": line.source_start, "source_end": line.source_end,
                        "timing": "measured synthesis-segment boundaries; not word alignment"})
                    cursor += line_frames
                if cursor <= 0:
                    raise ValueError("The scene has no renderable duration.")
                _check_space(store.root, cursor * 4 * (3 + len(stem_outputs)))
                speech_key = digest({"kind": "scene-speech-v2", "backend": backend.identity,
                                    "lines": [a.sha256 for _, a, _ in line_outputs], "sample_rate": rate})
                speech = journal.run(speech_key, "chapter speech", lambda output:
                    backend.concatenate([a.path for _, a, _ in line_outputs], output, cancel=cancel))
                tracks = [Track(speech.path, preserve_stereo=True)]
                track_records = [{"sha256": speech.sha256, "role": "speech", "preserve_stereo": True}]
                music_track = None
                if scene.music is not None:
                    cue = scene.music
                    music_key = digest({"kind": "procedural-music-v1", "duration_frames": cursor,
                        "sample_rate": rate, "mood": cue.mood, "tempo": cue.tempo, "seed": cue.seed,
                        "backend": backend.identity})
                    music_artifact = journal.run(music_key, "procedural music", lambda output:
                        render_procedural(backend, output, duration_seconds=cursor / rate,
                                          tempo=cue.tempo, mood=cue.mood, seed=cue.seed, cancel=cancel))
                    music_track = Track(music_artifact.path, gain=cue.gain, kind="music", preserve_stereo=True)
                    tracks.append(music_track)
                    track_records.append({"sha256": music_artifact.sha256, "role": "music", "gain": cue.gain})
                scene_key = digest({"kind": "scene-mix-v2", "backend": backend.identity,
                                    "rate": rate, "frames": cursor, "tracks": track_records, "duck": True})
                mixed = journal.run(scene_key, "scene mix", lambda output:
                                    backend.mix(tracks, output, duration_frames=cursor, duck_music=True, cancel=cancel))
                mixed_info = backend.probe(mixed.path)
                if mixed_info.frames != cursor or mixed_info.sample_rate != rate or mixed_info.channels != 2:
                    raise ValueError("The scene mix does not match the measured timeline.")
                scene_outputs.append(mixed)
                for role_id in stem_outputs:
                    if role_id == _MUSIC_BUS:
                        selected = [music_track] if music_track else []
                        stem_key = digest({"kind": "scene-music-stem-v2", "backend": backend.identity,
                            "rate": rate, "frames": cursor,
                            "tracks": [record for record in track_records if record["role"] == "music"]})
                        stem = journal.run(stem_key, "music stem", lambda output:
                            backend.mix(selected, output, duration_frames=cursor, duck_music=False, cancel=cancel))
                    else:
                        pieces = []
                        for speaker, piece, frames in line_outputs:
                            if speaker == role_id:
                                pieces.append(piece)
                            else:
                                silence_key = digest({"kind": "silence-v1", "backend": backend.identity,
                                                      "sample_rate": rate, "frames": frames})
                                pieces.append(journal.run(silence_key, "stem silence", lambda output, frames=frames:
                                    backend.mix([], output, duration_frames=frames, duck_music=False, cancel=cancel)))
                        stem_key = digest({"kind": "scene-speaker-stem-v2", "backend": backend.identity,
                            "pieces": [piece.sha256 for piece in pieces], "sample_rate": rate})
                        stem = journal.run(stem_key, "speaker stem", lambda output:
                            backend.concatenate([a.path for a in pieces], output, cancel=cancel))
                    stem_outputs[role_id].append(stem)
                chapters.append({"id": scene.id, "title": scene.title, "start_frame": position,
                                 "end_frame": position + cursor, "audio": f"chapter-{scene_number:04d}.wav"})
                position += cursor
            _check_space(store.root, position * 4 * (3 + 2 * len(stem_outputs)))
            master_key = digest({"kind": "master-v1", "backend": backend.identity,
                                 "scenes": [a.sha256 for a in scene_outputs], "sample_rate": rate})
            master = journal.run(master_key, "continuous master", lambda output:
                                 backend.concatenate([a.path for a in scene_outputs], output, cancel=cancel))
            master_info = backend.probe(master.path)
            if master_info.frames != position or master_info.sample_rate != rate or master_info.channels != 2:
                raise ValueError("The final audio length differs from its chapter timeline.")
            complete_stems = {}
            for role_id, pieces in stem_outputs.items():
                key = digest({"kind": "continuous-stem-v1", "backend": backend.identity,
                              "pieces": [a.sha256 for a in pieces], "sample_rate": rate})
                complete_stems[role_id] = journal.run(key, "continuous stem", lambda output, pieces=pieces:
                    backend.concatenate([a.path for a in pieces], output, cancel=cancel))
            measurements = backend.measure(master.path, cancel=cancel)
            if measurements.get("true_peak_dbfs") is not None and measurements["true_peak_dbfs"] > 0.1:
                raise ValueError("The rendered master exceeds the safe peak limit; no completed production was published.")
            stage = Path(tempfile.mkdtemp(prefix=".publish-", dir=exports))
            files = {}

            def publish(name, artifact):
                _check_cancel(cancel)
                _copy_verified(artifact.path, stage / name, artifact.sha256)
                files[name] = artifact.sha256

            def document(name, value, *, text=False):
                atomic_bytes(stage / name, value.encode("utf-8") if text else json_bytes(value))
                files[name] = file_sha256(stage / name)

            publish("master.wav", master)
            if project.export_format != "wav":
                destination = stage / f"master.{project.export_format}"
                backend.convert(master.path, destination, format=project.export_format, cancel=cancel)
                files[destination.name] = file_sha256(destination)
            for chapter, artifact in zip(chapters, scene_outputs):
                publish(chapter["audio"], artifact)
            for role_id, artifact in complete_stems.items():
                publish(f"stem-{role_id}.wav", artifact)
            document("script.txt", project.source_text, text=True)
            document("project.json", project.to_dict())
            document("chapters.json", {"sample_rate": rate, "chapters": chapters})
            document("timing.json", {"sample_rate": rate, "lines": timings})
            document("subtitles.srt", _subtitles(timings, rate), text=True)
            document("subtitles.vtt", _subtitles(timings, rate, vtt=True), text=True)
            manifest = {"schema_version": 1, "status": "complete", "project_sha256": project.sha256,
                "source_sha256": project.source_sha256, "sample_rate": rate, "frames": position,
                "duration_seconds": position / rate, "channels": 2, "files": files,
                "chapters": chapters, "local_only": True, "backend": backend.identity,
                "voices": [{"role": role.id, "name": role.name, "provider": role.provider,
                            "voice": role.voice, "engine_identity": voice_info[role.id].identity,
                            "languages": voice_info[role.id].languages} for role in project.roles],
                "music": "original deterministic procedural audio" if any(s.music for s in project.scenes) else "none",
                "timing_quality": "measured segment boundaries; word-level timing is not provided",
                "quality": {"frame_continuity": "verified", "human_listening": "NOT_RUN",
                            "native_nvda": "NOT_RUN", **measurements},
                "stem_processing": "isolated positioned voices and un-ducked music; final master limiting is separate",
                "created_checkpoints": journal.created, "reused_checkpoints": journal.reused}
            atomic_bytes(stage / "production.json", json_bytes(manifest))
            verify_production(stage, expected_project_sha256=project.sha256, backend=backend, cancel=cancel)
            _check_cancel(cancel)
            # Project edits during a run are allowed, but the produced version
            # is the immutable plan loaded at start, named by its own digest.
            publish_directory(stage, final)
            stage = None
            journal.finish(final / "production.json")
            return RenderResult(final / "production.json", final / "master.wav", position,
                                rate, journal.created, journal.reused)
        except BaseException as exc:
            journal.fail(exc)
            raise
        finally:
            if stage is not None and stage.exists():
                shutil.rmtree(stage)
