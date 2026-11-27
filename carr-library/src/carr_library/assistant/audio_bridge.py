"""Connect the reviewed local assistant to the shared Audio Studio renderer."""
from dataclasses import replace
import json
from pathlib import Path
import re

from .models import local_path


def _published_file_count(plan):
    """Count delivered renderer outputs, excluding resumable cache/checkpoints."""
    from ..audio.models import Line, Sound
    roles = {event.role for scene in plan.scenes for event in scene.events if isinstance(event, Line)}
    sounds = any(isinstance(event, Sound) for scene in plan.scenes for event in scene.events)
    music = [scene.music for scene in plan.scenes if scene.music is not None]
    stems = roles - {'Sound effects'} if plan.export_stems else set()
    if plan.export_stems:
        if sounds:
            stems.add('Sound effects')
        if music:
            stems.add('Music (dry)')
    # Master WAV, three text/JSON sidecars and complete.json; every chapter;
    # exported stems; score/provenance companions; optional transcoded master.
    return (5 + len(plan.scenes) + len(stems) + (plan.output_format != 'wav')
            + sum(2 if cue.engine == 'procedural' else 1 for cue in music))


class LocalAudioProduction:
    def __init__(self, private_root, *, registry=None, ffmpeg=None):
        from ..audio.engines import default_registry
        from ..audio.runtime import find_ffmpeg
        self.private_root = local_path(private_root)
        self.ffmpeg = find_ffmpeg(ffmpeg)
        self.voices = registry if registry is not None else default_registry(ffmpeg=self.ffmpeg)

    @staticmethod
    def _selected_language(voice, settings):
        selected = settings.get('language')
        if selected is None:
            if len(voice.languages) != 1:
                raise ValueError('Choose an explicit language for this multilingual voice before narration.')
            return voice.languages[0]
        code = selected.replace('_', '-').casefold() if isinstance(selected, str) else ''
        supported = {value.replace('_', '-').casefold(): value for value in voice.languages}
        if code not in supported:
            raise ValueError('The selected voice does not declare this exact narration language.')
        return supported[code]

    def _plan(self, settings):
        from ..audio.models import ProductionPlan
        from ..audio.production import plan_narration
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
                                  voice=voice.id, language=self._selected_language(voice, settings))
        return plan

    def validate_settings(self, settings):
        if self.ffmpeg is None:
            raise RuntimeError('The verified local audio runtime is unavailable. Check installation before producing audio.')
        self._plan(settings)

    def assistant_callback(self, settings, output_root, cancelled, progress):
        from ..audio.jobs import RenderCancelled
        try:
            return self._produce(settings, output_root, cancelled, progress)
        except RenderCancelled as exc:
            # The assistant uses InterruptedError for durable pause/cancel.
            # Preserve the renderer's reusable chunks and its cancellation state.
            raise InterruptedError(str(exc)) from exc

    def _produce(self, settings, output_root, cancelled, progress):
        from ..audio.jobs import atomic_json, checked_directory, check_cancelled, regular_file
        from ..audio.models import digest
        from ..operations.studio import render_with_resources

        check_cancelled(cancelled)
        if self.ffmpeg is None:
            raise RuntimeError('The verified local audio runtime is unavailable. Check installation before producing audio.')
        identity = settings['operation_id']
        if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
            raise ValueError('Invalid audio operation identity.')
        output_root = local_path(output_root)
        if output_root.is_relative_to(self.private_root):
            raise ValueError('Choose an audio output folder outside the private library data.')
        plan = self._plan(settings)
        changes = {'disk_budget_bytes': min(plan.disk_budget_bytes, settings['max_bytes'])}
        if 'format' in settings:
            changes['output_format'] = settings['format']
        if 'stems' in settings:
            changes['export_stems'] = settings['stems']
        if 'title' in settings:
            changes['title'] = settings['title']
        plan = replace(plan, **changes)
        max_files = settings['max_files']
        if type(max_files) is not int or max_files < 0:
            raise ValueError('Invalid remaining audio file budget.')
        expected_files = _published_file_count(plan)
        if expected_files > max_files:
            raise ValueError(f'Audio needs {expected_files} delivered files; the remaining file budget is {max_files}.')
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
        result = render_with_resources(self.private_root, plan, directory,
            engine=self.voices, ffmpeg=self.ffmpeg, operation_id='assistant-audio:' + identity,
            cancelled=cancelled, on_status=progress)
        if result.get('status') != 'complete':
            raise RuntimeError('The renderer has not verified a completed output.')
        # Return every published output, not the private reusable render cache.
        completed = local_path(result['manifest']).parent
        if not completed.is_relative_to(directory):
            raise ValueError('The renderer returned a different output directory.')
        artifacts, total = [], 0
        for path in sorted(completed.iterdir()):
            check_cancelled(cancelled)
            total += regular_file(path).st_size
            artifacts.append(str(path))
            if len(artifacts) > max_files or total > settings['max_bytes']:
                raise ValueError('Rendered audio exceeds the remaining artifact budget.')
        if len(artifacts) != expected_files:
            raise ValueError('The renderer returned an unexpected delivered-file inventory.')
        return {'artifacts': artifacts, 'output': result['output'], 'project': str(directory),
                'source_sha256': plan.source_sha256, 'input_kind': plan.input_kind,
                'notice': 'Actual local audio; line timings come from rendered segments. Source quotation approval is unchanged.'}
