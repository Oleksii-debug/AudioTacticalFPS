"""Non-destructive scene editing over the existing versioned Studio project."""
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_EVEN
import re
from hashlib import sha256
import os
from pathlib import Path
import shutil
from uuid import uuid4

from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending, finite
from ..audio.cleanup import retained_state_lock, retained_temporary_directory
from ..audio.process import publish_file
from ..durable_state import local_path, regular
from .backup_inputs import _checked_digest, _stamp
from .project import Cue, Marker, Project, Scene, _timeline_effects
from .render import ProductionService

def timeline_seconds(value, sample_rate, label='Timecode'):
    """Seconds or HH:MM:SS.ffffff, rounded once to the output sample grid."""
    if type(sample_rate) is not int or sample_rate not in (16000, 22050, 24000, 44100, 48000):
        raise ValueError('Choose a supported output sample rate.')
    if isinstance(value, str):
        if not value or len(value) > 96 or value != value.strip():
            raise ValueError(label + ' must be seconds or HH:MM:SS.ffffff.')
        if ':' in value:
            match = re.fullmatch(r'([0-9]{1,5}):([0-5][0-9]):([0-5][0-9](?:\.[0-9]{1,24})?)', value)
            if match is None:
                raise ValueError(label + ' must be seconds or HH:MM:SS.ffffff.')
            number = Decimal(match[1]) * 3600 + Decimal(match[2]) * 60 + Decimal(match[3])
        else:
            if re.fullmatch(r'(?:[0-9]{1,9}(?:\.[0-9]{0,24})?|\.[0-9]{1,24})(?:[eE][+-]?[0-9]{1,3})?', value) is None:
                raise ValueError(label + ' must be nonnegative decimal seconds.')
            number = Decimal(value)
    else:
        number = Decimal(str(finite(value, label, 0, 31_536_000)))
    if not number.is_finite() or not 0 <= number <= 31_536_000:
        raise ValueError(label + ' is outside the supported timeline.')
    frames = int((number * sample_rate).to_integral_value(rounding=ROUND_HALF_EVEN))
    return frames / sample_rate


class ClipEditor:
    """Retain the exact selected bytes; edits only change project instructions."""
    def __init__(self, library_root, *, service=None):
        self.service = service or ProductionService(Path(library_root) / 'state' / 'studio')
        self._owns_service, self._closed = service is None, False
        self._service_closed = not self._owns_service
        self._clipboard = None
        self._undo, self._redo = [], []
        self._history_head = None

    def create(self, title='Audio clips'):
        if self._closed:
            raise ValueError('This clip editor is closed.')
        project = Project(uuid4().hex, title, (),
            (Scene('scene-1', 'Audio clips', (), minimum_duration=1),),
            original_script='[Scene: Audio clips]\n[Duration: 1]', schema=2)
        return self.service.projects.save(project)

    def _current(self, project):
        if self._closed or not isinstance(project, Project):
            raise ValueError('Choose a saved project in an open clip editor.')
        current = self.service.projects.load(project.id)
        if current.fingerprint != project.fingerprint:
            raise ValueError('Project changed since it was opened. Reload before editing clips.')
        return current

    @staticmethod
    def _scene(project, scene_id):
        for scene in project.scenes:
            if scene.id == scene_id:
                return scene
        raise ValueError('Select a scene from this project.')

    def _save(self, project, scene):
        updated = replace(project, schema=max(project.schema, 3 if scene.effects or scene.markers
            or any(cue.effects or cue.effects_bypassed or cue.routing != 'pan' for cue in scene.cues) else 2),
            revision=project.revision + 1,
            scenes=tuple(scene if old.id == scene.id else old for old in project.scenes))
        return self._publish(project, updated)

    def retain(self, selected, *, expected_sha256=None, cancelled=lambda: False, progress=None):
        """Make a verified private copy, without granting access to its neighbours."""
        if self._closed:
            raise ValueError('This clip editor is closed.')
        source = local_path(selected, exists=True)
        before = regular(source)
        if not 0 < before.st_size <= 8 * 1024**3:
            raise ValueError('Select a nonempty local audio file of at most 8 GiB.')
        if shutil.disk_usage(self.service.inputs).free < before.st_size + 64 * 1024**2:
            raise ValueError('Not enough free disk space to retain the selected original audio.')
        inputs = local_path(self.service.inputs, exists=True)
        staged = inputs / ('.pending-' + uuid4().hex + '.audio')
        deferred = False
        runtime = self.service._runtime(reviewed_roots=(inputs,))
        with retained_state_lock(inputs, 'selected-clip'):
            try:
                with self.service._audio_admission(runtime, operation_id='clip-retain:' + uuid4().hex,
                        disk_bytes=2 * before.st_size, disk_path=inputs, cancelled=cancelled) as stopped:
                    digest, count = sha256(), 0
                    with source.open('rb') as reader, staged.open('xb') as writer:
                        if _stamp(os.fstat(reader.fileno())) != _stamp(before):
                            raise ValueError('Selected audio changed before retention.')
                        while block := reader.read(1024 * 1024):
                            if stopped():
                                raise AudioCancelled('Audio clip import cancelled.')
                            count += len(block)
                            if count > before.st_size:
                                raise ValueError('Selected audio grew during retention.')
                            writer.write(block)
                            digest.update(block)
                            if progress:
                                progress(f'Retaining selected audio: {count} of {before.st_size} bytes')
                        writer.flush()
                        os.fsync(writer.fileno())
                        if _stamp(os.fstat(reader.fileno())) != _stamp(before):
                            raise ValueError('Selected audio changed during retention.')
                    identity = digest.hexdigest()
                    if count != before.st_size or _checked_digest(source, before, stopped) != identity:
                        raise ValueError('Selected audio changed during retention.')
                    if expected_sha256 is not None and expected_sha256 != identity:
                        raise ValueError('The selected recording differs from its verified receipt.')
                    info = runtime.inspect(staged, cancelled=stopped)
                    if info.duration <= 0 or info.channels not in (1, 2):
                        raise ValueError('Select readable mono or stereo audio.')
                    if _checked_digest(staged, regular(staged), stopped) != identity:
                        raise ValueError('Retained audio changed during decoder verification.')
                    target = inputs / (identity + '.audio')
                    if target.exists():
                        retained = regular(target)
                        if retained.st_size != count or _checked_digest(target, retained, stopped) != identity:
                            raise ValueError('Previously retained audio differs from its content identity.')
                    else:
                        publish_file(staged, target, cancelled=stopped)
            except AudioCleanupPending as exc:
                deferred = True
                exc.cleanup_owner.add_quiescent_callback(lambda: staged.unlink(missing_ok=True))
                raise
            finally:
                if not deferred:
                    staged.unlink(missing_ok=True)
        return local_path(target, exists=True)

    def add(self, project, scene_id, selected, *, expected_sha256=None,
            cancelled=lambda: False, progress=None, **settings):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        if len(scene.cues) >= AudioBudget().max_inputs - 2:
            raise ValueError('This scene has reached its simultaneous audio input allowance.')
        # Reject invalid controls before reading or retaining selected media.
        settings = dict(settings)
        if not settings.get('title'):
            settings['title'] = Path(selected).name
        cue = Cue(uuid4().hex, str(selected), **settings)
        retained = self.retain(selected, expected_sha256=expected_sha256,
                               cancelled=cancelled, progress=progress)
        cue = replace(cue, path=str(retained))
        self._validate_selection(cue, cancelled=cancelled)
        if cancelled():
            raise AudioCancelled('Audio clip import cancelled before project publication.')
        return self._save(project, replace(scene, cues=(*scene.cues, cue)))

    def _validate_selection(self, cue, *, cancelled=lambda: False):
        self._cancel(cancelled)
        source = self.service._cue_source(cue, cancelled=cancelled)
        runtime = self.service._runtime(reviewed_roots=(source.parent,))
        with self.service._audio_admission(runtime, operation_id='clip-inspect:' + uuid4().hex,
                                           cancelled=cancelled) as stopped:
            self.service._cue_source(cue, cancelled=stopped)
            info = runtime.inspect(source, cancelled=stopped)
            self.service._cue_source(cue, cancelled=stopped)
        available = info.duration - cue.trim_start
        selected = available if cue.duration is None else cue.duration
        if available <= 0 or selected > available + 1 / info.sample_rate:
            raise ValueError('Clip trim or duration exceeds its retained original audio.')
        if max(cue.fade_in, cue.fade_out) > selected:
            raise ValueError('Clip fade exceeds its selected audio.')
        return info

    def edit(self, project, scene_id, clip_id, *, cancelled=lambda: False, **settings):
        allowed = {'start', 'gain_db', 'pan', 'role', 'trim_start', 'duration', 'fade_in', 'fade_out', 'title'}
        if not settings or not set(settings) <= allowed:
            raise ValueError('Choose supported clip timing, gain, pan, fade or track controls.')
        project = self._current(project)
        scene = self._scene(project, scene_id)
        cue = next((cue for cue in scene.cues if cue.id == clip_id), None)
        if cue is None:
            raise ValueError('Select an existing clip in this scene.')
        updated = replace(cue, **settings)
        self._validate_selection(updated, cancelled=cancelled)
        if cancelled():
            raise AudioCancelled('Audio clip edit cancelled before publication.')
        if updated == cue:
            return project
        return self._save(project, replace(scene,
            cues=tuple(updated if old.id == clip_id else old for old in scene.cues)))

    def remove(self, project, scene_id, clip_id):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        if not any(cue.id == clip_id for cue in scene.cues):
            raise ValueError('Select an existing clip in this scene.')
        cues = tuple(cue for cue in scene.cues if cue.id != clip_id)
        minimum = scene.minimum_duration
        if not cues and not scene.lines and not scene.music:
            minimum = max(minimum, 1)
        return self._save(project, replace(scene, cues=cues, minimum_duration=minimum))

    def scene_duration(self, project, scene_id, seconds):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        updated = replace(scene, minimum_duration=seconds)
        return project if scene == updated else self._save(project, updated)

    def move(self, project, scene_id, clip_id, target_scene_id):
        project = self._current(project)
        source = self._scene(project, scene_id)
        destination = self._scene(project, target_scene_id)
        cue = next((item for item in source.cues if item.id == clip_id), None)
        if cue is None:
            raise ValueError('Select an existing clip in this scene.')
        if source.id == destination.id:
            return project
        if any(item.id == cue.id for item in destination.cues):
            raise ValueError('The destination scene already contains this clip identity.')
        if len(destination.cues) >= AudioBudget().max_inputs - 2:
            raise ValueError('The destination scene has reached its simultaneous input allowance.')
        remaining = tuple(item for item in source.cues if item.id != cue.id)
        minimum = source.minimum_duration
        if not remaining and not source.lines and not source.music:
            minimum = max(minimum, 1)
        replacements = {
            source.id: replace(source, cues=remaining, minimum_duration=minimum),
            destination.id: replace(destination, cues=(*destination.cues, cue)),
        }
        updated = replace(project, schema=max(project.schema, 2), revision=project.revision + 1,
                          scenes=tuple(replacements.get(scene.id, scene) for scene in project.scenes))
        return self._publish(project, updated)

    def preview(self, project, scene_id, clip_id, *, dry=False, cancelled=lambda: False):
        from ..audio.models import Track
        project = self._current(project)
        scene = self._scene(project, scene_id)
        cue = next((item for item in scene.cues if item.id == clip_id), None)
        if cue is None:
            raise ValueError('Select an existing clip in this scene.')
        if type(dry) is not bool:
            raise ValueError('Choose explicitly whether to bypass clip effects for preview.')
        return self._preview(project, cue, dry=dry, cancelled=cancelled)

    def _preview(self, project, cue, *, dry=False, cancelled=lambda: False, output=None):
        from ..audio.models import Track
        source = self.service._cue_source(cue, cancelled=cancelled)
        runtime = self.service._runtime(reviewed_roots=(source.parent,))
        output = output or self.service.cache / ('clip-preview-' + uuid4().hex + '.wav')
        with self.service._audio_admission(runtime, operation_id='clip-preview-inspect:' + uuid4().hex,
                                           cancelled=cancelled) as stopped:
            self.service._cue_source(cue, cancelled=stopped)
            info = runtime.inspect(source, cancelled=stopped)
            self.service._cue_source(cue, cancelled=stopped)
        seconds = info.duration - cue.trim_start if cue.duration is None else cue.duration
        # The native renderer checks exact trim/fade/output limits again. This
        # declared reservation covers the PCM stage and final preview together.
        size = max(1, round(seconds * project.sample_rate * 2 * 2))
        with self.service._audio_admission(runtime, operation_id='clip-preview:' + uuid4().hex,
                disk_bytes=2 * size + runtime.budget.reserve_disk_bytes,
                disk_path=self.service.cache, cancelled=cancelled) as stopped:
            self.service._cue_source(cue, cancelled=stopped)
            runtime.mix((Track(source, gain_db=cue.gain_db, pan=cue.pan, role=cue.role,
                trim_start=cue.trim_start, duration=cue.duration,
                fade_in=cue.fade_in, fade_out=cue.fade_out, routing=cue.routing,
                effects=() if dry or cue.effects_bypassed else cue.effects),), output,
                sample_rate=project.sample_rate, cancelled=stopped)
            self.service._cue_source(cue, cancelled=stopped)
        return output

    def set_effects(self, project, scope, effects, *, scene_id=None, clip_id=None, role_id=None,
                    cancelled=lambda: False):
        """Persist one reviewed chain through the same revision/CAS/history owner."""
        project = self._current(project)
        chain = _timeline_effects(effects, project.sample_rate)
        self._cancel(cancelled)
        if scope == 'master':
            updated = replace(project, schema=3, effects=chain)
        elif scope == 'role':
            if role_id not in {role.id for role in project.roles}:
                raise ValueError('Choose an existing speaking role.')
            updated = replace(project, schema=3, roles=tuple(
                replace(role, effects=chain) if role.id == role_id else role for role in project.roles))
        elif scope in ('scene', 'clip'):
            scene = self._scene(project, scene_id)
            if scope == 'scene':
                changed = replace(scene, effects=chain)
            else:
                selected = self._selected(scene, clip_id)
                changed = replace(scene, cues=tuple(
                    replace(cue, effects=chain) if cue.id == selected.id else cue for cue in scene.cues))
            updated = replace(project, schema=3, scenes=tuple(
                changed if item.id == scene.id else item for item in project.scenes))
        else:
            raise ValueError('Choose clip, role, scene or master effects.')
        if updated == project:
            return project
        self._cancel(cancelled)
        return self._publish(project, replace(updated, revision=project.revision + 1))


    def sample_rate(self, project, value, *, cancelled=lambda: False):
        project = self._current(project)
        updated = replace(project, sample_rate=value)
        if updated == project:
            return project
        self._cancel(cancelled)
        return self._publish(project, replace(updated, revision=project.revision + 1))



    def _publish(self, before, after):
        """One canonical revision; history records only a successful publication."""
        saved = self.service.projects.save(after, expected_revision=before.revision,
                                          expected_fingerprint=before.fingerprint)
        if self._history_head != (before.id, before.fingerprint):
            self._undo.clear()
        self._redo.clear()
        self._undo.append((before, saved))
        del self._undo[:-32]
        self._history_head = saved.id, saved.fingerprint
        return saved

    @staticmethod
    def _selected(scene, clip_id):
        cue = next((item for item in scene.cues if item.id == clip_id), None)
        if cue is None:
            raise ValueError('Select an existing clip in this scene.')
        return cue

    @staticmethod
    def _cancel(cancelled):
        if cancelled():
            raise AudioCancelled('Clip edit cancelled before project publication.')


    def copy(self, project, scene_id, clip_id, *, cancelled=lambda: False):
        """An editor-local clipboard contains verified retained media, not commands."""
        project = self._current(project)
        cue = self._selected(self._scene(project, scene_id), clip_id)
        self._cancel(cancelled)
        source = self.service._cue_source(cue, cancelled=cancelled)
        retained = source if source.parent == self.service.inputs else self.retain(source, cancelled=cancelled)
        copied = replace(cue, path=str(retained))
        self._validate_selection(copied, cancelled=cancelled)
        self._current(project)
        self._cancel(cancelled)
        self._clipboard = copied
        return copied


    def cut(self, project, scene_id, clip_id, *, cancelled=lambda: False):
        self.copy(project, scene_id, clip_id, cancelled=cancelled)
        self._cancel(cancelled)
        return self.remove(project, scene_id, clip_id)


    def paste(self, project, scene_id, *, start=None, cancelled=lambda: False):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        if self._clipboard is None:
            raise ValueError('Copy or cut a clip in this editor before pasting.')
        if len(scene.cues) >= AudioBudget().max_inputs - 2:
            raise ValueError('This scene has reached its simultaneous audio input allowance.')
        copied = self._clipboard
        cue = replace(copied, id=uuid4().hex,
                      start=copied.start if start is None else start)
        self._validate_selection(cue, cancelled=cancelled)
        self._cancel(cancelled)
        return self._save(project, replace(scene, cues=(*scene.cues, cue)))


    def split(self, project, scene_id, clip_id, seconds, *, cancelled=lambda: False):
        """Split instructions at an output sample; original bytes remain shared."""
        project = self._current(project)
        scene = self._scene(project, scene_id)
        cue = self._selected(scene, clip_id)
        if not cue.effects_bypassed and any(effect.enabled for effect in cue.effects):
            raise ValueError('Bypass the clip effect chain before splitting so its processing state is not reset silently.')
        if len(scene.cues) >= AudioBudget().max_inputs - 2:
            raise ValueError('This scene has reached its simultaneous audio input allowance.')
        info = self._validate_selection(cue, cancelled=cancelled)
        duration = info.duration - cue.trim_start if cue.duration is None else cue.duration
        position = timeline_seconds(seconds, project.sample_rate, 'Clip split position')
        if not 0 < position < duration:
            raise ValueError('Choose a split strictly inside the selected clip.')
        if position < cue.fade_in or duration - position < cue.fade_out:
            raise ValueError('Choose a split outside the existing fade windows, or adjust the fades first.')
        left = replace(cue, duration=position, fade_out=0)
        right = replace(cue, id=uuid4().hex, start=cue.start + position,
                        trim_start=cue.trim_start + position, duration=duration - position,
                        fade_in=0, title=(cue.title[:503] + ' — part 2') if cue.title else '')
        self._cancel(cancelled)
        cues = []
        for item in scene.cues:
            cues.extend((left, right) if item.id == cue.id else (item,))
        return self._save(project, replace(scene, cues=tuple(cues)))


    def join(self, project, scene_id, first_id, second_id, *, cancelled=lambda: False):
        """Join contiguous cuts of the same source without a new render or mix."""
        project = self._current(project)
        scene = self._scene(project, scene_id)
        if first_id == second_id:
            raise ValueError('Choose two different clips to join.')
        selected = (self._selected(scene, first_id), self._selected(scene, second_id))
        left, right = sorted(selected, key=lambda item: (item.start, item.id))
        if (left.effects != right.effects or left.effects_bypassed != right.effects_bypassed
                or (not left.effects_bypassed and any(effect.enabled for effect in (*left.effects, *right.effects)))):
            raise ValueError('Join requires matching bypassed effect chains; active processing cannot be merged silently.')
        first = self._validate_selection(left, cancelled=cancelled)
        second = self._validate_selection(right, cancelled=cancelled)
        left_length = first.duration - left.trim_start if left.duration is None else left.duration
        right_length = second.duration - right.trim_start if right.duration is None else right.duration
        if (left.path != right.path or left.role != right.role or left.gain_db != right.gain_db
                or left.pan != right.pan or left.routing != right.routing
                or left.fade_out != 0 or right.fade_in != 0
                or abs(right.start - left.start - left_length) > 1e-9
                or abs(right.trim_start - left.trim_start - left_length) > 1e-9):
            raise ValueError('Join requires adjacent cuts of the same original, matching track/gain/pan, '
                             'and no fade at their shared boundary.')
        joined = replace(left, duration=left_length + right_length, fade_out=right.fade_out)
        self._cancel(cancelled)
        cues = tuple(joined if item.id == left.id else item for item in scene.cues if item.id != right.id)
        return self._save(project, replace(scene, cues=cues))


    def history(self, project, *, forward=False, cancelled=lambda: False):
        """Undo/redo this editor's latest 32 edits through new durable revisions."""
        if type(forward) is not bool:
            raise ValueError('Choose undo or redo explicitly.')
        project = self._current(project)
        if self._history_head != (project.id, project.fingerprint):
            raise ValueError('The project changed outside this editing history. Reload and review it.')
        stack, opposite = (self._redo, self._undo) if forward else (self._undo, self._redo)
        if not stack:
            raise ValueError('No clip edit is available to redo.' if forward else 'No clip edit is available to undo.')
        change = stack[-1]
        snapshot = change[1] if forward else change[0]
        # Effects retain immutable parameter mappings, so cues need value
        # comparison rather than hashing when checking restored selections.
        existing = tuple(cue for scene in project.scenes for cue in scene.cues)
        for scene in snapshot.scenes:
            for cue in scene.cues:
                self._cancel(cancelled)
                if cue not in existing:
                    self._validate_selection(cue, cancelled=cancelled)
        self._cancel(cancelled)
        restored = replace(snapshot, schema=max(project.schema, snapshot.schema, 2), revision=project.revision + 1)
        saved = self.service.projects.save(restored, expected_revision=project.revision,
                                          expected_fingerprint=project.fingerprint)
        stack.pop()
        opposite.append(change)
        self._history_head = saved.id, saved.fingerprint
        return saved


    def _range(self, project, cue, start, end, *, cancelled):
        info = self._validate_selection(cue, cancelled=cancelled)
        length = info.duration - cue.trim_start if cue.duration is None else cue.duration
        total = round(length * project.sample_rate)
        first = round(timeline_seconds(start, project.sample_rate, 'Range start') * project.sample_rate)
        last = total if end is None or end == '' else round(
            timeline_seconds(end, project.sample_rate, 'Range end') * project.sample_rate)
        if total <= 0 or not 0 <= first < last <= total:
            raise ValueError('Select at least one output sample inside the saved clip.')
        return first, last, total

    @staticmethod
    def _portion(cue, first, last, total, rate, *, identity=None):
        """Represent an exact source interval without approximating a partial fade."""
        fade_in, fade_out = round(cue.fade_in * rate), round(cue.fade_out * rate)
        if (0 < first < fade_in or 0 < last < fade_in
                or total - fade_out < first < total or total - fade_out < last < total):
            raise ValueError('The range crosses an existing fade window. Adjust that fade first.')
        return replace(cue, id=identity or cue.id,
            start=(round(cue.start * rate) + first) / rate,
            trim_start=(round(cue.trim_start * rate) + first) / rate,
            duration=(last - first) / rate,
            fade_in=cue.fade_in if first == 0 else 0,
            fade_out=cue.fade_out if last == total else 0)

    def copy_range(self, project, scene_id, clip_id, start=0, end=None, *, cancelled=lambda: False):
        project = self._current(project)
        cue = self._selected(self._scene(project, scene_id), clip_id)
        first, last, total = self._range(project, cue, start, end, cancelled=cancelled)
        copied = self._portion(cue, first, last, total, project.sample_rate)
        source = self.service._cue_source(copied, cancelled=cancelled)
        if source.parent != self.service.inputs:
            copied = replace(copied, path=str(self.retain(source, cancelled=cancelled)))
        self._validate_selection(copied, cancelled=cancelled)
        self._current(project)
        self._cancel(cancelled)
        self._clipboard = copied
        return copied

    def _remove_range(self, project, scene, cue, first, last, total, *, cancelled):
        if (first or last < total) and not cue.effects_bypassed and any(
                effect.enabled for effect in cue.effects):
            raise ValueError('Bypass clip effects before cutting inside their processing interval.')
        portions = []
        if first:
            portions.append(self._portion(cue, 0, first, total, project.sample_rate))
        if last < total:
            portions.append(self._portion(cue, last, total, total, project.sample_rate,
                identity=uuid4().hex if portions else cue.id))
        cues = tuple(part for item in scene.cues
                     for part in (portions if item.id == cue.id else (item,)))
        if len(cues) > AudioBudget().max_inputs - 2:
            raise ValueError('This scene has reached its simultaneous audio input allowance.')
        self._cancel(cancelled)
        # Muting/cutting one track never shifts another clip or shortens a scene.
        return self._save(project, replace(scene, cues=cues,
            minimum_duration=max(scene.minimum_duration, cue.start + total / project.sample_rate)))

    def cut_range(self, project, scene_id, clip_id, start=0, end=None, *, cancelled=lambda: False):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        cue = self._selected(scene, clip_id)
        first, last, total = self._range(project, cue, start, end, cancelled=cancelled)
        self.copy_range(project, scene_id, clip_id, start, end, cancelled=cancelled)
        return self._remove_range(project, scene, cue, first, last, total, cancelled=cancelled)

    def silence_range(self, project, scene_id, clip_id, start=0, end=None, *, cancelled=lambda: False):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        cue = self._selected(scene, clip_id)
        first, last, total = self._range(project, cue, start, end, cancelled=cancelled)
        return self._remove_range(project, scene, cue, first, last, total, cancelled=cancelled)

    def trim_range(self, project, scene_id, clip_id, start=0, end=None, *, cancelled=lambda: False):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        cue = self._selected(scene, clip_id)
        first, last, total = self._range(project, cue, start, end, cancelled=cancelled)
        if first == 0 and last == total:
            return project
        trimmed = self._portion(cue, first, last, total, project.sample_rate)
        self._cancel(cancelled)
        return self._save(project, replace(scene,
            cues=tuple(trimmed if item.id == cue.id else item for item in scene.cues),
            minimum_duration=max(scene.minimum_duration, cue.start + total / project.sample_rate)))

    def preview_range(self, project, scene_id, clip_id, start=0, end=None, *,
                      dry=False, cancelled=lambda: False):
        if type(dry) is not bool:
            raise ValueError('Choose explicitly whether to bypass clip effects for preview.')
        project = self._current(project)
        cue = self._selected(self._scene(project, scene_id), clip_id)
        first, last, total = self._range(project, cue, start, end, cancelled=cancelled)
        selected = self._portion(cue, first, last, total, project.sample_rate)
        self._cancel(cancelled)
        return self._preview(project, selected, dry=dry, cancelled=cancelled)

    def crossfade(self, project, scene_id, first_id, second_id, seconds, *, cancelled=lambda: False):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        if first_id == second_id:
            raise ValueError('Choose two different clips for a crossfade.')
        left, right = (self._selected(scene, first_id), self._selected(scene, second_id))
        left_info = self._validate_selection(left, cancelled=cancelled)
        right_info = self._validate_selection(right, cancelled=cancelled)
        left_frames = round((left_info.duration - left.trim_start if left.duration is None
                             else left.duration) * project.sample_rate)
        right_frames = round((right_info.duration - right.trim_start if right.duration is None
                              else right.duration) * project.sample_rate)
        overlap = round(timeline_seconds(seconds, project.sample_rate, 'Crossfade') * project.sample_rate)
        if not 0 < overlap < min(left_frames, right_frames):
            raise ValueError('Crossfade must be at least one sample and shorter than both clips.')
        changed = {
            left.id: replace(left, fade_out=overlap / project.sample_rate),
            right.id: replace(right, start=(round(left.start * project.sample_rate)
                              + left_frames - overlap) / project.sample_rate,
                              fade_in=overlap / project.sample_rate),
        }
        self._cancel(cancelled)
        return self._save(project, replace(scene, cues=tuple(changed.get(item.id, item) for item in scene.cues)))

    def set_marker(self, project, scene_id, position, label, *, marker_id=None, cancelled=lambda: False):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        if marker_id is not None and marker_id not in {item.id for item in scene.markers}:
            raise ValueError('Choose an existing marker to change.')
        marker = Marker(marker_id or uuid4().hex,
            timeline_seconds(position, project.sample_rate, 'Marker position'), label)
        markers = tuple(item for item in scene.markers if item.id != marker.id) + (marker,)
        if len(markers) > 10000:
            raise ValueError('This scene has reached its marker limit.')
        if next((item for item in scene.markers if item.id == marker.id), None) == marker:
            return project
        self._cancel(cancelled)
        return self._save(project, replace(scene, markers=tuple(sorted(markers, key=lambda item: (item.position, item.id)))))

    def remove_marker(self, project, scene_id, marker_id, *, cancelled=lambda: False):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        if marker_id not in {item.id for item in scene.markers}:
            raise ValueError('Choose an existing marker to remove.')
        self._cancel(cancelled)
        return self._save(project, replace(scene, markers=tuple(item for item in scene.markers if item.id != marker_id)))

    def bypass_effects(self, project, scene_id, clip_id, enabled, *, cancelled=lambda: False):
        if type(enabled) is not bool:
            raise ValueError('Clip bypass must be an explicit boolean.')
        project = self._current(project)
        scene = self._scene(project, scene_id)
        cue = self._selected(scene, clip_id)
        if cue.effects_bypassed == enabled:
            return project
        self._cancel(cancelled)
        changed = replace(cue, effects_bypassed=enabled)
        return self._save(project, replace(scene, cues=tuple(changed if item.id == cue.id else item for item in scene.cues)))

    def join_rendered(self, project, scene_id, first_id, second_id, *, cancelled=lambda: False):
        """Explicitly flatten two selected clips into a new retained stereo derivative."""
        project = self._current(project)
        scene = self._scene(project, scene_id)
        if first_id == second_id:
            raise ValueError('Choose two different clips to join.')
        left, right = (self._selected(scene, first_id), self._selected(scene, second_id))
        if left.role != right.role:
            raise ValueError('Choose the same track role before joining rendered clips.')
        originals = tuple(self.service._cue_source(cue, cancelled=cancelled) for cue in (left, right))
        identities = tuple(_checked_digest(path, regular(path), cancelled) for path in originals)
        with retained_temporary_directory(prefix='.clip-join-', dir=self.service.cache) as folder:
            folder = Path(folder)
            previews = tuple(self._preview(project, cue, cancelled=cancelled,
                output=folder / (str(index) + '.wav')) for index, cue in enumerate((left, right)))
            runtime = self.service._runtime(reviewed_roots=(folder,))
            sizes = sum(regular(path).st_size for path in previews)
            with self.service._audio_admission(runtime, operation_id='clip-join:' + uuid4().hex,
                    disk_bytes=2 * sizes + runtime.budget.reserve_disk_bytes,
                    disk_path=folder, cancelled=cancelled) as stopped:
                joined = runtime.concatenate(previews, folder / 'joined.wav',
                    sample_rate=project.sample_rate, cancelled=stopped)
            retained = self.retain(joined.path, expected_sha256=joined.sha256, cancelled=cancelled)
            duration = joined.duration
            for path, expected in zip(originals, identities):
                if _checked_digest(path, regular(path), cancelled) != expected:
                    raise ValueError('A selected original changed while joining clips.')
        # Temporary/native owners are released before publishing project history.
        self._current(project)
        self._cancel(cancelled)
        cue = replace(left, path=str(retained), duration=duration, trim_start=0,
            gain_db=0, pan=0, fade_in=0, fade_out=0, effects=(), effects_bypassed=False,
            routing='stereo', title=(left.title[:490] + ' — joined') if left.title else 'Joined clips')
        cues = tuple(cue if item.id == left.id else item for item in scene.cues if item.id != right.id)
        return self._save(project, replace(scene, cues=cues))

    def close(self):
        """Close only an owned service; failed release stays available for retry."""
        self._closed = True
        self._clipboard = None
        self._undo.clear()
        self._redo.clear()
        if not self._service_closed:
            if self.service.close() is False:
                return False
            self._service_closed = True
        return True
