"""Real local Git commits and wheel archives for source/artifact identity."""
import base64
import csv
from hashlib import sha256
import importlib.util
import io
import os
from pathlib import Path
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('source_wheel_probe', ROOT / 'packaging/source_wheel.py')
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def git(repo, *args):
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME='Synthetic Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                       GIT_COMMITTER_NAME='Synthetic Test', GIT_COMMITTER_EMAIL='test@example.invalid')
    return subprocess.check_output(['git', '-C', str(repo), *args], env=environment).decode().strip()


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / 'source repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    project = repo / 'carr-library'
    package = project / 'src/fixture_app'
    package.mkdir(parents=True)
    (project / 'pyproject.toml').write_text('''[project]
name = "fixture-application"
version = "1.0"
license-files = ["LICENSE"]
[project.scripts]
fixture-app = "fixture_app:main"
[tool.setuptools]
packages = ["fixture_app"]
package-dir = {"" = "src"}
[tool.setuptools.package-data]
"*" = ["*.sql", "*.md"]
''', encoding='utf-8')
    (project / 'LICENSE').write_text('Synthetic permissive license.\n')
    (package / '__init__.py').write_text('def main():\n    return "source version one"\n')
    (package / 'schema.sql').write_text('CREATE TABLE evidence (id INTEGER PRIMARY KEY);\n')
    (package / 'README.md').write_text('Synthetic source documentation.\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Synthetic source v1')
    first = git(repo, 'rev-parse', 'HEAD')
    payload = {f'fixture_app/{path.name}': path.read_bytes() for path in package.iterdir()}
    payload.update({
        'fixture_application-1.0.dist-info/METADATA': b'Metadata-Version: 2.4\nName: fixture-application\nVersion: 1.0\n',
        'fixture_application-1.0.dist-info/WHEEL': b'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
        'fixture_application-1.0.dist-info/entry_points.txt': b'[console_scripts]\nfixture-app = fixture_app:main\n',
        'fixture_application-1.0.dist-info/licenses/LICENSE': (project / 'LICENSE').read_bytes(),
    })
    (package / '__init__.py').write_text('def main():\n    return "source version two"\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Synthetic source v2, same release version')
    second = git(repo, 'rev-parse', 'HEAD')
    return repo, first, second, payload


def wheel(tmp_path, payload, *, record_mutation=None, extra=None):
    path = tmp_path / 'fixture_application-1.0-py3-none-any.whl'
    record = 'fixture_application-1.0.dist-info/RECORD'
    rows = [[name, 'sha256=' + base64.urlsafe_b64encode(sha256(data).digest()).decode().rstrip('='),
             str(len(data))] for name, data in payload.items()]
    rows.append([record, '', ''])
    if record_mutation:
        record_mutation(rows)
    output = io.StringIO(newline='')
    csv.writer(output, lineterminator='\n').writerows(rows)
    with ZipFile(path, 'w', ZIP_DEFLATED) as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
        archive.writestr(record, output.getvalue().encode())
        if extra:
            archive.writestr(*extra)
    return path


def test_exact_historical_commit_verified_independently_of_checkout_head(source, tmp_path):
    repo, first, second, payload = source
    candidate = wheel(tmp_path, payload)
    assert git(repo, 'rev-parse', 'HEAD') == second

    evidence = verifier.verify_application_wheel(candidate, repo, first)

    assert evidence['source_commit'] == first
    assert evidence['wheel_sha256'] == sha256(candidate.read_bytes()).hexdigest()
    assert evidence['required_runtime_files'] == 2
    assert evidence['payload_files'] == 3


def test_same_version_stale_wheel_cannot_be_labeled_as_new_commit(source, tmp_path):
    repo, first, second, payload = source
    candidate = wheel(tmp_path, payload)
    with pytest.raises(ValueError, match='differs from committed source'):
        verifier.verify_application_wheel(candidate, repo, second)
    payload['fixture_app/__init__.py'] = (repo / 'carr-library/src/fixture_app/__init__.py').read_bytes()
    assert verifier.verify_application_wheel(wheel(tmp_path, payload), repo, second)['source_commit'] == second


@pytest.mark.parametrize('missing', ['fixture_app/__init__.py', 'fixture_app/schema.sql'])
def test_required_runtime_file_cannot_be_omitted_with_updated_record(source, tmp_path, missing):
    repo, first, _, payload = source
    del payload[missing]
    with pytest.raises(ValueError, match='missing committed runtime files'):
        verifier.verify_application_wheel(wheel(tmp_path, payload), repo, first)


def test_modified_runtime_and_data_rejected_even_with_valid_record(source, tmp_path):
    repo, first, _, payload = source
    for name in ('fixture_app/__init__.py', 'fixture_app/README.md'):
        changed = dict(payload, **{name: b'not the committed bytes\n'})
        with pytest.raises(ValueError, match='differs from committed source'):
            verifier.verify_application_wheel(wheel(tmp_path, changed), repo, first)


@pytest.mark.parametrize(('missing', 'reason'), [
    ('fixture_app/README.md', 'missing committed package files'),
    ('fixture_application-1.0.dist-info/licenses/LICENSE', 'missing declared license files')])
def test_declared_documentation_and_license_cannot_be_omitted(source, tmp_path, missing, reason):
    repo, first, _, payload = source
    del payload[missing]
    with pytest.raises(ValueError, match=reason):
        verifier.verify_application_wheel(wheel(tmp_path, payload), repo, first)


@pytest.mark.parametrize('extra', ['fixture_app/extra.py', 'undeclared_package/__init__.py',
    'fixture_application-1.0.data/scripts/extra.py', 'other-1.0.dist-info/METADATA'])
def test_undeclared_payload_rejected_despite_complete_record(source, tmp_path, extra):
    repo, first, _, payload = source
    payload[extra] = b'not a declared source file\n'
    with pytest.raises(ValueError, match='Undeclared'):
        verifier.verify_application_wheel(wheel(tmp_path, payload), repo, first)


@pytest.mark.parametrize('extra', ['fixture_app/../escaped.py', 'fixture_app\\alias.py',
    'fixture_app/SCHEMA.sql', 'fixture_app/schema.sql.', 'fixture_app/__pycache__/cached.pyc'])
def test_path_alias_and_bytecode_rejected(source, tmp_path, extra):
    repo, first, _, payload = source
    payload[extra] = b'not safe payload\n'
    with pytest.raises(ValueError, match='path|bytecode'):
        verifier.verify_application_wheel(wheel(tmp_path, payload), repo, first)


def test_duplicate_zip_member_rejected(source, tmp_path):
    repo, first, _, payload = source
    with pytest.warns(UserWarning, match='Duplicate name'):
        candidate = wheel(tmp_path, payload, extra=('fixture_app/__init__.py', b'duplicate'))
    with pytest.raises(ValueError, match='Duplicate or aliased'):
        verifier.verify_application_wheel(candidate, repo, first)


def test_zip_symlink_rejected(source, tmp_path):
    repo, first, _, payload = source
    symlink = ZipInfo('fixture_app/link')
    symlink.create_system = 3
    symlink.external_attr = 0o120777 << 16
    candidate = wheel(tmp_path, payload, extra=(symlink, '../outside'))
    with pytest.raises(ValueError, match='regular'):
        verifier.verify_application_wheel(candidate, repo, first)


@pytest.mark.parametrize('mutation', [lambda rows: rows.pop(0),
    lambda rows: rows.append(rows[0]), lambda rows: rows[0].__setitem__(1, 'sha256=wrong')])
def test_record_must_be_complete_unique_and_correct(source, tmp_path, mutation):
    repo, first, _, payload = source
    with pytest.raises(ValueError, match='RECORD'):
        verifier.verify_application_wheel(wheel(tmp_path, payload, record_mutation=mutation), repo, first)


@pytest.mark.parametrize('identity', ['HEAD', '01234567', '0' * 40])
def test_commit_identity_must_resolve_exactly_and_locally(source, tmp_path, identity):
    repo, _, _, payload = source
    with pytest.raises(ValueError, match='commit'):
        verifier.verify_application_wheel(wheel(tmp_path, payload), repo, identity)


def test_metadata_identity_and_entry_points_are_bound_to_committed_project(source, tmp_path):
    repo, first, _, payload = source
    metadata = 'fixture_application-1.0.dist-info/METADATA'
    for content in (b'Name: wrong-project\nVersion: 1.0\n',
                    b'Name: fixture-application\nName: fixture-application\nVersion: 1.0\n',
                    b'Name: fixture-application\nVersion: 2.0\n'):
        changed = dict(payload, **{metadata: content})
        with pytest.raises(ValueError, match='METADATA'):
            verifier.verify_application_wheel(wheel(tmp_path, changed), repo, first)
    payload['fixture_application-1.0.dist-info/entry_points.txt'] = b'[console_scripts]\nfixture-app = wrong:main\n'
    with pytest.raises(ValueError, match='entry points'):
        verifier.verify_application_wheel(wheel(tmp_path, payload), repo, first)


def application_source(source, *, declare_audio):
    """Commit a new product subpackage, including source needed only at runtime."""
    repo, first, _, payload = source
    git(repo, 'checkout', '--detach', first)
    project = repo / 'carr-library'
    recipe = project / 'pyproject.toml'
    packages = ['fixture_app', 'carr_library']
    if declare_audio:
        packages.append('carr_library.audio')
    recipe.write_text(recipe.read_text().replace(
        'packages = ["fixture_app"]',
        'packages = [' + ', '.join('"' + name + '"' for name in packages) + ']'))
    additions = {
        'carr_library/__init__.py': b'"""Synthetic product package."""\n',
        'carr_library/audio/__init__.py': b'"""Synthetic audio package."""\n',
        'carr_library/audio/render.py': b'def render():\n    return "local PCM"\n',
        'carr_library/audio/queue.sql': b'CREATE TABLE render_jobs (id TEXT PRIMARY KEY);\n',
    }
    for name, contents in additions.items():
        path = project / 'src' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        if declare_audio or name == 'carr_library/__init__.py':
            payload[name] = contents
    # Compatibility probes are deliberately outside the application package.
    probe = project / 'bootstrap/compatibility/probe.py'
    probe.parent.mkdir(parents=True)
    probe.write_text('raise RuntimeError("build-time probe, not application code")\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Add a product audio package and a build-time probe')
    return repo, git(repo, 'rev-parse', 'HEAD'), payload


def test_committed_application_subpackage_cannot_be_hidden_by_package_recipe(source, tmp_path):
    repo, commit, payload = application_source(source, declare_audio=False)
    candidate = wheel(tmp_path, payload)
    with pytest.raises(ValueError, match='outside the declared package inventory') as error:
        verifier.verify_application_wheel(candidate, repo, commit)
    for name in ('__init__.py', 'render.py', 'queue.sql'):
        assert 'src/carr_library/audio/' + name in str(error.value)


def test_declared_application_subpackage_roundtrip_preserves_probe_exclusion(source, tmp_path):
    repo, commit, payload = application_source(source, declare_audio=True)
    candidate = wheel(tmp_path, payload)
    evidence = verifier.verify_application_wheel(candidate, repo, commit)
    assert evidence['source_commit'] == commit
    assert evidence['required_runtime_files'] == 6
    assert evidence['payload_files'] == 7
    assert 'bootstrap/compatibility/probe.py' not in payload
