"""Actual isolated CLI exercises the current imported source/installed package."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import bootstrap
from carr_library import application
from carr_library.application import LibraryApp
from carr_library.library_backup import verify_backup


def command(tmp_path, *arguments):
    application_path = str(Path(application.__file__).resolve())
    bootstrap_path = str(Path(bootstrap.__file__).resolve())
    roots = list(dict.fromkeys([str(Path(application_path).parents[1]),
                                str(Path(bootstrap_path).parents[1])]))
    code = (
        "import sys; from pathlib import Path; sys.path[:0]=" + repr(roots) + ";"
        "import bootstrap; import carr_library.application as app;"
        "assert str(Path(app.__file__).resolve())==" + repr(application_path) + ";"
        "assert str(Path(bootstrap.__file__).resolve())==" + repr(bootstrap_path) + ";"
        "from carr_library.__main__ import main; raise SystemExit(main(sys.argv[1:]))"
    )
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='ascii')
    return subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', code,
                           *map(str, arguments)], cwd=tmp_path, env=environment,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)


def test_actual_explicit_capacity_cli_backup_restore_preserves_original(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Unicode Ω')
    result = app.add_text(workspace, 'Original Україна', 'Untouched Україна Ω source.')
    selected = tmp_path / 'backup'
    completed = command(tmp_path, '--data-dir', app.root, '--backup', selected,
                        '--backup-capacity', 'large-source-v1')
    assert completed.returncode == 0, completed.stderr.decode('utf-8')
    hashes = re.findall(r'\b[0-9a-f]{64}\b', completed.stdout.decode('utf-8'))
    assert len(hashes) == 1
    expected = hashes[0]
    value = verify_backup(selected, expected, capacity_profile='large-source-v1')
    assert value['version'] == 4 and 'capacity_profile' not in value
    app.root.rename(tmp_path / 'retired')
    restored = tmp_path / 'restored'
    completed = command(tmp_path, '--data-dir', restored, '--restore', selected,
                        '--manifest-sha256', expected,
                        '--backup-capacity', 'large-source-v1')
    assert completed.returncode == 0, completed.stderr.decode('utf-8')
    recovered = LibraryApp(restored)
    assert recovered.assets.resolve(result['asset']).read_text(encoding='utf-8') == 'Untouched Україна Ω source.'
    assert not app.root.exists()
    assert json.loads((selected / 'manifest.json').read_bytes()) == value


def test_actual_cli_invalid_profile_cannot_create_data_or_backup(tmp_path):
    root, target = tmp_path / 'must-not-open', tmp_path / 'must-not-publish'
    completed = command(tmp_path, '--data-dir', root, '--backup', target,
                        '--backup-capacity', 'automatic')
    assert completed.returncode != 0
    assert 'invalid choice' in completed.stderr.decode('utf-8')
    assert not root.exists() and not target.exists()


def test_actual_cli_profile_without_backup_operation_is_refused(tmp_path):
    root = tmp_path / 'must-not-open'
    completed = command(tmp_path, '--data-dir', root,
                        '--backup-capacity', 'large-source-v1', '--list-libraries')
    assert completed.returncode != 0
    assert '--backup-capacity requires' in completed.stderr.decode('utf-8')
    assert not root.exists()
