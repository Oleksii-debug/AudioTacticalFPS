import ctypes, json, os, pathlib, sys
from ctypes import wintypes
import carr_library
from carr_library.native_runtime import activate_bundled_runtime
from carr_library.extract import ocr_runtime
root = pathlib.Path(sys.argv[1]).resolve()
assert pathlib.Path(carr_library.__file__).resolve().is_relative_to(root / '.venv')
assert pathlib.Path(ocr_runtime.__file__).resolve().is_relative_to(root / '.venv')
manifest = json.loads((root / 'bundle-manifest.json').read_text())
assert manifest['bundled_model'] and manifest['bundled_ocr'] and manifest['bundled_native_runtime']
# Test-only command discovery is scoped to this installed-package child.
# Production calls bind the verified runtime and never mutate PATH.
os.environ['PATH'] = str(root / 'ocr') + os.pathsep + os.environ.get('PATH', '')
os.environ['TESSDATA_PREFIX'] = str(root / 'ocr' / 'tessdata')
runtime_handle = activate_bundled_runtime(bundle_root=root)
assert runtime_handle is not None
import carr_library.acquisition.service, carr_library.acquisition.transport
import carr_library.acquisition.providers, carr_library.ui.acquisition_dialog
for component in (carr_library.acquisition.service, carr_library.acquisition.transport,
                  carr_library.acquisition.providers, carr_library.ui.acquisition_dialog):
    assert pathlib.Path(component.__file__).resolve().is_relative_to(root / '.venv')
import importlib.util
for name in (
        'carr_library.research.export_file',
        'carr_library.ui.catalog_tools_dialog',
        'carr_library.ui.academic_actions',
        'carr_library.ui.catalog_insights_actions',
        'carr_library.ui.desktop_app',
        'carr_library.ui.academic_library_dialog',
        'carr_library.ui.reading_desk',
        'carr_library.application',
        'carr_library.ingest.worker',
        'carr_library.ui.operation_progress',
        'carr_library.ui.speech_dialog',
        'carr_library.ui.studio_dialog',
        'carr_library.speech.progress_speech',
        'carr_library.studio.render',
        'carr_library.state_backup',
        'carr_library.pending_backup',
        'carr_library.library_backup',
        'carr_library.speech.reading',
        'carr_library.speech.source',
        'carr_library.speech.runtime',
        'carr_library.academic_integration.doi',
        'carr_library.speech.session',
        'carr_library.storage.revisions',
        'carr_library.storage.schema_identity',
):
    origin = importlib.util.find_spec(name).origin
    assert origin and pathlib.Path(origin).resolve().is_relative_to(root / '.venv'), name
print(json.dumps({'installed_package': True, 'source_commit': manifest['source_commit']}), flush=True)
import pytest
# Keep the shared workflow list intact while exercising the additive
# contents reader in this same installed-package/native-runtime process.
contents_suites = ['tests/reader/test_ebook_contents.py',
                  'tests/e2e/test_import_progress.py',
                  'tests/ingest/test_import_metadata_shape.py',
                  'tests/ui/test_operation_progress.py',
                  'tests/ui/test_progress_numeric_limits.py',
                  'tests/ui/test_progress_job_review.py',
                  'tests/ui/test_studio_progress_isolation.py',
                  'tests/ui/test_progress_media_quiet.py',
                  'tests/ui/test_progress_voice_actions.py',
                  'tests/storage/test_progress_preferences_backup.py',
                  'tests/e2e/test_epub_library.py', 'tests/e2e/test_epub_reading_desk.py',
                  'tests/acquisition/test_providers.py',
                  'tests/acquisition/test_acquisition_security.py',
                  'tests/acquisition/test_identifier_lookup_security.py',
                  'tests/acquisition/test_acquisition_workflow.py',
                  'tests/acquisition/test_acquisition_cli.py',
                  'tests/ui/test_native_acquisition.py',
                  'tests/reader/test_export_publication_boundary.py',
                  'tests/ui/test_catalog_tools_dialog.py',
                  'tests/ui/test_academic_export_handler.py',
                  'tests/ui/test_academic_actions.py',
                  'tests/ui/probe_catalog_insights_actions.py',
                  'tests/ui/test_desktop_handlers.py',
                  'tests/ui/test_academic_library_dialog.py',
                  'tests/ui/test_reading_desk_actions.py',
                  'tests/integrations/test_dordiai_voices.py',
                  'tests/integrations/test_dordiai_speech.py',
                  'tests/integrations/test_dordiai_settings.py',
                  'tests/integrations/test_dordiai_probe_cancellation.py',
                  'tests/integrations/test_dordiai_convergence.py',
                  'tests/speech/test_discovery_cancellation.py',
                  'tests/storage/test_state_snapshot_identity.py',
                  'tests/ingest/test_worker_publication_reconcile.py',
                  'tests/storage/test_v3_state_backup.py',
                  'tests/storage/test_pending_backup.py',
                  'tests/storage/test_complete_pending_backup.py',
                  'tests/storage/test_academic_migration.py',
                  'tests/storage/test_backup_schema_identity.py',
                  'tests/storage/test_atomic_state_publication.py',
                  'tests/e2e/test_import_worker_publication.py',
                  'tests/ingest/test_import_replay_identity.py',
                  'tests/speech/test_reading.py',
                  'tests/speech/test_source.py',
                  'tests/speech/test_session.py',
                  'tests/packaging/test_worker_bytecode.py',
                  'tests/extract/test_ocr_model_catalog.py',
                  'tests/extract/test_ocr_model_cli.py',
                  'tests/extract/test_ocr_model_store.py',
                  'tests/extract/test_managed_ocr.py',
                  'tests/e2e/test_managed_ocr_import.py',
                  'tests/extract/test_ocr_languages.py',
                  'tests/extract/test_ocr_runtime.py::test_unreadable_directory_rejects_entire_inventory',
                  'tests/extract/test_ocr_runtime.py::test_empty_directory_inventory_has_a_bound',
                  'tests/extract/test_ocr_runtime.py::test_directory_inventory_checks_budget_between_entries']
status = pytest.main([*sys.argv[2:], *contents_suites])
if status:
    raise SystemExit(status)
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel.GetModuleHandleW.restype = wintypes.HMODULE
kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
kernel.GetModuleFileNameW.restype = wintypes.DWORD
for name in ('msvcp140.dll', 'msvcp140_1.dll'):
    module = kernel.GetModuleHandleW(name)
    assert module, name + ' was not loaded by the native workflow'
    actual = ctypes.create_unicode_buffer(32768)
    length = kernel.GetModuleFileNameW(module, actual, len(actual))
    assert 0 < length < len(actual)
    assert pathlib.Path(actual.value).samefile(root / 'native-runtime' / 'bin' / name), name + ' loaded outside the private bundle'
print(json.dumps({'status': 'PASS', 'private_native_dll_paths': 'PASS'}), flush=True)
