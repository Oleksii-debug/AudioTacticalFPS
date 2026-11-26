"""CLI source fidelity and interruption with actual local audio production."""
from hashlib import sha256
import json
from pathlib import Path
import shutil
import signal

import pytest

from carr_library.studio import cli
from carr_library.studio.project import parse_script
from carr_library.studio.render import ProductionBudget, ProductionService


def test_bounded_input_keeps_utf8_signature_and_mixed_line_endings(tmp_path):
    raw = b'\xef\xbb\xbf[Scene: Source]\r\nAlice: First.\r\nBob: Second.\n'
    source = tmp_path / 'source.txt'
    source.write_bytes(raw)
    text = cli._read_utf8(source, len(raw))
    assert text.encode('utf-8') == raw
    project = parse_script(text, mode='authored')
    assert project.original_script.encode('utf-8') == raw
    assert project.scenes[0].title == 'Source'
    assert [role.name for role in project.roles] == ['Alice', 'Bob']
    assert source.read_bytes() == raw


@pytest.mark.parametrize('raw,maximum', [(b'', 100), (b'larger than allowed', 4), (b'\xff', 100)])
def test_empty_oversize_or_invalid_utf8_input_is_not_accepted(tmp_path, raw, maximum):
    source = tmp_path / 'script.txt'
    source.write_bytes(raw)
    with pytest.raises((ValueError, UnicodeError)):
        cli._read_utf8(source, maximum)
    assert source.read_bytes() == raw


def test_instruction_signature_is_metadata_and_does_not_mutate_file(tmp_path, capsys):
    source = tmp_path / 'instructions.txt'
    raw = b'\xef\xbb\xbfList libraries\r\n'
    source.write_bytes(raw)
    assert cli.assistant_main(['--data-dir', str(tmp_path / 'library'),
                               '--instructions', str(source)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['steps'] == [{'tool': 'library.list', 'arguments': {}}]
    assert source.read_bytes() == raw


@pytest.fixture
def actual_cli(tmp_path, monkeypatch):
    from carr_library.speech import FliteProvider, VoiceRegistry
    executable = shutil.which('ffmpeg')
    if not executable or not shutil.which('ffprobe'):
        pytest.skip('This real CLI production test requires installed FFmpeg and ffprobe.')
    registry = VoiceRegistry([FliteProvider(executable)])
    if 'flite:slt' not in {voice.id for voice in registry.voices()}:
        pytest.skip('This real CLI production test requires installed Flite speech.')

    class CountSpeech:
        completed = 0
        interrupt_next = False

        def __getattr__(self, name):
            return getattr(registry, name)

        def synthesize(self, *args, **kwargs):
            result = registry.synthesize(*args, **kwargs)
            self.completed += 1
            if self.interrupt_next:
                self.interrupt_next = False
                signal.raise_signal(signal.SIGINT)
            return result

    voices = CountSpeech()
    monkeypatch.setattr(cli, 'ProductionService',
                        lambda *args, **kwargs: ProductionService(*args, voices=voices, **kwargs))
    library, outputs = tmp_path / 'library', tmp_path / 'outputs'
    outputs.mkdir()
    source = tmp_path / 'source.txt'
    raw = b'\xef\xbb\xbf[Scene: Archive]\r\nAlice: Preserve this exact source.\r\n'
    source.write_bytes(raw)
    args = ['--data-dir', str(library), 'render', '--script', str(source),
            '--mode', 'authored', '--voice', 'flite:slt', '--output', str(outputs), '--no-stems']
    return voices, library, source, raw, args


def test_actual_cli_render_and_reexport_keep_exact_original_and_reuse_audio(actual_cli, capsys, tmp_path):
    voices, library, source, raw, args = actual_cli
    assert cli.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    original = json.loads(Path(first['destination'], 'project.json').read_text(encoding='utf-8'))
    assert original['original_script'].encode('utf-8') == raw
    assert first['result']['source_sha256'] == sha256(raw).hexdigest()
    assert voices.completed == 1
    expected = sha256(Path(first['destination'], 'master.wav').read_bytes()).hexdigest()
    shutil.rmtree(first['destination'])
    second_parent = tmp_path / 'restored-exports'
    second_parent.mkdir()
    assert cli.main(['--data-dir', str(library), 'reexport', first['id'],
                     '--output', str(second_parent)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert voices.completed == 1
    assert sha256(Path(second['destination'], 'master.wav').read_bytes()).hexdigest() == expected
    assert source.read_bytes() == raw


def test_actual_cli_sigint_receipts_accepted_speech_and_resume_does_not_repeat(actual_cli, capsys):
    voices, library, source, raw, args = actual_cli
    previous = signal.getsignal(signal.SIGINT)
    voices.interrupt_next = True
    assert cli.main(args) == 130
    diagnostic = capsys.readouterr().err
    identity = diagnostic.split('Production job: ', 1)[1].splitlines()[0]
    service = ProductionService(library / 'state' / 'studio', voices=voices)
    saved = service.get(identity)
    assert saved['status'] == 'paused'
    assert any(node['kind'] == 'speech' for node in saved['nodes'].values())
    assert voices.completed == 1
    assert signal.getsignal(signal.SIGINT) == previous
    assert cli.main(['--data-dir', str(library), 'resume', identity]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'complete'
    assert voices.completed == 1 and source.read_bytes() == raw


def test_many_empty_cache_directories_are_bounded_before_actual_speech(actual_cli):
    voices, library, source, raw, args = actual_cli
    service = ProductionService(library / 'state' / 'studio', voices=voices,
                                budget=ProductionBudget(max_cache_files=20))
    for index in range(30):
        (service.cache / str(index)).mkdir()
    project = parse_script('Preserve storage.', voice_ids=('flite:slt',))
    identity = service.submit(project, source.parent, stems=False)
    with pytest.raises(ValueError, match='file and directory entry budget'):
        service.run(identity)
    assert voices.completed == 0
