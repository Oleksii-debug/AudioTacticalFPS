"""Real local Git commits and wheel archives for source/artifact identity."""
import base64
import csv
from hashlib import sha256
import importlib.util
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import tomllib
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
            # Keep deliberately unsafe raw names on Windows too; ZipInfo's
            # constructor otherwise changes backslashes before RECORD checks.
            info = ZipInfo('fixture-placeholder')
            info.filename = info.orig_filename = name
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)
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


def test_actual_sdist_keeps_ci_helpers_and_runtime_without_private_payloads(tmp_path):
    # Copy only tracked public sources. Never build alongside a user's models,
    # books, recordings or a previous egg-info manifest in their checkout.
    tracked = subprocess.check_output(
        ['git', 'ls-files', '-z', '--', '.'], cwd=ROOT, timeout=30,
    ).decode('utf-8').split('\0')[:-1]
    assert 0 < len(tracked) < 10000
    with TemporaryDirectory(prefix='carr-sdist-', dir=tmp_path) as directory:
        project = Path(directory) / 'source'
        project.mkdir()
        total = 0
        for name in tracked:
            relative = Path(name)
            assert not relative.is_absolute() and '..' not in relative.parts
            source_path = ROOT / relative
            info = source_path.lstat()
            assert stat.S_ISREG(info.st_mode), name
            total += info.st_size
            assert total <= 64 * 1024 * 1024
            destination = project / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, destination)

        configuration = tomllib.loads((project / 'pyproject.toml').read_text('utf-8'))
        admitted, _ = verifier._inventory(
            configuration, {f'carr-library/{name}': None for name in tracked},
        )
        required = {name.removeprefix('carr-library/') for name in admitted.values()}
        required.update(configuration['project']['license-files'])
        required.update({
            'scripts/ci_check.py',
            'scripts/ocr_catalog_smoke.py',
            'scripts/windows_academic_smoke.py',
            'scripts/windows_audio_smoke.py',
            'scripts/windows_installer_launchers_smoke.py',
            'scripts/windows_ocr_languages_smoke.py',
            'scripts/windows_v3_reading_smoke.py',
            'bootstrap/semantic_compat/test_probe.py',
            'bootstrap/semantic_compat/probe.py',
            'src/carr_library/extract/ocr-model-catalog.json',
        })
        required.update(name for name in tracked
                        if name.startswith('scripts/') and name.endswith('.py'))
        assert required <= set(tracked)

        # Synthetic untracked payloads exercise the real manifest exclusion;
        # they contain no user data and are removed with this temporary tree.
        excluded = {
            'private-books/synthetic-private.txt',
            'models/synthetic-model.safetensors',
            'audio-renders/synthetic-output.wav',
            'scripts/synthetic-recording.wav',
            'scripts/__pycache__/synthetic.pyc',
            'bootstrap/semantic_compat/synthetic-model.onnx',
            'src/carr_library/audio/synthetic-recording.wav',
        }
        for name in excluded:
            destination = project / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'Synthetic exclusion fixture.\n')
        environment = dict(os.environ, PIP_NO_INDEX='1', PYTHONNOUSERSITE='1')
        with (project / 'build.log').open('wb+') as log:
            result = subprocess.run(
                [sys.executable, '-I', '-c',
                 'from setuptools.build_meta import build_sdist; build_sdist("dist")'],
                cwd=project, env=environment, stdout=log, stderr=subprocess.STDOUT,
                timeout=90, check=False,
            )
            log.seek(max(0, log.tell() - 8000))
            assert result.returncode == 0, log.read().decode('utf-8', errors='replace')
        archives = list((project / 'dist').glob('*.tar.gz'))
        assert len(archives) == 1
        assert archives[0].stat().st_size <= 16 * 1024 * 1024
        with tarfile.open(archives[0], 'r:gz') as archive:
            members = archive.getmembers()
            assert len(members) < 10000
            assert sum(member.size for member in members) <= 64 * 1024 * 1024
            assert all(member.isdir() or member.isfile() for member in members)
            names = {member.name.partition('/')[2] for member in members if member.isfile()}
            assert required <= names, sorted(required - names)
            assert not excluded.intersection(names)
            for member in members:
                name = member.name.partition('/')[2]
                if name in required:
                    assert archive.extractfile(member).read() == (project / name).read_bytes(), name


        # A source archive intentionally contains development helpers. Building
        # the installed wheel from that same manifest must admit only declared
        # packages/data, even after egg-info has recorded the wider sdist tree.
        with (project / 'wheel-build.log').open('wb+') as log:
            result = subprocess.run(
                [sys.executable, '-I', '-c',
                 'from setuptools.build_meta import build_wheel; build_wheel("wheels")'],
                cwd=project, env=environment, stdout=log, stderr=subprocess.STDOUT,
                timeout=90, check=False,
            )
            log.seek(max(0, log.tell() - 8000))
            assert result.returncode == 0, log.read().decode('utf-8', errors='replace')
        wheels = list((project / 'wheels').glob('*.whl'))
        assert len(wheels) == 1
        with ZipFile(wheels[0]) as archive:
            runtime_names = {name for name in archive.namelist()
                             if '.dist-info/' not in name and not name.endswith('/')}
            assert runtime_names == set(admitted), sorted(runtime_names.symmetric_difference(admitted))
            for destination, source_name in admitted.items():
                assert archive.read(destination) == (project / source_name.removeprefix('carr-library/')).read_bytes()
            assert not excluded.intersection(archive.namelist())
