"""VoiceWorkshop interoperability with small, generated PCM fixtures."""
import json
from pathlib import Path
import wave

import pytest

from carr_library.integrations.voiceworkshop_review import audit_voiceworkshop_review
from carr_library.studio.cli import main


def _row(root, index, *, source_hash, session, frame):
    path = root / f'take-{index}.wav'
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        output.writeframes(frame * 8000)
    return {'id': f'take-{index}', 'audio_path': str(path), 'source_sha256': source_hash,
            'speaker': 'Owner', 'session': session, 'text': 'Аудіотест.',
            'speaker_verified': True, 'text_verified': True, 'clean_verified': True}


@pytest.fixture
def reviewed(tmp_path):
    audio_root = tmp_path / 'Аудіо'
    audio_root.mkdir()
    rows = [
        _row(audio_root, 1, source_hash='a' * 64, session='Tuesday', frame=b'\x00\0'),
        _row(audio_root, 2, source_hash='b' * 64, session='Friday', frame=b'\x10\0'),
    ]
    return tmp_path / 'review.json', audio_root, rows


def test_voice_corpus_command_reads_without_creating_library(reviewed, capsys):
    review, root, rows = reviewed
    rows.append({'speaker_verified': False, 'audio_path': '//unreviewed.invalid/voice.wav'})
    review.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    before = review.read_bytes()
    unused_library = review.parent / 'Unused library'
    assert main(['--data-dir', str(unused_library), 'voice-corpus', str(review), '--audio-root', str(root),
                 '--speaker', 'owner']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['sessions'] == [{'clips': 1, 'seconds': 1}, {'clips': 1, 'seconds': 1}]
    assert report['reviewed_unique_clips'] == report['total_seconds'] == 2
    assert report['unapproved_rows'] == 1 and not report['training_or_listening_quality_verified']
    assert 'Аудіотест' not in str(report) and 'Tuesday' not in str(report)
    assert review.read_bytes() == before and not unused_library.exists()


@pytest.mark.parametrize(('problem', 'match'), [
    ('speaker', 'different verified speaker'),
    ('source', 'one recording to multiple sessions'),
    ('audio', 'duplicates a WAV file in a different session'),
    ('outside', 'outside the selected directory'),
    ('session', 'two independent recording sessions'),
])
def test_unsafe_or_unqualified_review_is_rejected(reviewed, problem, match):
    review, root, rows = reviewed
    first, second = rows
    if problem == 'speaker':
        second['speaker'] = 'Different'
    elif problem == 'source':
        second['source_sha256'] = first['source_sha256']
    elif problem == 'audio':
        Path(second['audio_path']).write_bytes(Path(first['audio_path']).read_bytes())
    elif problem == 'outside':
        second['audio_path'] = str(review.parent / 'outside.wav')
        Path(second['audio_path']).write_bytes(Path(first['audio_path']).read_bytes())
    else:
        second['session'] = first['session']
    review.write_text(json.dumps(rows), encoding='utf-8')
    with pytest.raises(ValueError, match=match):
        audit_voiceworkshop_review(review, root, 'Owner')


def test_voice_corpus_cancellation_stops_before_reading_private_data(tmp_path):
    missing_review = tmp_path / 'unread.json'
    with pytest.raises(InterruptedError, match='cancelled'):
        audit_voiceworkshop_review(missing_review, tmp_path, 'Owner', cancelled=lambda: True)
    assert not missing_review.exists()
