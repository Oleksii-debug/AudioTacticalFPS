"""Read-only publication adapters for the two existing production journals.

No project is converted into another project's identity. A private immutable
view supplies the publisher's measured-line layout while provenance retains the
original project fingerprint, exact source spans and accepted narration mapping.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
import re

from ..durable_state import encode, local_path, read_json, regular
from ..audio.projects import Project, ProjectStore, identity, plain
from ..audio.narration import spoken_cue
from .project import _id


@dataclass(frozen=True)
class _LineView:
    id: str
    role: str
    text: str
    source_ref: str
    source_details: dict


@dataclass(frozen=True)
class _SceneView:
    id: str
    title: str
    lines: tuple


@dataclass(frozen=True)
class _ProjectView:
    id: str
    title: str
    original_script: str
    revision: int
    sample_rate: int
    roles: tuple
    scenes: tuple
    fingerprint: str


def _details(project, cue, spoken):
    return {
        'original_text': cue.text,
        'source_start': cue.source_start,
        'source_end': cue.source_end,
        'source_mapping': spoken['mapping'],
        'reading_copy_sha256': spoken['derivative_sha256'],
    }


def _leaf(value):
    if (not isinstance(value, str) or not value or value in ('.', '..')
            or any(char in value for char in '/\\:') or any(ord(char) < 32 for char in value)):
        raise ValueError('Invalid primary Studio artifact name.')
    return value


def validate_primary_provenance(provenance, text_nodes):
    """Pure checks against the embedded original project; no file paths opened."""
    source = provenance.get('source', {})
    if source.get('family') != 'director':
        return
    project = Project.from_dict(source.get('project_snapshot'))
    if (project.id != source.get('project_id') or project.fingerprint != source.get('project_sha256')
            or project.script_sha256 != source.get('source_script_sha256')
            or project.script != provenance.get('original_script')
            or project.source != source.get('source_reference')
            or not re.fullmatch('[0-9a-f]{64}', source.get('cache_receipts_sha256', ''))):
        raise ValueError('Primary Studio publication lost its original project or source identity.')
    lines = [row for row in provenance.get('intervals', []) if row.get('kind') == 'line']
    expected = [(scene, cue) for scene in project.scenes for cue in scene.cues if cue.kind == 'speech']
    if len(lines) != len(expected):
        raise ValueError('Primary Studio publication has incomplete original cue coverage.')
    for row, (scene, cue) in zip(lines, expected):
        spoken = spoken_cue(project, cue)
        if (row.get('scene') != scene.id or row.get('line') != cue.id or row.get('role') != cue.role
                or row.get('source_details') != _details(project, cue, spoken)
                or text_nodes.get(row.get('id')) != spoken['text']):
            raise ValueError('A primary Studio overlay changed its accepted reading copy or original source mapping.')


class DirectorPublicationSource:
    """One primary project, using its existing render lock and immutable jobs."""

    def __init__(self, library_root, project_id):
        self.private_root = local_path(library_root, exists=True)
        self.store = ProjectStore(self.private_root)
        identity(project_id, 'project identity')
        self.project_id = project_id
        self.folder = self.store.path(project_id)
        self.root = self.private_root / 'state' / 'studio'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime_factory = None

    def get(self, job_id):
        from ..audio.projects import read_json as read_primary_json
        identity(job_id, 'job identity')
        jobs = self.folder / 'jobs'
        plain(jobs, directory=True)
        state = read_primary_json(jobs / (job_id + '.json'))
        if (type(state) is not dict or state.get('version') != 1 or state.get('id') != job_id
                or state.get('project_id') != self.project_id
                or state.get('state') not in ('queued', 'running', 'paused', 'failed', 'complete')):
            raise ValueError('Invalid primary Studio production journal.')
        project = Project.from_dict(state['project'])
        if project.id != self.project_id or state.get('project_fingerprint') != project.fingerprint:
            raise ValueError('Primary Studio journal differs from its original project.')
        return state

    def publication_jobs(self):
        rows = []
        for path in sorted((self.folder / 'jobs').glob('*.json')):
            state = self.get(path.stem)
            if state['state'] == 'complete':
                project = Project.from_dict(state['project'])
                rows.append({'id': state['id'], 'title': project.title,
                    'duration': state.get('result', {}).get('duration_seconds'),
                    'lines': sum(cue.kind == 'speech' for scene in project.scenes for cue in scene.cues)})
        return tuple(rows)

    @contextmanager
    def publication_lock(self, job_id):
        from ..operations._locks import NativeLock
        self.get(job_id)
        # Same render lock and native identity checks; release may run on the
        # retained cleanup callback thread after a pending decoder exits.
        lock = NativeLock(self.folder / '.render.lock')
        if not lock.acquire():
            raise RuntimeError('This primary Studio project already has an active render or publication.')
        try:
            yield
        finally:
            lock.release()

    def publication_source_fields(self, state):
        project = Project.from_dict(state['project'])
        return {'family': 'director', 'project_snapshot': state['project'],
                'source_reference': dict(project.source),
                'timeline_artifact': 'production.json',
                'cache_receipts_sha256': sha256(encode(state['steps'])).hexdigest()}

    def publication_snapshot(self, state, budget, check, progress):
        from .publishing import _read, _json, _segments, _text, _HASH
        from ..audio.models import finite

        restored = state.get('restore_requires_new_output', False)
        if type(restored) is not bool or restored:
            raise ValueError('This restored primary Studio job requires a newly approved output before publication.')
        if state.get('state') != 'complete':
            raise ValueError('Choose a completed primary Studio job.')
        project = Project.from_dict(state['project'])
        destination = local_path(state['output_dir'], exists=True)
        regular(destination, directory=True)
        if read_json(destination / '.production-job.json') != {
                'job_id': state['id'], 'project_fingerprint': project.fingerprint}:
            raise ValueError('The primary Studio output belongs to another production.')
        artifacts = state.get('outputs')
        if not isinstance(artifacts, list) or not 3 <= len(artifacts) <= 100000:
            raise ValueError('The primary Studio job lacks a bounded output receipt graph.')
        paths, names, total, metadata = {}, set(), 0, None
        for index, artifact in enumerate(artifacts, 1):
            check()
            if (type(artifact) is not dict or set(artifact) != {'file', 'sha256', 'size_bytes'}
                    or type(artifact['size_bytes']) is not int or artifact['size_bytes'] < 0
                    or not isinstance(artifact['sha256'], str) or not _HASH.fullmatch(artifact['sha256'])):
                raise ValueError('Invalid primary Studio artifact receipt.')
            name = _leaf(artifact['file'])
            if name.casefold() in names:
                raise ValueError('Duplicate primary Studio artifact.')
            names.add(name.casefold())
            total += artifact['size_bytes']
            if total > budget.max_output_bytes:
                raise ValueError('The complete source artifact graph exceeds the publication byte budget.')
            path = local_path(destination / name, root=destination, exists=True)
            receipt = {'path': str(path), 'size': artifact['size_bytes'], 'sha256': artifact['sha256']}
            paths[name] = (path, receipt)
            progress(f'Verifying primary Studio source artifact {index}/{len(artifacts)}')
            raw = _read(path, receipt, check, collect=name == 'production.json')
            if name == 'production.json':
                metadata = _json(raw)
        if not {'master.wav', 'production.json', 'narration.srt'} <= paths.keys():
            raise ValueError('The completed primary Studio job lacks its master, timeline or narration receipt.')
        result = state.get('result')
        if (not isinstance(result, dict) or result.get('state') != 'complete'
                or result.get('job_id') != state['id'] or result.get('project_id') != project.id
                or result.get('output_dir') != state['output_dir'] or result.get('outputs') != artifacts):
            raise ValueError('The primary Studio completion receipt differs from its original job.')
        expected_header = {'version': 1, 'kind': 'research-audio-production', 'project_id': project.id,
            'revision': project.revision, 'project_fingerprint': project.fingerprint,
            'script_sha256': project.script_sha256, 'job_id': state['id'], 'mode': 'LOCAL_ONLY',
            'sample_rate': project.settings.sample_rate, 'channels': project.settings.channels}
        # Compare JSON declarations, not Python tuple/list representations.
        # Empty legacy chains keep the historical omitted effects block.
        expected_effects = None
        if (project.effects or any(role.effects for role in project.roles)
                or any(scene.effects for scene in project.scenes)):
            expected_effects = {
                'master': [effect.to_dict() for effect in project.effects],
                'roles': {role.id: [effect.to_dict() for effect in role.effects] for role in project.roles},
                'scenes': {scene.id: [effect.to_dict() for effect in scene.effects] for scene in project.scenes}}
        if (not isinstance(metadata, dict) or any(metadata.get(k) != v for k, v in expected_header.items())
                or encode(metadata.get('voices')) != encode([role.to_dict() for role in project.roles])
                or (expected_effects is None and 'effects' in metadata)
                or (expected_effects is not None and encode(metadata.get('effects')) != encode(expected_effects))
                or metadata.get('outputs') != [row for row in artifacts if row['file'] not in ('production.json', 'narration.srt')]):
            raise ValueError('Primary Studio timeline does not belong to the original project and completed artifacts.')
        frames, rate = metadata.get('frames'), project.settings.sample_rate
        if type(frames) is not int or frames <= 0:
            raise ValueError('The primary Studio master requires a measured positive frame count.')
        duration = finite(metadata.get('duration_seconds'), 'Master duration', 1 / rate)
        if abs(duration - frames / rate) > 1 / rate or result.get('duration_seconds') != duration:
            raise ValueError('Primary Studio duration differs from the measured master frames.')
        chapters, measured = metadata.get('chapters'), metadata.get('timings')
        if not isinstance(chapters, list) or len(chapters) != len(project.scenes) or not isinstance(measured, list):
            raise ValueError('Primary Studio measured chapters or cue timings are incomplete.')
        expected = [(scene, cue) for scene in project.scenes for cue in scene.cues if cue.kind == 'speech']
        if len(measured) != len(expected):
            raise ValueError('Every primary Studio speech cue requires its original measured interval.')
        by_scene = {scene.id: [] for scene in project.scenes}
        lines = []
        roles = {role.id: role for role in project.roles}
        for row, (scene, cue) in zip(measured, expected):
            check()
            spoken = spoken_cue(project, cue)
            details = _details(project, cue, spoken)
            if (not isinstance(row, dict) or row.get('scene_id') != scene.id or row.get('cue_id') != cue.id
                    or row.get('role') != roles[cue.role].name or row.get('text') != spoken['text']
                    or any(row.get(k) != v for k, v in details.items())):
                raise ValueError('Primary Studio timing changed the original cue or accepted narration source mapping.')
            line = _LineView(cue.id, cue.role, spoken['text'], '', details)
            by_scene[scene.id].append(line)
            lines.append({'scene': scene.id, 'line': cue.id, 'role': cue.role, 'text': spoken['text'],
                          'source_ref': '', 'start': row.get('start_seconds'), 'end': row.get('end_seconds')})
        chapter_rows, cursor = [], 0
        for index, (scene, row) in enumerate(zip(project.scenes, chapters), 1):
            check()
            name = f'chapter-{index:04}.wav'
            if (not isinstance(row, dict) or row.get('id') != scene.id or row.get('title') != scene.title
                    or type(row.get('start_frame')) is not int or row['start_frame'] != cursor
                    or type(row.get('frames')) is not int or row['frames'] <= 0
                    or name not in paths or row.get('sha256') != paths[name][1]['sha256']):
                raise ValueError('A primary Studio chapter differs from its measured frame/artifact receipt.')
            chapter_rows.append({'id': scene.id, 'title': scene.title,
                                 'start': cursor / rate, 'end': (cursor + row['frames']) / rate})
            cursor += row['frames']
        if cursor != frames:
            raise ValueError('Primary Studio chapters do not exactly cover the measured master.')
        view = _ProjectView(project.id, project.title, project.script, project.revision, rate, project.roles,
                            tuple(_SceneView(scene.id, scene.title, tuple(by_scene[scene.id]))
                                  for scene in project.scenes), project.fingerprint)
        timeline = {'schema': 1, 'project': project.id, 'revision': project.revision,
                    'source_sha256': project.script_sha256, 'sample_rate': rate,
                    'channels': project.settings.channels, 'duration': duration,
                    'chapters': chapter_rows, 'lines': lines}
        _text(view.original_script, 'Original script')
        _segments(view, timeline, check)
        return view, timeline, paths

    def inspect_silence(self, job_id, *, threshold_dbfs, minimum_seconds,
                        budget=None, broker=None, cancelled=lambda: False, progress=None):
        """Measure the existing primary master through its publication authority."""
        from time import monotonic
        from ..audio import AudioBudget, AudioCancelled, AudioError
        from ..audio.cleanup import retained_context, retained_temporary_directory
        from ..audio.recording import _file_identity, _verified_reader
        from ..audio.silence import SilenceCriteria, runtime_for_inspection
        from ..operations import ResourceBroker, ResourceRequest
        from .publishing import _HASH, _json, _read

        criteria = SilenceCriteria(threshold_dbfs, minimum_seconds)
        selected = AudioBudget() if budget is None else budget
        if not isinstance(selected, AudioBudget):
            raise TypeError('Use the existing AudioBudget for silence inspection.')
        started, lease = monotonic(), None
        deadline = started + selected.timeout

        def base_check():
            if cancelled():
                raise AudioCancelled('Primary Studio silence inspection cancelled.')
            if monotonic() >= deadline:
                raise AudioError('Silence inspection exceeded its total time budget.')
            return False

        def check():
            base_check()
            if lease is not None:
                lease.checkpoint()
            return False

        def phase(message):
            check()
            if progress is not None:
                progress(message)
            check()

        base_check()
        self.get(job_id)  # Unknown identities must not create a job or output.
        with retained_context(self.publication_lock(job_id)):
            job_file = self.folder / 'jobs' / (job_id + '.json')
            stamp = _file_identity(plain(job_file))
            state = self.get(job_id)
            if _file_identity(plain(job_file)) != stamp:
                raise ValueError('The primary Studio job changed before silence inspection.')
            restored = state.get('restore_requires_new_output', False)
            if type(restored) is not bool or restored or state['state'] != 'complete':
                raise ValueError('Choose a completed primary Studio job with a newly reviewed output.')
            saved = encode(state)
            outputs = state.get('outputs')
            if not isinstance(outputs, list) or not 3 <= len(outputs) <= 100000:
                raise ValueError('The completed primary job lacks its bounded artifact receipts.')
            masters = [item for item in outputs
                       if type(item) is dict and item.get('file') == 'master.wav']
            if len(masters) != 1:
                raise ValueError('The completed primary job needs one exact master PCM receipt.')
            master = masters[0]
            if (set(master) != {'file', 'sha256', 'size_bytes'}
                    or type(master['size_bytes']) is not int or master['size_bytes'] <= 0
                    or not isinstance(master['sha256'], str)
                    or _HASH.fullmatch(master['sha256']) is None):
                raise ValueError('Invalid primary master PCM receipt.')
            cache = self.folder / 'cache'
            plain(cache, directory=True)
            request = ResourceRequest(cpu_slots=selected.threads,
                memory_bytes=max(512 * 1024**2, 2 * selected.max_alloc_bytes),
                disk_bytes=min(selected.max_output_bytes, master['size_bytes'] * 4) + 65536,
                disk_path=cache)
            owner = broker if broker is not None else ResourceBroker.for_app(self.private_root)
            if not isinstance(owner, ResourceBroker):
                raise TypeError('Use the existing shared ResourceBroker for silence inspection.')

            def unchanged_job():
                check()
                if (encode(self.get(job_id)) != saved
                        or _file_identity(plain(job_file)) != stamp):
                    raise ValueError('The completed primary Studio job changed during silence inspection.')
                check()

            base_check()
            with retained_context(owner.reserve(request,
                    operation_id='director-silence:' + self.project_id + ':' + job_id,
                    cancelled=base_check,
                    timeout=min(60, max(0, deadline - monotonic())))) as lease:
                unchanged_job()
                phase('Verifying the completed primary Studio artifact receipts.')
                project, timeline, paths = self.publication_snapshot(state, selected, check, phase)
                source, receipt = paths['master.wav']
                metadata_path, metadata_receipt = paths['production.json']
                metadata = _json(_read(metadata_path, metadata_receipt, check, collect=True))
                with retained_temporary_directory(prefix='.carr-inspect-', dir=cache) as temporary:
                    runtime = runtime_for_inspection(Path(temporary), state_root=self.root.parent,
                        input_roots=(source.parent,), budget=selected, cancelled=check,
                        runtime_factory=self.runtime_factory)
                    deadline = min(deadline, started + runtime.budget.timeout)
                    check()
                    # Keep the source bound through the last whole-artifact and
                    # journal checks, including deferred native cleanup.
                    with retained_context(_verified_reader(source)):
                        result = runtime.inspect_silence(source,
                            threshold_dbfs=criteria.threshold_dbfs,
                            minimum_seconds=criteria.minimum_seconds,
                            cancelled=check, progress=phase, deadline=deadline)
                        if (result['source']['sha256'] != receipt['sha256']
                                or result['source']['size'] != receipt['size']
                                or result['audio']['frames'] != metadata['frames']
                                or result['audio']['sample_rate'] != timeline['sample_rate']
                                or result['audio']['channels'] != timeline['channels']):
                            raise ValueError('The measured primary PCM differs from its completed source receipts.')
                        phase('Rechecking the primary artifacts and saved job after measurement.')
                        if self.publication_snapshot(state, selected, check, phase) != (project, timeline, paths):
                            raise ValueError('The completed primary source changed during silence inspection.')
                        unchanged_job()
                        check()
                        result.update(job_id=job_id, project_fingerprint=project.fingerprint,
                                      source_family='director')
        base_check()
        return result

    def publication_audio_name(self, state, paths):
        return 'audio.mp3' if state['project']['settings']['format'] == 'mp3' and 'audio.mp3' in paths else 'master.wav'


class LibraryPublicationService:
    """A menu-level router over existing jobs, with one shared receipt store."""

    def __init__(self, library_root, *, budget=None):
        from .publishing import PublicationService
        from .render import ProductionService
        self.library_root = local_path(library_root, exists=True)
        self.production = ProductionService(self.library_root / 'state' / 'studio')
        self.production.private_root = self.library_root
        self.service = PublicationService(self.production, budget=budget)
        self.budget = budget

    def jobs(self):
        from .publishing import PublicationService
        rows = [dict(row, id='scene:' + row['id'], family='Scene Studio') for row in self.service.jobs()]
        store = ProjectStore(self.library_root)
        for project in store.list_projects():
            source = DirectorPublicationSource(self.library_root, project['id'])
            for row in PublicationService(source, budget=self.budget).jobs():
                rows.append(dict(row, id='director:' + project['id'] + ':' + row['id'], family='Audio Studio'))
        return tuple(rows)

    def publications(self):
        return self.service.publications()

    def export_epub(self, selection, output, **options):
        from .publishing import PublicationService
        if not isinstance(selection, str):
            raise ValueError('Explicitly select one completed Studio job.')
        parts = selection.split(':')
        if len(parts) == 2 and parts[0] == 'scene':
            return self.service.export_epub(_id(parts[1]), output, **options)
        if len(parts) == 3 and parts[0] == 'director':
            source = DirectorPublicationSource(self.library_root, parts[1])
            return PublicationService(source, budget=self.budget).export_epub(parts[2], output, **options)
        raise ValueError('Unknown publication source family or job identity.')

    def export_daisy(self, selection, output, **options):
        from .publishing import PublicationService
        if not isinstance(selection, str):
            raise ValueError('Explicitly select one completed Studio job.')
        parts = selection.split(':')
        if len(parts) == 2 and parts[0] == 'scene':
            return self.service.export_daisy(_id(parts[1]), output, **options)
        if len(parts) == 3 and parts[0] == 'director':
            source = DirectorPublicationSource(self.library_root, parts[1])
            return PublicationService(source, budget=self.budget).export_daisy(parts[2], output, **options)
        raise ValueError('Unknown publication source family or job identity.')

    def inspect_silence(self, selection, *, threshold_dbfs, minimum_seconds,
                        cancelled=lambda: False, progress=None):
        """Route an explicitly selected existing job without changing its family."""
        self.production._check_open()
        if not isinstance(selection, str):
            raise ValueError('Explicitly select one completed Studio job.')
        parts = selection.split(':')
        options = dict(threshold_dbfs=threshold_dbfs, minimum_seconds=minimum_seconds,
            budget=self.budget, cancelled=lambda: self.production._closed or bool(cancelled()),
            progress=progress)
        if len(parts) == 2 and parts[0] == 'scene':
            expected_id = _id(parts[1])
            result = self.production.inspect_silence(expected_id, **options)
            expected_family = None
        elif len(parts) == 3 and parts[0] == 'director':
            source = DirectorPublicationSource(self.library_root, parts[1])
            expected_id = identity(parts[2], 'job identity')
            result = source.inspect_silence(expected_id, broker=self.production.broker, **options)
            expected_family = 'director'
        else:
            raise ValueError('Unknown inspection source family or job identity.')
        if (type(result) is not dict or result.get('job_id') != expected_id
                or result.get('source_family') != expected_family
                or ('selected_source_id' in result and result['selected_source_id'] != selection)):
            raise ValueError('The inspection result belongs to another selected source.')
        return dict(result, selected_source_id=selection)

    def close(self):
        self.production.close()

    def verify(self, publication_id, **options):
        return self.service.verify(publication_id, **options)
