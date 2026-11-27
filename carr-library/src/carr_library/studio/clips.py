"""Non-destructive scene editing over the existing versioned Studio project."""
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
from uuid import uuid4

from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending
from ..audio.cleanup import retained_state_lock
from ..audio.process import publish_file
from ..durable_state import local_path, regular
from .backup_inputs import _checked_digest, _stamp
from .project import Cue, Project, Scene
from .render import ProductionService


class ClipEditor:
    """Retain the exact selected bytes; edits only change project instructions."""
    def __init__(self, library_root, *, service=None):
        self.service = service or ProductionService(Path(library_root) / 'state' / 'studio')

    def create(self, title='Audio clips'):
        project = Project(uuid4().hex, title, (),
            (Scene('scene-1', 'Audio clips', (), minimum_duration=1),),
            original_script='[Scene: Audio clips]\n[Duration: 1]', schema=2)
        return self.service.projects.save(project)

    def _current(self, project):
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
        return self.service.projects.save(updated, expected_revision=project.revision)

    def retain(self, selected, *, expected_sha256=None, cancelled=lambda: False, progress=None):
        """Make a verified private copy, without granting access to its neighbours."""
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
        return self.service.projects.save(updated, expected_revision=project.revision)

    def preview(self, project, scene_id, clip_id, *, cancelled=lambda: False):
        from ..audio.models import Track
        project = self._current(project)
        scene = self._scene(project, scene_id)
        cue = next((item for item in scene.cues if item.id == clip_id), None)
        if cue is None:
            raise ValueError('Select an existing clip in this scene.')
        source = self.service._cue_source(cue, cancelled=cancelled)
        runtime = self.service._runtime(reviewed_roots=(source.parent,))
        output = self.service.cache / ('clip-preview-' + uuid4().hex + '.wav')
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
                fade_in=cue.fade_in, fade_out=cue.fade_out),), output,
                sample_rate=project.sample_rate, cancelled=stopped)
            self.service._cue_source(cue, cancelled=stopped)
        return output
