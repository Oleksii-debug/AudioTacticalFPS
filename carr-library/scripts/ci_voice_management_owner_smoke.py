"""Current Carr voice manager against three pinned public owner API modules.

Run with -I -B. Only generated synthetic PCM/text, inert model/worker markers
and a labelled interpreter substitution are used. Owner source and all generated
data stay in runner-private temporary directories; only bounded JSON is retained.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from hashlib import sha1, sha256
import importlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
from tempfile import TemporaryDirectory
import time
import types

FIXTURE_BLOB = 'ef5be0ea225a4222146edfb05ff03f9037dd0eb5'
OWNER_HELPER_BLOB = 'e22c226ef24cf48957d04c32f25cb472e48c764e'
MODULES = (
    'carr_library.runtime_settings', 'carr_library.durable_state',
    'carr_library.catalog.intake', 'carr_library.audio.models',
    'carr_library.audio.cleanup', 'carr_library.audio.process',
    'carr_library.audio.windows_job', 'carr_library.audio.voice_manifest',
    'carr_library.audio.voice_ownership', 'carr_library.audio.voice_transport',
    'carr_library.audio.voice_settings', 'carr_library.audio.voice_packages',
    'carr_library.audio.voice_lifecycle', 'carr_library.audio.package_publication',
    'carr_library.operations.broker', 'carr_library.operations._locks',
    'carr_library.research.export_file', 'carr_library.studio.publishing',
)
ACTIONS = {'prepare_lock', 'catalog', 'inspect', 'install', 'versions',
           'review_version', 'activate', 'export', 'remove'}


def git_blob(raw):
    return sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()


def read_source(path, maximum=1024 * 1024, *, expected=None):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or not 0 < info.st_size <= maximum):
        raise ValueError('The selected source is not a bounded ordinary single-link file.')
    raw = path.read_bytes()
    after = path.lstat()
    stamp = lambda value: (value.st_dev, value.st_ino, value.st_size,
                           value.st_mtime_ns, value.st_ctime_ns, value.st_nlink)
    if (stamp(after) != stamp(info) or len(raw) != info.st_size
            or expected is not None and git_blob(raw) != expected):
        raise ValueError('The selected source identity changed.')
    return raw


def load_companion(name, path, expected):
    raw = read_source(path, expected=expected)
    if name in sys.modules:
        raise ValueError('The selected acceptance companion was already loaded.')
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module, sha256(raw).hexdigest()


def origins(source_root, installed):
    result = {}
    selected_root = source_root / 'carr_library'
    for name in MODULES:
        module = importlib.import_module(name)
        actual = Path(module.__file__).resolve(strict=True)
        expected = source_root / Path(*name.split('.')).with_suffix('.py')
        raw, wanted = read_source(actual), read_source(expected)
        if raw != wanted:
            raise ValueError('The executed application module differs from selected source: ' + name)
        if installed:
            if (not actual.is_relative_to(Path(sys.prefix).resolve())
                    or actual.is_relative_to(source_root)):
                raise ValueError('The isolated installed application used checkout code: ' + name)
        elif actual != expected.resolve(strict=True) or not actual.is_relative_to(selected_root):
            raise ValueError('The source application used another installed module: ' + name)
        result[name] = sha256(raw).hexdigest()
    return result


def network_offline(event, args):
    # Actual metadata children are required; their current fixed bootstrap
    # independently denies both network and further child creation.
    if event.startswith(('socket.', 'urllib.')):
        raise RuntimeError('Owner management acceptance is offline after explicit acquisition.')



def existing_owner(directory, helper):
    requested = Path(directory).absolute()
    # The external directory belongs to the caller and is read-only here.
    for path in reversed((requested, *requested.parents)):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('The supplied owner directory has a linked or invalid ancestor.')
    selected = requested.resolve(strict=True)
    ancestry = []
    for path in reversed((selected, *selected.parents)):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('The supplied owner directory ancestry changed.')
        ancestry.append((str(path), info.st_dev, info.st_ino))
    entries = list(selected.iterdir())
    if len(entries) != 3 or {path.name for path in entries} != set(helper.SOURCE_BLOBS):
        raise ValueError('Supply only the three exact pinned public owner source files.')
    buffers, receipts = {}, {}
    for name, expected in helper.SOURCE_BLOBS.items():
        raw = read_source(selected / name, maximum=128 * 1024, expected=expected)
        buffers[name] = raw
        receipts[name] = {'git_blob': expected, 'sha256': sha256(raw).hexdigest(),
                          'size_bytes': len(raw)}
    return selected, buffers, receipts, tuple(ancestry)


def exercise(root, owner_root, helper, fixture, buffers, source_receipts, checks, report):
    from carr_library import runtime_settings
    from carr_library.audio import process, voice_transport, windows_job
    from carr_library.audio.voice_packages import bridge_identity, managed_catalog, _hash
    from carr_library.audio.voice_lifecycle import VoiceProfileLifecycle
    from carr_library.operations import ResourceBroker
    import pytest

    started, deadline = time.monotonic(), time.monotonic() + 900
    children, child_rows, invocations = [], [], []
    report['management'] = {'children': child_rows, 'child_count': 0,
                            'current_broker_cleanup': 'NOT_RUN',
                            'actual_process_cleanup': 'NOT_RUN'}
    manager = None

    def stopped():
        if time.monotonic() >= deadline:
            raise TimeoutError('The current manager acceptance exceeded its finite 900-second allowance.')
        return False

    def check(name, condition):
        stopped()
        if not condition:
            raise AssertionError(name)
        if name in checks:
            raise AssertionError('Duplicate acceptance check name.')
        checks.append(name)
        print('PASS: ' + name, flush=True)

    def rejects(name, action, *, calls=None, settings=None):
        before = len(child_rows)
        try:
            action()
        except ValueError:
            pass
        else:
            raise AssertionError(name + ': unexpectedly accepted')
        check(name, (calls is None or len(child_rows) == before)
              and (settings is None or manager.store.path.read_bytes() == settings))

    # Execute the actual owner's deterministic package writer only for producing
    # the synthetic input archives. All manager operations run the real fixed
    # Carr child with these same exact source buffers.
    owner_packages, _ = helper.load_owner(owner_root, buffers)
    first_source, second_source = root / 'source-one', root / 'source-two'
    first = helper.make_reference(first_source, '1.0.0')
    second = helper.make_reference(second_source, '2.0.0')
    first_zip, second_zip = root / 'first.dordiai-voice', root / 'second.dordiai-voice'
    owner_packages.create_package(first_source, first, first_zip)
    owner_packages.create_package(second_source, second, second_zip)
    originals = {
        'one': helper.ordinary_inventory(first_source),
        'two': helper.ordinary_inventory(second_source),
        'archive-one': sha256(first_zip.read_bytes()).hexdigest(),
        'archive-two': sha256(second_zip.read_bytes()).hexdigest(),
    }
    identity = first['id']
    actual_process = voice_transport.run_process
    actual_job, actual_popen = windows_job.JobProcess, process.subprocess.Popen

    def observed_job(*args, **kwargs):
        child = actual_job(*args, **kwargs)
        children.append(('windows-job', child))
        return child

    def observed_popen(*args, **kwargs):
        child = actual_popen(*args, **kwargs)
        children.append(('posix-process', child))
        return child

    def observed_process(command, **kwargs):
        stopped()
        if (len(command) != 8 or Path(command[0]).resolve() != Path(sys.executable).resolve()
                or list(command[1:5]) != ['-I', '-B', '-u', '-c']
                or sha256(command[5].encode('utf-8')).hexdigest() != bridge_identity()):
            raise AssertionError('The actual metadata child did not use the current fixed bootstrap.')
        request_raw = read_source(Path(command[-2]), maximum=2 * 1024 * 1024)
        if sha256(request_raw).hexdigest() != command[-1]:
            raise AssertionError('The actual metadata child request differs from its argument digest.')
        request = json.loads(request_raw)
        if request['action'] not in ACTIONS or set(request['modules']) != {
                'voice_policy', 'voice_packages', 'voice_assets'}:
            raise AssertionError('The acceptance invoked another owner API action.')
        for module_name, row in request['modules'].items():
            name = module_name + '.py'
            raw = read_source(Path(row['path']), maximum=128 * 1024)
            if (raw != buffers[name] or sha256(raw).hexdigest() != row['sha256']
                    or git_blob(raw) != helper.SOURCE_BLOBS[name]):
                raise AssertionError('The current child did not bind the exact real owner source.')
        broker = ResourceBroker.for_app(root / 'library')
        if not broker.snapshot()['leases']:
            raise AssertionError('The actual owner child started without current broker admission.')
        if kwargs.get('owned_windows_job') is not (os.name == 'nt'):
            raise AssertionError('The current owner child changed native Job ownership.')
        row = {'action': request['action'], 'returned': False,
               'request_sha256': command[-1], 'owner_sources_verified': True,
               'broker_admitted': True, 'windows_job_required': os.name == 'nt'}
        child_rows.append(row)
        report['actual_owner_api'] = report['current_manager'] = 'FAIL'
        if len(child_rows) > 128:
            raise AssertionError('The owner operation inventory exceeded its fixed bound.')
        result = actual_process(command, **kwargs)
        row.update(returned=True, returncode=result.returncode)
        if result.returncode:
            # Retain frame locations and a type, never owner source lines or
            # arbitrary asset text from the captured diagnostic streams.
            diagnostic = result.stderr.decode('utf-8', errors='replace')
            frames = re.findall(r'File "([^"]+)", line ([0-9]+), in ([^\r\n]+)', diagnostic)
            last = diagnostic.rstrip().splitlines()[-1] if diagnostic.rstrip() else ''
            error_type = re.match(r'([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception))(?::|$)', last)
            row['failure'] = {'stderr_sha256': sha256(result.stderr).hexdigest(),
                'stderr_bytes': len(result.stderr),
                'exception_type': error_type.group(1) if error_type else 'UNCLASSIFIED',
                'frames': [{'file': Path(name).name[:80], 'line': int(line),
                            'function': function[:80]} for name, line, function in frames[-12:]]}
        return result

    # This is the exact frozen fixture: only its three source constants are
    # replaced. Its existing inert interpreter substitution is the sole child
    # execution substitution; current run_process/broker/Job code is unchanged.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fixture, 'POLICY', buffers['voice_policy.py'].decode('utf-8'))
        patch.setattr(fixture, 'PACKAGES', buffers['voice_packages.py'].decode('utf-8'))
        patch.setattr(fixture, 'ASSETS', buffers['voice_assets.py'].decode('utf-8'))
        patch.setattr(voice_transport, 'run_process', observed_process)
        if os.name == 'nt':
            patch.setattr(windows_job, 'JobProcess', observed_job)
        else:
            patch.setattr(process.subprocess, 'Popen', observed_popen)
        original_error = None
        try:
            library, manifest_path, manager, invocations = fixture.installed.__wrapped__(root, patch)
            runtime = manifest_path.parent
            before_binding = deepcopy(manager.store.read())
            check('actual current registration binds the real owner closure and existing package lock',
                  before_binding['revision'] == 1 and child_rows
                  and child_rows[0]['action'] == 'prepare_lock'
                  and (runtime / 'models/voices/packages/.voice-packages.lock').is_file())
            for name, raw in buffers.items():
                if (runtime / name).read_bytes() != raw:
                    raise AssertionError('The fixture altered actual owner source bytes.')
            base_manifest = json.loads(manifest_path.read_bytes())
            immutable = {name: checksum for name, checksum in base_manifest['files'].items()
                         if not name.startswith('models/voices/packages/')}
            preferences = {'favorites': ['legacy'], 'defaults': {'uk': 'legacy'}, 'profiles': {}}
            runtime_settings.save_voice_preferences(library / 'state', preferences,
                expected=runtime_settings.load_voice_preferences(library / 'state'))
            configured = manager.configure('omnivoice', 'models', 'models/OmniVoice', 'models/voices',
                expected_revision=before_binding['revision'], cancelled=stopped)
            check('current configuration explicitly selects the supported OmniVoice package recipe',
                  configured['revision'] == before_binding['revision'] + 1
                  and manager.configuration(cancelled=stopped)['reference_engine'] == 'omnivoice')
            review = manager.inspect(first_zip, cancelled=stopped)
            check('actual current inspection returns exact generated data roles and languages',
                  review.document == first and review.operation == 'install'
                  and review.source_sha256 == originals['archive-one'])
            saved = manager.store.path.read_bytes()
            rejects('unchecked reference rights refuse before another child or settings mutation',
                    lambda: manager.apply(review, rights_approved=False,
                                          supported_languages=['uk'], cancelled=stopped),
                    calls=True, settings=saved)
            installed = manager.apply(review, rights_approved=True,
                                      supported_languages=['uk'], cancelled=stopped)
            first_directory = runtime / 'models/voices/packages' / identity / '1.0.0'
            first_inventory = helper.ordinary_inventory(first_directory)
            current = json.loads(manifest_path.read_bytes())
            check('real owner installation publishes exact first version and current explicit rights',
                  owner_packages.verify_assets(first_directory, first) == first
                  and installed['revision'] == configured['revision'] + 1
                  and current['settings']['profile_reviews'][identity] == {
                      'version': '1.0.0', 'document_sha256': _hash(first),
                      'references': {row['role']: row['sha256'] for row in first['files'].values()},
                      'rights_approved': True}
                  and current['settings']['profile_languages'][identity] == {
                      'supported': ['uk'], 'candidates': ['sk']})
            second_review = manager.inspect(second_zip, cancelled=stopped)
            installed_second = manager.apply(second_review, rights_approved=True,
                                             supported_languages=['uk'], cancelled=stopped)
            check('second actual install preserves immutable first-version bytes and advances binding',
                  installed_second['revision'] == installed['revision'] + 1
                  and helper.ordinary_inventory(first_directory) == first_inventory
                  and [row['version'] for row in manager.versions(identity, cancelled=stopped)]
                  == ['1.0.0', '2.0.0'])
            stale_second = manager.review_version(identity, '2.0.0', cancelled=stopped)
            rollback = manager.review_version(identity, '1.0.0', cancelled=stopped)
            activated = manager.apply(rollback, rights_approved=True,
                                      supported_languages=['uk'], cancelled=stopped)
            index_path = runtime / 'models/voices/packages/voice-packages.json'
            check('explicit current version activation reaches the real owner and refreshes authority',
                  json.loads(index_path.read_bytes())['active'][identity] == '1.0.0'
                  and activated['revision'] == installed_second['revision'] + 1
                  and helper.ordinary_inventory(first_directory) == first_inventory)
            saved = manager.store.path.read_bytes()
            rejects('stale saved version review cannot silently select a later authority',
                    lambda: manager.apply(stale_second, rights_approved=True,
                        supported_languages=['uk'], cancelled=stopped), calls=True, settings=saved)
            lifecycle = VoiceProfileLifecycle(library, manager=manager, broker=manager.broker)
            exported_path = root / 'exported.dordiai-voice'
            export_review = manager.review_version(identity, '1.0.0', cancelled=stopped)
            rejects('export requires its own explicit reference permission before owner access',
                    lambda: lifecycle.export(export_review, exported_path,
                        export_rights_approved=False, cancelled=stopped), calls=True)
            check('refused export creates no selected output', not exported_path.exists())
            exported = lifecycle.export(export_review, exported_path,
                                         export_rights_approved=True, cancelled=stopped)
            check('actual current export has exact owner document and independently verified receipt',
                  owner_packages.inspect_package(exported_path) == first
                  and exported['sha256'] == sha256(exported_path.read_bytes()).hexdigest()
                  and exported['size_bytes'] == exported_path.stat().st_size
                  and exported['profile'] == identity and exported['version'] == '1.0.0'
                  and exported_path.read_bytes() == first_zip.read_bytes())
            removal = lifecycle.review_removal(identity, cancelled=stopped)
            revoked = lifecycle.remove(removal, confirmation=removal.confirmation, cancelled=stopped)
            index = json.loads(index_path.read_bytes())
            check('actual current revoke preserves the owner tombstone and exact installed files',
                  identity not in index['active'] and index['inactive'][identity] == '1.0.0'
                  and revoked['revision'] == activated['revision'] + 1
                  and helper.ordinary_inventory(first_directory) == first_inventory)
            with manager.operation(cancelled=stopped) as admitted:
                _, selected = manager._current(cancelled=admitted)
                legacy, profiles, owned = managed_catalog(selected, {identity: 'Controlled legacy collision'},
                                                           cancelled=admitted)
            check('revoked real owner identity cannot fall back to an old legacy profile',
                  identity not in legacy and identity not in profiles and identity in owned
                  and identity in selected.settings['revoked_profiles'])
            removal = lifecycle.review_removal(identity, delete_files=True, cancelled=stopped)
            saved = manager.store.path.read_bytes()
            rejects('deletion requires the exact fresh reviewed action',
                    lambda: lifecycle.remove(removal, confirmation='REVOKE ' + identity,
                                             cancelled=stopped), calls=True, settings=saved)
            deleted = lifecycle.remove(removal, confirmation=removal.confirmation, cancelled=stopped)
            index = json.loads(index_path.read_bytes())
            check('actual current delete removes only the selected retained versions',
                  not first_directory.parent.exists() and identity not in index['active']
                  and identity not in index.get('inactive', {})
                  and deleted['revision'] == revoked['revision'] + 1)
            with manager.operation(cancelled=stopped) as admitted:
                _, selected = manager._current(cancelled=admitted)
                legacy, profiles, owned = managed_catalog(selected, {identity: 'Controlled legacy collision'},
                                                           cancelled=admitted)
            check('local revocation survives real owner registration deletion',
                  identity not in legacy and identity not in profiles and identity in owned
                  and identity in selected.settings['revoked_profiles'])
            check('every original runtime helper worker model marker and legacy reference remains exact',
                  all(sha256((runtime / name).read_bytes()).hexdigest() == digest
                      for name, digest in immutable.items()))
            check('unrelated current voice preferences remain exact across all management commits',
                  runtime_settings.load_voice_preferences(library / 'state') == preferences)
            check('all generated source recordings text and original input archives remain exact',
                  helper.ordinary_inventory(first_source) == originals['one']
                  and helper.ordinary_inventory(second_source) == originals['two']
                  and sha256(first_zip.read_bytes()).hexdigest() == originals['archive-one']
                  and sha256(second_zip.read_bytes()).hexdigest() == originals['archive-two'])
            check('all metadata actions use actual current child admission and exact real owner bytes',
                  len(invocations) == len(child_rows) == len(children)
                  and all(row['returned'] and row['returncode'] == 0
                          and row['broker_admitted'] and row['owner_sources_verified']
                          for row in child_rows)
                  and ACTIONS <= {row['action'] for row in child_rows})
            check('all actual metadata processes are reaped and Windows handles are closed',
                  all(child.poll() == 0 for _, child in children)
                  and all(kind != 'windows-job' or (
                      child._job is None and not child._process.hProcess and not child._process.hThread)
                      for kind, child in children))
            check('no current broker lease survives actual owner management',
                  not manager.broker.snapshot()['leases'])
            check('no model synthesis microphone playback or private runtime was executed',
                  all(command[4] == '-c' for command in invocations)
                  and not any(name == 'torch' or name.startswith(('torch.', 'nvda', 'synthDrivers'))
                              for name in sys.modules))
            report['management'] = {
                'profile': identity, 'versions': ['1.0.0', '2.0.0'],
                'registration_revision': before_binding['revision'],
                'final_revision': deleted['revision'],
                'final_binding_sha256': deleted['bindings']['dordiai_manifest'],
                'package_sha256': {key: originals[key] for key in ('archive-one', 'archive-two')},
                'export_sha256': exported['sha256'],
                'bridge_sha256': bridge_identity(),
                'action_counts': dict(sorted(Counter(row['action'] for row in child_rows).items())),
                'children': child_rows, 'child_count': len(children),
                'current_broker_cleanup': 'PASS', 'actual_process_cleanup': 'PASS',
                'operation_seconds': time.monotonic() - started,
            }
        except BaseException as error:
            original_error = error
            raise
        finally:
            # Current run_process already owns settlement. This failure-only
            # cleanup retains the same fixture routes until its native owners
            # are closed; it never turns a failed ownership assertion green.
            cleanup_errors = []
            for kind, child in children:
                try:
                    if child.poll() is None:
                        cleanup_errors.append('An actual owner child survived its current process boundary.')
                        process._terminate(child)
                    if kind == 'windows-job':
                        child.close()
                except BaseException as error:
                    cleanup_errors.append(type(error).__name__)
            report['management']['child_count'] = len(children)
            report['management']['operation_seconds'] = time.monotonic() - started
            if not cleanup_errors:
                report['management']['actual_process_cleanup'] = 'PASS'
                if manager is not None:
                    report['management']['current_broker_cleanup'] = (
                        'PASS' if not manager.broker.snapshot()['leases'] else 'FAIL')
            if cleanup_errors:
                report['management']['actual_process_cleanup'] = 'FAIL'
                report['cleanup_errors'] = cleanup_errors[:16]
                if original_error is None:
                    raise RuntimeError('Actual owner child cleanup failed; see bounded receipt.')
    return child_rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--require-installed', action='store_true')
    acquisition = parser.add_mutually_exclusive_group(required=True)
    acquisition.add_argument('--acquire', action='store_true')
    acquisition.add_argument('--owner-api-directory', type=Path,
        help='Read only the caller-acquired exact three-file owner closure; perform no download.')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.report.suffix.lower() != '.json':
        raise ValueError('Retain only the bounded JSON acceptance receipt.')
    if not sys.flags.isolated or not sys.flags.dont_write_bytecode or not __debug__:
        raise RuntimeError('Use -I -B with the nonoptimized interpreter.')
    source_root = args.source_root.resolve(strict=True)
    if not args.require_installed:
        sys.path.insert(0, str(source_root))
    helper_path = source_root.parent / 'scripts/ci_owner_voice_api_smoke.py'
    fixture_path = source_root.parent / 'tests/audio/voice_management_fixtures.py'
    report = {'schema': 'carr-current-manager-real-owner/1', 'status': 'FAIL',
              'mode': 'installed' if args.require_installed else 'source',
              'platform': platform.system(), 'checks': [],
              'owner_input': 'explicit-download' if args.acquire else 'preacquired-closed-directory',
              'actual_owner_api': 'NOT_RUN', 'current_manager': 'NOT_RUN',
              'interpreter': 'Only the exact frozen fixture inert marker is replaced with this interpreter.',
              'synthetic_model_and_worker_markers': True,
              'scope': 'Actual public owner package API through current Carr metadata transport and settings.',
              'qualifications': {'real_engine_inference': 'NOT_RUN', 'acoustic_quality': 'NOT_RUN',
                  'physical_recording': 'NOT_RUN', 'manual_nvda': 'NOT_RUN', 'release': 'NOT_RUN',
                  'owner_source_or_data_in_public_artifacts': False}}
    code, temporary = 1, None
    try:
        helper, helper_sha = load_companion('_carr_real_owner_api_companion', helper_path, OWNER_HELPER_BLOB)
        fixture, fixture_sha = load_companion('_carr_real_owner_manager_fixture', fixture_path, FIXTURE_BLOB)
        before = origins(source_root, args.require_installed)
        report.update(owner_repository=helper.OWNER, owner_revision=helper.REVISION,
                      owner_prefix=helper.PREFIX, origins_before=before,
                      fixture_sha256=fixture_sha, owner_helper_sha256=helper_sha)
        with TemporaryDirectory(prefix='carr-current-real-owner-') as temporary:
            root = Path(temporary).resolve(strict=True)
            if args.acquire:
                owner_root = root / 'owner'
                buffers, receipts = helper.acquire(owner_root)
                _, checked, _, owner_identity = existing_owner(owner_root, helper)
                if checked != buffers:
                    raise AssertionError('The acquired source changed before current-manager admission.')
                supplied = owner_root
            else:
                supplied = args.owner_api_directory
                owner_root, buffers, receipts, owner_identity = existing_owner(supplied, helper)
                if args.report.resolve().is_relative_to(owner_root):
                    raise ValueError('The report cannot modify the read-only supplied owner closure.')
            report['owner_source_receipts'] = receipts
            sys.addaudithook(network_offline)
            exercise(root, owner_root, helper, fixture, buffers, receipts, report['checks'], report)
            after = origins(source_root, args.require_installed)
            if after != before:
                raise AssertionError('Application module source changed during actual owner management.')
            report['origins_after'] = after
            if sha256(read_source(helper_path, expected=OWNER_HELPER_BLOB)).hexdigest() != helper_sha:
                raise AssertionError('The owner acquisition helper changed during acceptance.')
            if sha256(read_source(fixture_path, expected=FIXTURE_BLOB)).hexdigest() != fixture_sha:
                raise AssertionError('The labelled interpreter fixture changed during acceptance.')
            _, after_buffers, after_receipts, after_identity = existing_owner(supplied, helper)
            if (after_buffers != buffers or after_receipts != receipts or after_identity != owner_identity):
                raise AssertionError('Actual owner source or its directory identity changed during acceptance.')
            report['checks'].append('executed application and exact acquisition companions remain unchanged')
        if root.exists():
            raise AssertionError('Temporary source and generated package data were not retired.')
        report['checks'].append('all probe-owned generated data were retired')
        report['supplied_owner_directory_unchanged'] = not args.acquire
        report['probe_downloaded_owner_source_retired'] = bool(args.acquire)
        report['actual_owner_api'] = report['current_manager'] = 'PASS'
        report['status'], code = 'PASS', 0
    except Exception as error:
        message = str(error)
        if temporary is not None:
            message = message.replace(str(temporary), '<owned-temporary-root>')
        report['error'] = {'type': type(error).__name__, 'message': message[:4096]}
    raw = (json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(raw) > 64 * 1024:
        raise RuntimeError('The bounded current-manager receipt exceeds 64 KiB.')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('xb') as stream:
        stream.write(raw)
    print(json.dumps({'status': report['status'], 'mode': report['mode'],
                      'checks': len(report['checks']), 'report_sha256': sha256(raw).hexdigest(),
                      'error': report.get('error')}, sort_keys=True), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
