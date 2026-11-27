"""Installed-compatible command invocations using real files and private stdin."""
import json
from pathlib import Path
import os
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp


def invoke(root, *arguments, password=None):
    return subprocess.run([sys.executable, '-m', 'carr_library', 'sync', '--data-dir', str(root), *arguments],
        input=password, text=True, capture_output=True, timeout=60)


@pytest.mark.parametrize('encrypted', [False, True])
def test_cli_real_publish_inspect_restore_without_echoing_password(tmp_path, encrypted):
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('CLI synthetic library')
    app.add_text(workspace, 'Source', 'A precise source quotation.')
    folder = str(tmp_path / 'portable')
    password = 'CLI private passphrase 4321\n' if encrypted else None
    secret = '--password-stdin' if encrypted else '--plain'
    published = invoke(app.root, 'publish', '--folder', folder, '--empty', '--label', 'CLI checkpoint', secret, password=password)
    assert published.returncode == 0, published.stderr
    receipt = json.loads(published.stdout)
    for command in ('list', 'inspect', 'restore'):
        arguments = [command, '--folder', folder]
        if command != 'list':
            arguments += ['--token', receipt['token'], secret]
        if command == 'restore':
            arguments += ['--destination', str(tmp_path / 'restored')]
        result = invoke(app.root, *arguments, password=password if command != 'list' else None)
        assert result.returncode == 0, result.stderr
        value = json.loads(result.stdout)
        if command == 'list':
            assert value['trusted'] is False
        else:
            assert value['verified'] is True
        if password:
            assert password.strip() not in result.stdout + result.stderr
    assert LibraryApp(tmp_path / 'restored').search(workspace, 'precise source')
    assert app.search(workspace, 'precise source')


def test_publish_requires_explicit_plain_or_hidden_secret_and_parent(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    result = invoke(app.root, 'publish', '--folder', str(tmp_path / 'portable'), '--empty')
    assert result.returncode == 2 and '--plain' in result.stderr
    assert not (tmp_path / 'portable').exists()
    result = invoke(app.root, 'publish', '--folder', str(tmp_path / 'portable'), '--plain')
    assert result.returncode == 2 and '--empty' in result.stderr


def test_password_never_accepted_as_argument_or_printed_on_failure(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    secret = 'Wrong password stays private'
    result = invoke(app.root, 'inspect', '--folder', str(tmp_path / 'missing'), '--token', '0' * 64,
                    '--password-stdin', password=secret + '\n')
    assert result.returncode == 2
    assert secret not in result.stderr + result.stdout
    assert 'Traceback' not in result.stderr


def test_missing_library_does_not_initialize_data_on_read_request(tmp_path):
    result = invoke(tmp_path / 'missing-data', 'list', '--folder', str(tmp_path / 'portable'))
    assert result.returncode == 2
    assert not (tmp_path / 'missing-data').exists()


def test_cli_publish_explicit_audio_flag_reaches_the_verified_manifest(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    folder = str(tmp_path / 'portable')
    published = invoke(app.root, 'publish', '--folder', folder, '--empty', '--plain',
                       '--include-generated-audio')
    assert published.returncode == 0, published.stderr
    receipt = json.loads(published.stdout)
    assert receipt['include_generated'] is True
    inspected = invoke(app.root, 'inspect', '--folder', folder, '--token', receipt['token'], '--plain')
    assert inspected.returncode == 0, inspected.stderr
    verified = json.loads(inspected.stdout)
    assert verified['verified'] and verified['include_generated'] is True


@pytest.mark.parametrize('command', ['list', 'inspect', 'restore'])
def test_generated_audio_flag_is_rejected_outside_sync_publication(tmp_path, command):
    app = LibraryApp(tmp_path / 'data')
    arguments = [command, '--folder', str(tmp_path / 'portable'), '--include-generated-audio']
    if command != 'list':
        arguments += ['--token', '0' * 64, '--plain']
    if command == 'restore':
        arguments += ['--destination', str(tmp_path / 'restored')]
    result = invoke(app.root, *arguments)
    assert result.returncode == 2 and 'unrecognized arguments: --include-generated-audio' in result.stderr
    assert not (tmp_path / 'portable').exists() and not (tmp_path / 'restored').exists()
