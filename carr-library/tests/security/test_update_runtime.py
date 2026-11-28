"""Real source-copy/seal/configuration boundaries for explicit host preparation.

The existing offline installer is an explicitly substituted fixture in portable
cases. Those cases never claim Windows setup, package installation or rollout.
"""
import base64
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.security import update_runtime as runtime
from carr_library.security.update_handoff import OwnerUpdateConfiguration
from carr_library.security._files import TrustError, json_bytes

from .test_managed_installation import _payload, _bootstrap, maintenance


@pytest.fixture
def recipe(tmp_path, repository):
    source = tmp_path / 'pristine-A'
    _payload(source, '3.1.0', '1' * 40)
    for name in ('runtime-sbom.cdx.json', 'trusted-client-dependencies.json'):
        (source / name).write_bytes(b'{"synthetic_fixture":true}')
    manifest = json.loads((source / 'bundle-manifest.json').read_bytes())
    manifest['bundled_trusted_client'] = True
    for name in ('runtime-sbom.cdx.json', 'trusted-client-dependencies.json'):
        manifest['files'][name] = sha256((source / name).read_bytes()).hexdigest()
    raw = json_bytes(manifest)
    (source / 'bundle-manifest.json').write_bytes(raw)
    bundle_sha = sha256(raw).hexdigest()
    setup = tmp_path / 'setup.exe'
    setup.write_bytes(b'Explicit inert original setup marker')
    root = tmp_path / 'current-A'
    maintenance.install(source, root, bundle_sha, sha256(setup.read_bytes()).hexdigest(),
                        runner=_bootstrap, setup_source=setup)
    library = LibraryApp(tmp_path / 'original-library')
    workspace = library.create_workspace('Original user research')
    library.add_text(workspace, 'Original owned source', 'This source must remain exactly unchanged.')
    work = tmp_path / 'recovery'
    work.mkdir()
    marker = maintenance.marker(root)
    policy = repository.policy()
    publisher = asdict(policy)
    publisher['bootstrap'] = base64.b64encode(policy.bootstrap).decode('ascii')
    value = dict(schema=1, kind='carr-managed-update-provision',
        runtime=dict(root=str(tmp_path / 'external-host'), manifest=str(tmp_path / 'host-seal.json')),
        installation=dict(root=str(root), work=str(work)), library=str(library.root), publisher=publisher,
        repository=dict(endpoint=None, target=repository.target_path, timeout=60, tls_ca_pem=None),
        anchor=dict(reference='carr/provider/update-test/' + '1' * 32,
                    binding='2' * 64, profile_id='update-test'),
        source={key: marker[key] for key in ('bundle_sha256', 'setup_sha256', 'owner_sha256')},
        configuration=str(tmp_path / 'prepared-owner.json'), close_timeout=3, operation_timeout=60)
    path = tmp_path / 'explicit-recipe.json'
    encoded = json_bytes(value)
    path.write_bytes(encoded)
    item = runtime.UpdateHostProvisioning(path, sha256(encoded).hexdigest())
    item.fixture_library, item.fixture_workspace = library, workspace
    return item


def _rewrite(recipe, change):
    value = json.loads(recipe.raw)
    change(value)
    raw = json_bytes(value)
    recipe.path.write_bytes(raw)
    return runtime.UpdateHostProvisioning(recipe.path, sha256(raw).hexdigest())


def _portable_bootstrap(monkeypatch, recipe, *, failure=None):
    # Select the Windows-only outer admission explicitly, leaving all actual
    # filesystem/native-lease implementations on this real test platform.
    monkeypatch.setattr(runtime, 'os', SimpleNamespace(**{**vars(os), 'name': 'nt'}))
    import carr_library.audio.process as owner
    calls = []
    def process(command, **options):
        calls.append((tuple(command), options))
        assert options['owned_windows_job'] is True
        assert not options['cancelled']()
        assert options['env']['PIP_NO_INDEX'] == '1'
        app = recipe.destination / 'app'
        if len(calls) == 1:
            assert command == [str(app / 'runtime/python.exe'), '-I', '-B', '-X', 'utf8',
                               str(app / 'install_windows.py')]
            assert (app / 'install_windows.py').read_bytes() == (recipe.root / 'app/install_windows.py').read_bytes()
            assert not (app / '.venv').exists()
            if failure is not None:
                raise failure
            _bootstrap(app)
        else:
            assert command[:4] == [str(app / '.venv/Scripts/python.exe'), '-I', '-B', '-c']
            assert command[4] == 'import carr_library.security.update_host; import tuf.ngclient; import cryptography'
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(owner, 'run_process', process)
    return calls


def test_preparation_copies_only_exact_distribution_then_seals_new_offline_host(recipe, monkeypatch):
    secret = recipe.root / 'app/.venv/private-runtime-secret.txt'
    secret.write_bytes(b'Private active-environment material must never be copied')
    untouched = recipe.fixture_library.root / 'user-private.txt'
    untouched.write_bytes(b'Owned original library bytes')
    before = {path.relative_to(recipe.root).as_posix(): path.read_bytes()
              for path in recipe.root.rglob('*') if path.is_file()}
    calls = _portable_bootstrap(monkeypatch, recipe)
    result = runtime.prepare_update_host(recipe)
    assert len(calls) == 2 and result['status'] == 'PREPARED'
    assert result['installed_application_changed'] is False and result['vendor_rollout'] == 'NOT_RUN'
    configuration = OwnerUpdateConfiguration(result['configuration'], result['configuration_sha256'])
    assert configuration.verify_runtime()['files'] > 8
    assert configuration.python == recipe.destination / 'app/.venv/Scripts/python.exe'
    assert set(path.name for path in (recipe.destination / 'maintenance').iterdir()) == runtime._HELPERS
    assert not (recipe.destination / 'app/.venv/private-runtime-secret.txt').exists()
    assert not any(path.name in {'user-private.txt', 'library.sqlite'} for path in recipe.destination.rglob('*'))
    assert untouched.read_bytes() == b'Owned original library bytes'
    assert before == {path.relative_to(recipe.root).as_posix(): path.read_bytes()
                      for path in recipe.root.rglob('*') if path.is_file()}
    assert recipe.fixture_library.search(recipe.fixture_workspace, 'unchanged')[0].quote == 'unchanged'


@pytest.mark.parametrize('field', ['bundle_sha256', 'owner_sha256', 'setup_sha256'])
def test_wrong_independent_a_identity_is_refused_before_copy_or_child(recipe, monkeypatch, field):
    altered = _rewrite(recipe, lambda value: value['source'].__setitem__(field, 'f' * 64))
    calls = _portable_bootstrap(monkeypatch, altered)
    with pytest.raises(TrustError, match='selected A|pinned A'):
        runtime.prepare_update_host(altered)
    assert calls == [] and not altered.destination.exists()
    assert not altered.configuration.exists() and not altered.manifest.exists()


@pytest.mark.parametrize('name', ['install.lock', 'runtime/python.exe', 'wheelhouse'])
def test_changed_exact_distribution_source_never_starts_offline_installer(recipe, monkeypatch, name):
    path = recipe.root / 'app' / name
    if path.is_dir():
        path = next(path.iterdir())
    path.write_bytes(b'Changed reviewed distribution')
    calls = _portable_bootstrap(monkeypatch, recipe)
    with pytest.raises(TrustError, match='distribution file changed'):
        runtime.prepare_update_host(recipe)
    assert calls == [] and not recipe.destination.exists()


@pytest.mark.parametrize('name', ['maintenance.py', 'process.py', 'installation_lease.py'])
def test_changed_fixed_helper_is_refused_before_import_or_copy(recipe, monkeypatch, name):
    (recipe.root / 'maintenance' / name).write_bytes(b'raise AssertionError("must never execute")')
    calls = _portable_bootstrap(monkeypatch, recipe)
    with pytest.raises(TrustError, match='maintenance helper changed'):
        runtime.prepare_update_host(recipe)
    assert calls == [] and not recipe.destination.exists()


@pytest.mark.parametrize('kind', ['program-child', 'library-child', 'work-child', 'existing'])
def test_explicit_external_destination_cannot_overlap_or_replace_owned_material(recipe, kind):
    value = json.loads(recipe.raw)
    paths = {'program-child': recipe.root / 'new-host',
             'library-child': recipe.library / 'new-host',
             'work-child': recipe.work / 'new-host', 'existing': recipe.root}
    value['runtime']['root'] = str(paths[kind])
    raw = json_bytes(value)
    recipe.path.write_bytes(raw)
    with pytest.raises(TrustError, match='separate|new host'):
        runtime.UpdateHostProvisioning(recipe.path, sha256(raw).hexdigest())
    assert (recipe.root / 'app/version.txt').read_bytes() == b'3.1.0'
    assert not (recipe.library / 'new-host').exists() and not (recipe.work / 'new-host').exists()


def test_cancellation_before_preparation_creates_no_runtime_or_receipts(recipe, monkeypatch):
    calls = _portable_bootstrap(monkeypatch, recipe)
    with pytest.raises(InterruptedError):
        runtime.prepare_update_host(recipe, cancelled=lambda: True)
    assert calls == [] and not recipe.destination.exists()
    assert not recipe.manifest.exists() and not recipe.configuration.exists()


def test_offline_installer_failure_retains_owned_attempt_and_leaves_a_untouched(recipe, monkeypatch):
    failure = RuntimeError('Explicit synthetic offline installer failure')
    calls = _portable_bootstrap(monkeypatch, recipe, failure=failure)
    before = (recipe.root / 'managed-install.ini').read_bytes()
    with pytest.raises(RuntimeError) as caught:
        runtime.prepare_update_host(recipe)
    assert caught.value is failure and len(calls) == 1
    assert (recipe.destination / 'app/install_windows.py').is_file()
    assert not recipe.manifest.exists() and not recipe.configuration.exists()
    assert (recipe.root / 'managed-install.ini').read_bytes() == before
    assert (recipe.root / 'app/version.txt').read_bytes() == b'3.1.0'


def test_real_competing_publication_is_never_overwritten(recipe, monkeypatch):
    original = runtime.os.link
    expected = b'Independently created destination must survive'
    def compete(source, target, *args, **kwargs):
        if Path(target) == recipe.manifest:
            Path(target).write_bytes(expected)
        return original(source, target, *args, **kwargs)
    _portable_bootstrap(monkeypatch, recipe)
    # Only the local facade is replaced; real os.link still enforces no-replace.
    monkeypatch.setattr(runtime.os, 'link', compete)
    with pytest.raises(FileExistsError):
        runtime.prepare_update_host(recipe)
    assert recipe.manifest.read_bytes() == expected and not recipe.configuration.exists()
    assert (recipe.root / 'app/version.txt').read_bytes() == b'3.1.0'


def test_visible_final_configuration_retains_truth_if_acknowledgment_fails(recipe, monkeypatch):
    _portable_bootstrap(monkeypatch, recipe)
    original = runtime._publish_new
    def publish(path, raw):
        original(path, raw)
        if path == recipe.configuration:
            raise OSError('Synthetic late acknowledgment failure')
    monkeypatch.setattr(runtime, '_publish_new', publish)
    with pytest.raises(OSError) as caught:
        runtime.prepare_update_host(recipe)
    result = caught.value.update_host_prepared
    assert result['status'] == 'PREPARED' and result['vendor_rollout'] == 'NOT_RUN'
    assert OwnerUpdateConfiguration(recipe.configuration, result['configuration_sha256']).verify_runtime()
    assert (recipe.root / 'app/version.txt').read_bytes() == b'3.1.0'


def _recipe_with_runtime_files(tmp_path, repository, monkeypatch, files):
    # Build the real original installation and ownership receipt from this
    # synthetic distribution; do not bypass the admission or copy functions.
    import sys
    original = _payload
    def payload(root, *args, **kwargs):
        original(root, *args, **kwargs)
        manifest = json.loads((root / 'bundle-manifest.json').read_bytes())
        for name, raw in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            manifest['files'][name] = sha256(raw).hexdigest()
        (root / 'bundle-manifest.json').write_bytes(json_bytes(manifest))
    with monkeypatch.context() as patch:
        patch.setattr(sys.modules[__name__], '_payload', payload)
        return recipe.__wrapped__(tmp_path, repository)


def _repin_source_recipe(item, *, manifest=None, owner=None):
    # Deliberately resign only the fixture's independent metadata so malformed
    # declared ownership reaches the real per-file admission boundary.
    source = dict(item.source)
    if manifest is not None:
        raw = json_bytes(manifest)
        (item.root / 'app/bundle-manifest.json').write_bytes(raw)
        source['bundle_sha256'] = sha256(raw).hexdigest()
    if owner is None:
        owner = json.loads((item.root / 'owned-files.json').read_bytes())
    owner['bundle_sha256'] = source['bundle_sha256']
    raw = json_bytes(owner)
    (item.root / 'owned-files.json').write_bytes(raw)
    source['owner_sha256'] = sha256(raw).hexdigest()
    maintenance.set_marker(item.root, 'complete', source['bundle_sha256'],
                           source['setup_sha256'], source['owner_sha256'])
    return _rewrite(item, lambda value: value.update(source=source))


@pytest.mark.parametrize('form', ['source', 'bytecode'])
def test_exact_observed_pip_modules_copy_only_as_owned_pinned_distribution_code(
        tmp_path, repository, monkeypatch, form):
    # Independent names from the retained original install-A.log: all thirteen
    # source modules and their exact CPython311 bytecode counterparts.
    modules = ('__init__', 'candidate', 'direct_url', 'format_control', 'index',
               'installation_report', 'link', 'release_control', 'scheme',
               'search_scope', 'selection_prefs', 'target_python', 'wheel')
    prefix = 'runtime/Lib/site-packages/pip/_internal/models/'
    names = [prefix + (name + '.py' if form == 'source'
                       else '__pycache__/' + name + '.cpython-311.pyc')
             for name in modules]
    files = {name: ('Explicit inert pinned runtime-code fixture: ' + name).encode('ascii')
             for name in names}
    item = _recipe_with_runtime_files(tmp_path, repository, monkeypatch, files)
    before_owner = (item.root / 'owned-files.json').read_bytes()
    before_manifest = (item.root / 'app/bundle-manifest.json').read_bytes()
    calls = _portable_bootstrap(monkeypatch, item)
    result = runtime.prepare_update_host(item)
    assert result['status'] == 'PREPARED' and len(calls) == 2
    assert len(files) == 13
    for name, raw in files.items():
        assert (item.root / 'app' / name).read_bytes() == raw
        assert (item.destination / 'app' / name).read_bytes() == raw
    assert (item.root / 'owned-files.json').read_bytes() == before_owner
    assert (item.root / 'app/bundle-manifest.json').read_bytes() == before_manifest
    configured = runtime.OwnerUpdateConfiguration(
        item.configuration, result['configuration_sha256'])
    assert configured.verify_runtime()['sha256'] == result['manifest_sha256']
    assert item.fixture_library.search(item.fixture_workspace, 'unchanged')[0].quote == 'unchanged'


@pytest.mark.parametrize('component', [
    '.venv', 'data', 'state', 'models', 'voices', 'references', 'voice-packages',
    'dordiai-voices', 'node_modules', '.git', '.env',
])
def test_pip_code_exception_preserves_every_private_component_refusal(
        tmp_path, repository, monkeypatch, component):
    canonical = 'runtime/Lib/site-packages/pip/_internal/models/wheel.py'
    private = 'extra/' + component + '/private.bin'
    files = {canonical: b'Explicit inert runtime code', private: b'Private fixture must not be copied'}
    item = _recipe_with_runtime_files(tmp_path, repository, monkeypatch, files)
    calls = _portable_bootstrap(monkeypatch, item)
    with pytest.raises(TrustError, match='private, ambiguous or unowned'):
        runtime.prepare_update_host(item)
    assert calls == []
    assert not item.destination.exists() and not item.manifest.exists() and not item.configuration.exists()
    for name, raw in files.items():
        assert (item.root / 'app' / name).read_bytes() == raw


@pytest.mark.parametrize('unreviewed', [
    'runtime/Lib/site-packages/pip/_internal/models/extra.py',
    'runtime/Lib/site-packages/other/models/wheel.py',
    'runtime/Lib/site-packages/pip/_internal/models/__pycache__/wheel.cpython-312.pyc',
    'runtime/Lib/site-packages/pip/_internal/models/__pycache__/wheel.cpython-311.opt-1.pyc',
])
def test_pip_code_exception_is_closed_to_exact_observed_relative_files(
        tmp_path, repository, monkeypatch, unreviewed):
    files = {'runtime/Lib/site-packages/pip/_internal/models/wheel.py': b'Known inert module',
             unreviewed: b'Unreviewed sibling or misplaced code'}
    item = _recipe_with_runtime_files(tmp_path, repository, monkeypatch, files)
    calls = _portable_bootstrap(monkeypatch, item)
    with pytest.raises(TrustError, match='private, ambiguous or unowned'):
        runtime.prepare_update_host(item)
    assert calls == [] and not item.destination.exists()
    assert not item.manifest.exists() and not item.configuration.exists()
    assert (item.root / 'app' / unreviewed).read_bytes() == files[unreviewed]


@pytest.mark.parametrize('fault', [
    'owner-entry', 'source-bytes', 'hardlink', 'case-duplicate', 'traversal',
])
def test_pip_code_exception_cannot_bypass_owner_hash_or_path_identity(
        tmp_path, repository, monkeypatch, fault):
    name = 'runtime/Lib/site-packages/pip/_internal/models/wheel.py'
    item = _recipe_with_runtime_files(tmp_path, repository, monkeypatch,
                                     {name: b'Known exact inert runtime module'})
    target = item.root / 'app' / name
    if fault == 'owner-entry':
        owner = json.loads((item.root / 'owned-files.json').read_bytes())
        owner['files'][name] = '0' * 64
        item = _repin_source_recipe(item, owner=owner)
    elif fault == 'source-bytes':
        target.write_bytes(b'Changed after the independent A hashes were pinned')
    elif fault == 'hardlink':
        os.link(target, tmp_path / 'retained-alias')
    else:
        manifest = json.loads((item.root / 'app/bundle-manifest.json').read_bytes())
        owner = json.loads((item.root / 'owned-files.json').read_bytes())
        additional = (name[:-8] + 'WHEEL.py' if fault == 'case-duplicate'
                      else name[:-8] + '../models/wheel.py')
        manifest['files'][additional] = manifest['files'][name]
        owner['files'][additional] = owner['files'][name]
        item = _repin_source_recipe(item, manifest=manifest, owner=owner)
    original = target.read_bytes()
    original_owner = (item.root / 'owned-files.json').read_bytes()
    original_manifest = (item.root / 'app/bundle-manifest.json').read_bytes()
    calls = _portable_bootstrap(monkeypatch, item)
    with pytest.raises((TrustError, ValueError)):
        runtime.prepare_update_host(item)
    assert calls == []
    assert not item.destination.exists() and not item.manifest.exists() and not item.configuration.exists()
    assert target.read_bytes() == original
    assert (item.root / 'owned-files.json').read_bytes() == original_owner
    assert (item.root / 'app/bundle-manifest.json').read_bytes() == original_manifest
    if fault == 'hardlink':
        assert (tmp_path / 'retained-alias').read_bytes() == original
