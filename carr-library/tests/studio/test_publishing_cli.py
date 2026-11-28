"""Actual CLI subprocess publication through the selected source/installed package."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from carr_library.durable_state import atomic_json

_spec = importlib.util.spec_from_file_location('publishing_cli_fixtures', Path(__file__).with_name('test_publishing.py'))
_fixtures = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_fixtures)
actual_job = _fixtures.actual_job
source_job = _fixtures.source_job


def cli(root, *arguments):
    import bootstrap
    import carr_library
    selected = dict.fromkeys(str(Path(module.__file__).resolve().parent.parent) for module in (carr_library, bootstrap))
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONPATH=os.pathsep.join(selected))
    return subprocess.run([sys.executable, '-B', '-m', 'carr_library', 'studio', '--data-dir', str(root),
        *map(str, arguments)], env=environment, cwd=root.parent, text=True, encoding='utf-8',
        capture_output=True, timeout=30)


def test_actual_cli_export_list_verify_and_no_clobber(source_job, tmp_path):
    service, state = source_job
    root = service.root.parent.parent
    target = tmp_path / 'cli.epub'
    exported = cli(root, 'publish-epub', state['id'], '--output', target, '--language', 'uk')
    assert exported.returncode == 0, exported.stderr
    record = json.loads(exported.stdout)
    assert record['status'] == 'complete' and target.exists()
    assert 'Prepared publication receipt:' in exported.stderr
    listed = cli(root, 'publications')
    assert listed.returncode == 0 and json.loads(listed.stdout)[0]['id'] == record['id']
    checked = cli(root, 'verify-publication', record['id'])
    assert checked.returncode == 0 and json.loads(checked.stdout)['artifact'] == record['artifact']
    original = target.read_bytes()
    repeated = cli(root, 'publish-epub', state['id'], '--output', target)
    assert repeated.returncode == 1 and not repeated.stdout and 'not be overwritten' in repeated.stderr
    assert target.read_bytes() == original


def test_actual_cli_rejects_unfinished_job_without_publishing(source_job, tmp_path):
    service, state = source_job
    state['status'] = 'paused'
    atomic_json(service.jobs_root / state['id'] / 'job.json', state, replace=True)
    target = tmp_path / 'incomplete.epub'
    result = cli(service.root.parent.parent, 'publish-epub', state['id'], '--output', target)
    assert result.returncode == 1 and 'completed Studio job' in result.stderr
    assert not target.exists()


def test_actual_cli_invalid_metadata_and_help_do_not_publish(source_job, tmp_path):
    service, state = source_job
    root = service.root.parent.parent
    target = tmp_path / 'invalid.epub'
    result = cli(root, 'publish-epub', state['id'], '--output', target, '--language', 'not a tag')
    assert result.returncode == 1 and not target.exists()
    help_result = cli(tmp_path / 'unused', '--help')
    assert help_result.returncode == 0 and 'publish-epub' in help_result.stdout
    assert not (tmp_path / 'unused').exists()


def test_actual_cli_verification_detects_later_edited_epub(source_job, tmp_path):
    service, state = source_job
    root = service.root.parent.parent
    target = tmp_path / 'edited.epub'
    exported = cli(root, 'publish-epub', state['id'], '--output', target)
    assert exported.returncode == 0, exported.stderr
    record = json.loads(exported.stdout)
    target.write_bytes(b'Edited after export')
    result = cli(root, 'verify-publication', record['id'])
    assert result.returncode == 1 and 'receipt' in result.stderr and not result.stdout
    assert target.read_bytes() == b'Edited after export'


def test_actual_cli_rejects_new_epub_inside_selected_library(source_job):
    service, state = source_job
    root = service.root.parent.parent
    target = root / 'private-library-output.epub'
    result = cli(root, 'publish-epub', state['id'], '--output', target)
    assert result.returncode == 1 and 'outside the private library' in result.stderr
    assert not result.stdout and not target.exists()
    assert service.get(state['id']) == state


def test_actual_cli_daisy_export_verify_and_required_publisher(source_job, tmp_path):
    from carr_library.studio.daisy import inspect_daisy
    service, state = source_job
    root = service.root.parent.parent
    target = tmp_path / 'cli-daisy.zip'
    missing = cli(root, 'publish-daisy', state['id'], '--output', target)
    assert missing.returncode == 2 and '--publisher' in missing.stderr and not target.exists()
    exported = cli(root, 'publish-daisy', state['id'], '--output', target,
                   '--language', 'uk', '--publisher', 'Explicit synthetic publisher')
    assert exported.returncode == 0, exported.stderr
    record = json.loads(exported.stdout)
    assert record['format'] == 'daisy-2.02' and record['status'] == 'complete'
    assert inspect_daisy(target)['publisher'] == 'Explicit synthetic publisher'
    checked = cli(root, 'verify-publication', record['id'])
    assert checked.returncode == 0 and json.loads(checked.stdout)['artifact'] == record['artifact']
    before = target.read_bytes()
    repeated = cli(root, 'publish-daisy', state['id'], '--output', target, '--publisher', 'Explicit synthetic publisher')
    assert repeated.returncode == 1 and target.read_bytes() == before


def test_actual_cli_rejects_daisy_inside_selected_private_library(source_job):
    service, state = source_job
    root = service.root.parent.parent
    target = root / 'private-daisy.zip'
    result = cli(root, 'publish-daisy', state['id'], '--output', target,
                 '--publisher', 'Explicit synthetic publisher')
    assert result.returncode == 1 and 'outside the private library' in result.stderr
    assert not result.stdout and not target.exists()
    assert service.get(state['id']) == state
