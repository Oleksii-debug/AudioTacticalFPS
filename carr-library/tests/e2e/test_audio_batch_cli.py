"""The public headless entry point uses the same durable audio producer."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from carr_library.__main__ import main
from carr_library.application import LibraryApp
from carr_library.audio.batch_models import Selection
from carr_library.audio.batches import AudioBatchService
from carr_library.audio_cli import add_audio_arguments, validate_audio_arguments
from tests.audio.test_audio_batches import ffmpeg, pcm, preset, output_file


def test_actual_cli_creates_previews_runs_lists_and_exports_one_verified_queue(tmp_path, ffmpeg, capsys):
    app = LibraryApp(tmp_path / 'library')
    source = pcm(tmp_path / 'source.wav')
    original = source.read_bytes()
    selection = tmp_path / 'selected-plan.json'
    selection.write_text(json.dumps({'schema': 'carr-audio-batch-selection/1',
        'items': [asdict(Selection('audio', str(source), 'CLI exact source', preset()))]}), encoding='utf-8')
    base = ['--data-dir', str(app.root)]
    assert main([*base, '--audio-batch-plan', str(selection), '--ffmpeg', str(ffmpeg)]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned['status'] == 'queued'
    identity = planned['id']
    assert main([*base, '--audio-batch', identity, '--audio-preview']) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['plan_sha256'] == planned['plan_sha256'] and preview['items'] == planned['items']
    assert main([*base, '--audio-batch', identity, '--audio-batch-action', 'run', '--ffmpeg', str(ffmpeg)]) == 0
    complete = json.loads(capsys.readouterr().out)
    assert complete['status'] == 'complete'
    assert main([*base, '--audio-batches']) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing == [{'id': identity, 'title': 'Local audio queue', 'status': 'complete',
                        'items': 1, 'plan_sha256': planned['plan_sha256']}]
    output = tmp_path / 'cli-export.wav'
    item = complete['items'][0]
    assert main([*base, '--audio-batch', identity, '--audio-batch-action', 'export',
                 '--audio-batch-item', item['id'], '--audio-batch-file', Path(item['outputs'][0]).name,
                 '--audio-batch-export', str(output)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported['status'] == 'complete' and output.read_bytes() == Path(item['outputs'][0]).read_bytes()
    assert source.read_bytes() == original


def test_cli_pause_is_a_durable_request_and_resume_does_real_work(tmp_path, ffmpeg, capsys):
    app = LibraryApp(tmp_path / 'library')
    service = AudioBatchService(app.root, ffmpeg=ffmpeg)
    state = service.create((Selection('audio', str(pcm(tmp_path / 'source.wav')), 'CLI pause', preset()),))
    args = ['--data-dir', str(app.root), '--audio-batch', state.id]
    assert main([*args, '--audio-batch-action', 'pause']) == 0
    request = json.loads(capsys.readouterr().out)
    assert request['requested'] is True and request['acknowledged'] is False
    assert main([*args, '--audio-batch-action', 'run', '--ffmpeg', str(ffmpeg)]) == 130
    paused = json.loads(capsys.readouterr().out)
    assert paused['status'] == 'paused' and paused['control_acknowledged'] is True
    assert not output_file(service, state).exists()
    assert main([*args, '--audio-batch-action', 'resume', '--ffmpeg', str(ffmpeg)]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed['status'] == 'complete' and resumed['items'][0]['attempt'] == 1


@pytest.mark.parametrize('options', [
    ['--audio-batch', 'a' * 32, '--audio-batch-action', 'run'],
    ['--audio-batch-plan', 'inputs.json', '--audio-batch-action', 'run', '--ffmpeg', '/selected/ffmpeg'],
    ['--audio-batch', 'a' * 32, '--audio-batch-action', 'create', '--ffmpeg', '/selected/ffmpeg'],
    ['--audio-batch', 'a' * 32, '--audio-batch-action', 'export'],
    ['--audio-batch', 'a' * 32, '--audio-batch-item', 'b' * 32],
    ['--audio-batches', '--audio-batch-action', 'cancel'],
    ['--audio-batch-plan', 'inputs.json', '--audio-preview', '--ffmpeg', '/selected/ffmpeg'],
    ['--audio-batch', 'a' * 32, '--audio-output', '/foreign/output'],
])
def test_cli_refuses_ambiguous_or_unselected_batch_controls(options):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    add_audio_arguments(parser)
    args = parser.parse_args(['--data-dir', '/explicit/library', *options])
    with pytest.raises(SystemExit) as error:
        validate_audio_arguments(args, parser)
    assert error.value.code == 2


def test_cli_requires_explicit_selected_library_for_queue_state():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    add_audio_arguments(parser)
    args = parser.parse_args(['--audio-batches'])
    with pytest.raises(SystemExit) as error:
        validate_audio_arguments(args, parser)
    assert error.value.code == 2
