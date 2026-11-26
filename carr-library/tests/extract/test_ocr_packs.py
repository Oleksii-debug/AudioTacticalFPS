"""Offline pack integrity, no-clobber publication and actual multilingual OCR."""
from dataclasses import replace
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from carr_library.extract import language_manager as packs, ocr_model_catalog as catalogue
from carr_library.extract import ocr_languages, ocr_runtime, runtime_identity
from carr_library.extract.page_ocr import Config, extract_page


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / 'prepared'
    root.mkdir()
    models = {'eng': b'Synthetic English model', 'ukr': b'Synthetic Ukrainian model',
              'script/Latin': b'Synthetic script model'}
    inventory = {}
    for code, content in models.items():
        path = root / (code + '.traineddata')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
        inventory[code] = dict(size=len(content), sha256=sha256(content).hexdigest())
    manifest = dict(schema=1, upstream='https://example.invalid/synthetic-model-fixture',
                    license='Synthetic test license.\nFull license retained.', models=inventory)
    raw = (json.dumps(manifest, ensure_ascii=False) + '\n').encode()
    (root / packs.MANIFEST).write_bytes(raw)
    (root / 'LICENSE.txt').write_text(manifest['license'])
    return root, sha256(raw).hexdigest(), manifest


def install(prepared, tmp_path):
    root, digest, _ = prepared
    result = packs.install_offline_pack(root, tmp_path / 'installed', expected_manifest_sha256=digest)
    return Path(result['path']), digest


def test_installer_preserves_exact_approved_bytes_and_license(prepared, tmp_path):
    root, digest, manifest = prepared
    output, _ = install(prepared, tmp_path)
    verified = packs.verify_offline_pack(output, expected_manifest_sha256=digest)
    assert verified.languages == ('eng', 'script/Latin', 'ukr')
    assert (output / 'models.json').read_bytes() == (root / 'models.json').read_bytes()
    assert (output / 'LICENSE.txt').read_bytes() == manifest['license'].encode()
    for code in manifest['models']:
        copied = output / (code + '.traineddata')
        assert copied.read_bytes() == (root / (code + '.traineddata')).read_bytes()
        assert copied.stat().st_nlink == 1
    assert not (output / packs.INCOMPLETE).exists()
    assert not list(tmp_path.glob('.ocr-install-*'))
    assert str(output) not in json.dumps(verified.fingerprint())


@pytest.mark.parametrize('problem', ['wrong-digest', 'changed-model', 'unlisted', 'symlink', 'hardlink'])
def test_invalid_prepared_pack_cannot_publish(prepared, tmp_path, problem):
    source, digest, _ = prepared
    if problem == 'wrong-digest':
        digest = '0' * 64
    elif problem == 'changed-model':
        (source / 'eng.traineddata').write_bytes(b'Changed model bytes')
    elif problem == 'unlisted':
        (source / 'unreviewed.traineddata').write_bytes(b'Unapproved model')
    elif problem == 'symlink':
        outside = tmp_path / 'outside'
        outside.write_bytes((source / 'eng.traineddata').read_bytes())
        (source / 'eng.traineddata').unlink()
        try:
            (source / 'eng.traineddata').symlink_to(outside)
        except OSError:
            pytest.skip('Creating symbolic links requires native permission')
    else:
        os.link(source / 'eng.traineddata', tmp_path / 'alias')
    with pytest.raises((OSError, ValueError)):
        packs.install_offline_pack(source, tmp_path / 'installed', expected_manifest_sha256=digest)
    assert not (tmp_path / 'installed').exists()


@pytest.mark.parametrize('problem', ['model', 'manifest', 'license', 'receipt', 'extra', 'missing', 'incomplete'])
def test_changed_installed_inventory_is_rejected(prepared, tmp_path, problem):
    root, digest = install(prepared, tmp_path)
    names = {'model': 'eng.traineddata', 'manifest': 'models.json',
             'license': 'LICENSE.txt', 'receipt': packs.READY,
             'extra': 'unexpected.txt', 'incomplete': packs.INCOMPLETE}
    if problem == 'missing':
        (root / 'ukr.traineddata').unlink()
    else:
        (root / names[problem]).write_bytes(b'Changed or incomplete')
    with pytest.raises((OSError, ValueError)):
        packs.verify_offline_pack(root, expected_manifest_sha256=digest)


def test_installer_keeps_existing_destination_and_winning_race(prepared, tmp_path, monkeypatch):
    source, digest, _ = prepared
    destination = tmp_path / 'keep'
    destination.mkdir()
    (destination / 'user.txt').write_text('User content')
    with pytest.raises(FileExistsError):
        packs.install_offline_pack(source, destination, expected_manifest_sha256=digest)
    assert (destination / 'user.txt').read_text() == 'User content'

    original = os.link
    raced = []
    def race(source, target):
        if not raced:
            Path(target).write_bytes(b'Concurrent user file')
            raced.append(Path(target))
        original(source, target)
    monkeypatch.setattr(packs.os, 'link', race)
    with pytest.raises(FileExistsError):
        packs.install_offline_pack(source, tmp_path / 'raced', expected_manifest_sha256=digest)
    assert raced[0].read_bytes() == b'Concurrent user file'
    assert not (tmp_path / 'raced' / packs.READY).exists()


def test_no_hardlink_filesystem_uses_exclusive_durable_copies(prepared, tmp_path, monkeypatch):
    def unsupported(*args):
        raise OSError(errno.EOPNOTSUPP, 'Synthetic filesystem without hard links')
    monkeypatch.setattr(packs.os, 'link', unsupported)
    root, digest = install(prepared, tmp_path)
    assert packs.verify_offline_pack(root, expected_manifest_sha256=digest).languages


@pytest.mark.parametrize('when', ['before', 'copy', 'publish'])
def test_cancellation_cannot_leave_a_ready_pack(prepared, tmp_path, monkeypatch, when):
    source, digest, _ = prepared
    stop = when == 'before'
    if when != 'before':
        name = '_copy_model' if when == 'copy' else '_publish'
        original = getattr(packs, name)
        def finish_then_cancel(*args):
            nonlocal stop
            original(*args)
            stop = True
        monkeypatch.setattr(packs, name, finish_then_cancel)
    with pytest.raises(InterruptedError):
        packs.install_offline_pack(source, tmp_path / 'installed', expected_manifest_sha256=digest,
                                   cancelled=lambda: stop)
    assert not (tmp_path / 'installed' / packs.READY).exists()
    assert not (tmp_path / 'installed').exists()
    assert (source / 'eng.traineddata').is_file()


def test_installer_refuses_sealed_portable_directories_and_small_budget(prepared, tmp_path):
    source, digest, _ = prepared
    with pytest.raises(ValueError, match='budget'):
        packs.install_offline_pack(source, tmp_path / 'small', expected_manifest_sha256=digest, max_bytes=1)
    for marker in ('ocr-manifest.json', 'bundle-manifest.json'):
        sealed = tmp_path / marker.replace('.', '-')
        sealed.mkdir()
        (sealed / marker).write_text('{}')
        with pytest.raises(ValueError, match='sealed'):
            packs.install_offline_pack(source, sealed / 'models', expected_manifest_sha256=digest)
        assert not (sealed / 'models').exists()


def test_pack_binding_preserves_the_full_base_runtime_inventory(prepared, tmp_path):
    pack, digest = install(prepared, tmp_path)
    root = tmp_path / 'portable'
    (root / 'tessdata').mkdir(parents=True)
    (root / 'tesseract.exe').write_bytes(b'Synthetic executable, never executed')
    (root / 'tessdata' / 'eng.traineddata').write_bytes(b'Synthetic bundled model')
    files = {path.relative_to(root).as_posix(): dict(size=path.stat().st_size,
              sha256=sha256(path.read_bytes()).hexdigest()) for path in root.rglob('*') if path.is_file()}
    (root / ocr_runtime.MANIFEST).write_text(json.dumps(dict(schema=1, target='win_amd64',
        executable='tesseract.exe', tessdata='tessdata', files=files)))
    base = ocr_runtime.verify_ocr_bundle(root)
    bound = ocr_runtime.bind_model_pack(base, str(pack), digest, languages='ukr')
    assert bound.command([base.executable, 'input.png', 'stdout'])[1:3] == ['--tessdata-dir', str(pack)]
    bound.assert_unchanged()
    with pytest.raises(ValueError, match='replace'):
        bound.command([base.executable, '--tessdata-dir', 'unapproved'])
    with pytest.raises(ValueError, match='replace'):
        bound.command([base.executable, '--tessdata-dir', str(pack), '--tessdata-dir', 'unapproved'])
    (root / 'unlisted.dll').write_bytes(b'Even an unused base runtime file is rejected')
    with pytest.raises(ValueError, match='unlisted'):
        bound.assert_unchanged()


def test_offline_cli_install_requires_approved_hash_and_never_overwrites(prepared, tmp_path, capsys):
    source, digest, _ = prepared
    destination = tmp_path / 'Встановлені моделі'
    arguments = ['install', '--source', str(source), '--destination', str(destination),
                 '--manifest-sha256', digest]
    assert catalogue.main(arguments) == 0
    raw = capsys.readouterr().out
    assert raw.isascii()
    result = json.loads(raw)
    assert result['path'] == str(destination) and result['manifest_sha256'] == digest
    before = (destination / 'eng.traineddata').read_bytes()
    assert catalogue.main(arguments) == 1
    assert 'new' in capsys.readouterr().err.lower()
    assert (destination / 'eng.traineddata').read_bytes() == before


@pytest.mark.parametrize('path,digest', [('relative', 'a' * 64), ('/absolute', None),
                                      (None, 'a' * 64), ('//remote/models', 'a' * 64)])
def test_serialized_config_cannot_select_an_unbound_pack(path, digest):
    with pytest.raises(ValueError):
        Config(model_pack_path=path, model_pack_sha256=digest)


def test_preset_v2_keeps_pack_binding_and_v1_presets(prepared, tmp_path):
    root, digest = install(prepared, tmp_path)
    library = tmp_path / 'library'
    library.mkdir()
    ocr_languages.store_preset(library, 'Old setting', Config())
    config = Config(languages='ukr+eng', model_pack_path=str(root), model_pack_sha256=digest)
    ocr_languages.store_preset(library, 'Verified pack', config)
    raw = (library / ocr_languages.FILENAME).read_bytes()
    assert json.loads(raw)['version'] == 2
    old, saved = ocr_languages.load_presets(library)
    assert 'model_pack_path' not in old
    assert Config(**{key: value for key, value in saved.items() if key != 'name'}) == config


@pytest.fixture(scope='module')
def real_pack(tmp_path_factory):
    runtime = ocr_runtime.discover_ocr_runtime()
    if runtime is None:
        pytest.skip('Actual local Tesseract is required for pack recognition')
    report = subprocess.check_output(runtime.command([runtime.executable, '--list-langs']), text=True)
    match = re.search(r'^List of available languages in "([^"\r\n]+)"', report, re.M)
    assert match, 'The actual engine must report its tessdata directory'
    source = Path(match[1])
    if any(not (source / (code + '.traineddata')).is_file() for code in ('eng', 'ukr', 'slk')):
        pytest.skip('Official English, Ukrainian and Slovak fixture models are required')
    root = tmp_path_factory.mktemp('real-ocr-pack')
    prepared = catalogue.prepare_offline_pack(source, root / 'prepared', ['eng', 'ukr', 'slk'])
    result = packs.install_offline_pack(prepared.path, root / 'installed',
                                       expected_manifest_sha256=prepared.manifest_sha256)
    return Path(result['path']), result['manifest_sha256']


@pytest.mark.parametrize('code,text', [('eng', 'Railway bridge'), ('ukr', 'Міст через річку'),
                                     ('slk', 'Železničný most')])
def test_actual_official_pack_recognizes_each_selected_language(real_pack, tmp_path, code, text):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas
    root, digest = real_pack
    image = Image.new('RGB', (1400, 320), 'white')
    font = None
    for name in ('DejaVuSans.ttf', 'arial.ttf', str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/arial.ttf')):
        try:
            font = ImageFont.truetype(name, 72)
            break
        except OSError:
            pass
    assert font is not None, 'A real Unicode test font is required'
    ImageDraw.Draw(image).text((30, 70), text, font=font, fill='black')
    source = tmp_path / 'synthetic-source.pdf'
    canvas = Canvas(str(source), pagesize=(612, 792))
    canvas.drawImage(ImageReader(image), 40, 500, 525, 120)
    canvas.save()
    config = Config(languages=code, model_pack_path=str(root), model_pack_sha256=digest)
    result = extract_page(source, 1, config)
    assert result['raw_text'].strip() == text
    assert result['status'] == 'review_required' and result['printed_label'] is None
    assert result['provenance']['ocr_runtime']['model_pack']['manifest_sha256'] == digest


def test_actual_pack_discovery_and_changed_cache_identity(real_pack, tmp_path):
    root, digest = real_pack
    models = ocr_languages.discover_languages(model_pack_path=str(root), model_pack_sha256=digest)
    assert models.codes == ('eng', 'slk', 'ukr')
    assert models.integrity == 'verified_model_pack' and models.manifest_sha256 == digest
    copied = tmp_path / 'copied-installed-pack'
    shutil.copytree(root, copied)
    config = Config(languages='eng+ukr+slk', model_pack_path=str(copied), model_pack_sha256=digest)
    identity = runtime_identity.runtime_identity(config)
    assert identity['ocr']['model_pack']['manifest_sha256'] == digest
    assert str(copied) not in json.dumps(identity)
    (copied / 'ukr.traineddata').write_bytes(b'Changed bytes cannot reuse an OCR checkpoint')
    with pytest.raises(runtime_identity.IdentityError):
        runtime_identity.runtime_identity(config)


def test_actual_pack_import_reopens_and_rejects_changed_model_before_checkpoint_reuse(real_pack, tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas
    from carr_library.application import LibraryApp
    from carr_library.ingest.worker import load_request
    root, digest = real_pack
    installed = tmp_path / 'private-models'
    shutil.copytree(root, installed)
    config = Config(model_pack_path=str(installed), model_pack_sha256=digest)
    image = Image.new('RGB', (1200, 300), 'white')
    ImageDraw.Draw(image).text((30, 70), 'Railway pack source',
                               font=ImageFont.load_default(size=72), fill='black')
    source = tmp_path / 'scanned.pdf'
    canvas = Canvas(str(source))
    canvas.drawImage(ImageReader(image), 50, 500, 500, 125)
    canvas.save()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Verified pack checkpoint')
    result = app.import_file(workspace, source, config=config)
    assert result['state'] == 'quarantined' and app.browse(workspace) == ()
    request_id = result['request_id']
    record = load_request(app.root, request_id)
    assert record['config']['model_pack_path'] == str(installed)
    assert record['config']['model_pack_sha256'] == digest
    pending = app.pending_review(workspace, request_id)
    assert pending['raw_text'].strip() == 'Railway pack source'
    app.confirm_import_review(workspace, request_id, 'Synthetic reviewer', confirmed=True,
                              expected_checksum=pending['checksum'])
    source.unlink()
    (installed / 'eng.traineddata').write_bytes(b'Changed after review')
    reopened = LibraryApp(app.root)
    rejected = reopened.resume_import(workspace, request_id)
    assert rejected['state'] == 'failed'
    assert reopened.browse(workspace) == ()
    shutil.copyfile(root / 'eng.traineddata', installed / 'eng.traineddata')
    restored = LibraryApp(app.root).resume_import(workspace, request_id)
    assert restored['state'] == 'complete' and restored['id'] == result['id']
    hit, = reopened.search(workspace, 'Railway')
    assert hit.verified and reopened.copy(workspace, hit.ref, hit.revision, 'quote') == 'Railway'
