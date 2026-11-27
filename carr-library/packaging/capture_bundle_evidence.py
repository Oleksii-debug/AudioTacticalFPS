"""Retain a verified source wheel and text evidence from an accepted CI bundle.

This is a read-only verifier and evidence collector, not a bundle builder or a
Windows test runner. Native acceptance reports must come from the real producer
and installed-application probes. No executable, model or runtime is copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import build_windows_bundle as builder
from install_windows import verify
from prepare_windows_audio import local, regular, _publish_directory


PROCESS_CHECKS = {
    'unicode-spaces-fixed-argv-offline-optional-runtimes', 'read-only-ready-probe',
    'fixed-installation-check', 'child-exit-status-propagated',
    'extra-arguments-rejected', 'incomplete-state-preserved-without-reinstall',
    'missing-runtime-does-not-search-path', 'fresh-package-reuses-fixed-offline-installer',
    'research-data-sentinel-preserved',
}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Duplicate evidence property.')
        result[name] = value
    return result


def _json(path, limit=65536):
    if not 0 < regular(path).st_size <= limit:
        raise ValueError('Evidence JSON exceeds its size limit.')
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise ValueError('Evidence JSON changed while being read.')
    value = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise ValueError('Evidence must be a JSON object.')
    return raw, value


def _encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')


def capture(bundle, wheel, source_commit, output, *, process_probe, installed_probe):
    bundle, wheel = local(bundle, exists=True), local(wheel, exists=True)
    output = local(output)
    if output.exists() or output.is_relative_to(bundle) or bundle.is_relative_to(output):
        raise ValueError('Use a new evidence directory outside the private bundle.')
    manifest_raw, manifest = _json(bundle / 'bundle-manifest.json', 8 * 1024**2)
    verify(bundle, manifest_bytes=manifest_raw)
    identity = builder.verify_application_source(wheel, source_commit)
    if (manifest.get('source_commit') != source_commit
            or manifest.get('source_tree') != identity['source_tree']
            or manifest.get('bundled_native_launcher') is not True):
        raise ValueError('The bundle does not declare the selected application and native source.')
    source_raw, source = _json(bundle / 'application-source.json')
    if source != identity:
        raise ValueError('The sealed application proof differs from the selected wheel.')
    wheel_entry = 'wheelhouse/' + wheel.name
    if not 0 < regular(wheel).st_size <= 64 * 1024**2:
        raise ValueError('Application wheel exceeds the source verifier limit.')
    with wheel.open('rb') as stream:
        wheel_raw = stream.read(64 * 1024**2 + 1)
    if (_digest(wheel_raw) != identity['wheel_sha256']
            or manifest['files'].get(wheel_entry) != identity['wheel_sha256']):
        raise ValueError('The retained wheel differs from the sealed, installed application.')
    native = builder.capture_native_launcher(
        bundle / 'ResearchLibrary.exe', bundle / 'native-launcher-build.json',
        manifest.get('native_launcher_sha256'), source_commit,
        bundled_python=manifest.get('bundled_python') is True)
    if native is None or native['report']['source_tree'] != identity['source_tree']:
        raise ValueError('Native and application source identities disagree.')
    sbom_raw, _ = _json(bundle / 'runtime-sbom.cdx.json', 1024**2)
    sealed = {'application-source.json': source_raw,
              'native-launcher-build.json': native['proof_bytes'],
              'runtime-sbom.cdx.json': sbom_raw}
    if any(manifest['files'].get(name) != _digest(raw) for name, raw in sealed.items()):
        raise ValueError('A captured source, native or runtime proof differs from the bundle seal.')
    process_raw, process = _json(local(process_probe, exists=True))
    installed_raw, installed = _json(local(installed_probe, exists=True))
    for report in (process, installed):
        if (report.get('schema') != 1 or report.get('status') != 'passed'
                or report.get('launcher_sha256') != native['report']['launcher_sha256']
                or report.get('nvda_verified') is not False
                or report.get('authenticode_verified') is not False):
            raise ValueError('A native Windows report does not match the sealed launcher.')
    if (process.get('native_launcher_execution_verified') is not True
            or process.get('actual_python_installation_verified') is not False
            or process.get('check_count') != len(PROCESS_CHECKS)
            or not isinstance(process.get('checks'), list)
            or len(process['checks']) != len(PROCESS_CHECKS)
            or set(process['checks']) != PROCESS_CHECKS):
        raise ValueError('The Windows process probe is incomplete.')
    if (installed.get('actual_installed_app_self_test_verified') is not True
            or installed.get('actual_fresh_installation_verified') is not False
            or installed.get('gui_verified') is not False
            or installed.get('checks') != ['--check-installation', '--self-test']):
        raise ValueError('The actual installed-application launcher probe is incomplete.')
    if (bundle / 'bundle-manifest.json').read_bytes() != manifest_raw:
        raise ValueError('The bundle manifest changed during evidence capture.')
    wheel_proof = dict(identity, status='PASS', wheel_filename=wheel.name)
    report = {
        'schema': 1, 'status': 'PASS', 'source_commit': source_commit,
        'source_tree': identity['source_tree'], 'wheel_filename': wheel.name,
        'wheel_sha256': identity['wheel_sha256'],
        'bundle_manifest_sha256': _digest(manifest_raw),
        'launcher_sha256': native['report']['launcher_sha256'],
        'native_build_proof_sha256': _digest(native['proof_bytes']),
        'process_probe_sha256': _digest(process_raw),
        'installed_probe_sha256': _digest(installed_raw),
        'native_windows_reports_verified': True,
        'runtime_payload_included': False, 'authenticode_verified': False,
        'nvda_verified': False,
    }
    payload = {
        'tested-application-wheel/' + wheel.name: wheel_raw,
        'tested-application-wheel/source-wheel-proof.json': _encoded(wheel_proof),
        'tested-native-launcher/build-evidence.json': native['proof_bytes'],
        'tested-native-launcher/source-launcher-proof.json': _encoded(native['report']),
        'tested-native-launcher/windows-process-probe.json': process_raw,
        'tested-native-launcher/installed-launcher-probe.json': installed_raw,
        'sealed-bundle/bundle-manifest.json': manifest_raw,
        'sealed-bundle/application-source.json': source_raw,
        'sealed-bundle/runtime-sbom.cdx.json': sbom_raw,
        'accepted-bundle.json': _encoded(report),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.accepted-evidence-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'evidence'
        stage.mkdir()
        for name, raw in payload.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        _publish_directory(stage, output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('bundle', 'wheel', 'source-commit', 'output', 'process-probe', 'installed-probe'):
        parser.add_argument('--' + argument, required=True)
    args = parser.parse_args()
    print(json.dumps(capture(args.bundle, args.wheel, args.source_commit, args.output,
                             process_probe=args.process_probe, installed_probe=args.installed_probe),
                     indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
