"""Non-destructive scene editing over the existing versioned Studio project."""
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
from uuid import uuid4

from ..audio.models import AudioBudget, AudioCancelled, finite
from ..audio.process import publish_file
from ..durable_state import local_path, regular, state_lock
from .backup_inputs import _checked_digest, _stamp
from .project import Cue, Project, Scene
from .render import ProductionService


class ClipEditor:
    """Retain the exact selected bytes; edits only change project instructions."""
    def __init__(self, library_root, *, service=None):
        self.service = service or ProductionService(Path(library_root) / 'state' / 'studio')
        self._owns_service, self._closed = service is None, False
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
        if not isinstance(project, Project):
            raise ValueError('Choose an existing saved clip project.')
        if self._closed:
            raise ValueError('This clip editor is closed.')
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
        updated = replace(project, schema=2, revision=project.revision + 1,
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
        with state_lock(inputs, 'selected-clip'):
            try:
                digest, count = sha256(), 0
                with source.open('rb') as reader, staged.open('xb') as writer:
                    if _stamp(os.fstat(reader.fileno())) != _stamp(before):
                        raise ValueError('Selected audio changed before retention.')
                    while block := reader.read(1024 * 1024):
                        if cancelled():
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
                if count != before.st_size or _checked_digest(source, before, cancelled) != identity:
                    raise ValueError('Selected audio changed during retention.')
                if expected_sha256 is not None and expected_sha256 != identity:
                    raise ValueError('The selected recording differs from its verified receipt.')
                runtime = self.service._runtime(reviewed_roots=(inputs,))
                info = runtime.inspect(staged, cancelled=cancelled)
                if info.duration <= 0 or info.channels not in (1, 2):
                    raise ValueError('Select readable mono or stereo audio.')
                if _checked_digest(staged, regular(staged), cancelled) != identity:
                    raise ValueError('Retained audio changed during decoder verification.')
                target = inputs / (identity + '.audio')
                if target.exists():
                    retained = regular(target)
                    if retained.st_size != count or _checked_digest(target, retained, cancelled) != identity:
                        raise ValueError('Previously retained audio differs from its content identity.')
                else:
                    publish_file(staged, target, cancelled=cancelled)
            finally:
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
        info = self.service._runtime().inspect(self.service._cue_source(cue), cancelled=cancelled)
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
        updated = replace(project, schema=2, revision=project.revision + 1,
                          scenes=tuple(replacements.get(scene.id, scene) for scene in project.scenes))
        return self._publish(project, updated)

    def preview(self, project, scene_id, clip_id, *, cancelled=lambda: False):
        from ..audio.models import Track
        project = self._current(project)
        scene = self._scene(project, scene_id)
        cue = next((item for item in scene.cues if item.id == clip_id), None)
        if cue is None:
            raise ValueError('Select an existing clip in this scene.')
        source = self.service._cue_source(cue)
        runtime = self.service._runtime()
        output = self.service.cache / ('clip-preview-' + uuid4().hex + '.wav')
        runtime.mix((Track(source, gain_db=cue.gain_db, pan=cue.pan, role=cue.role,
            trim_start=cue.trim_start, duration=cue.duration,
            fade_in=cue.fade_in, fade_out=cue.fade_out),), output,
            sample_rate=project.sample_rate, cancelled=cancelled)
        return output


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
        source = self.service._cue_source(cue)
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
        if len(scene.cues) >= AudioBudget().max_inputs - 2:
            raise ValueError('This scene has reached its simultaneous audio input allowance.')
        info = self._validate_selection(cue, cancelled=cancelled)
        duration = info.duration - cue.trim_start if cue.duration is None else cue.duration
        position = round(finite(seconds, 'Clip split position', .000001) * project.sample_rate) / project.sample_rate
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
        first = self._validate_selection(left, cancelled=cancelled)
        second = self._validate_selection(right, cancelled=cancelled)
        left_length = first.duration - left.trim_start if left.duration is None else left.duration
        right_length = second.duration - right.trim_start if right.duration is None else right.duration
        if (left.path != right.path or left.role != right.role or left.gain_db != right.gain_db
                or left.pan != right.pan or left.fade_out != 0 or right.fade_in != 0
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
        existing = {cue for scene in project.scenes for cue in scene.cues}
        for scene in snapshot.scenes:
            for cue in scene.cues:
                self._cancel(cancelled)
                if cue not in existing:
                    self._validate_selection(cue, cancelled=cancelled)
        self._cancel(cancelled)
        restored = replace(snapshot, schema=2, revision=project.revision + 1)
        saved = self.service.projects.save(restored, expected_revision=project.revision,
                                          expected_fingerprint=project.fingerprint)
        stack.pop()
        opposite.append(change)
        self._history_head = saved.id, saved.fingerprint
        return saved

    def close(self):
        if self._closed:
            return
        if self._owns_service:
            self.service.close()
        self._closed = True
        self._clipboard = None
        self._undo.clear()
        self._redo.clear()
