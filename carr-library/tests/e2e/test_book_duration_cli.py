"""Public headless duration review and publication use the same saved service."""
import argparse
import json
from pathlib import Path

import pytest

from carr_library.__main__ import main
from carr_library.audio_cli import add_audio_arguments, validate_audio_arguments
from tests.audio.test_audio_batches import ffmpeg
from tests.audio.test_book_duration import completed_book, root_for


def test_public_cli_reviews_reopens_publishes_and_exports_exact_measured_parts(tmp_path, ffmpeg, capsys):
    app, service, batch, studio, original, voice = completed_book(tmp_path, ffmpeg)
    calls = list(voice.calls)
    base = ['--data-dir', str(app.root), '--audio-batch', batch.id,
            '--audio-batch-item', batch.items[0].id]
    assert main([*base, '--audio-batch-action', 'duration-review',
                 '--audio-duration-seconds', '0.137', '--audio-duration-format', 'wav']) == 0
    review = json.loads(capsys.readouterr().out)
    assert review['schema'] == 'carr-book-duration-review/1'
    assert main([*base, '--audio-batch-action', 'duration-list']) == 0
    listing = json.loads(capsys.readouterr().out)
    assert [row['id'] for row in listing['reviews']] == [review['id']]
    saved = [*base, '--audio-duration-review', review['id']]
    assert main([*saved, '--audio-batch-action', 'duration-show']) == 0
    assert json.loads(capsys.readouterr().out)['review'] == review
    assert main([*saved, '--audio-batch-action', 'duration-publish',
                 '--audio-duration-approved', review['sha256'], '--ffmpeg', str(ffmpeg)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['status'] == 'complete'
    target = tmp_path / 'headless-measured-part.wav'
    filename = receipt['files'][0]['name']
    assert main([*saved, '--audio-batch-action', 'duration-export',
                 '--audio-batch-file', filename, '--audio-batch-export', str(target)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported['review_id'] == review['id']
    assert target.read_bytes() == (root_for(service, batch, review) / 'outputs' / filename).read_bytes()
    assert studio.project_path.read_bytes() == original and voice.calls == calls


@pytest.mark.parametrize('options', [
    ['--audio-duration-seconds', '10'],
    ['--audio-batch', 'a' * 32, '--audio-duration-seconds', '10'],
    ['--audio-batch', 'a' * 32, '--audio-batch-action', 'duration-review'],
    ['--audio-batch', 'a' * 32, '--audio-batch-item', 'b' * 32, '--audio-batch-action', 'duration-review'],
    ['--audio-batch', 'a' * 32, '--audio-batch-item', 'b' * 32, '--audio-batch-action', 'duration-publish',
     '--audio-duration-review', 'c' * 32, '--ffmpeg', '/selected/ffmpeg'],
    ['--audio-batch', 'a' * 32, '--audio-batch-item', 'b' * 32, '--audio-batch-action', 'duration-list',
     '--audio-duration-approved', 'd' * 64],
    ['--audio-batch', 'a' * 32, '--audio-batch-item', 'b' * 32, '--audio-batch-action', 'duration-show',
     '--audio-duration-review', 'c' * 32, '--audio-duration-seconds', '10'],
    ['--audio-batch', 'a' * 32, '--audio-batch-item', 'b' * 32, '--audio-batch-action', 'duration-export',
     '--audio-duration-review', 'c' * 32],
])
def test_headless_duration_requires_explicit_scope_review_and_publication_approval(options):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path)
    add_audio_arguments(parser)
    args = parser.parse_args(['--data-dir', '/explicit/library', *options])
    with pytest.raises(SystemExit) as raised:
        validate_audio_arguments(args, parser)
    assert raised.value.code == 2
