"""Connect the reviewed local assistant to the shared Audio Studio renderer."""
from dataclasses import replace
import json
from pathlib import Path
import re

from .models import local_path


class LocalAudioProduction:
    def __init__(self, private_root, *, registry=None, ffmpeg=None):
        from ..audio.engines import default_registry
        from ..audio.ffmpeg import find_local_tool
        self.private_root = local_path(private_root)
        self.ffmpeg = ffmpeg or find_local_tool('ffmpeg')
        self.voices = registry if registry is not None else default_registry(ffmpeg=self.ffmpeg)

    def assistant_callback(self, settings, output_root, cancelled, progress):
        from ..audio.jobs import atomic_json, checked_directory, check_cancelled, regular_file
        from ..audio.models import ProductionPlan, digest
        from ..audio.production import plan_narration, render_project

        check_cancelled(cancelled)
        if self.ffmpeg is None:
            raise RuntimeError('The verified local audio runtime is unavailable. Check installation before producing audio.')
        identity = settings['operation_id']
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
            raise ValueError('Invalid audio operation identity.')
        output_root = local_path(output_root)
        if output_root.is_relative_to(self.private_root):
            raise ValueError('Choose an audio output folder outside the private library data.')
        if 'script' in settings:
            plan = ProductionPlan.from_dict(settings['script'])
        else:
            selected = settings.get('voice_ids', ())
            if len(selected) != 1:
                raise ValueError('Choose one installed voice for literal narration, or provide a typed multivoice production plan.')
            voice = next((item for item in self.voices.voices() if item.id == selected[0]), None)
            if voice is None or not voice.available or not voice.languages:
                raise ValueError('The approved local voice is no longer available; choose it again before running.')
            plan = plan_narration(settings['text'], title=settings.get('title', 'Assistant narration'),
                                  voice=voice.id, language=voice.languages[0])
        changes = {'disk_budget_bytes': min(plan.disk_budget_bytes, settings['max_bytes'])}
        if 'format' in settings:
            changes['output_format'] = settings['format']
        if 'stems' in settings:
            changes['export_stems'] = settings['stems']
        if 'title' in settings:
            changes['title'] = settings['title']
        plan = replace(plan, **changes)
        expected = {'schema': 'carr-assistant-audio/1', 'operation_id': identity,
                    'data_dir': str(self.private_root), 'plan_sha256': digest(plan.to_dict())}
        directory = output_root / ('assistant-audio-' + identity)
        if directory.exists() or directory.is_symlink():
            checked_directory(directory)
            receipt = directory / 'assistant-owner.json'
            if regular_file(receipt).st_size > 4096:
                raise ValueError('Invalid existing audio project ownership receipt.')
            if json.loads(receipt.read_text(encoding='utf-8')) != expected:
                raise ValueError('The selected audio project belongs to another operation or plan.')
        else:
            directory.mkdir(mode=0o700, exist_ok=False)
            atomic_json(directory / 'assistant-owner.json', expected)
        result = render_project(plan, directory, engine=self.voices, ffmpeg=self.ffmpeg,
                                cancelled=cancelled, on_status=progress)
        if result.get('status') != 'complete':
            raise RuntimeError('The renderer has not verified a completed output.')
        # Return every published output, not the private reusable render cache.
        completed = local_path(result['manifest']).parent
        if not completed.is_relative_to(directory):
            raise ValueError('The renderer returned a different output directory.')
        artifacts = []
        for path in sorted(completed.iterdir()):
            regular_file(path)
            artifacts.append(str(path))
        return {'artifacts': artifacts, 'output': result['output'], 'project': str(directory),
                'source_sha256': plan.source_sha256, 'input_kind': plan.input_kind,
                'notice': 'Actual local audio; line timings come from rendered segments. Source quotation approval is unchanged.'}
