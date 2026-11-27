"""Real local FFmpeg and signed synthetic permission, without acoustic claims."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import struct
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.production import parse_script, render_project
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.security.entitlements import ReviewRequired
from .test_domain_admission import authority, expire


class ToneEngine:
    """A tiny deterministic PCM producer; never evidence of speech quality."""
    def __init__(self, after_first=None):
        self.calls = []
        self.after_first = after_first

    def cache_identity(self, voice):
        return {'fixture': 'signed-admission-pcm-v1', 'voice': voice}

    def synthesize(self, text, output, **kwargs):
        self.calls.append(text)
        samples = b''.join(struct.pack('<h', 4000 if index % 20 < 10 else -4000)
                           for index in range(1600))
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 8000, 1600, 'NONE', 'not compressed'))
            stream.writeframes(samples)
        if len(self.calls) == 1 and self.after_first is not None:
            self.after_first()
        return output


def local_ffmpeg():
    binary = resolve_ffmpeg()
    assert binary is not None and binary.is_file(), 'This gate requires its existing reviewed local FFmpeg.'
    return binary


def production():
    return parse_script('[output sample_rate=8000 stems=false]\n# First\nFirst spoken fixture.\n# Second\nSecond spoken fixture.',
                        title='Signed admission fixture')


@pytest.mark.parametrize('surface', ['domain', 'audio-cli', 'studio-controller', 'assistant-tool'])
def test_real_completed_audio_replay_remains_accessible_and_fresh_work_is_denied(
        authority, tmp_path, monkeypatch, capsys, surface):
    app = LibraryApp(tmp_path / 'library', trust_controller=authority.controller)
    root = tmp_path / 'outputs'
    root.mkdir()
    binary, engine, plan = local_ffmpeg(), ToneEngine(), production()
    if surface == 'domain':
        def invoke(changed=False):
            selected = parse_script(plan.original_script + ('\nNew material.' if changed else ''),
                                    title=plan.title)
            return render_project(selected, root, engine=engine, ffmpeg=binary,
                                  trust_controller=authority.controller)
    elif surface == 'audio-cli':
        from carr_library.audio_cli import add_audio_arguments, run_audio
        import carr_library.audio.engines as engines
        monkeypatch.setattr(engines, 'default_registry', lambda **kwargs: engine)
        plan_path = tmp_path / 'plan.json'
        parser = argparse.ArgumentParser()
        add_audio_arguments(parser)
        args = parser.parse_args(['--audio-plan', str(plan_path), '--audio-output', str(root),
                                  '--ffmpeg', str(binary)])
        args.data_dir = app.root
        def invoke(changed=False):
            selected = parse_script(plan.original_script + ('\nNew material.' if changed else ''),
                                    title=plan.title)
            plan_path.write_text(json.dumps(selected.to_dict()), encoding='utf-8')
            capsys.readouterr()
            assert run_audio(args, trust_controller=authority.controller) == 0
            return json.loads(capsys.readouterr().out)
    elif surface == 'studio-controller':
        from carr_library.ui.audio_studio import StudioController
        studio = StudioController(private_root=app.root, registry=engine, ffmpeg=binary,
                                  trust_controller=authority.controller)
        studio.new(plan.original_script, title=plan.title, input_kind='script')
        studio.configure(output_dir=root)
        def invoke(changed=False):
            if changed:
                studio.edit_script(plan.original_script + '\nNew material.')
            return studio.render()
    else:
        from carr_library.assistant import AssistantService, Plan, Scope, Step
        from carr_library.assistant.production import LocalProduction
        producer = LocalProduction(app.root, registry=engine, ffmpeg=binary)
        service = AssistantService(app, production=producer)
        scope = Scope(output_root=root, max_bytes=64 * 1024**2)
        identity = service.submit(Plan(scope, (Step('audio.produce', {'script': plan.to_dict()}),)))
        def invoke(changed=False):
            selected = identity
            if changed:
                updated = parse_script(plan.original_script + '\nNew material.', title=plan.title)
                selected = service.submit(Plan(scope, (Step('audio.produce', {'script': updated.to_dict()}),)))
            report = service.run(selected)
            if changed:
                assert report.status == 'failed' and report.completed == 0, report
                return None
            assert report.status == 'complete', report
            return report.results[0]

    first = invoke()
    output = Path(first['output'])
    original = output.read_bytes()
    assert len(engine.calls) == 2
    expire(authority)
    repeat = invoke()
    assert Path(repeat['output']) == output and output.read_bytes() == original
    assert len(engine.calls) == 2
    if surface == 'assistant-tool':
        invoke(changed=True)
    else:
        with pytest.raises(ReviewRequired):
            invoke(changed=True)
    assert len(engine.calls) == 2 and output.read_bytes() == original


def test_actual_two_scene_render_finishes_when_permission_expires_after_first_chunk(
        authority, tmp_path):
    binary, plan = local_ffmpeg(), production()
    engine = ToneEngine(after_first=lambda: expire(authority))
    result = render_project(plan, tmp_path / 'active', engine=engine, ffmpeg=binary,
                            trust_controller=authority.controller)
    assert result['status'] == 'complete' and len(engine.calls) == 2
    master = Path(result['output'])
    digest = sha256(master.read_bytes()).hexdigest()
    with pytest.raises(ReviewRequired):
        render_project(plan, tmp_path / 'fresh', engine=engine, ffmpeg=binary,
                       trust_controller=authority.controller)
    assert sha256(master.read_bytes()).hexdigest() == digest and len(engine.calls) == 2


def test_actual_renderer_cannot_replace_the_active_tool_authority(authority, tmp_path):
    from carr_library.security.admission import operation_context
    from carr_library.security.controller import TrustController
    binary, plan, engine = local_ffmpeg(), production(), ToneEngine()
    other = TrustController()
    expire(authority)
    with operation_context(authority.controller):
        with pytest.raises(ValueError, match='authority'):
            render_project(plan, tmp_path / 'refused', engine=engine, ffmpeg=binary,
                           trust_controller=other)
    assert engine.calls == []
    private = render_project(plan, tmp_path / 'private', engine=engine, ffmpeg=binary,
                             trust_controller=other)
    assert private['status'] == 'complete' and len(engine.calls) == 2


def test_real_clip_preview_retains_authority_and_cached_pcm_after_expiry(authority, tmp_path, monkeypatch):
    from carr_library.audio import clips
    from carr_library.ui.audio_studio import StudioController
    app = LibraryApp(tmp_path / 'clip-library', trust_controller=authority.controller)
    folder = tmp_path / 'clip-project'
    (folder / 'assets').mkdir(parents=True)
    source = folder / 'assets/source.wav'
    ToneEngine().synthesize('Synthetic clip fixture', source)
    original = sha256(source.read_bytes()).hexdigest()
    studio = StudioController(private_root=app.root, ffmpeg=local_ffmpeg(),
                              trust_controller=authority.controller)
    script = '[output sample_rate=8000 stems=false]\n[clip source.wav id=c1 duration=.1]'
    studio.new(script, input_kind='script')
    studio.configure(output_dir=folder)
    editor = studio.clip_editor(studio.clip_snapshot())
    request = {'action': 'preview', 'clip_id': 'c1'}
    first = studio.prepare_clip_edit(editor, request)
    rendered = Path(first['preview']).read_bytes()
    expire(authority)
    monkeypatch.setattr(clips, 'process_clip',
                        lambda *args, **kwargs: pytest.fail('Expired permission started fresh clip DSP.'))
    repeated = studio.prepare_clip_edit(editor, request)
    assert repeated['preview'] == first['preview'] and Path(repeated['preview']).read_bytes() == rendered
    studio.edit_script(script.replace('duration=.1', 'duration=.1 gain_db=-3'))
    changed = studio.clip_editor(studio.clip_snapshot())
    with pytest.raises(ReviewRequired):
        studio.prepare_clip_edit(changed, request)
    assert sha256(source.read_bytes()).hexdigest() == original
    assert Path(first['preview']).read_bytes() == rendered
