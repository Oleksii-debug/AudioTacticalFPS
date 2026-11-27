"""Actual explicit-runtime selected-format acceptance; no optional successful skip.

Run formats and old-suite separately for source and installed packages. The
calling workflow acquires the exact public lock first, reads back external
firewall rules for Python AND every native converter executable, and retains
JSON/text/JUnit only. This script downloads nothing and changes no policy.
"""
import argparse
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import threading
import time

OLD_COUNTS = {"test_exact_unicode_txt_and_source_hash":1,"test_decode_requires_explicit_legacy_encoding":1,"test_html_scripts_frames_styles_entities_and_alt_are_safe":1,"test_eml_chooses_body_and_never_reads_attachment":1,"test_real_docx_reuses_reviewed_parser":1,"test_pdf_physical_page_positions_are_real_and_unverified":1,"test_safe_opendocument_text_spreadsheet_and_slides":3,"test_actual_xlsx_cached_cells_do_not_evaluate_formulas":1,"test_presentation_order_uses_relationship_order":1,"test_fictionbook_text_retains_unicode":2,"test_existing_epub_spine_reader_is_reused":1,"test_archives_reject_traversal_entities_and_active_code":1,"test_cancellation_budget_and_no_overwrite_keep_sources":1,"test_unverified_legacy_formats_are_not_falsely_supported":9,"test_read_and_write_scope_cannot_escape_or_follow_links":1,"test_actual_installed_pandoc_rtf_or_explicit_unavailable":1,"test_actual_pandoc_does_not_fetch_iframe_or_execute_filters":1,"test_actual_local_image_ocr_requires_selected_languages":1,"test_image_worker_bounds_all_tiff_frames_before_ocr":3,"test_provenance_publication_failure_rolls_back_new_derivative":1,"test_source_change_between_snapshot_and_publication_is_rejected":1}
OLD_CHILD = "\nfrom collections import Counter\nfrom hashlib import sha256\nimport importlib\nimport ipaddress\nimport json\nimport os\nfrom pathlib import Path\nimport socket\nimport sys\nimport xml.etree.ElementTree as ET\n\nmode, source, staged, xml_path, expected_json, helper_path, helper_sha, receipt_path = sys.argv[1:]\nif mode == 'source':\n    sys.path.insert(0, source)\nos.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'\nos.environ['PYTEST_ADDOPTS'] = ''\nos.environ['PYTHONDONTWRITEBYTECODE'] = '1'\nsys.dont_write_bytecode = True\nimport importlib.util\nhelper_path = Path(helper_path).resolve(strict=True)\nif sha256(helper_path.read_bytes()).hexdigest() != helper_sha:\n    raise AssertionError('The native whole-suite origin helper changed before the child imported it.')\nspec = importlib.util.spec_from_file_location('_actual_old_conversion_origin_helper', helper_path)\norigin_helper = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(origin_helper)\nbefore_origins = origin_helper.origins(Path(source), mode)\n# Keep the old suite's real 127.0.0.1 server, deny every other socket peer.\n# Native executables remain covered by the caller's read-back firewall.\nreal_connect, real_connect_ex, real_bind = socket.socket.connect, socket.socket.connect_ex, socket.socket.bind\nreal_getaddrinfo = socket.getaddrinfo\ndef local(address):\n    if not isinstance(address, tuple) or len(address) < 2 or address[0] != '127.0.0.1':\n        raise OSError('Only the explicit conversion loopback fixture is admitted.')\n    return address\ndef connect(sock, address):\n    return real_connect(sock, local(address))\ndef connect_ex(sock, address):\n    return real_connect_ex(sock, local(address))\ndef bind(sock, address):\n    return real_bind(sock, local(address))\ndef getaddrinfo(host, *args, **kwargs):\n    if host != '127.0.0.1':\n        raise OSError('Conversion qualification does not admit DNS.')\n    return real_getaddrinfo(host, *args, **kwargs)\nsocket.socket.connect, socket.socket.connect_ex, socket.socket.bind = connect, connect_ex, bind\nsocket.getaddrinfo = getaddrinfo\nexpected = json.loads(expected_json)\nimport pytest\nclass Strict:\n    selected = ()\n    expected_failure = False\n    def pytest_collection_modifyitems(self, items):\n        self.selected = tuple(item.nodeid.rsplit('::', 1)[-1] for item in items)\n        if Counter(name.split('[', 1)[0] for name in self.selected) != Counter(expected):\n            raise pytest.UsageError('The entire old conversion suite and its exact cases are required.')\n    def pytest_runtest_logreport(self, report):\n        if hasattr(report, 'wasxfail'):\n            self.expected_failure = True\n        if report.failed:\n            print('\\nIMMEDIATE_FAILURE ' + report.nodeid + ' [' + report.when + ']', flush=True)\n            print(report.longreprtext, flush=True)\npolicy = Strict()\nroot = Path(staged).parent.parent\nresult = pytest.main(['-vv', '-rA', '-c', os.devnull, '--rootdir=' + str(root),\n    '--confcutdir=' + str(root), '--import-mode=importlib', '-o', 'addopts=',\n    '-o', 'pythonpath=', '-o', 'xfail_strict=true', '--junitxml=' + xml_path,\n    staged], plugins=[policy])\nafter_origins = origin_helper.origins(Path(source), mode)\nif before_origins != after_origins or sha256(helper_path.read_bytes()).hexdigest() != helper_sha:\n    raise AssertionError('The actual pytest child changed its imported origins or helper.')\ncases = list(ET.parse(xml_path).iter('testcase')) if Path(xml_path).is_file() else []\nactual = Counter(case.get('name', '').split('[', 1)[0] for case in cases)\nvalid = (int(result) == 0 and not policy.expected_failure\n    and actual == Counter(expected) and len(cases) == sum(expected.values())\n    and all(case.get('classname', '').rsplit('.', 1)[-1] == 'test_conversion'\n        and not any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped')) for case in cases))\nPath(receipt_path).write_text(json.dumps({'status': 'PASS' if valid else 'FAIL',\n    'mode': mode, 'before': before_origins, 'after': after_origins,\n    'helper_sha256': helper_sha}, sort_keys=True) + '\\n', encoding='utf-8')\nprint(json.dumps({'old_conversion_status': 'PASS' if valid else 'FAIL',\n    'cases': len(cases), 'required': expected, 'xfail_or_xpass': policy.expected_failure}), flush=True)\nraise SystemExit(0 if valid else 1)\n"
REQUIRED_FORMATS = frozenset(('.azw', '.azw3', '.chm', '.djvu', '.doc', '.lit',
    '.mobi', '.pdb', '.prc', '.ppt', '.tcr', '.wpd', '.xls', '.rtf'))


def support(source_root):
    path = source_root.parent / 'scripts' / 'selected_conversion_fixture_support.py'
    spec = importlib.util.spec_from_file_location('_selected_fixture_support', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


def formats(args, fixture, report, children):
    from carr_library.application import LibraryApp
    from carr_library.conversion import ConversionService
    from carr_library.conversion.selected import import_result, read_result
    from carr_library.operations import ResourceBroker
    from carr_library.conversion.models import ConversionError
    with TemporaryDirectory(prefix='carr-selected-native-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Selected native reading copies')
        rows, versions = fixture.prepare(root / 'inputs', library, args.calibre,
            args.office, args.pandoc, args.downloads)
        report['actual_versions'] = versions
        observed = set()
        evidence = []
        for number, (source, needles, classification) in enumerate(rows, 1):
            raw = source.read_bytes()
            output = root / ('reading-' + str(number))
            output.mkdir()
            service = ConversionService(output, input_roots=(source,),
                calibre=args.calibre, office=args.office, pandoc=args.pandoc,
                state_root=library.root / 'state')
            started = time.monotonic()
            result = service.extract(source, 'reading.txt')
            text = read_result(result)
            if not all(needle in text for needle in needles):
                raise AssertionError('Actual selected converter lost the required source text: ' + source.suffix)
            if (source.read_bytes() != raw or result.original_path.read_bytes() != raw
                    or result.report['source_sha256'] != sha256(raw).hexdigest()
                    or result.report['text_sha256'] != sha256(result.path.read_bytes()).hexdigest()
                    or result.report['source_format'] != source.suffix
                    or result.report['original_modified'] is not False
                    or result.report['verified_for_quotation'] is not False
                    or result.report['printed_pages_reconstructed'] is not False):
                raise AssertionError('Actual native conversion did not preserve exact original/receipt identity.')
            if {path.name for path in result.path.parent.iterdir()} != {
                    'reading.txt', 'original' + source.suffix, 'provenance.json'}:
                raise AssertionError('The committed reading folder is not the exact three-file result.')
            for item in result.report['sections']:
                if item['physical_index'] is not None or item['printed_label'] is not None or item['status'] != 'review_required':
                    raise AssertionError('An unverified native source gained physical/quotation authority.')
            before = {p.name: p.read_bytes() for p in result.path.parent.iterdir()}
            try:
                service.extract(source, 'reading.txt')
            except FileExistsError:
                pass
            else:
                raise AssertionError('An existing conversion folder was overwritten or adopted.')
            if {p.name: p.read_bytes() for p in result.path.parent.iterdir()} != before:
                raise AssertionError('The no-clobber retry altered a committed reading folder.')
            actual = import_result(result, library, workspace)
            if len(actual) != 1 or actual[0]['state'] != 'complete':
                raise AssertionError('The real canonical reading-copy import did not complete.')
            evidence.append({'format': source.suffix, 'classification': classification,
                'source_bytes': len(raw), 'source_sha256': sha256(raw).hexdigest(),
                'reading_sha256': sha256(result.path.read_bytes()).hexdigest(),
                'report_sha256': sha256(result.report_path.read_bytes()).hexdigest(),
                'conversion_seconds': time.monotonic() - started,
                'engine': result.report['engine'], 'verified_copy_read': 'PASS',
                'canonical_text_import': 'PASS', 'no_clobber': 'PASS'})
            observed.add(source.suffix)
            print('PASS actual selected format ' + source.suffix, flush=True)
        if observed != REQUIRED_FORMATS or len(evidence) != 14:
            raise AssertionError('Every selected format must complete without skips.')
        # Observe a real native descendant in the existing Job, then cancel it.
        # No synthetic runner or fallback kill may satisfy this assertion.
        source = next(path for path, _, _ in rows if path.suffix == '.chm')
        cancelled_output = root / 'cancelled-native'
        cancelled_output.mkdir()
        cancel_service = ConversionService(cancelled_output, input_roots=(source,),
            calibre=args.calibre, state_root=library.root / 'state')
        start_count = len(children)
        saw_descendant = []
        def cancel_actual_descendant():
            for child in children[start_count:]:
                if child._job is not None and child._active_processes() >= 2:
                    saw_descendant.append(True)
                    return True
            return bool(saw_descendant)
        try:
            cancel_service.extract(source, 'cancelled.txt', cancelled=cancel_actual_descendant)
        except InterruptedError:
            pass
        else:
            raise AssertionError('The actual native descendant did not reach the cancellation observation.')
        if (not saw_descendant or len(children) != start_count + 1
                or any(child.poll() is None or child._job is not None for child in children[start_count:])
                or list(cancelled_output.iterdir())):
            raise AssertionError('Actual native cancellation returned before Job/staging settlement.')
        report['actual_native_descendant_cancel'] = 'PASS'
        broker = ResourceBroker.for_app(library.root)
        if broker.snapshot()['leases']:
            raise AssertionError('Actual native conversion left shared admission active.')
        if any(path.name.startswith('.pending-conversion-') for path in root.rglob('*')):
            raise AssertionError('Actual native conversion left owned staging.')
        library.check()
        retained_hashes = {sha256(path.read_bytes()).hexdigest()
                           for path in library.assets_dir.rglob('*') if path.is_file()}
        if not {row['reading_sha256'] for row in evidence} <= retained_hashes:
            raise AssertionError('The canonical importer did not retain every actual reading-text byte snapshot.')
        report.update(formats=evidence, required_format_count=14, actual_format_conversions='PASS',
                      canonical_reading_copy_import='PASS', source_originals_unchanged='PASS',
                      native_children_and_leases_settled='PASS')
    report['owned_temporary_cleanup'] = 'PASS'


def old_suite(args, fixture, report, children):
    from carr_library.audio.models import AudioBudget, AudioCleanupPending
    from carr_library.audio.process import run_process
    from carr_library.application import LibraryApp
    from carr_library.operations import ResourceBroker, ResourceRequest
    original = args.source_root.parent / 'tests/conversion/test_conversion.py'
    companion = original.parent / 'fixtures/ordered_slides.pptx'
    expected = {str(path.relative_to(args.source_root.parent)): sha256(path.read_bytes()).hexdigest()
                for path in (original, companion)}
    with TemporaryDirectory(prefix='carr-old-conversion-gate-') as temporary:
        root = Path(temporary)
        test = root / 'tests/conversion/test_conversion.py'
        test.parent.mkdir(parents=True)
        target_companion = test.parent / 'fixtures/ordered_slides.pptx'
        target_companion.parent.mkdir()
        shutil.copyfile(original, test)
        shutil.copyfile(companion, target_companion)
        for path in (test, target_companion):
            if sha256(path.read_bytes()).hexdigest() != expected[str(path.relative_to(root))]:
                raise AssertionError('The old whole conversion suite or binary fixture changed during staging.')
        library = LibraryApp(root / 'admission')
        broker = ResourceBroker.for_app(library.root)
        xml = args.output / ('old-conversion-' + args.mode + '.xml')
        child_receipt = args.output / ('old-conversion-child-origins-' + args.mode + '.json')
        helper_path = args.source_root.parent / 'scripts/selected_conversion_fixture_support.py'
        helper_sha = sha256(helper_path.read_bytes()).hexdigest()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                   PYTEST_ADDOPTS='', PYTHONPATH='', PYTHONUTF8='1')
        env['PATH'] = str(args.pandoc.parent) + os.pathsep + env.get('PATH', '')
        if Path(shutil.which('pandoc', path=env['PATH'])).resolve(strict=True) != args.pandoc:
            raise AssertionError('The old mandatory runtime test would discover another Pandoc.')
        request = ResourceRequest(cpu_slots=2, memory_bytes=3 * 1024**3,
                                  disk_bytes=512 * 1024**2, disk_path=root)
        with broker.reserve(request, operation_id='native-old-conversion-suite',
                            timeout=30, run_timeout=610) as lease:
            try:
                result = run_process([sys.executable, '-I', '-B', '-X', 'utf8', '-c', OLD_CHILD,
                    args.mode, str(args.source_root), str(test), str(xml),
                    json.dumps(OLD_COUNTS, sort_keys=True), str(helper_path), helper_sha,
                    str(child_receipt)], env=env, cwd=root,
                    owned_windows_job=True, allow_failure=True,
                    budget=AudioBudget(timeout=600, max_log_bytes=4 * 1024**2,
                        max_output_bytes=512 * 1024**2), watch=(root,))
            except AudioCleanupPending as error:
                lease.settle_cleanup(error)
                raise
        (args.output / ('old-conversion-' + args.mode + '.txt')).write_bytes(result.stdout + b'\n' + result.stderr)
        print(result.stdout.decode('utf-8', 'replace'), flush=True)
        print(result.stderr.decode('utf-8', 'replace'), flush=True)
        if result.returncode != 0:
            raise AssertionError('The entire old conversion suite failed actual selected-runtime qualification.')
        import xml.etree.ElementTree as ET
        from collections import Counter
        cases = list(ET.parse(xml).iter('testcase'))
        if (len(cases) != 34 or Counter(case.get('name', '').split('[', 1)[0] for case in cases) != Counter(OLD_COUNTS)
                or any(case.find(tag) is not None for case in cases for tag in ('failure', 'error', 'skipped'))):
            raise AssertionError('Old conversion JUnit is incomplete or failed.')
        from carr_library.backup_sidecars import read_regular
        child_proof = json.loads(read_regular(child_receipt, 64 * 1024))
        actual_origins = fixture.origins(args.source_root, args.mode)
        if (child_proof.get('status') != 'PASS' or child_proof.get('mode') != args.mode
                or child_proof.get('before') != actual_origins
                or child_proof.get('after') != actual_origins
                or child_proof.get('helper_sha256') != helper_sha):
            raise AssertionError('The actual old-suite pytest child did not retain exact package-origin proof.')
        report['old_child_source_origins'] = child_proof
        report['old_child_receipt_sha256'] = sha256(child_receipt.read_bytes()).hexdigest()
        if broker.snapshot()['leases']:
            raise AssertionError('Old whole-suite admission survived its owned child.')
        for relative, digest in expected.items():
            if sha256((args.source_root.parent / relative).read_bytes()).hexdigest() != digest:
                raise AssertionError('The original conversion suite changed during qualification.')
        report.update(old_whole_conversion_suite='PASS', old_cases=34, old_test_sha256=expected,
            old_junit_sha256=sha256(xml.read_bytes()).hexdigest(), actual_loopback_denial='PASS')
    report['owned_temporary_cleanup'] = 'PASS'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('formats', 'old-suite'), required=True)
    parser.add_argument('--mode', choices=('source', 'installed'), required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--calibre', type=Path, required=True)
    parser.add_argument('--office', type=Path, required=True)
    parser.add_argument('--pandoc', type=Path, required=True)
    parser.add_argument('--downloads', type=Path, required=True)
    parser.add_argument('--runtime-lock', type=Path, required=True)
    parser.add_argument('--firewall-receipt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Actual selected native converters require the owned Windows Job route.')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    for key in ('source_root', 'calibre', 'office', 'pandoc', 'downloads', 'runtime_lock', 'firewall_receipt'):
        setattr(args, key, getattr(args, key).resolve(strict=True))
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.mode == 'source':
        sys.path.insert(0, str(args.source_root))
    sys.dont_write_bytecode = True
    fixture, companion = support(args.source_root)
    companion_hash = sha256(companion.read_bytes()).hexdigest()
    report = {'status': 'FAIL', 'phase': args.phase, 'mode': args.mode,
        'actual_format_quality': 'NOT_RUN', 'native_catalog_fidelity': 'NOT_RUN',
        'drm_bypass': 'NOT_RUN', 'scanned_djvu_ocr': 'NOT_RUN', 'nvda': 'NOT_RUN',
        'qualification': 'Selected reading derivatives; exact originals remain external, imports retain TXT only.'}
    done = threading.Event()
    def watchdog():
        if not done.wait(1200):
            print('FAIL selected conversion native qualification exceeded its cleanup-inclusive 1200-second deadline.', flush=True)
            os._exit(124)
    threading.Thread(target=watchdog, name='selected-conversion-native-deadline', daemon=True).start()
    try:
        report['source_origins_before'] = fixture.origins(args.source_root, args.mode)
        _, report['external_inputs'] = fixture.load_lock(args.runtime_lock, args.downloads)
        programs = fixture.native_programs(args.calibre, args.office, args.pandoc)
        report['firewall'] = fixture.firewall(args.firewall_receipt, args.source_root, programs)
        from carr_library.audio import windows_job
        actual_job = windows_job.JobProcess
        children = []
        def observed_job(*values, **options):
            child = actual_job(*values, **options)
            children.append(child)
            return child
        windows_job.JobProcess = observed_job
        try:
            (formats if args.phase == 'formats' else old_suite)(args, fixture, report, children)
        finally:
            # Keep the actual factory installed until all operation/staging
            # cleanup has returned; restoring it earlier changes live workers.
            windows_job.JobProcess = actual_job
            report['actual_owned_jobs'] = len(children)
            if any(child.poll() is None or child._job is not None for child in children):
                raise AssertionError('Selected native qualification left an actual private Job or child alive.')
            report['all_owned_jobs_closed'] = 'PASS'
        report['source_origins_after'] = fixture.origins(args.source_root, args.mode)
        if report['source_origins_before'] != report['source_origins_after']:
            raise AssertionError('Selected native qualification changed imported package origins.')
        if sha256(companion.read_bytes()).hexdigest() != companion_hash:
            raise AssertionError('The source-bound fixture companion changed.')
        report['fixture_companion_sha256'] = companion_hash
        report['status'] = 'PASS'
    except Exception as error:
        report['error_type'] = type(error).__name__
        report['error'] = str(error)[:4000]
        import traceback
        traceback.print_exc()
    finally:
        done.set()
    path = args.output / ('selected-conversion-' + args.phase + '-' + args.mode + '.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
