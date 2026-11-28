"""The assistant's narrow adapter to the application's one audio renderer."""
from dataclasses import replace
from pathlib import Path
import re

from ..audio.engines import _invoke
from ..audio.jobs import regular_file, sha256_file
from ..audio.models import ProductionPlan
from ..audio.production import parse_script, plan_narration, render_project
from ..audio.runtime import resolve_ffmpeg
from .models import local_path


class LocalProduction:
    def __init__(self, private_root, *, registry=None, ffmpeg=None, renderer=None,
                 cancelled=lambda: False):
        self.private_root = local_path(private_root)
        self.registry = registry
        self.ffmpeg = Path(ffmpeg) if ffmpeg else None
        self.renderer = render_project if renderer is None else renderer
        self._default_renderer = renderer is None or renderer is render_project
        self.cancelled = cancelled

    def _registry(self):
        if self.registry is None:
            from ..runtime_settings import configured_voice_registry
            self.registry = configured_voice_registry(self.private_root, ffmpeg=resolve_ffmpeg(self.ffmpeg))
        return self.registry

    def voices(self, *, cancelled=None):
        return _invoke(self._registry().voices, cancelled=self.cancelled if cancelled is None else cancelled)

    def plan(self, arguments, *, max_bytes, cancelled=None):
        if type(max_bytes) is not int or max_bytes < 1024 ** 2:
            raise ValueError('Audio production needs an explicit disk budget of at least one MiB.')
        if 'script' in arguments:
            if 'input_kind' in arguments or 'voice_ids' in arguments or 'language' in arguments:
                raise ValueError('A structured production project already specifies its voices and input mode.')
            plan = ProductionPlan.from_dict(arguments['script'])
        else:
            voices = arguments.get('voice_ids', ('sapi5:default',))
            if len(voices) != 1:
                raise ValueError('Plain narration has one default voice; use an explicit structured project for multiple roles.')
            voice = voices[0]
            language = arguments.get('language')
            if language is None:
                selected = next((item for item in self.voices(cancelled=cancelled) if item.id == voice), None)
                language = selected.languages[0] if selected and selected.languages else 'en'
            kind = arguments.get('input_kind', 'narration')
            if kind not in ('narration', 'script'):
                raise ValueError('Choose literal narration or an explicit structured script.')
            parser = plan_narration if kind == 'narration' else parse_script
            plan = parser(arguments['text'], title=arguments.get('title', 'Assistant narration'),
                          voice=voice, language=language)
        overrides = {'disk_budget_bytes': min(plan.disk_budget_bytes, max_bytes)
                     if 'script' in arguments else max_bytes}
        if 'format' in arguments:
            overrides['output_format'] = arguments['format']
        if 'stems' in arguments:
            overrides['export_stems'] = arguments['stems']
        if 'title' in arguments:
            overrides['title'] = arguments['title']
        return replace(plan, **overrides)

    def __call__(self, arguments, output_directory, cancelled, progress):
        identity = arguments.get('operation_id')
        if not isinstance(identity, str) or re.fullmatch('[0-9a-f]{32}', identity) is None:
            raise ValueError('Audio production requires the executor-assigned operation identity.')
        budget = arguments.get('_artifact_budget_bytes')
        plan = self.plan(arguments, max_bytes=budget, cancelled=cancelled)
        root = local_path(output_directory)
        project = local_path(root / ('audio-' + identity), must_exist=False)
        if project.is_relative_to(self.private_root) or self.private_root.is_relative_to(project):
            raise PermissionError('Choose an audio destination outside the private library.')
        binary = resolve_ffmpeg(self.ffmpeg)
        if binary is None:
            raise ValueError('Install or configure the reviewed local FFmpeg runtime before production.')
        options = {}
        if self._default_renderer:
            from ..operations import ResourceBroker
            from ..runtime_settings import EngineProfileStore
            def music_profile(reference, engine, *, cancelled):
                return EngineProfileStore(self.private_root).music_manifest(
                    reference, engine, cancelled=cancelled)
            options['music_manifest_resolver'] = music_profile
            options['broker'] = ResourceBroker.for_app(self.private_root, cancelled=cancelled)
            options['operation_id'] = 'assistant:' + identity
        result = self.renderer(plan, project, engine=self._registry(), ffmpeg=binary,
                               cancelled=cancelled, on_status=progress, **options)
        if not isinstance(result, dict) or result.get('status') != 'complete':
            raise RuntimeError('The audio renderer did not complete its verified output.')
        output = local_path(result['output'])
        if not output.is_relative_to(project):
            raise PermissionError('The audio renderer returned a file outside its operation directory.')
        records = result.get('files')
        if not isinstance(records, list) or not records:
            raise ValueError('The audio renderer returned no artifact inventory.')
        artifacts, total = [], 0
        for record in records:
            name = record.get('name') if isinstance(record, dict) else None
            if not isinstance(name, str) or Path(name).name != name or '/' in name or '\\' in name:
                raise ValueError('The audio renderer returned an invalid artifact name.')
            path = local_path(output.parent / name)
            size = regular_file(path).st_size
            total += size
            if total > budget:
                raise ValueError('Rendered artifacts exceed the approved byte budget.')
            if size != record.get('size') or sha256_file(path, cancelled=cancelled) != record.get('sha256'):
                raise ValueError('A completed audio artifact failed its renderer checksum.')
            artifacts.append(str(path))
        if str(output) not in artifacts:
            raise ValueError('The audio inventory omits the selected master.')
        return {'artifacts': artifacts, 'output': str(output), 'operation_id': identity,
                'source_sha256': plan.source_sha256, 'input_kind': plan.input_kind,
                'duration_seconds': result.get('duration_seconds'), 'run_key': result.get('run_key')}
