"""Actual CLI registration/errors; inferred text uses an explicitly injected fixture."""
import json
from pathlib import Path

from carr_library.transcription_cli import main


def test_register_existing_model_cli_never_downloads(tmp_path, capsys):
    root = tmp_path / 'model'
    root.mkdir()
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (root / name).write_bytes(b'synthetic fixture')
    assert main(['register-model', str(root), '--name', 'Fixture', '--license', 'test-only',
                 '--accept-license', '--trust-source']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['network_access'] is False
    assert Path(result['manifest']).is_file()


def test_required_review_or_missing_model_is_an_explicit_failure(tmp_path, capsys):
    assert main(['register-model', str(tmp_path), '--name', 'Fixture', '--license', 'test-only']) == 1
    assert 'Review' in capsys.readouterr().err
    assert main(['transcribe', str(tmp_path / 'audio.wav'), '--model-manifest', str(tmp_path / 'missing.json'),
                 '--output', str(tmp_path / 'results')]) == 1
    assert not (tmp_path / 'results').exists()


def test_batch_cli_uses_one_session_and_exact_settings(tmp_path, monkeypatch, capsys):
    from carr_library.audio import transcription
    calls, created = [], []
    class Session:
        def __init__(self, manifest): created.append(manifest)
        def transcribe(self, source, output, **kwargs):
            calls.append((source, output, kwargs))
            return {'status': 'complete', 'identity': str(source), 'cached': False}
    monkeypatch.setattr(transcription, 'TranscriptionSession', Session)
    args = ['transcribe', 'one.wav', 'two.wav', '--model-manifest', 'model.json', '--output', 'results',
            '--language', 'uk', '--word-timestamps', '--format', 'srt', '--format', 'json']
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result['results']) == 2 and len(created) == 1 and len(calls) == 2
    assert calls[0][2]['settings'].language == 'uk'
    assert calls[0][2]['settings'].word_timestamps is True
    assert calls[0][2]['formats'] == ('srt', 'json')


def test_invalid_settings_are_rejected_before_any_inference(monkeypatch, capsys):
    from carr_library.audio import transcription
    def forbidden(*args, **kwargs):
        raise AssertionError('Session should not start')
    monkeypatch.setattr(transcription, 'TranscriptionSession', forbidden)
    assert main(['transcribe', 'audio.wav', '--model-manifest', 'model.json', '--output', 'results',
                 '--beam-size', '999']) == 1
    assert 'Beam size' in capsys.readouterr().err


def test_cli_later_failure_preserves_prior_receipts(monkeypatch, capsys):
    from carr_library.audio import transcription
    class Session:
        def __init__(self, manifest): pass
        def transcribe(self, source, output, **kwargs):
            if str(source) == 'two.wav':
                raise OSError('synthetic disk full')
            return {'status': 'complete', 'files': {'txt': 'saved-result.txt'}}
    monkeypatch.setattr(transcription, 'TranscriptionSession', Session)
    assert main(['transcribe', 'one.wav', 'two.wav', '--model-manifest', 'model.json',
                 '--output', 'results']) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result['status'] == 'partial' and len(result['results']) == 1
    assert 'disk full' in captured.err


def test_cli_shows_cleanup_paths_on_success_and_preserves_them_on_later_failure(monkeypatch, capsys, tmp_path):
    from carr_library.audio import transcription
    previous_snapshot = tmp_path / '.transcription-previous'
    current_snapshot = tmp_path / '.transcription-current'
    class Session:
        def __init__(self, manifest): pass
        def transcribe(self, source, output, **kwargs):
            if str(source) == 'two.wav':
                error = OSError('Synthetic inference failure')
                error.add_note('The temporary audio snapshot also needs cleanup: ' + str(current_snapshot))
                raise error
            return {'status': 'complete', 'files': {'txt': 'saved.txt'},
                    'warnings': ['Previous audio snapshot could not be removed.'],
                    'cleanup_pending': str(previous_snapshot)}
    monkeypatch.setattr(transcription, 'TranscriptionSession', Session)
    assert main(['transcribe', 'one.wav', '--model-manifest', 'model.json', '--output', 'results']) == 0
    success = capsys.readouterr()
    assert str(previous_snapshot) in success.err
    assert json.loads(success.out)['status'] == 'complete'
    assert main(['transcribe', 'one.wav', 'two.wav', '--model-manifest', 'model.json', '--output', 'results']) == 1
    partial = capsys.readouterr()
    assert str(previous_snapshot) in partial.err and str(current_snapshot) in partial.err
    result = json.loads(partial.out)
    assert result['status'] == 'partial' and len(result['results']) == 1
    assert result['results'][0]['cleanup_pending'] == str(previous_snapshot)
