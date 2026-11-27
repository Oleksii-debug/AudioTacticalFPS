"""Literal reading and actual file/resource ownership contracts."""
import hashlib
from pathlib import Path
import wave

import pytest

from carr_library.audio.reading import capture_reading_text, reading_plan, render_reading
from carr_library.audio.jobs import RenderCancelled
from carr_library.operations import ResourceBroker


def wav(path, *, frames=2400):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b'\0\0' * frames)


class NativeText:
    def __init__(self):
        self.requested = None
    def GetValue(self):
        return '😀source [Music: calm]\nEdited reading copy.'
    def GetStringSelection(self):
        return 'Edited reading copy.'
    def GetInsertionPoint(self):
        return 2  # Native UTF-16 position after the supplementary character.
    def GetLastPosition(self):
        return 42
    def GetRange(self, start, end):
        self.requested = start, end
        return 'source [Music: calm]\nEdited reading copy.'


def test_native_selection_cursor_and_all_preserve_exact_copy_text():
    editor = NativeText()
    assert capture_reading_text(editor, 'all') == editor.GetValue()
    assert capture_reading_text(editor, 'selection') == 'Edited reading copy.'
    assert capture_reading_text(editor, 'cursor') == 'source [Music: calm]\nEdited reading copy.'
    assert editor.requested == (2, 42)


def test_empty_selection_reads_all_and_unknown_mode_is_rejected():
    editor = NativeText()
    editor.GetStringSelection = lambda: ''
    assert capture_reading_text(editor, 'selection') == editor.GetValue()
    with pytest.raises(ValueError):
        capture_reading_text(editor, 'original')


def test_plan_keeps_commands_as_literal_source_and_applies_native_controls():
    text = '[Music: suspense]\nSpeaker [left]:  literal.\fSecond page.'
    plan = reading_plan(text, voice='synthetic:one', language='uk', rate=-3, volume=42)
    assert plan.original_script == text and plan.input_kind == 'narration'
    assert all(scene.music is None for scene in plan.scenes)
    assert all(event.literal for scene in plan.scenes for event in scene.events)
    assert plan.roles[0].rate == -3 and plan.roles[0].volume == .42
    assert plan.sample_rate == 24000 and plan.output_format == 'wav' and not plan.export_stems
    for scene in plan.scenes:
        for event in scene.events:
            assert text[event.source_start:event.source_end] == event.text


@pytest.mark.parametrize('kwargs', [{'rate': True}, {'rate': 11}, {'volume': True},
                                    {'volume': -1}, {'volume': 101}])
def test_invalid_reading_controls_fail_before_audio(kwargs):
    with pytest.raises(ValueError):
        reading_plan('Text', voice='synthetic:one', language='en', **kwargs)


def fixture_renderer(root, seen):
    def render(plan, directory, **kwargs):
        snapshot = ResourceBroker.for_app(root).snapshot()
        assert snapshot['exclusive_devices'] == ['heavy-media']
        assert len(snapshot['leases']) == 1
        assert snapshot['leases'][0]['operation_id'].startswith('reading:')
        seen.append(plan.original_script)
        output = directory / 'master.wav'
        wav(output)
        return {'output': str(output)}
    return render


def test_render_uses_existing_resource_broker_and_separate_private_output(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    seen = []
    audio = render_reading(root, 'Exact copy', registry=object(), voice='synthetic:one', language='en',
                           renderer=fixture_renderer(root, seen))
    assert seen == ['Exact copy']
    assert audio.text_sha256 == hashlib.sha256(b'Exact copy').hexdigest()
    assert audio.duration_seconds == .1
    assert audio.directory.parent == root
    assert not ResourceBroker.for_app(root).snapshot()['leases']
    assert not (root / 'audio-project.json').exists()
    directory = audio.directory
    audio.close()
    assert not directory.exists()
    audio.close()


def test_renderer_failure_releases_resource_and_removes_uncommitted_preview(tmp_path):
    def fail(*args, **kwargs):
        raise RenderCancelled('actual cooperative stop')
    with pytest.raises(RenderCancelled):
        render_reading(tmp_path, 'Exact copy', registry=object(), voice='synthetic:one',
                       language='en', renderer=fail)
    assert not list(tmp_path.glob('.reading-preview-*'))
    assert not ResourceBroker.for_app(tmp_path).snapshot()['leases']


def test_cancellation_before_start_makes_no_preview_directory(tmp_path):
    with pytest.raises(RenderCancelled):
        render_reading(tmp_path, 'Exact copy', registry=object(), voice='synthetic:one',
                       language='en', cancelled=lambda: True, renderer=lambda *a, **k: None)
    assert not list(tmp_path.glob('.reading-preview-*'))


def test_external_renderer_output_is_rejected_and_never_removed(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    outside = tmp_path / 'outside.wav'
    wav(outside)
    original = outside.read_bytes()
    with pytest.raises(ValueError, match='outside'):
        render_reading(root, 'copy', registry=object(), voice='synthetic:one', language='en',
                       renderer=lambda *a, **k: {'output': str(outside)})
    assert outside.read_bytes() == original
    assert not list(root.glob('.reading-preview-*'))


def test_wav_export_is_independent_and_never_overwrites_existing_destination(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    audio = render_reading(root, 'copy', registry=object(), voice='synthetic:one', language='en',
                           renderer=fixture_renderer(root, []))
    target = tmp_path / 'reading.wav'
    audio.save_wav(target)
    assert target.read_bytes() == audio.path.read_bytes()
    assert target.stat().st_nlink == 1 and audio.path.stat().st_nlink == 1
    with pytest.raises(FileExistsError):
        audio.save_wav(target)
    audio.close()
    assert target.exists()


def test_export_rechecks_bytes_and_cancel_before_publication(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    audio = render_reading(root, 'copy', registry=object(), voice='synthetic:one', language='en',
                           renderer=fixture_renderer(root, []))
    target = tmp_path / 'reading.wav'
    with pytest.raises(InterruptedError):
        audio.save_wav(target, cancelled=lambda: True)
    assert not target.exists()
    audio.path.write_bytes(audio.path.read_bytes()[:-2] + b'\1\0')
    with pytest.raises(ValueError, match='changed'):
        audio.save_wav(target)
    assert not target.exists()
    audio.close()


def test_export_publication_race_keeps_competing_destination(tmp_path, monkeypatch):
    import carr_library.audio.reading as reading
    root = tmp_path / 'library'
    root.mkdir()
    audio = render_reading(root, 'copy', registry=object(), voice='synthetic:one', language='en',
                           renderer=fixture_renderer(root, []))
    target = tmp_path / 'reading.wav'
    real_publish = reading.publish_noreplace
    def race(stage, destination):
        destination.write_bytes(b'other accepted output')
        return real_publish(stage, destination)
    monkeypatch.setattr(reading, 'publish_noreplace', race)
    with pytest.raises(FileExistsError):
        audio.save_wav(target)
    assert target.read_bytes() == b'other accepted output'
    assert not list(tmp_path.glob('.reading-export-*'))
    audio.close()
