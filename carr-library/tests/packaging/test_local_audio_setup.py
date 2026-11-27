"""Public synthetic bytes for the source-only explicit FULL setup flow.

The real existing archive preparer and sealed core builder are exercised. No
remote download or FFmpeg execution is performed by these portable tests.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZIP_STORED, ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location('setup_tests_' + name,
                                                  ROOT / 'packaging' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = load('setup_local_audio')


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    helpers = tmp_path / 'installed' / 'tools' / 'audio-setup'
    (helpers / 'audio-runtime').mkdir(parents=True)
    shutil.copyfile(ROOT / 'packaging' / 'prepare_ffmpeg_runtime.py',
                    helpers / 'prepare_ffmpeg_runtime.py')
    root = 'ffmpeg-9.1-full_build'
    payloads = {
        'ffmpeg.exe': ('bin/ffmpeg.exe', b'PUBLIC NONEXECUTABLE FFMPEG FIXTURE'),
        'ffprobe.exe': ('bin/ffprobe.exe', b'PUBLIC NONEXECUTABLE FFPROBE FIXTURE'),
        'FFmpeg-LICENSE.txt': ('LICENSE', b'Synthetic license fixture\n'),
        'FFmpeg-README.txt': ('README.txt', b'Synthetic original documentation\n'),
    }
    archive = tmp_path / 'retained-full.zip'
    members = {}
    with ZipFile(archive, 'w', ZIP_STORED) as output:
        for name, (relative, data) in payloads.items():
            member = root + '/' + relative
            output.writestr(member, data)
            members[name] = (member, len(data), hashlib.sha256(data).hexdigest())
    raw = archive.read_bytes()
    monkeypatch.setattr(setup.runtime, 'ARCHIVE_MEMBERS', members)
    monkeypatch.setattr(setup.runtime, 'FFMPEG_ARCHIVE_BYTES', len(raw))
    monkeypatch.setattr(setup.runtime, 'FFMPEG_ARCHIVE_SHA256', hashlib.sha256(raw).hexdigest())
    source_lock = json.loads((ROOT / 'packaging' / 'audio-runtime' / 'source-lock.json').read_text())
    source_lock['archive']['size_bytes'] = len(raw)
    source_lock['archive']['sha256'] = hashlib.sha256(raw).hexdigest()
    source_lock['original_files'] = {relative: {
        'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        for relative, data in payloads.values()}
    (helpers / 'audio-runtime' / 'source-lock.json').write_text(
        json.dumps(source_lock, indent=2), encoding='utf-8')
    monkeypatch.setattr(setup, 'HERE', helpers)
    monkeypatch.setattr(setup, '_require_windows', lambda: None)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('Setup must not execute binaries.'))
    requests = []

    class Response(io.BytesIO):
        status = 200

        def geturl(self):
            return setup.runtime.FFMPEG_ARCHIVE_URL

    class Opener:
        def open(self, request, *, timeout):
            requests.append((request, timeout))
            return Response(raw)

    monkeypatch.setattr(setup, '_opener', lambda: Opener())
    return {'archive': archive, 'raw': raw, 'helpers': helpers, 'members': members,
            'requests': requests, 'output': tmp_path / 'local audio ü'}


def test_default_plan_does_not_load_preparer_write_or_download(prepared, monkeypatch):
    monkeypatch.setattr(setup, '_load_prepare', lambda: pytest.fail('A plan must not load the preparer.'))
    assert setup.main(['--output', str(prepared['output'])]) == 0
    assert not prepared['output'].exists()
    assert prepared['requests'] == []


@pytest.mark.parametrize('answer', ['NO', '', 'yes'])
def test_interactive_refusal_preserves_selected_sources(prepared, monkeypatch, answer):
    monkeypatch.setattr('builtins.input', lambda _: answer)
    assert setup.main(['--output', str(prepared['output']), '--interactive']) == 0
    assert not prepared['output'].exists() and prepared['requests'] == []
    assert prepared['archive'].read_bytes() == prepared['raw']


def test_explicit_download_uses_existing_preparer_and_retains_full_inventory(prepared):
    result = setup._prepare(prepared['output'], setup.inputs())
    root = Path(result['runtime'])
    assert result['native_execution'] == 'NOT_RUN' and result['registration_changed'] is False
    assert len(prepared['requests']) == 1
    request, timeout = prepared['requests'][0]
    assert request.full_url == setup.runtime.FFMPEG_ARCHIVE_URL and timeout == 15
    assert request.get_header('Authorization') is None
    assert setup.runtime.verify_runtime(root)['files'] == {
        name: {'sha256': checksum, 'bytes': size}
        for name, (_, size, checksum) in prepared['members'].items()}
    assert (prepared['output'] / setup.inputs()['name']).read_bytes() == prepared['raw']


def test_explicit_existing_archive_needs_no_network_or_registration(prepared, monkeypatch):
    monkeypatch.setattr('builtins.input', lambda _: 'YES')
    before = list(sys.path), sys.dont_write_bytecode
    assert setup.main(['--output', str(prepared['output']), '--archive',
                       str(prepared['archive']), '--interactive']) == 0
    assert prepared['requests'] == [] and prepared['archive'].read_bytes() == prepared['raw']
    assert setup.runtime.verify_runtime(prepared['output'] / 'runtime')['native_validation']['status'] == 'NOT_RUN'
    assert (list(sys.path), sys.dont_write_bytecode) == before


@pytest.mark.parametrize('case', ['installation-child', 'installation-parent', 'existing', 'changed-plan', 'disk'])
def test_refusal_precedes_download_or_output_publication(prepared, tmp_path, monkeypatch, case):
    output, record = prepared['output'], setup.inputs()
    if case == 'installation-child':
        output = prepared['helpers'].parent.parent / 'runtime'
    elif case == 'installation-parent':
        output = prepared['helpers'].parents[3]
    elif case == 'existing':
        output.mkdir()
        (output / 'original.txt').write_bytes(b'keep')
    elif case == 'changed-plan':
        record['sha256'] = '0' * 64
    elif case == 'disk':
        usage = shutil.disk_usage(tmp_path)
        monkeypatch.setattr(shutil, 'disk_usage', lambda _: usage._replace(free=0))
    with pytest.raises((ValueError, OSError)):
        setup._prepare(output, record)
    assert prepared['requests'] == []
    if case == 'existing':
        assert (output / 'original.txt').read_bytes() == b'keep'
    elif case not in ('installation-parent',):
        assert not output.exists()


@pytest.mark.parametrize('case', ['corrupt', 'overflow', 'insecure-redirect', 'foreign-host', 'cancel'])
def test_download_failure_never_publishes_or_executes_unverified_archive(prepared, tmp_path, monkeypatch, case):
    destination = tmp_path / 'download'
    destination.mkdir()
    original = destination / 'original.txt'
    original.write_bytes(b'unrelated')
    raw = prepared['raw']
    data = raw[:-1] + bytes([raw[-1] ^ 1]) if case == 'corrupt' else (
        raw + b'overflow' if case == 'overflow' else raw)

    class Response(io.BytesIO):
        status = 200

        def geturl(self):
            if case == 'insecure-redirect':
                return 'http://github.com/not-accepted'
            if case == 'foreign-host':
                return 'https://unexpected.example/not-accepted'
            return setup.runtime.FFMPEG_ARCHIVE_URL

        def read1(self, count):
            if case == 'cancel':
                raise KeyboardInterrupt()
            return super().read1(count)

    class Opener:
        def open(self, request, *, timeout):
            return Response(data)

    monkeypatch.setattr(setup, '_opener', lambda: Opener())
    with pytest.raises(KeyboardInterrupt if case == 'cancel' else ValueError):
        setup._download(setup.inputs(), destination)
    assert sorted(path.name for path in destination.iterdir()) == ['original.txt']
    assert original.read_bytes() == b'unrelated'


def test_download_cannot_replace_a_concurrent_destination(prepared, tmp_path, monkeypatch):
    folder = tmp_path / 'download'
    folder.mkdir()
    publish = setup.publish_noreplace

    def competing(stage, target):
        target.write_bytes(b'concurrent owner')
        publish(stage, target)

    monkeypatch.setattr(setup, 'publish_noreplace', competing)
    with pytest.raises(FileExistsError):
        setup._download(setup.inputs(), folder)
    assert (folder / setup.inputs()['name']).read_bytes() == b'concurrent owner'
    assert not list(folder.glob('*.partial'))


@pytest.mark.parametrize('case', ['wrong-digest', 'missing-original-notice', 'boolean-schema'])
def test_lock_must_match_current_full_identity_and_inventory(prepared, case):
    path = prepared['helpers'] / 'audio-runtime' / 'source-lock.json'
    value = json.loads(path.read_text())
    if case == 'wrong-digest':
        value['archive']['sha256'] = '0' * 64
    elif case == 'missing-original-notice':
        value['original_files'].pop('LICENSE')
    else:
        value['version'] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        setup.inputs()


@pytest.mark.parametrize('url', [
    'http://github.com/archive', 'https://localhost/archive',
    'https://github.com.example/archive', 'https://user:password@github.com/archive',
    'https://github.com:444/archive', 'https://github.com/archive#fragment',
])
def test_redirects_cannot_expand_the_fixed_public_https_scope(url):
    with pytest.raises(ValueError):
        setup._remote_url(url)



def test_source_lock_read_is_bounded_even_if_the_observed_payload_grows(prepared, monkeypatch):
    from contextlib import contextmanager

    original = setup.verified_reader
    reads = []

    @contextmanager
    def growing(path):
        with original(path) as stream:
            class View:
                def fileno(self):
                    return stream.fileno()

                def read(self, maximum):
                    reads.append(maximum)
                    assert maximum == setup.MAX_LOCK_BYTES + 1
                    return b'x' * maximum
            yield View()

    monkeypatch.setattr(setup, 'verified_reader', growing)
    with pytest.raises(ValueError, match='byte budget'):
        setup.inputs()
    assert reads == [setup.MAX_LOCK_BYTES + 1]
    assert not prepared['output'].exists() and prepared['requests'] == []


def test_download_revisits_deadline_between_single_buffer_reads(prepared, tmp_path, monkeypatch):
    folder = tmp_path / 'bounded download'
    folder.mkdir()
    clock, reads = [0.0], []
    monkeypatch.setattr(setup.time, 'monotonic', lambda: clock[0])

    class Response(io.BytesIO):
        status = 200

        def geturl(self):
            return setup.runtime.FFMPEG_ARCHIVE_URL

        def read(self, count):
            pytest.fail('An aggregating read can hide an unbounded slow-drip peer.')

        def read1(self, count):
            reads.append(count)
            clock[0] += setup.DOWNLOAD_SECONDS + 1
            return super().read1(1)

    class Opener:
        def open(self, request, *, timeout):
            assert timeout == 15
            return Response(prepared['raw'])

    monkeypatch.setattr(setup, '_opener', lambda: Opener())
    with pytest.raises(TimeoutError, match='time budget'):
        setup._download(setup.inputs(), folder)
    assert len(reads) == 1
    assert not list(folder.iterdir())
