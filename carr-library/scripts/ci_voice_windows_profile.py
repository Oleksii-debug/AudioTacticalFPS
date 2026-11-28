"""Measure fixed synthetic voice cases without changing any qualification gate.

The diagnostic profiles the actual source tests, fixtures, metadata child and
Windows Job path. cProfile covers the pytest main thread, not child CPU or other
threads. Its overhead is included; these samples are not throughput qualification.
"""
import argparse
import cProfile
from hashlib import sha1, sha256
import importlib.util
import json
import math
import os
from pathlib import Path
import pstats
import re
import subprocess
import sys
import tempfile
from time import perf_counter
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_SHA = 'f7e9c97c2f480114713e8f4911c8003676364d5c'
CI_SHA = '6ad3055cfcd7c6a1b1af8544ae462c5d51adb473'
CASES = {
    'package': 'tests/audio/test_voice_package_management.py::test_actual_version_activation_keeps_both_immutable_versions_and_rebinds_review',
    'reference': 'tests/audio/test_voice_reference_management.py::test_actual_pcm_and_owner_child_create_exact_local_data_without_installing',
    'managed': 'tests/integrations/test_dordiai_managed_authority.py::test_actual_managed_pcm_uses_existing_registry_and_releases_only_after_child_death',
}
WATCHED = (
    'voice_manifest.py', 'voice_packages.py', 'voice_settings.py',
    'voice_ownership.py', 'voice_transport.py', 'voice_reference.py',
    'voice_lifecycle.py', '_dordiai_authority.py', 'dordiai_voices.py',
    'dordiai_speech.py', 'process.py', 'windows_job.py', 'cleanup.py',
    'durable_state.py', 'intake.py', 'broker.py', '_locks.py', 'pathlib.py',
    'ntpath.py', 'subprocess.py', 'voice_management_fixtures.py',
)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def write(path, value):
    raw = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('utf-8')
    require(len(raw) <= 8 * 1024**2, 'Bounded diagnostic JSON exceeded.')
    path.write_bytes(raw)


def blob(path):
    raw = path.read_bytes()
    return sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()


def load(name, relative, expected):
    path = ROOT / relative
    require(path.is_file() and not path.is_symlink() and blob(path) == expected,
            'Changed exact diagnostic dependency: ' + relative)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def label(filename):
    if filename.startswith(('<', '~')):
        return filename[:256]
    path = Path(filename)
    try:
        return path.resolve().relative_to(ROOT).as_posix()[:256]
    except ValueError:
        # No runtime directories, argument values or frame locals are retained.
        return path.name[:256]


def function_row(key, value):
    filename, line, name = key
    primitive, total, own, cumulative = value[:4]
    require(all(math.isfinite(number) and number >= 0 for number in (own, cumulative)),
            'Profile returned invalid numeric time.')
    return dict(file=label(filename), line=line, function=name[:192],
                primitive_calls=primitive, total_calls=total,
                self_seconds=own, cumulative_seconds=cumulative)


def profile_rows(profile):
    stats = pstats.Stats(profile).stats
    ordered = sorted(stats, key=lambda key: (-stats[key][3], key))[:100]
    ordered += sorted(stats, key=lambda key: (-stats[key][2], key))[:100]
    ordered += [key for key in stats if Path(key[0]).name in WATCHED]
    keys = list(dict.fromkeys(ordered))
    # Finite output; retain the top self/cumulative rows even when the watched
    # source has more functions than this independent diagnostic allowance.
    selected = keys[:600]
    return dict(functions_total=len(stats), selected_functions=len(selected),
                selection_truncated=len(keys) > len(selected),
                rows=[function_row(key, stats[key]) for key in selected])


def child(args):
    require(os.name == 'nt' and __debug__, 'This diagnostic requires actual Windows and assertions.')
    contract = load('_voice_profile_contract', 'scripts/ci_voice_management_contracts.py', CONTRACT_SHA)
    ci = load('_voice_profile_ci', 'scripts/ci_check.py', CI_SHA)
    before = contract.sources()
    # Same explicit source and sibling bootstrap as the normal source runner.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'src'))
    import pytest
    from unittest.mock import patch

    evidence = args.evidence.resolve(strict=True)
    started = perf_counter()
    phases, reports = [], []
    events = evidence / 'events.jsonl'
    expected_node = CASES[args.case]

    def emit(value):
        value = dict(value, since_child_start_seconds=perf_counter() - started)
        raw = (json.dumps(value, sort_keys=True, ensure_ascii=True) + '\n').encode('utf-8')
        require(len(raw) < 8192 and (not events.exists() or events.stat().st_size < 128 * 1024),
                'Bounded phase events exceeded.')
        with events.open('ab') as stream:
            stream.write(raw)
            stream.flush()

    class Observation:
        @pytest.hookimpl
        def pytest_collection_modifyitems(self, items):
            require(len(items) == 1 and items[0].nodeid == expected_node,
                    'Exactly the fixed reviewed diagnostic case is required.')
            emit(dict(event='collected', nodeid=expected_node))

        def phase(self, name, item):
            require(item.nodeid == expected_node, 'Unexpected diagnostic node.')
            profile = cProfile.Profile()
            phase_started = perf_counter()
            error_type = None
            emit(dict(event='phase-start', phase=name, nodeid=expected_node))
            profile.enable()
            try:
                return (yield)
            except BaseException as error:
                error_type = type(error).__name__
                raise
            finally:
                profile.disable()
                elapsed = perf_counter() - phase_started
                # Save each completed phase immediately. A later parent timeout
                # cannot turn a partial setup or call into a completed result.
                record = dict(phase=name, elapsed_seconds=elapsed, error_type=error_type,
                              **profile_rows(profile))
                phases.append(record)
                # The same bounded aggregate is also visible in the closed job
                # log; artifact extraction is not required to diagnose a cost.
                print('VOICE_PROFILE_PHASE ' + json.dumps(record, sort_keys=True,
                      ensure_ascii=True, separators=(',', ':')), flush=True)
                emit(dict(event='phase-end', phase=name, elapsed_seconds=elapsed,
                          error_type=error_type))
                write(evidence / 'profile.json', dict(
                    schema='carr-voice-windows-profile/1', case=args.case,
                    qualification='DIAGNOSTIC_ONLY', phases=phases,
                    profiler_scope='pytest main thread; child CPU and other threads are not profiled',
                    profiler_overhead_included=True, function_arguments_retained=False))

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_setup(self, item):
            return (yield from self.phase('setup', item))

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_call(self, item):
            return (yield from self.phase('call', item))

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_teardown(self, item):
            return (yield from self.phase('teardown', item))

        @pytest.hookimpl
        def pytest_runtest_logreport(self, report):
            require(report.nodeid == expected_node, 'Report refers to another diagnostic node.')
            row = dict(when=report.when, outcome=report.outcome,
                       duration_seconds=report.duration,
                       expected_failure=hasattr(report, 'wasxfail'))
            reports.append(row)
            emit(dict(event='pytest-report', **row))

    observation = Observation()
    actual_main = pytest.main

    def observed_main(*positional, **keywords):
        # Forward the exact canonical policy, timed-dump plugins and arguments.
        keywords = dict(keywords)
        keywords['plugins'] = [*keywords.get('plugins', ()), observation]
        return actual_main(*positional, **keywords)

    arguments = ['-v', '-s', '-o', 'addopts=', '-o', 'xfail_strict=true',
                 '-o', 'faulthandler_timeout=120', '-o', 'faulthandler_exit_on_timeout=false',
                 '--junitxml', str(evidence / 'pytest.xml'), '--basetemp', str(args.work / 'pytest'),
                 '--', str(ROOT / expected_node.split('::', 1)[0]) + '::' + expected_node.split('::', 1)[1]]
    sys.argv = ['voice-profile-child', *arguments]
    try:
        with patch.object(pytest, 'main', observed_main):
            # Byte-identical current offline/XFAIL/GIL-owned timed-dump policy.
            exec(compile(ci.PYTEST_CHILD, str(ROOT / 'scripts/ci_check.py'), 'exec'),
                 {'__name__': '__main__'})
    finally:
        after = contract.sources()
        require(after == before, 'Pinned source changed during diagnostic case.')
        origins = {}
        for name in contract.MODULES:
            module = sys.modules.get(name)
            if module is None:
                continue
            path = Path(module.__file__).resolve(strict=True)
            relative = path.relative_to(ROOT).as_posix()
            require(relative in before and sha256(path.read_bytes()).hexdigest() == before[relative]['sha256'],
                    'Loaded module differs from exact pinned source: ' + name)
            origins[name] = dict(path=relative, sha256=before[relative]['sha256'])
        require(all(name in origins for name in (
            'carr_library.audio.voice_packages', 'carr_library.audio.voice_transport',
            'carr_library.audio.voice_ownership')),
                'Required current source origins were not observed.')
        write(evidence / 'child-summary.json', dict(
            schema='carr-voice-windows-profile-child/1', case=args.case, nodeid=expected_node,
            elapsed_seconds=perf_counter() - started, phases_completed=[row['phase'] for row in phases],
            reports=reports, origins=origins, source_identity='UNCHANGED',
            qualification='DIAGNOSTIC_ONLY', whole_suite_qualification='NOT_RUN',
            installed_qualification='NOT_RUN', actual_owner_api='NOT_RUN',
            native_ui='NOT_RUN', acoustic_quality='NOT_RUN'))


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=tuple(CASES), required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--audio-directory', type=Path, required=True)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--work', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        require(args.work is not None and args.work.is_dir(), 'Parent-owned diagnostic workspace is required.')
        return child(args)
    require(os.name == 'nt' and __debug__ and re.fullmatch('[0-9a-f]{40}', args.expected_head),
            'Actual Windows, assertions and an exact source head are required.')
    contract = load('_voice_profile_contract_parent', 'scripts/ci_voice_management_contracts.py', CONTRACT_SHA)
    ci = load('_voice_profile_ci_parent', 'scripts/ci_check.py', CI_SHA)
    require(contract.git('rev-parse', 'HEAD') == args.expected_head, 'Different exact diagnostic head.')
    own_path = Path(__file__).resolve()
    own_relative = own_path.relative_to(ROOT.parent).as_posix()
    require(contract.git('show', args.expected_head + ':' + own_relative) == own_path.read_text(encoding='utf-8').strip(),
            'Diagnostic helper differs from its committed source.')
    evidence = args.evidence.resolve()
    evidence.mkdir(mode=0o700, exist_ok=False)
    before, audio = contract.sources(), contract.audio_inputs(args.audio_directory.resolve(strict=True))
    write(evidence / 'identity-before.json', dict(
        source_commit=args.expected_head, source_tree=contract.git('rev-parse', 'HEAD^{tree}'),
        inputs=before, helper_sha256=sha256(own_path.read_bytes()).hexdigest(),
        audio=audio, python=sys.version, platform=sys.platform, selected_case=CASES[args.case],
        qualification='DIAGNOSTIC_ONLY'))
    began = perf_counter()
    code, error_type, verdict = None, None, 'INCOMPLETE'
    try:
        # Reuse the normal finite child owner, including family termination on
        # timeout. The representative case retains the original 600 s limit.
        with tempfile.TemporaryDirectory(prefix='carr-voice-profile-', dir=os.environ['RUNNER_TEMP']) as work:
            code = contract.child([
                sys.executable, '-B', str(own_path), '--child', '--case', args.case,
                '--evidence', str(evidence), '--expected-head', args.expected_head,
                '--audio-directory', str(args.audio_directory.resolve(strict=True)), '--work', work],
                evidence / 'pytest.txt', cwd=ROOT, ci=ci, timeout=600)
        cases = list(ET.parse(evidence / 'pytest.xml').iter('testcase'))
        require(len(cases) == 1 and cases[0].get('name') == CASES[args.case].split('::')[1],
                'Representative diagnostic JUnit membership differs.')
        failures = sum(any(case.find(tag) is not None for tag in ('failure', 'error')) for case in cases)
        skips = sum(case.find('skipped') is not None for case in cases)
        summary = json.loads((evidence / 'child-summary.json').read_text(encoding='utf-8'))
        require(code == 0 and failures == skips == 0 and summary['phases_completed'] == ['setup', 'call', 'teardown']
                and len(summary['reports']) == 3
                and all(row['outcome'] == 'passed' and not row['expected_failure'] for row in summary['reports']),
                'The real diagnostic case did not complete all existing assertions.')
        verdict = 'COMPLETED'
        return 0
    except BaseException as error:
        error_type = type(error).__name__
        raise
    finally:
        after = contract.sources()
        after_audio = contract.audio_inputs(args.audio_directory.resolve(strict=True))
        require(after == before and after_audio == audio and contract.git('rev-parse', 'HEAD') == args.expected_head,
                'Source or exact codec changed during diagnostic execution.')
        write(evidence / 'identity-after.json', dict(inputs=after, audio=after_audio, source_commit=args.expected_head))
        write(evidence / 'summary.json', dict(
            schema='carr-voice-windows-profile-run/1', case=args.case, nodeid=CASES[args.case],
            case_outcome=verdict, pytest_exit=code, error_type=error_type,
            elapsed_seconds=perf_counter() - began, child_deadline_seconds=600,
            qualification='DIAGNOSTIC_ONLY', whole_suite_qualification='NOT_RUN',
            installed_qualification='NOT_RUN', actual_owner_api='NOT_RUN',
            native_ui='NOT_RUN', acoustic_quality='NOT_RUN',
            caution='cProfile overhead included; the profiler observes only the pytest main thread.'))


if __name__ == '__main__':
    raise SystemExit(main())
