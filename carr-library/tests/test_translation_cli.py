"""Actual CLI dispatcher, files and durable jobs; provider responses stay synthetic."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library import translation_cli as module
from carr_library.assistant.models import canonical
from tests.assistant.test_translation import ready, text_calls


def invoke(service, capsys, *arguments):
    status = module.main(['--data-dir', str(service.root), *arguments],
                         service_factory=lambda root: service)
    raw = capsys.readouterr().out
    return status, json.loads(raw)


def options(service, profile, tmp_path):
    source = tmp_path / 'Original.md'
    source.write_bytes('# Heading\n\nExact source.\n'.encode('utf-8'))
    return source, ['--profile', profile.id, '--input', str(source), '--markdown',
        '--source', 'en', '--target', 'uk', '--budget-micros', '1000000',
        '--title', 'Exact structured source']


def test_actual_cli_preview_queue_inspect_run_and_export_keep_explicit_transfer(ready, tmp_path, capsys):
    service, profile, vault, transport = ready
    source, args = options(service, profile, tmp_path)
    original = source.read_bytes()
    status, preview = invoke(service, capsys, 'preview', *args)
    assert status == 0 and preview['calls'] == 2 and preview['text_transferred'] is False
    assert not text_calls(transport)
    approval = ['--approve-profile', profile.fingerprint,
                '--approve-preview', preview['review_sha256'], '--approve-transfer']
    status, queued = invoke(service, capsys, 'queue', *args, *approval)
    assert status == 0 and queued['status'] == 'queued' and not text_calls(transport)
    identity = queued['id']
    status, inspected = invoke(service, capsys, 'inspect', '--job', identity)
    assert status == 0 and inspected['job_sha256'] == queued['job_sha256']
    status, refused = invoke(service, capsys, 'run', '--job', identity,
                             '--approve-job-sha256', queued['job_sha256'])
    assert status == 1 and not text_calls(transport)
    status, completed = invoke(service, capsys, 'run', '--job', identity,
        '--approve-job-sha256', queued['job_sha256'], '--allow-network')
    assert status == 0 and completed['status'] == 'complete' and len(text_calls(transport)) == 2
    output = tmp_path / 'translation-output'
    status, exported = invoke(service, capsys, 'export', '--job', identity, '--destination', str(output))
    assert status == 0 and exported['status'] == 'complete'
    assert (output / 'original.txt').read_bytes() == original == source.read_bytes()
    status, again = invoke(service, capsys, 'export', '--job', identity, '--destination', str(output))
    assert status == 1 and (output / 'original.txt').read_bytes() == original
    assert len(text_calls(transport)) == 2


@pytest.mark.parametrize('changed', ['source', 'title', 'profile', 'budget'])
def test_changed_cli_review_cannot_queue_even_when_remote_language_pair_is_unchanged(
        ready, tmp_path, capsys, changed):
    service, profile, vault, transport = ready
    source, args = options(service, profile, tmp_path)
    _, preview = invoke(service, capsys, 'preview', *args)
    permit_profile = profile.fingerprint
    if changed == 'source':
        source.write_text('# Changed\n\nExact source.\n', encoding='utf-8')
    elif changed == 'title':
        args[args.index('--title') + 1] = 'Changed title'
    elif changed == 'profile':
        permit_profile = '0' * 64
    else:
        args[args.index('--budget-micros') + 1] = '2000000'
    status, result = invoke(service, capsys, 'queue', *args, '--approve-transfer',
        '--approve-profile', permit_profile, '--approve-preview', preview['review_sha256'])
    assert status == 1 and service.list() == () and not text_calls(transport)


def test_language_refresh_requires_exact_separate_consent_and_no_text(ready, capsys):
    service, profile, vault, transport = ready
    count = len(transport.calls)
    status, cached = invoke(service, capsys, 'languages', '--profile', profile.id)
    assert status == 0 and not cached['refreshed'] and len(transport.calls) == count
    status, refused = invoke(service, capsys, 'languages', '--profile', profile.id, '--refresh')
    assert status == 1 and len(transport.calls) == count
    status, refreshed = invoke(service, capsys, 'languages', '--profile', profile.id,
        '--refresh', '--allow-network', '--approve-profile', profile.fingerprint)
    assert status == 0 and refreshed['refreshed'] and len(transport.calls) == count + 1
    assert not text_calls(transport)


def test_committed_cli_queue_identity_survives_following_inspection_failure(ready, tmp_path, capsys, monkeypatch):
    service, profile, vault, transport = ready
    source, args = options(service, profile, tmp_path)
    _, preview = invoke(service, capsys, 'preview', *args)
    saved = service.jobs.load
    def unavailable(identity):
        raise OSError('Committed state needs a later inspection.')
    monkeypatch.setattr(service.jobs, 'load', unavailable)
    status, result = invoke(service, capsys, 'queue', *args, '--approve-transfer',
        '--approve-profile', profile.fingerprint, '--approve-preview', preview['review_sha256'])
    assert status == 0 and result['status'] == 'queued_inspection_pending'
    assert saved(result['id'])['status'] == 'queued' and not text_calls(transport)


def test_top_level_translation_route_preserves_existing_dispatch(monkeypatch):
    from carr_library import __main__ as launcher
    calls = []
    monkeypatch.setattr(module, 'main', lambda arguments: calls.append(arguments) or 23)
    assert launcher.main(['translation', '--data-dir', 'chosen', 'profiles']) == 23
    assert calls == [['--data-dir', 'chosen', 'profiles']]


def test_isolated_installed_translation_origin_matches_exact_source(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'src' / 'carr_library' / 'translation_cli.py'
    expected = sha256(source.read_bytes()).hexdigest()
    program = ('import hashlib,json,pathlib,sys; import carr_library.translation_cli as m; '
        'p=pathlib.Path(m.__file__).resolve(); root=pathlib.Path(sys.prefix).resolve(); '
        'assert root in p.parents; assert hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[1]; '
        'print(json.dumps(dict(origin=str(p),sha256=sys.argv[1])))')
    checked = subprocess.run([sys.executable, '-I', '-B', '-c', program, expected],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)['sha256'] == expected
    root = tmp_path / 'library'
    root.mkdir()
    listed = subprocess.run([sys.executable, '-I', '-B', '-m', 'carr_library',
        'translation', '--data-dir', str(root), 'profiles'], cwd=tmp_path,
        capture_output=True, text=True, timeout=30)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    assert json.loads(listed.stdout) == []


def test_sigint_returns_130_with_the_durable_paused_job_and_without_remote_text(
        ready, tmp_path, capsys, monkeypatch):
    import signal
    service, profile, vault, transport = ready
    source, args = options(service, profile, tmp_path)
    _, preview = invoke(service, capsys, 'preview', *args)
    _, queued = invoke(service, capsys, 'queue', *args, '--approve-transfer',
        '--approve-profile', profile.fingerprint, '--approve-preview', preview['review_sha256'])
    resume = service.resume
    def interrupted(*arguments, **keywords):
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        return resume(*arguments, **keywords)
    monkeypatch.setattr(service, 'resume', interrupted)
    previous = signal.getsignal(signal.SIGINT)
    status, result = invoke(service, capsys, 'run', '--job', queued['id'], '--allow-network',
        '--approve-job-sha256', queued['job_sha256'])
    assert status == 130 and result['status'] == 'paused'
    assert service.jobs.load(queued['id'])['status'] == 'paused' and not text_calls(transport)
    assert signal.getsignal(signal.SIGINT) is previous
