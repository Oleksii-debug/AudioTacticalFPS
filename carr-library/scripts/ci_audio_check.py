"""Progress and required native evidence for the unchanged audio selection.

Deliberate platform and optional-engine skips remain visible. Requested native
Windows and enabled long-PCM cases must actually pass; expected failures never
count as acceptance. Progress does not print long parameter IDs.
"""
import os
from pathlib import Path
import sys
import time

import pytest

# Support both the script entry point and source-only importlib test loading.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from ci_watchdog import CaseWatchdog, without_scheduled_faulthandler
finally:
    sys.path.pop(0)


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_CASES = frozenset({
    'tests/audio/test_windows_process.py::test_windows_cancellation_stops_descendant_after_its_helper_exits[run]',
    'tests/audio/test_windows_process.py::test_windows_cancellation_stops_descendant_after_its_helper_exits[dordiai_stream]',
    'tests/audio/test_windows_process.py::test_native_concurrent_unrelated_process_survives_owned_cancellation',
    'tests/audio/test_engines.py::test_actual_installed_windows_sapi_voice',
})
LONG_PCM_CASES = frozenset(
    f'tests/audio/test_long_streaming_integrity.py::test_exact_multi_hour_streamed_pcm[{hours}]'
    for hours in (1, 3, 10))


def required_cases(arguments, *, platform=None, long_audio=None):
    """Bind required cases to explicit paths, even if -k/-m later deselects them."""
    platform = sys.platform if platform is None else platform
    long_audio = os.environ.get('CARR_LONG_AUDIO_TESTS') == '1' if long_audio is None else long_audio
    candidates = (WINDOWS_CASES if platform == 'win32' else frozenset()) | (
        LONG_PCM_CASES if long_audio else frozenset())
    required = set()
    for case in candidates:
        filename, node = case.split('::', 1)
        target = (ROOT / filename).resolve()
        for argument in arguments:
            if argument.startswith('-'):
                continue
            path, separator, selected_node = argument.partition('::')
            requested = (ROOT / path).resolve()
            if requested == target and (not separator or node == selected_node
                                       or node.startswith(selected_node + '[')):
                required.add(case)
            elif not separator and target.is_relative_to(requested):
                required.add(case)
    return required


class CaseProgress:
    def __init__(self, required=()):
        self.required = frozenset(required)
        self.passed = set()
        self.expected_failure = False
        self.watchdog = CaseWatchdog()
        self.case_token = None

    def pytest_sessionstart(self, session):
        self.reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        self.number = 0

    def pytest_runtest_logstart(self, nodeid, location):
        self.number += 1
        self.started = time.monotonic()
        label = nodeid.partition("[")[0][:240]
        self.reporter.write_line(f"CASE_START {self.number:03d} {label}")
        self.case_token = self.watchdog.start(nodeid)

    def pytest_runtest_logfinish(self, nodeid, location):
        self.watchdog.finish(self.case_token)
        self.case_token = None
        elapsed = time.monotonic() - self.started
        self.reporter.write_line("")
        self.reporter.write_line(f"CASE_END {self.number:03d} {elapsed:.3f}s")

    def pytest_sessionfinish(self, session, exitstatus):
        self.watchdog.close()

    def pytest_runtest_logreport(self, report):
        if hasattr(report, 'wasxfail'):
            self.expected_failure = True
        elif report.when == 'call' and report.passed:
            self.passed.add(report.nodeid)


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    for name in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        os.environ.pop(name, None)
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    policy = CaseProgress(required_cases(arguments))
    try:
        status = pytest.main(without_scheduled_faulthandler(
            ['-o', 'addopts=', '-o', 'xfail_strict=true', *arguments]), plugins=[policy])
    finally:
        policy.watchdog.close()
    missing = policy.required - policy.passed
    if policy.expected_failure:
        print('FAIL: audio acceptance reported XFAIL or XPASS', flush=True)
    if missing:
        print('FAIL: requested native/long-PCM cases did not pass: ' + ', '.join(sorted(missing)), flush=True)
    if not policy.passed:
        print('FAIL: audio acceptance did not execute a passing test', flush=True)
    if status == pytest.ExitCode.OK and (policy.expected_failure or missing or not policy.passed):
        return int(pytest.ExitCode.TESTS_FAILED)
    return int(status)


if __name__ == "__main__":
    raise SystemExit(main())
