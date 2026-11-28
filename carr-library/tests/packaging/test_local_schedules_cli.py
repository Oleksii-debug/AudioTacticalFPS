"""Actual CLI children with explicit source/installed origin and UTF-8 evidence."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library import incremental_backup, local_schedules_cli
from carr_library.local_schedules import ScheduleStore


def invoke(root, *arguments):
    script = r'''
import sys
from pathlib import Path
from carr_library import local_schedules_cli
assert Path(local_schedules_cli.__file__).resolve() == Path(sys.argv[1]).resolve()
from carr_library.__main__ import main
raise SystemExit(main(['local-schedules', '--data-dir', sys.argv[2], *sys.argv[3:]]))
'''
    return subprocess.run([sys.executable, '-B', '-c', script,
        str(Path(local_schedules_cli.__file__).resolve()), str(root), *map(str, arguments)],
        capture_output=True, timeout=90,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'ascii',
             'PYTHONUTF8': '0'})


def value(result):
    assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
    return json.loads(result.stdout.decode('utf-8'))


@pytest.fixture
def world(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Локальна бібліотека')
    app.add_text(workspace, 'Existing', 'An exact retained original.')
    watch = tmp_path / 'джерела'
    watch.mkdir()
    repository = tmp_path / 'repository'
    incremental_backup.initialize_repository(repository)
    return app, workspace, watch, repository


def create_current(world, kind='watch'):
    app, workspace, watch, repository = world
    now = datetime.now(timezone.utc)
    store = ScheduleStore(app, clock=lambda: now)
    rule = store.create(kind, 'Поточне явне правило', repository if kind == 'backup' else watch,
        first_due=now, interval_seconds=60, lateness_seconds=300,
        **({'workspace': workspace} if kind == 'watch' else {}))
    return store, rule


def test_actual_cli_save_and_list_are_utf8_and_never_arm(world):
    app, workspace, watch, _ = world
    (watch / 'не змінювати.txt').write_text('Unchanged external original Ω.', encoding='utf-8')
    raw = (watch / 'не змінювати.txt').read_bytes()
    saved = value(invoke(app.root, 'create-watch', '--title', 'Архів Ω', '--target', watch,
        '--workspace', workspace, '--at', (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
        '--interval-seconds', 120))
    assert saved['armed'] is False and saved['saved']['record']['title'] == 'Архів Ω'
    listed = value(invoke(app.root, 'list'))
    assert listed['armed'] is False and len(listed['rules']) == 1
    assert listed['rules'][0]['sha256'] == saved['saved']['sha256']
    assert len(app.browse(workspace)) == 1 and not (app.root / 'import-batches').exists()
    assert (watch / 'не змінювати.txt').read_bytes() == raw


def test_actual_cli_finite_watch_run_uses_verified_existing_batch(world):
    app, workspace, watch, _ = world
    path = watch / 'new.txt'
    path.write_text('An actual CLI watched-folder source.', encoding='utf-8')
    store, rule = create_current(world)
    result = value(invoke(app.root, 'run', '--rule', rule.id, '--sha256', rule.sha256, '--run'))
    assert result['status'] == 'complete' and result['armed'] is False and len(result['outcomes']) == 1
    outcome = result['outcomes'][0]
    batch = app.import_batch(workspace, outcome['result']['batch_id'])
    assert batch['counts']['complete'] == 1 and batch['rows'][0]['result']['verified']
    assert len(app.browse(workspace)) == 2 and store.get(rule.id).record['last']['status'] == 'complete'


def test_actual_cli_snapshot_restores_through_canonical_validator(world, tmp_path):
    app, workspace, _, repository = world
    _, rule = create_current(world, 'backup')
    result = value(invoke(app.root, 'run', '--rule', rule.id, '--sha256', rule.sha256, '--run'))
    receipt = result['outcomes'][0]['result']
    assert result['armed'] is False and receipt['published'] is True
    assert incremental_backup.verify_snapshot(repository, receipt['snapshot_sha256'])['payloads_verified']
    destination = tmp_path / 'restored'
    incremental_backup.restore_snapshot(repository, receipt['snapshot_sha256'], destination)
    recovered = LibraryApp(destination)
    assert len(recovered.browse(workspace)) == 1


def test_actual_cli_requires_separate_run_approval(world):
    app, workspace, watch, _ = world
    (watch / 'new.txt').write_text('No command consent yet.', encoding='utf-8')
    store, rule = create_current(world)
    result = invoke(app.root, 'run', '--rule', rule.id, '--sha256', rule.sha256)
    assert result.returncode == 2 and b'--run' in result.stderr
    assert store.get(rule.id).record['last'] is None and len(app.browse(workspace)) == 1


@pytest.mark.parametrize('options', [
    ('--wait-seconds', 'nan'), ('--wait-seconds', '86401'),
    ('--max-occurrences', '0'), ('--max-occurrences', '33'),
])
def test_actual_cli_rejects_unbounded_or_invalid_foreground_lifetimes(world, options):
    app, _, _, _ = world
    store, rule = create_current(world)
    result = invoke(app.root, 'run', '--rule', rule.id, '--sha256', rule.sha256, '--run', *options)
    assert result.returncode == 2 and store.get(rule.id).record['last'] is None


def test_actual_cli_does_not_initialize_a_missing_library(tmp_path):
    target = tmp_path / 'not-a-library'
    result = invoke(target, 'list')
    assert result.returncode == 1 and not target.exists()


def test_actual_cli_rejects_stale_rule_hash_and_preserves_disabled_declaration(world):
    app, workspace, _, _ = world
    store, rule = create_current(world)
    disabled = store.disable(rule.id, expected=rule.sha256)
    result = invoke(app.root, 'run', '--rule', rule.id, '--sha256', rule.sha256, '--run')
    assert result.returncode == 1
    assert store.get(rule.id).sha256 == disabled.sha256
    assert len(app.browse(workspace)) == 1 and not (app.root / 'import-batches').exists()


def test_actual_cli_zero_wait_disarms_a_future_rule_without_execution(world):
    app, _, watch, _ = world
    created = value(invoke(app.root, 'create-watch', '--title', 'Future only', '--target', watch,
        '--workspace', world[1], '--at', (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()))
    rule = created['saved']
    result = invoke(app.root, 'run', '--rule', rule['record']['id'], '--sha256', rule['sha256'], '--run')
    assert result.returncode == 3
    report = json.loads(result.stdout.decode('utf-8'))
    assert report == {'armed': False, 'status': 'not-due', 'outcomes': []}
    assert ScheduleStore(app).get(rule['record']['id']).record['last'] is None
