"""Strict source/installed local HTTP acceptance inside the existing CI firewall.

The calling Windows workflow installs and reads back the process-scoped
external-address rules before this program. Actual CLI children retain -I/-B
and select the same checked package explicitly. This runner neither installs
dependencies nor changes network policy.
"""
import argparse
from collections import Counter
from hashlib import sha256
import importlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET


MODULES = (
    'carr_library.application',
    'carr_library.assistant.models',
    'carr_library.assistant.profiles',
    'carr_library.assistant.providers',
    'carr_library.assistant.state',
    'carr_library.assistant.translation',
    'carr_library.assistant.translation_local',
    'carr_library.assistant.translation_documents',
    'carr_library.assistant.translation_cli',
    'carr_library.reader.reading_desk',
    'carr_library.speech.source',
    'carr_library.speech.reading',
    'carr_library.durable_state',
)
TEST = 'tests/assistant/test_translation_local_loopback.py'
REQUIRED = (
    'test_actual_loopback_preserves_exact_selected_request_and_receipt',
    'test_actual_local_http_failure_never_redirects_or_retries[302]',
    'test_actual_local_http_failure_never_redirects_or_retries[401]',
    'test_actual_local_http_failure_never_redirects_or_retries[503]',
    'test_actual_automatic_response_must_match_declared_pair',
    'test_actual_cli_requires_local_approval_and_keeps_complete_work_offline',
    'test_actual_cli_retained_batch_reuses_only_existing_jobs_and_originals',
)


PROVIDER_MODULES = MODULES + (
    'carr_library.assistant.credentials',
    'carr_library.assistant.llm',
    'carr_library.assistant.google_oauth',
)
PROVIDER_TEST = 'tests/assistant/test_providers.py'
PROVIDER_REQUIRED = (
    "test_local_only_denies_before_any_vault_or_connection",
    "test_remote_envelope_rejects_wrong_scope_before_credentials[change0]",
    "test_remote_envelope_rejects_wrong_scope_before_credentials[change1]",
    "test_remote_envelope_rejects_wrong_scope_before_credentials[change2]",
    "test_remote_envelope_rejects_wrong_scope_before_credentials[change3]",
    "test_remote_envelope_rejects_wrong_scope_before_credentials[change4]",
    "test_remote_envelope_rejects_wrong_scope_before_credentials[change5]",
    "test_remote_envelope_rejects_wrong_scope_before_credentials[change6]",
    "test_unknown_cost_rejected_and_budget_retains_uncertain_reservation",
    "test_actual_loopback_chat_and_separate_account_routing",
    "test_redirect_never_forwarded_and_error_is_redacted",
    "test_invalid_remote_endpoint_cannot_save_credentials[http://api.example.test/v1]",
    "test_invalid_remote_endpoint_cannot_save_credentials[https://user:password@example.com/v1]",
    "test_invalid_remote_endpoint_cannot_save_credentials[https://example.com/v1?api_key=secret]",
    "test_invalid_remote_endpoint_cannot_save_credentials[https://example.com/#token]",
    "test_invalid_remote_endpoint_cannot_save_credentials[https://127.0.0.1/v1]",
    "test_invalid_remote_endpoint_cannot_save_credentials[https://localhost/v1]",
    "test_invalid_remote_endpoint_cannot_save_credentials[https://example.com/%2Fescape]",
    "test_untrusted_response_tool_call_and_wrong_content_type_are_rejected",
    "test_response_bounds_and_pretransmission_cancel",
    "test_profiles_export_never_contains_secret_references_or_enabled_state",
    "test_credential_references_cannot_cross_accounts",
    "test_windows_vault_has_no_plaintext_fallback",
    "test_actual_local_planner_output_cannot_grant_tool_or_scope_permissions",
    "test_elevenlabs_templates_are_typed_and_locked_to_official_host",
    "test_untrusted_provider_response_cannot_echo_a_credential",
    "test_native_header_encoding_error_does_not_expose_secret",
    "test_authorized_remote_route_budget_and_unsafe_retry_with_local_mock",
)


def firewall_receipt(path):
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if value.get('status') != 'PASS' or value.get('allowed_test_peer') != '127.0.0.1':
        raise ValueError('The calling workflow did not attest the explicit loopback firewall boundary.')
    programs = value.get('blocked_programs')
    if (not isinstance(programs, list) or not programs
            or any(type(item) is not str for item in programs)):
        raise ValueError('The firewall evidence has no bounded executable inventory.')
    programs = {os.path.normcase(str(Path(item).resolve(strict=True))) for item in programs}
    selected = {os.path.normcase(str(Path(item).resolve(strict=True)))
                for item in (sys.executable, sys._base_executable)}
    if not selected.issubset(programs):
        raise ValueError('The firewall does not cover this interpreter and its base interpreter.')
    def ranges(rows):
        if not isinstance(rows, list) or len(rows) != 3:
            raise ValueError('The expected external-address firewall ranges are missing.')
        result = set()
        for row in rows:
            if type(row) is not str or row.count('-') != 1:
                raise ValueError('Invalid external-address firewall range.')
            first, last = row.split('-')
            result.add((str(ipaddress.ip_address(first)), str(ipaddress.ip_address(last))))
        return result
    expected = ranges(['0.0.0.1-126.255.255.255', '128.0.0.0-255.255.255.255',
                       '::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'])
    if ranges(value.get('blocked_remote_ranges')) != expected:
        raise ValueError('The firewall ranges differ from the reviewed loopback-only gate.')
    return {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest(),
            'selected_interpreters': sorted(selected),
            'boundary': 'calling workflow read back active process-scoped external-address blocks'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('source', 'installed'), required=True)
    parser.add_argument('--suite', choices=('translation', 'providers'), default='translation')
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--firewall-receipt', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    provider_suite = args.suite == 'providers'
    module_names = PROVIDER_MODULES if provider_suite else MODULES
    test_relative = PROVIDER_TEST if provider_suite else TEST
    required = PROVIDER_REQUIRED if provider_suite else REQUIRED
    case_count = len(required)
    test_module = Path(test_relative).stem
    report_prefix = 'provider-loopback-' if provider_suite else 'translation-loopback-'
    acceptance = 'exact 28 provider' if provider_suite else 'exact seven'
    transport = ('127.0.0.1 synthetic HTTP provider fixtures' if provider_suite
                 else '127.0.0.1 synthetic HTTP and isolated CLI subprocesses')
    case_label = 'local provider HTTP cases' if provider_suite else 'local translation HTTP/CLI cases'
    if os.name != 'nt':
        parser.error('This required gate uses the calling Windows process-level firewall.')
    source_root = args.source_root.resolve(strict=True)
    checkout = source_root.parent
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    boundary = firewall_receipt(args.firewall_receipt.resolve(strict=True))
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.environ['PYTEST_ADDOPTS'] = ''
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.dont_write_bytecode = True
    if args.mode == 'source':
        sys.path.insert(0, str(source_root))
    expected = {name: sha256((source_root / Path(*name.split('.')).with_suffix('.py')).read_bytes()).hexdigest()
                for name in module_names}
    modules = {name: importlib.import_module(name) for name in module_names}
    def origins():
        result = {}
        for name, module in modules.items():
            actual = Path(module.__file__).resolve(strict=True)
            selected = source_root / Path(*name.split('.')).with_suffix('.py')
            digest = sha256(actual.read_bytes()).hexdigest()
            if digest != expected[name] or sha256(selected.read_bytes()).hexdigest() != expected[name]:
                raise AssertionError('The exact selected module bytes changed: ' + name)
            if args.mode == 'source':
                if actual != selected.resolve(strict=True):
                    raise AssertionError('A source gate imported another package: ' + name)
            elif (actual.is_relative_to(checkout) or 'site-packages' not in actual.parts
                  or not actual.is_relative_to(Path(sys.prefix).resolve())):
                raise AssertionError('The installed gate imported a checkout module: ' + name)
            result[name] = {'path': str(actual), 'sha256': digest}
        return result
    before_origins = origins()
    import pytest
    class Strict:
        expected_failure = False
        selected = ()
        def pytest_collection_modifyitems(self, items):
            self.selected = tuple(item.nodeid.rsplit('::', 1)[-1] for item in items)
            if Counter(self.selected) != Counter(required):
                raise pytest.UsageError('The required ' + acceptance + ' local HTTP cases were not collected.')
        def pytest_runtest_logreport(self, report):
            if hasattr(report, 'wasxfail'):
                self.expected_failure = True
            if report.failed:
                print('\nIMMEDIATE_FAILURE ' + report.nodeid + ' [' + report.when + ']', flush=True)
                print(report.longreprtext, flush=True)
    policy = Strict()
    xml = output / (report_prefix + args.mode + '.xml')
    receipt_path = output / (report_prefix + args.mode + '.json')
    test_source = checkout / test_relative
    test_sha = sha256(test_source.read_bytes()).hexdigest()
    with TemporaryDirectory(prefix='carr-translation-loopback-') as temporary:
        root = Path(temporary)
        selected_test = root / test_relative
        selected_test.parent.mkdir(parents=True)
        shutil.copyfile(test_source, selected_test)
        if sha256(selected_test.read_bytes()).hexdigest() != test_sha:
            raise AssertionError('The staged loopback tests changed.')
        # Reuse the exact reviewed timed diagnostic without executing its child policy.
        from hashlib import sha1 as _ci_diagnostic_sha1
        import stat as _ci_diagnostic_stat
        _ci_diagnostic_path = checkout / 'scripts/ci_check.py'
        _ci_diagnostic_info = _ci_diagnostic_path.lstat()
        assert (_ci_diagnostic_stat.S_ISREG(_ci_diagnostic_info.st_mode)
                and _ci_diagnostic_info.st_nlink == 1
                and not getattr(_ci_diagnostic_info, 'st_file_attributes', 0) & 0x400)
        _ci_diagnostic_raw = _ci_diagnostic_path.read_bytes()
        assert _ci_diagnostic_sha1(b'blob ' + str(len(_ci_diagnostic_raw)).encode('ascii') + b'\0' + _ci_diagnostic_raw).hexdigest() == '6ad3055cfcd7c6a1b1af8544ae462c5d51adb473', 'Reviewed pytest diagnostic source differs'
        _ci_diagnostic_scope = {'__name__': '_carr_direct_pytest_diagnostics', '__file__': str(_ci_diagnostic_path)}
        exec(compile(_ci_diagnostic_raw, str(_ci_diagnostic_path), 'exec'), _ci_diagnostic_scope)
        _ci_diagnostic_plugins = _ci_diagnostic_scope['pytest_diagnostic_plugins']()
        result = pytest.main(['-v', '-rA', '-c', os.devnull,
            '--rootdir=' + str(root), '--confcutdir=' + str(root),
            '--import-mode=importlib', '-o', 'addopts=', '-o', 'pythonpath=',
            '-o', 'xfail_strict=true', '-o', 'faulthandler_timeout=120',
            '--junitxml=' + str(xml), str(selected_test)], plugins=[policy, *_ci_diagnostic_plugins])
        assert _ci_diagnostic_path.read_bytes() == _ci_diagnostic_raw, 'Reviewed pytest diagnostic source changed during tests'
        _ci_diagnostic_after = _ci_diagnostic_path.lstat()
        assert (_ci_diagnostic_after.st_dev, _ci_diagnostic_after.st_ino, _ci_diagnostic_after.st_size, _ci_diagnostic_after.st_mtime_ns) == (_ci_diagnostic_info.st_dev, _ci_diagnostic_info.st_ino, _ci_diagnostic_info.st_size, _ci_diagnostic_info.st_mtime_ns), 'Reviewed pytest diagnostic identity changed during tests'
        assert _ci_diagnostic_after.st_nlink == 1 and not getattr(_ci_diagnostic_after, 'st_file_attributes', 0) & 0x400
        print(json.dumps({'direct_pytest_diagnostic_source': '6ad3055cfcd7c6a1b1af8544ae462c5d51adb473', 'selected_plugins': len(_ci_diagnostic_plugins)}, sort_keys=True), flush=True)
    after_origins = origins()
    if before_origins != after_origins:
        raise AssertionError('The package origins changed during actual HTTP/CLI execution.')
    if sha256(test_source.read_bytes()).hexdigest() != test_sha:
        raise AssertionError('The checked-out tests changed during execution.')
    cases = list(ET.parse(xml).iter('testcase')) if xml.is_file() else []
    names = tuple(case.get('name') for case in cases)
    valid = (int(result) == 0 and not policy.expected_failure
        and Counter(policy.selected) == Counter(required)
        and Counter(names) == Counter(required) and len(cases) == case_count
        and all(case.get('classname', '').rsplit('.', 1)[-1] == test_module
                and not any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped'))
                for case in cases))
    receipt = {'status': 'PASS' if valid else 'FAIL', 'mode': args.mode, 'suite': args.suite,
        'cases': len(cases), 'required': list(required), 'pytest_exit': int(result),
        'xfail_or_xpass': policy.expected_failure, 'source_origins': after_origins,
        'test_sha256': test_sha, 'junit_sha256': sha256(xml.read_bytes()).hexdigest() if xml.is_file() else None,
        'firewall': boundary, 'actual_transport': transport,
        'actual_local_model_inference': 'NOT_RUN', 'quality_or_135_language_qualification': 'NOT_RUN'}
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False), flush=True)
    if not valid:
        return 1
    print('PASS: ' + acceptance + ' ' + args.mode + ' ' + case_label + '; no skips/XFAIL/XPASS', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
