"""Installed command dispatch reaches the same saved policy and job views."""
import json

from carr_library.__main__ import main
from carr_library.operations import ResourceBroker, ResourceLimits


def test_cli_resources_and_empty_jobs_are_local_and_durable(tmp_path, capsys):
    root = tmp_path / 'library'
    assert main(['operations', '--data-dir', str(root), 'list']) == 0
    assert json.loads(capsys.readouterr().out) == []
    policy = ResourceLimits(cpu_slots=2, memory_bytes=1024**3, disk_bytes=2*1024**3,
                            min_free_disk_bytes=0)
    path = tmp_path / 'resource-policy.json'
    path.write_text(json.dumps(policy.to_dict()))
    assert main(['operations','--data-dir',str(root),'configure-resources',str(path)]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated['leases'] == []
    assert ResourceBroker.for_app(root).limits == policy
    assert main(['operations','--data-dir',str(root),'resources']) == 0
    assert json.loads(capsys.readouterr().out)['leases'] == []


def test_cli_rejects_invalid_policy_preserving_last_good_limits(tmp_path, capsys):
    root = tmp_path / 'library'
    assert main(['operations', '--data-dir', str(root), 'resources']) == 0
    capsys.readouterr()
    before = ResourceBroker.for_app(root).limits
    path = tmp_path / 'invalid.json'
    path.write_text('{"cpu_slots": 1000, "arbitrary_command": "must not run"}')
    assert main(['operations','--data-dir',str(root),'configure-resources',str(path)]) == 1
    assert 'Unsupported resource limit fields' in capsys.readouterr().err
    assert ResourceBroker.for_app(root).limits == before


def test_cli_existing_assistant_job_list_get_pause_and_cancel_never_resubmit(tmp_path, capsys):
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService, Scope
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Explicit CLI scope')
    service = AssistantService(app)
    identity = service.submit(service.preview('List books', Scope(workspace)))
    prefix = ['operations', '--data-dir', str(app.root), '--workspace', workspace]
    assert main(prefix + ['list']) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(row['kind'], row['id'], row['status']) for row in rows] == [('assistant', identity, 'queued')]
    assert main(prefix + ['get', 'assistant', identity]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'queued'
    for action in ('pause', 'cancel'):
        assert main(prefix + [action, 'assistant', identity]) == 0
        value = json.loads(capsys.readouterr().out)
        assert value['requested'] == action and value['status'] == 'queued'
        assert service.store.control(identity) == action
    assert len(service.jobs()) == 1 and service.get(identity).status == 'queued'
    assert not tuple(app.root.rglob('job_result.json'))


def test_cli_fresh_scope_guard_preserves_archived_job_and_unsupported_actions(tmp_path, capsys):
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService, Scope
    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Archived control guard')
    service = AssistantService(app)
    identity = service.submit(service.preview('List books', Scope(workspace)))
    app.archive_workspace(workspace)
    assert main(['operations', '--data-dir', str(app.root), 'cancel', 'assistant', identity]) == 1
    assert 'archived' in capsys.readouterr().err.casefold()
    assert service.store.control(identity) == 'run'


def test_actual_isolated_cli_reuses_imported_package_and_utf8_resource_paths(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import carr_library
    import bootstrap as selected_bootstrap
    from carr_library.operations import ResourceRequest
    from carr_library.operations import cli

    root = tmp_path / 'бібліотека'
    root.mkdir()
    selected = root / 'тимчасові-дані'
    selected.mkdir()
    broker = ResourceBroker.for_app(root, limits=ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    bootstrap = (
        "import importlib.util,pathlib,runpy,sys; "
        "helper=pathlib.Path(sys.argv[3]).resolve(strict=True); "
        "helper_spec=importlib.util.spec_from_file_location('bootstrap',helper,submodule_search_locations=[str(helper.parent)]); "
        "helper_module=importlib.util.module_from_spec(helper_spec);sys.modules['bootstrap']=helper_module;helper_spec.loader.exec_module(helper_module); "
        "assert pathlib.Path(helper_module.__file__).resolve()==helper; "
        "origin=pathlib.Path(sys.argv[1]).resolve(strict=True); "
        "spec=importlib.util.spec_from_file_location('carr_library',origin,submodule_search_locations=[str(origin.parent)]); "
        "module=importlib.util.module_from_spec(spec);sys.modules['carr_library']=module;spec.loader.exec_module(module); "
        "from carr_library.operations import cli; "
        "assert pathlib.Path(cli.__file__).resolve()==pathlib.Path(sys.argv[2]).resolve(); "
        "sys.argv=['carr_library',*sys.argv[4:]];runpy.run_module('carr_library',run_name='__main__')"
    )
    with broker.reserve(ResourceRequest(cpu_slots=1, disk_bytes=4096, disk_path=selected),
                        operation_id='unicode-path-owner', timeout=0):
        result = subprocess.run([sys.executable, '-I', '-B', '-c', bootstrap,
            str(Path(carr_library.__file__).resolve()), str(Path(cli.__file__).resolve()),
            str(Path(selected_bootstrap.__file__).resolve()),
            'operations', '--data-dir', str(root), 'resources'], capture_output=True,
            env=dict(os.environ, PYTHONIOENCODING='ascii', PYTHONDONTWRITEBYTECODE='0'), timeout=30)
        assert result.returncode == 0, result.stderr.decode('utf-8', 'replace')
        value = json.loads(result.stdout.decode('utf-8', 'strict'))
        assert [row['operation_id'] for row in value['leases']] == ['unicode-path-owner']
        assert value['leases'][0]['request']['disk_path'] == str(selected)
    assert broker.snapshot()['leases'] == []
