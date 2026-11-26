"""Add the pinned C++ supplement to a private offline installation bundle.

This is a local packaging operation, with no download or executable launch.
Microsoft's original terms are retained. This operation does not establish a
right to redistribute Microsoft DLLs publicly or to third parties.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


HERE = Path(__file__).resolve().parent


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def attach_runtime(bundle, runtime, output):
    """Create a fresh complete bundle; preserve the source bundle unchanged."""
    bundle, runtime, output = (Path(value).absolute() for value in (bundle, runtime, output))
    if output.exists() or output.is_symlink():
        raise FileExistsError('Use a new output folder.')
    if output.is_relative_to(bundle) or bundle.is_relative_to(output):
        raise ValueError('The output must be outside the source bundle.')
    spec = importlib.util.spec_from_file_location('carr_bundle_installer', HERE / 'install_windows.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.verify(bundle)
    manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('bundled_native_runtime') or (bundle / 'native-runtime').exists()
            or any(name.startswith('native-runtime/') for name in manifest['files'])):
        raise ValueError('The source bundle already contains a native runtime.')
    if not manifest.get('bundled_python'):
        raise ValueError('This supplement requires the pinned bundled Python.')
    applications = [name for name in manifest['files']
                    if name.startswith('wheelhouse/universal_research_library-') and name.endswith('.whl')]
    if len(applications) != 1:
        raise ValueError('Expected one application wheel.')
    with ZipFile(bundle / applications[0]) as wheel:
        if not {'carr_library/native_runtime.py', 'carr_library/native_checks.py'}.issubset(wheel.namelist()):
            raise ValueError('Rebuild the application with native runtime activation and diagnostics first.')
    sys.path.insert(0, str(HERE.parent / 'src'))
    from carr_library.native_runtime import HOST_DLLS, verify_native_runtime
    report = verify_native_runtime(runtime)
    for name, expected in HOST_DLLS.items():
        if digest(bundle / 'runtime' / name) != expected:
            raise ValueError('The bundled Python differs from the reviewed runtime compatibility evidence.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='carr-native-bundle-', dir=output.parent) as scratch:
        stage = Path(scratch) / 'bundle'
        stage.mkdir()
        # Copy only the verified installation inventory. Environments, caches,
        # research databases and other unlisted local files are never collected.
        for name, expected in manifest['files'].items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(bundle / name, target)
            if digest(target) != expected:
                raise ValueError('Source installation changed while being copied.')
        shutil.copytree(runtime, stage / 'native-runtime')
        verified = verify_native_runtime(stage / 'native-runtime')
        if verified != report:
            raise ValueError('Native runtime changed while being copied.')
        manifest['bundled_native_runtime'] = True
        manifest.setdefault('bundled_ocr', False)
        manifest['files'].update({p.relative_to(stage).as_posix(): digest(p)
                                  for p in (stage / 'native-runtime').rglob('*') if p.is_file()})
        raw = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode('utf-8')
        (stage / 'bundle-manifest.json').write_bytes(raw)
        installer.verify(stage)
        output.mkdir(exist_ok=False)
        for item in stage.iterdir():
            shutil.move(str(item), output / item.name)
    return {'output': str(output), 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'native_runtime': report, 'native_windows_execution': 'NOT_RUN'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--native-runtime', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(attach_runtime(args.bundle, args.native_runtime, args.output), indent=2))


if __name__ == '__main__':
    main()
