"""Non-destructive scene editing over the existing versioned Studio project."""
from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from ..audio.models import AudioBudget, AudioCancelled, finite, integer
from ..audio.process import publish_file
from ..durable_state import local_path, regular, state_lock
from .project import Cue, Project, Scene, _timeline_effects
from .render import ProductionService


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _checked_digest(source, before, cancelled):
    digest, count = sha256(), 0
    if cancelled():
        raise InterruptedError('Studio input verification cancelled.')
    source = local_path(source, exists=True)
    with source.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A required Studio input changed before verification.')
        while block := stream.read(1024 * 1024):
            if cancelled():
                raise InterruptedError('Studio input verification cancelled.')
            count += len(block)
            if count > before.st_size:
                raise ValueError('A required Studio input grew during verification.')
            digest.update(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A required Studio input changed during verification.')
    if count != before.st_size or _stamp(regular(local_path(source, exists=True))) != _stamp(before):
        raise ValueError('A required Studio input changed during verification.')
    return digest.hexdigest()


def _pending_native_owners(error):
    """Find every existing native barrier without mistaking another failure."""
    from ..audio.cleanup import AudioCleanupPending
    waiting, seen, owners = [error], set(), {}
    while waiting:
        current = waiting.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, AudioCleanupPending):
            owners[id(current.cleanup_owner)] = current.cleanup_owner
        waiting.extend((current.__context__, current.__cause__))
    return tuple(owners.values())


def _pending_native_owner(error):
    """Compatibility helper for callers inspecting a single pending owner."""
    return next(iter(_pending_native_owners(error)), None)


def _retain_until_quiescent(ownership, owners):
    from threading import Lock
    retained = ownership.pop_all()
    remaining, guard = {id(owner) for owner in owners}, Lock()

    def completed(identity):
        with guard:
            remaining.discard(identity)
            ready = not remaining
        if ready:
            retained.close()

    for owner in owners:
        owner.add_quiescent_callback(lambda identity=id(owner): completed(identity))


def retain_audio_input(inputs, selected, *, inspect, expected_sha256=None,
                       cancelled=lambda: False, progress=None):
    """Retain only reviewed bytes; pending native cleanup keeps stage and lock."""
    from ..operations._locks import NativeLock
    from ..audio.audiobook import _owned_publication

    if not callable(inspect) or not callable(cancelled):
        raise ValueError('A bounded audio-format review and cancellation callback are required.')
    if expected_sha256 is not None and (type(expected_sha256) is not str
            or re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is None):
        raise ValueError('Invalid expected recording receipt digest.')
    if cancelled():
        raise AudioCancelled('Audio clip import cancelled.')
    inputs = local_path(inputs, exists=True)
    directory = regular(inputs, directory=True)
    directory_identity = directory.st_dev, directory.st_ino

    def guard_inputs():
        current = regular(local_path(inputs, exists=True), directory=True)
        if (current.st_dev, current.st_ino) != directory_identity:
            raise ValueError('The retained Studio input directory changed during the operation.')

    source = local_path(selected, exists=True)
    before = regular(source)
    if not 0 < before.st_size <= 8 * 1024**3:
        raise ValueError('Select a nonempty local audio file of at most 8 GiB.')
    if shutil.disk_usage(inputs).free < before.st_size + 64 * 1024**2:
        raise ValueError('Not enough free disk space to retain the selected original audio.')
    staged = inputs / ('.pending-' + uuid4().hex + '.audio')
    owned_identity = None
    publication_owner = None
    accepted_target = None
    target = None

    def prove_publication():
        if publication_owner is None or target is None:
            raise ValueError('An unreviewed link to the private Studio input stage was preserved.')
        try:
            # The same existing two-link proof used by media publication:
            # exact staged/target inode, captured directory, bounded bytes/hash.
            _owned_publication(staged, target, publication_owner, directory,
                               identity, before.st_size, check=guard_inputs)
        except (OSError, ValueError, RuntimeError) as error:
            raise ValueError('The retained Studio input is not this attempt\'s exact publication; '
                             'its private stage and existing files were preserved.') from error

    def discard_stage():
        nonlocal accepted_target
        if owned_identity is None:
            return
        guard_inputs()
        try:
            retained = staged.lstat()
        except FileNotFoundError:
            return
        if (retained.st_dev, retained.st_ino) != owned_identity:
            raise ValueError('The private Studio input stage was replaced; it was preserved.')
        linked = retained.st_nlink != 1 or (
            publication_owner is not None and target is not None and os.path.lexists(target))
        if linked:
            # Never weaken regular(): only this attempt's exact two links are
            # admitted here. Foreign, extra, replaced or changed links remain.
            prove_publication()
        else:
            regular(staged)
        staged.unlink()
        if linked:
            current = regular(local_path(target, exists=True))
            if ((current.st_dev, current.st_ino) != owned_identity
                    or current.st_size != count
                    or _checked_digest(target, current, lambda: False) != identity):
                raise ValueError('The retained Studio input changed during final stage cleanup.')
            accepted_target = _stamp(current)

    # The native backend uses the same lock pathname as the previous selected-
    # clip transaction, with a non-thread-local owner and held-descriptor checks.
    # CleanupOwner may release it later on its existing retry thread.
    with ExitStack() as ownership:
        lock = NativeLock(inputs / '.selected-clip.lock')
        if not lock.acquire():
            raise RuntimeError('This Studio input is already being changed by another operation.')
        ownership.callback(lock.release)
        guard_inputs()
        ownership.callback(discard_stage)
        try:
            digest, count = sha256(), 0
            with source.open('rb') as reader, staged.open('xb') as writer:
                opened_stage = os.fstat(writer.fileno())
                owned_identity = opened_stage.st_dev, opened_stage.st_ino
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
            guard_inputs()
            inspect(staged, cancelled=cancelled)
            guard_inputs()
            reviewed_stage = regular(staged)
            if _checked_digest(staged, reviewed_stage, cancelled) != identity:
                raise ValueError('Retained audio changed during decoder verification.')
            target = inputs / (identity + '.audio')
            if target.exists():
                retained = regular(target)
                if retained.st_size != count or _checked_digest(target, retained, cancelled) != identity:
                    raise ValueError('Previously retained audio differs from its content identity.')
                accepted_target = _stamp(retained)
            else:
                guard_inputs()
                publication_owner = reviewed_stage

                def before_commit():
                    if cancelled():
                        return True
                    guard_inputs()
                    if _stamp(regular(staged)) != _stamp(reviewed_stage):
                        raise ValueError('The reviewed Studio input stage changed before publication.')
                    return False

                try:
                    publish_file(staged, target, cancelled=before_commit)
                except (Exception, KeyboardInterrupt) as error:
                    if not os.path.lexists(target):
                        raise
                    prove_publication()
                    if not isinstance(error, AudioCancelled):
                        raise ValueError('Selected audio was retained with verified bytes, but '
                                         'publication finalization needs review. The retained input was kept.') from error
                    # A cancellation reported after this attempt's verified
                    # link cannot turn its completed retention into a discard.
            guard_inputs()
        except BaseException as error:
            pending_owners = _pending_native_owners(error)
            if pending_owners:
                _retain_until_quiescent(ownership, pending_owners)
            raise
    guard_inputs()
    result = local_path(target, exists=True)
    if accepted_target is None or _stamp(regular(result)) != accepted_target:
        raise ValueError('The retained Studio input changed before completion.')
    return result


class ClipEditor:
    """Retain the exact selected bytes; edits only change project instructions."""
    def __init__(self, library_root, *, service=None):
        self.service = service or ProductionService(Path(library_root) / 'state' / 'studio')
        self._owns_service, self._closed = service is None, False
        self._close_complete = False
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
        schema = max(project.schema, 3 if scene.effects or any(cue.effects for cue in scene.cues) else 2)
        updated = replace(project, schema=schema, revision=project.revision + 1,
            scenes=tuple(scene if old.id == scene.id else old for old in project.scenes))
        return self._publish(project, updated)

    def retain(self, selected, *, expected_sha256=None, cancelled=lambda: False, progress=None):
        """Make a verified private copy, without granting access to its neighbours."""
        self._open()
        self._cancel(cancelled)
        if expected_sha256 is not None and (type(expected_sha256) is not str
                or re.fullmatch(r'[0-9a-f]{64}', expected_sha256) is None):
            raise ValueError('Invalid expected recording receipt digest.')
        source = local_path(selected, exists=True)
        before = regular(source)
        if not 0 < before.st_size <= 8 * 1024**3:
            raise ValueError('Select a nonempty local audio file of at most 8 GiB.')
        identity = _checked_digest(source, before, cancelled)
        if expected_sha256 is not None and identity != expected_sha256:
            raise ValueError('The selected recording differs from its verified receipt.')
        runtime = self.service._runtime()

        def inspect(path, *, cancelled):
            # The one retained-input admission also covers its decoder. The
            # runtime already admits service.inputs; no neighbouring roots are
            # granted and no nested resource lease is acquired.
            info = runtime.inspect(path, cancelled=cancelled)
            self._audio_info(info)

        with self.service.audio_work(runtime, cancelled=cancelled,
                disk_bytes=before.st_size + 64 * 1024**2,
                disk_path=self.service.inputs):
            self._open()
            self._cancel(cancelled)
            if _stamp(regular(local_path(source, exists=True))) != _stamp(before):
                raise ValueError('Selected audio changed while waiting for resources.')
            if _checked_digest(source, before, cancelled) != identity:
                raise ValueError('Selected audio changed while waiting for resources.')
            return retain_audio_input(self.service.inputs, source, inspect=inspect,
                expected_sha256=identity, cancelled=cancelled, progress=progress)

    def _open(self):
        if self._closed:
            raise ValueError('This clip editor is closed.')

    @staticmethod
    def _audio_info(info):
        finite(info.duration, 'Selected audio duration', .000001)
        integer(info.sample_rate, 'Selected audio sample rate', 1, 2**31 - 1)
        integer(info.channels, 'Selected audio channel count', 1, 2)

    def _source_snapshot(self, cue, cancelled):
        source = self.service._cue_source(cue, cancelled=cancelled)
        before = regular(source)
        identity = _checked_digest(source, before, cancelled)
        return source, before, identity

    def _verify_source(self, cue, snapshot, cancelled):
        source, before, identity = snapshot
        current = self.service._cue_source(cue, cancelled=cancelled)
        if current != source or _stamp(regular(current)) != _stamp(before):
            raise ValueError('Selected audio changed while waiting or being inspected.')
        if _checked_digest(current, before, cancelled) != identity:
            raise ValueError('Selected audio changed while waiting or being inspected.')
        return source

    def _inspect_selection(self, cue, runtime, *, project=None, cancelled=lambda: False):
        self._open()
        self._cancel(cancelled)
        if project is not None:
            self._current(project)
        snapshot = self._source_snapshot(cue, cancelled)
        with self.service.audio_work(runtime, cancelled=cancelled):
            self._open()
            self._cancel(cancelled)
            if project is not None:
                self._current(project)
            source = self._verify_source(cue, snapshot, cancelled)
            info = runtime.inspect(source, cancelled=cancelled)
            self._audio_info(info)
            self._verify_source(cue, snapshot, cancelled)
            if project is not None:
                self._current(project)
            available = info.duration - cue.trim_start
            selected = available if cue.duration is None else cue.duration
            if available <= 0 or selected > available + 1 / info.sample_rate:
                raise ValueError('Clip trim or duration exceeds its retained original audio.')
            if max(cue.fade_in, cue.fade_out) > selected:
                raise ValueError('Clip fade exceeds its selected audio.')
            self._cancel(cancelled)
        return snapshot, info

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
        self._validate_selection(cue, project=project, cancelled=cancelled)
        if cancelled():
            raise AudioCancelled('Audio clip import cancelled before project publication.')
        return self._save(project, replace(scene, cues=(*scene.cues, cue)))

    def _validate_selection(self, cue, *, project=None, cancelled=lambda: False):
        _, info = self._inspect_selection(cue, self.service._runtime(),
                                         project=project, cancelled=cancelled)
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
        self._validate_selection(updated, project=project, cancelled=cancelled)
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
        if type(dry) is not bool:
            raise ValueError('Choose explicitly whether to bypass clip effects for preview.')
        scene = self._scene(project, scene_id)
        cue = self._selected(scene, clip_id)
        runtime = self.service._runtime()
        # Inspect under a separate metadata lease, then release it before
        # waiting for the actual selected PCM output allowance.
        snapshot, info = self._inspect_selection(cue, runtime, project=project, cancelled=cancelled)
        seconds = info.duration - cue.trim_start if cue.duration is None else cue.duration
        frames = round(seconds * project.sample_rate)
        if frames <= 0:
            raise ValueError('The selected clip must retain at least one output sample.')
        pcm_bytes = frames * 2 * 2 + 4096
        # The final PCM and its private staging copy can coexist. Include the
        # bounded graph/scratch allowance and configured free-space reserve.
        disk_bytes = pcm_bytes * 2 + 1024**2 + runtime.budget.reserve_disk_bytes
        if disk_bytes > runtime.budget.max_output_bytes:
            raise ValueError('This clip preview exceeds the configured audio output allowance.')
        with self.service.audio_work(runtime, cancelled=cancelled,
                disk_bytes=disk_bytes, disk_path=self.service.cache):
            self._current(project)
            self._cancel(cancelled)
            source = self._verify_source(cue, snapshot, cancelled)
            output = self.service.cache / ('clip-preview-' + uuid4().hex + '.wav')
            runtime.mix((Track(source, gain_db=cue.gain_db, pan=cue.pan, role=cue.role,
                trim_start=cue.trim_start, duration=cue.duration,
                fade_in=cue.fade_in, fade_out=cue.fade_out,
                effects=() if dry else cue.effects),), output,
                sample_rate=project.sample_rate, cancelled=cancelled)
            # Completed private cache bytes are retained if a late edit/cancel
            # invalidates this preview; they never become a playback approval.
            self._current(project)
            self._verify_source(cue, snapshot, cancelled)
            self._cancel(cancelled)
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

    def sample_rate(self, project, value):
        project = self._current(project)
        updated = replace(project, sample_rate=value)
        if updated == project:
            return project
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
        self._validate_selection(copied, project=project, cancelled=cancelled)
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
        self._validate_selection(cue, project=project, cancelled=cancelled)
        self._cancel(cancelled)
        return self._save(project, replace(scene, cues=(*scene.cues, cue)))

    def split(self, project, scene_id, clip_id, seconds, *, cancelled=lambda: False):
        """Split instructions at an output sample; original bytes remain shared."""
        project = self._current(project)
        scene = self._scene(project, scene_id)
        cue = self._selected(scene, clip_id)
        if any(effect.enabled for effect in cue.effects):
            raise ValueError('Bypass the clip effect chain before splitting so its processing state is not reset silently.')
        if len(scene.cues) >= AudioBudget().max_inputs - 2:
            raise ValueError('This scene has reached its simultaneous audio input allowance.')
        info = self._validate_selection(cue, project=project, cancelled=cancelled)
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
        if left.effects != right.effects or any(effect.enabled for effect in (*left.effects, *right.effects)):
            raise ValueError('Join requires matching bypassed effect chains; active processing cannot be merged silently.')
        first = self._validate_selection(left, project=project, cancelled=cancelled)
        second = self._validate_selection(right, project=project, cancelled=cancelled)
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
        # Effects retain immutable parameter mappings, so cues need value
        # comparison rather than hashing when checking restored selections.
        existing = tuple(cue for scene in project.scenes for cue in scene.cues)
        for scene in snapshot.scenes:
            for cue in scene.cues:
                self._cancel(cancelled)
                if cue not in existing:
                    self._validate_selection(cue, project=project, cancelled=cancelled)
        self._cancel(cancelled)
        restored = replace(snapshot, schema=max(project.schema, snapshot.schema, 2), revision=project.revision + 1)
        saved = self.service.projects.save(restored, expected_revision=project.revision,
                                          expected_fingerprint=project.fingerprint)
        stack.pop()
        opposite.append(change)
        self._history_head = saved.id, saved.fingerprint
        return saved

    def close(self):
        if self._close_complete:
            return
        # A failed owned release forbids new edits but keeps explicit Close
        # retry available; the service retains the exact registry/child owner.
        self._closed = True
        self._clipboard = None
        self._undo.clear()
        self._redo.clear()
        if self._owns_service:
            self.service.close()
        self._close_complete = True
