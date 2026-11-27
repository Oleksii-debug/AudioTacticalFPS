"""Non-destructive scene editing over the existing versioned Studio project."""
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import shutil
from uuid import uuid4

from ..audio.models import AudioBudget, AudioCancelled
from ..audio.process import publish_file
from ..durable_state import local_path, regular, state_lock
from .managed_inputs import _checked_digest, _stamp, managed_input_sha256
from .project import Cue, Project, Scene, _timeline_effects
from .render import ProductionService


def retain_audio_input(inputs, selected, *, inspect, expected_sha256=None,
                       cancelled=lambda: False, progress=None):
    """Retain reviewed audio in the existing Studio content-addressed inputs.

    The caller validates its supported audio format through inspect. This is the
    same selected-file copy transaction used by ClipEditor, not path approval
    for any sibling file or a decoder invocation during passive state restore.
    """
    if not callable(inspect):
        raise ValueError('An audio-format review is required before retention.')
    source = local_path(selected, exists=True)
    before = regular(source)
    if not 0 < before.st_size <= 8 * 1024**3:
        raise ValueError('Select a nonempty local audio file of at most 8 GiB.')
    if shutil.disk_usage(inputs).free < before.st_size + 64 * 1024**2:
        raise ValueError('Not enough free disk space to retain the selected original audio.')
    inputs = local_path(inputs, exists=True)
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
            inspect(staged, cancelled=cancelled)
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


class ClipEditor:
    """Retain the exact selected bytes; edits only change project instructions."""
    def __init__(self, library_root, *, service=None):
        self.service = service or ProductionService(Path(library_root) / 'state' / 'studio')

    def create(self, title='Audio clips'):
        self.service._check_open()
        project = Project(uuid4().hex, title, (),
            (Scene('scene-1', 'Audio clips', (), minimum_duration=1),),
            original_script='[Scene: Audio clips]\n[Duration: 1]', schema=2)
        return self.service.projects.save(project)

    def _current(self, project):
        self.service._check_open()
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

    @staticmethod
    def _portable(project):
        try:
            for scene in project.scenes:
                for cue in scene.cues:
                    managed_input_sha256(cue.path)
        except ValueError:
            raise ValueError('This legacy project contains non-managed cue paths. '
                'Create a new portable clip project and explicitly select its original audio; '
                'legacy sources are not opened or silently migrated.') from None

    def _save(self, project, scene, *, portable=False):
        if portable:
            self._portable(project)
        updated = replace(project, schema=max(2, project.schema) if portable else project.schema, revision=project.revision + 1,
            scenes=tuple(scene if old.id == scene.id else old for old in project.scenes))
        return self.service.projects.save(updated, expected_revision=project.revision)

    def retain(self, selected, *, expected_sha256=None, cancelled=lambda: False, progress=None):
        """Make a verified private copy, without granting access to its neighbours."""
        self.service._check_open()
        def inspect(path, *, cancelled):
            info = self.service._runtime().inspect(path, cancelled=cancelled)
            if info.duration <= 0 or info.channels not in (1, 2):
                raise ValueError('Select readable mono or stereo audio.')
        return retain_audio_input(self.service.inputs, selected, inspect=inspect,
            expected_sha256=expected_sha256, cancelled=cancelled, progress=progress)

    def add(self, project, scene_id, selected, *, expected_sha256=None,
            cancelled=lambda: False, progress=None, **settings):
        project = self._current(project)
        self._portable(project)
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
        cue = replace(cue, path='inputs/' + retained.name)
        self._validate_selection(cue, cancelled=cancelled)
        if cancelled():
            raise AudioCancelled('Audio clip import cancelled before project publication.')
        return self._save(project, replace(scene, cues=(*scene.cues, cue)), portable=True)

    def _validate_selection(self, cue, *, cancelled=lambda: False):
        source = self.service._cue_source(cue, cancelled=cancelled)
        info = self.service._runtime().inspect(source, cancelled=cancelled)
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
        self._portable(project)
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
            cues=tuple(updated if old.id == clip_id else old for old in scene.cues)), portable=True)

    def remove(self, project, scene_id, clip_id):
        project = self._current(project)
        scene = self._scene(project, scene_id)
        if sum(cue.id == clip_id for cue in scene.cues) != 1:
            raise ValueError('Select one unambiguous existing clip in this scene.')
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
        matches = [item for item in source.cues if item.id == clip_id]
        if len(matches) != 1:
            raise ValueError('Select one unambiguous existing clip in this scene.')
        cue = matches[0]
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
        updated = replace(project, revision=project.revision + 1,
                          scenes=tuple(replacements.get(scene.id, scene) for scene in project.scenes))
        return self.service.projects.save(updated, expected_revision=project.revision)

    @staticmethod
    def _cancel(cancelled):
        if cancelled():
            raise AudioCancelled('Studio effect operation cancelled before publication.')

    @staticmethod
    def _editable_effects(project):
        if any(line.source_ref.startswith('carr-book-audio:')
               for scene in project.scenes for line in scene.lines):
            raise ValueError('This project belongs to a reviewed book-audio plan. '
                'Its source and effects require a separate BookPlan review; '
                'the generic Studio editor cannot change that approved plan.')

    def effect_capabilities(self, project, *, cancelled=lambda: False):
        """Inspect only the configured runtime under current shared admission."""
        from ..operations import ResourceRequest
        self._cancel(cancelled)
        project = self._current(project)
        self._portable(project)
        self._editable_effects(project)
        runtime = self.service._runtime()
        broker = self.service._admission_broker(cancelled)
        request = ResourceRequest(cpu_slots=runtime.budget.threads,
            memory_bytes=max(512 * 1024**2, 2 * runtime.budget.max_alloc_bytes))
        with broker.reserve(request, operation_id='studio-effects:' + uuid4().hex,
                            cancelled=cancelled, timeout=60) as lease:
            self._cancel(lease.cancelled)
            self._current(project)
            return runtime.capabilities(cancelled=lease.cancelled)['effects']

    def set_effects(self, project, scope, effects, *, scene_id=None, clip_id=None, role_id=None,
                    cancelled=lambda: False):
        """Save one explicitly reviewed chain in the existing project history."""
        self._cancel(cancelled)
        project = self._current(project)
        self._portable(project)
        self._editable_effects(project)
        chain = _timeline_effects(effects, project.sample_rate)
        if scope == 'master':
            previous = project.effects
            changes = {'effects': chain}
        elif scope == 'role':
            selected = next((role for role in project.roles if role.id == role_id), None)
            if selected is None:
                raise ValueError('Choose an existing speaking role.')
            previous = selected.effects
            changes = {'roles': tuple(replace(role, effects=chain) if role.id == role_id else role
                                      for role in project.roles)}
        elif scope in ('scene', 'clip'):
            scene = self._scene(project, scene_id)
            if scope == 'scene':
                previous = scene.effects
                changed = replace(scene, effects=chain)
            else:
                matches = [cue for cue in scene.cues if cue.id == clip_id]
                if len(matches) != 1:
                    raise ValueError('Choose one existing clip in this scene.')
                previous = matches[0].effects
                changed = replace(scene, cues=tuple(
                    replace(cue, effects=chain) if cue.id == clip_id else cue for cue in scene.cues))
            changes = {'scenes': tuple(changed if item.id == scene.id else item
                                       for item in project.scenes)}
        else:
            raise ValueError('Choose clip, role, scene or master effects.')
        if chain == previous:
            return project
        updated = replace(project, schema=3, revision=project.revision + 1, **changes)
        self._cancel(cancelled)
        return self.service.projects.save(updated, expected_revision=project.revision,
                                          expected_fingerprint=project.fingerprint)

    def preview(self, project, scene_id, clip_id, *, cancelled=lambda: False, dry=False):
        from ..audio.models import Track
        from ..operations import ResourceRequest
        if type(dry) is not bool:
            raise ValueError('Choose explicitly whether to bypass clip effects for preview.')
        self._cancel(cancelled)
        project = self._current(project)
        self._portable(project)
        scene = self._scene(project, scene_id)
        cue = next((item for item in scene.cues if item.id == clip_id), None)
        if cue is None:
            raise ValueError('Select an existing clip in this scene.')
        runtime = self.service._runtime()
        broker = self.service._admission_broker(cancelled)
        operation_id = 'studio-clip-preview:' + uuid4().hex
        inspection = ResourceRequest(cpu_slots=runtime.budget.threads,
            memory_bytes=max(512 * 1024**2, 2 * runtime.budget.max_alloc_bytes))
        with broker.reserve(inspection, operation_id=operation_id,
                            cancelled=cancelled, timeout=60) as lease:
            self._cancel(lease.cancelled)
            self._current(project)
            source = self.service._cue_source(cue, cancelled=lease.cancelled, schema=project.schema)
            info = runtime.inspect(source, cancelled=lease.cancelled)
        selected = info.duration - cue.trim_start if cue.duration is None else cue.duration
        if selected <= 0 or cue.trim_start + selected > info.duration + 1 / info.sample_rate:
            raise ValueError('Clip preview selection exceeds its retained original.')
        frames = round(selected * project.sample_rate)
        output_bytes = frames * 2 * 2 + 65536
        if frames <= 0 or output_bytes > runtime.budget.max_output_bytes:
            raise ValueError('Clip preview exceeds the configured output budget.')
        request = ResourceRequest(cpu_slots=runtime.budget.threads,
            memory_bytes=inspection.memory_bytes,
            disk_bytes=2 * output_bytes + runtime.budget.reserve_disk_bytes,
            disk_path=self.service.cache)
        previous_budget = runtime.budget
        try:
            with broker.reserve(request, operation_id=operation_id,
                                cancelled=cancelled, timeout=60) as lease:
                self._cancel(lease.cancelled)
                self._current(project)
                source = self.service._cue_source(cue, cancelled=lease.cancelled, schema=project.schema)
                output = self.service.cache / ('clip-preview-' + uuid4().hex + '.wav')
                runtime.budget = replace(previous_budget, max_output_bytes=output_bytes)
                runtime.mix((Track(source, gain_db=cue.gain_db, pan=cue.pan, role=cue.role,
                    trim_start=cue.trim_start, duration=cue.duration,
                    fade_in=cue.fade_in, fade_out=cue.fade_out,
                    effects=() if dry else cue.effects),), output,
                    sample_rate=project.sample_rate, cancelled=lease.cancelled)
                return output
        finally:
            runtime.budget = previous_budget
