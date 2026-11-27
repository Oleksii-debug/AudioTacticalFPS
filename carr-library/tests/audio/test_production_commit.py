"""Committed PCM output survives late progress failures; actual FFmpeg runs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import wave

import pytest

from carr_library.audio import production
from carr_library.audio.ffmpeg import verify_encoded, wav_info
from carr_library.audio.jobs import RenderCancelled


class SyntheticVoice:
    """A small PCM fixture for publication tests, not speech-quality evidence."""
    def __init__(self):
        self.calls = 0

    def cache_identity(self, voice):
        return {'fixture': 'publication-pcm-v1', 'voice': voice}

    def synthesize(self, text, output, **kwargs):
        self.calls += 1
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 8000, 1600, 'NONE', 'not compressed'))
            stream.writeframes(b'\x00\x00' * 1600)
        return output


@pytest.fixture
def renderer(tmp_path):
    binary = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    assert binary and Path(binary).is_file(), 'Actual FFmpeg is required for this gate.'
    voice = SyntheticVoice()
    plan = production.parse_script('[output sample_rate=8000 stems=false]\nNarrator: Synthetic publication test.')

    def render(plan_override=None, **kwargs):
        return production.render_project(plan_override or plan, tmp_path, engine=voice,
                                         ffmpeg=Path(binary).absolute(), **kwargs)

    return render, voice, tmp_path


def assert_complete(result):
    path = Path(result['output'])
    assert result['status'] == 'complete' and wav_info(path).frames == 1600
    record = next(record for record in result['files'] if record['name'] == path.name)
    assert sha256(path.read_bytes()).hexdigest() == record['sha256']
    assert json.loads(Path(result['manifest']).read_text())['status'] == 'complete'


@pytest.mark.parametrize('error', [RuntimeError, OSError, RenderCancelled])
def test_late_status_failure_returns_complete_receipt(renderer, error):
    render, voice, root = renderer

    def failed_notification(message):
        if message.startswith('Audio production complete:'):
            raise error('Injected late notification failure')

    result = render(on_status=failed_notification)
    assert_complete(result)
    assert result['warnings'] and 'notification' in result['warnings'][0]
    assert json.loads((root / 'render-journal.json').read_text())['state'] == 'complete'
    assert_complete(render())
    assert voice.calls == 1


def test_late_journal_failure_preserves_output_and_resume_repairs_state(renderer, monkeypatch):
    render, voice, root = renderer
    original = production.atomic_json

    def failed_final_journal(path, value):
        if Path(path).name == 'render-journal.json' and value.get('state') == 'complete':
            assert list((root / 'renders').glob('*/complete.json'))
            raise OSError('Injected final journal I/O failure')
        return original(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(production, 'atomic_json', failed_final_journal)
        result = render()
    assert_complete(result)
    assert result['warnings'] and 'journal' in result['warnings'][0]
    assert json.loads((root / 'render-journal.json').read_text())['state'] == 'running'
    reopened = render()
    assert_complete(reopened)
    assert reopened['output'] == result['output'] and voice.calls == 1
    assert json.loads((root / 'render-journal.json').read_text())['state'] == 'complete'


def test_process_interruption_after_commit_carries_complete_receipt(renderer):
    render, voice, root = renderer

    def interrupted(message):
        if message.startswith('Audio production complete:'):
            raise KeyboardInterrupt()

    with pytest.raises(production.ProductionPublishedError, match='complete and saved') as raised:
        render(on_status=interrupted)
    assert_complete(raised.value.result)
    assert json.loads((root / 'render-journal.json').read_text())['state'] == 'complete'
    assert_complete(render())
    assert voice.calls == 1


def test_notification_failure_on_verified_reopen_does_not_damage_completion(renderer):
    render, voice, root = renderer
    original = render()

    def interrupted(message):
        if message.startswith('This exact production is already complete;'):
            raise OSError('Injected reopen notification failure')

    reopened = render(on_status=interrupted)
    assert_complete(reopened)
    assert reopened['warnings'] and reopened['output'] == original['output']
    assert voice.calls == 1
    assert json.loads((root / 'render-journal.json').read_text())['state'] == 'complete'


def test_result_and_progress_state_the_actual_storage_budget_scope(renderer):
    render, _, _ = renderer
    messages = []
    result = render(on_status=messages.append)
    assert any('additional space' in message for message in messages)
    assert any('not total project size' in limitation for limitation in result['limitations'])


def test_actual_compressed_production_uses_verified_export_and_resumes(renderer):
    render, voice, root = renderer
    probe = os.environ.get('CARR_FFPROBE') or shutil.which('ffprobe')
    binary = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    assert probe and Path(probe).is_file(), 'Actual ffprobe is required for this gate.'
    plan = production.parse_script('[output format=m4b sample_rate=8000 stems=false]\nNarrator: Compressed publication test.')
    result = render(plan, ffprobe=Path(probe).absolute())
    output = Path(result['output'])
    assert output.suffix == '.m4b' and result['status'] == 'complete'
    checked = verify_encoded(output, wav_info(output.parent / 'master.wav'), format='m4b',
                             ffmpeg=Path(binary).absolute(), ffprobe=Path(probe).absolute())
    assert checked['codec'] == 'aac' and 1600 <= checked['decoded_frames'] <= 2623
    assert render(plan, ffprobe=Path(probe).absolute())['output'] == result['output']
    assert voice.calls == 1
    assert json.loads((root / 'render-journal.json').read_text())['state'] == 'complete'


@pytest.mark.parametrize('malformed', [None, [], 'not a completion record'])
def test_valid_json_with_wrong_completion_shape_is_repaired(renderer, malformed):
    render, voice, _ = renderer
    original = render()
    Path(original['manifest']).write_text(json.dumps(malformed))
    repaired = render()
    assert_complete(repaired)
    assert voice.calls == 1


def test_empty_directory_created_at_commit_is_not_replaced(renderer, monkeypatch):
    render, voice, root = renderer
    actual_publish = production.publish_directory_no_replace
    competitor = []

    def racing_publish(stage, destination):
        # This occurs after production's existence check and uses a real native
        # rename. It does not simulate another operating system or syscall.
        destination.mkdir()
        competitor.append((destination, destination.stat().st_ino))
        actual_publish(stage, destination)

    with monkeypatch.context() as patch:
        patch.setattr(production, 'publish_directory_no_replace', racing_publish)
        with pytest.raises(FileExistsError):
            render()
    destination, identity = competitor[0]
    assert destination.is_dir() and destination.stat().st_ino == identity
    assert list(destination.iterdir()) == []
    assert not list(root.glob('.work-*'))
    assert json.loads((root / 'render-journal.json').read_text())['state'] == 'failed'
    destination.rmdir()  # Only the test owner removes its competing directory.
    assert_complete(render())
    assert voice.calls == 1


def test_directory_sync_failure_after_native_commit_is_reported_as_complete(renderer, monkeypatch):
    render, _, root = renderer

    def sync_failure(path):
        assert Path(path) == root / 'renders'
        assert list(Path(path).glob('*/complete.json'))
        raise OSError('Injected directory durability failure')

    monkeypatch.setattr(production, '_sync_directory', sync_failure)
    result = render()
    assert_complete(result)
    assert any('durability was not confirmed' in warning for warning in result['warnings'])
    assert json.loads((root / 'render-journal.json').read_text())['state'] == 'complete'


@pytest.mark.parametrize('legacy_version,format', [(2, 'flac'), (3, 'mp3')])
def test_legacy_completion_cannot_bypass_current_encoded_policy(renderer, monkeypatch, legacy_version, format):
    render, voice, _ = renderer
    probe = os.environ.get('CARR_FFPROBE') or shutil.which('ffprobe')
    binary = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    assert probe and Path(probe).is_file(), 'Actual ffprobe is required for this gate.'
    plan = production.parse_script(f'[output format={format} sample_rate=8000 stems=false]\nNarrator: Legacy cache fixture.')
    actual_digest = production.digest

    def legacy_completion_key(value):
        if 'plan' in value:
            value = {**value, 'pipeline_version': legacy_version}
        return actual_digest(value)

    with monkeypatch.context() as patch:
        patch.setattr(production, 'digest', legacy_completion_key)
        legacy = render(plan, ffprobe=Path(probe).absolute())
    # Legacy completion verification checked only these self-described file
    # hashes. A valid old marker cannot grant this artifact the new decode gate.
    old_output = Path(legacy['output'])
    old_output.write_bytes(b'not a valid encoded audio file')
    old_manifest = Path(legacy['manifest'])
    document = json.loads(old_manifest.read_text())
    record = next(item for item in document['files'] if item['name'] == old_output.name)
    record.update(size=old_output.stat().st_size, sha256=sha256(old_output.read_bytes()).hexdigest())
    old_manifest.write_text(json.dumps(document))
    current = render(plan, ffprobe=Path(probe).absolute())
    assert current['run_key'] != legacy['run_key'] and current['output'] != legacy['output']
    verify_encoded(current['output'], wav_info(Path(current['output']).parent / 'master.wav'),
                   format=format, ffmpeg=Path(binary).absolute(), ffprobe=Path(probe).absolute())
    assert old_output.read_bytes() == b'not a valid encoded audio file'
    assert voice.calls == 1

