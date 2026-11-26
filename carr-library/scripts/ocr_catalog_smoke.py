"""Exercise the installed OCR catalogue and, optionally, real bundled weights.

Run with the installed interpreter's -I flag. No source-tree import, download,
network access, recognition-quality claim or modification of the sealed bundle.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from carr_library.extract import ocr_model_catalog as catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-directory', type=Path)
    args = parser.parse_args()
    package_path = Path(catalog.__file__).resolve()
    assert 'site-packages' in package_path.parts, 'The smoke must import the installed wheel.'
    counts = {}
    for profile in ('fast', 'best'):
        source = catalog.load_source(profile)
        assert len(source.models) == 162
        assert {model.code for model in source.models if model.kind == 'special'} == {'equ', 'osd'}
        assert len(catalog.list_models(profile, kind='script')) == 37
        assert [model.code for model in catalog.list_models(profile, query='Українська')] == ['ukr']
        result = subprocess.run([sys.executable, '-I', '-B', '-m',
            'carr_library.extract.ocr_model_catalog', 'list', '--profile', profile, '--query', 'ukr'],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        assert len(result.stdout) < 8192 and not result.stderr
        rows = json.loads(result.stdout)
        assert len(rows) == 1 and rows[0]['code'] == 'ukr' and rows[0]['profile'] == profile
        counts[profile] = len(source.models)
    prepared = False
    if args.model_directory is not None:
        incoming = args.model_directory / 'eng.traineddata'
        expected = '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'
        assert sha256(incoming.read_bytes()).hexdigest() == expected
        with tempfile.TemporaryDirectory(prefix='carr-installed-ocr-') as temporary:
            destination = Path(temporary) / 'Підготовлені моделі'
            result = subprocess.run([sys.executable, '-I', '-B', '-m',
                'carr_library.extract.ocr_model_catalog', 'prepare', '--profile', 'fast',
                '--source', str(args.model_directory), '--destination', str(destination),
                '--models', 'eng'], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            assert len(result.stdout) < 8192 and not result.stderr
            receipt = json.loads(result.stdout)
            assert Path(receipt['path']).samefile(destination)
            assert receipt['models'] == ['eng'] and receipt['size_bytes'] == 4113088
            manifest_raw = (destination / 'models.json').read_bytes()
            manifest = json.loads(manifest_raw)
            assert sha256(manifest_raw).hexdigest() == receipt['manifest_sha256']
            assert manifest['models']['eng']['sha256'] == expected
            assert catalog.load_source('fast').license_text in manifest['license']
            assert sha256((destination / 'eng.traineddata').read_bytes()).hexdigest() == expected
        assert sha256(incoming.read_bytes()).hexdigest() == expected
        prepared = True
    print(json.dumps({'status': 'PASS', 'installed_package': True, 'platform': sys.platform,
                      'catalogue_models': counts, 'module_cli': 'PASS',
                      'real_bundled_model_preparation': 'PASS' if prepared else 'NOT_REQUESTED',
                      'nvda': 'NOT_RUN'}))


if __name__ == '__main__':
    main()
