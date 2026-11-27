"""Actual command-line backup processes and complete data recovery."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup


ROOT = Path(__file__).resolve().parents[2]


def invoke(*args):
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT / 'src'), str(ROOT))),
                       PYTHONUTF8='1')
    return subprocess.run([sys.executable, '-m', 'carr_library', *map(str, args)],
                          env=environment, capture_output=True, text=True, encoding='utf-8', timeout=30)


@pytest.mark.parametrize('generated', [False, True])
def test_actual_backup_flag_is_recorded_and_source_restores(tmp_path, generated):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic backup')
    app.add_text(workspace, 'Original', 'A source passage for complete recovery.')
    destination = tmp_path / 'backup'
    arguments = ['--data-dir', app.root, '--backup', destination]
    if generated:
        arguments.append('--include-generated-audio')
    result = invoke(*arguments)
    assert result.returncode == 0, (result.stdout, result.stderr)
    manifest = json.loads((destination / 'manifest.json').read_text())
    assert manifest['generated_audio_included'] is generated
    checksum = result.stdout.strip().rsplit(' ', 1)[-1]
    restored = tmp_path / 'restored'
    restore_backup(destination, restored, expected_manifest_sha256=checksum)
    reopened = LibraryApp(restored)
    hit, = reopened.search(workspace, 'complete recovery')
    assert hit.quote == 'complete recovery'
    original_files = {p.name: p.read_bytes() for p in app.assets_dir.iterdir() if p.is_file()}
    restored_files = {p.name: p.read_bytes() for p in reopened.assets_dir.iterdir() if p.is_file()}
    assert original_files and restored_files == original_files


def test_generated_flag_without_backup_fails_before_creating_library(tmp_path):
    root = tmp_path / 'not-created'
    result = invoke('--data-dir', root, '--include-generated-audio', '--document-formats')
    assert result.returncode == 2
    assert '--include-generated-audio requires --backup' in result.stderr
    assert not root.exists()
