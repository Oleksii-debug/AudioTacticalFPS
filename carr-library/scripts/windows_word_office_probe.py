"""Opt-in actual Office qualification on an already-open disposable document.

This is never a default CI requirement and never creates/opens/saves/quits Word.
An operator must prepare the exact plain marker document, keep it active and move
the caret when prompted between fully reaped sessions. No child or lease remains
held while waiting for that operator input.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
import threading

MARKER = 'Resonance Word bridge disposable qualification\r'
ORIGINS = (
    'carr_library.academic_integration.word_bridge', 'carr_library.academic_integration.word_worker',
    'carr_library.academic_integration.word_native', 'carr_library.academic_integration.word_records',
    'carr_library.academic_integration.formatting', 'carr_library.citations.bibliography_workbench',
    'carr_library.citations.csl', 'carr_library.audio.process', 'carr_library.audio.windows_job',
    'carr_library.audio.cleanup', 'carr_library.operations.broker',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--require-installed', action='store_true')
    parser.add_argument('--document-name', required=True)
    parser.add_argument('--style', choices=('apa', 'chicago-notes'), default='apa')
    parser.add_argument('--approve-disposable-edits', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    if os.name != 'nt' or not options.approve_disposable_edits or not sys.stdin.isatty():
        raise ValueError('Actual Windows, interactive operator input and explicit --approve-disposable-edits are required.')
    if options.output.exists():
        raise FileExistsError('Choose a new report file.')
    from carr_library.application import LibraryApp
    from carr_library.ui.academic_library_dialog import AcademicController
    from carr_library.citations.bibliography_workbench import BibliographyWorkbench
    from carr_library.academic_integration.word_bridge import WordBridge, WordDecision
    from carr_library.academic_integration.word_records import parse_json, fingerprint
    from carr_library.operations import ResourceBroker
    from carr_library.speech._io import new_output

    source_root = options.source_root.resolve(strict=True)
    installed = Path(os.environ.get('CARR_PROGRAM_DIR') or sysconfig.get_path('purelib')).resolve(strict=True)
    def origins():
        rows = {}
        for name in ORIGINS:
            actual = Path(importlib.import_module(name).__file__).resolve(strict=True)
            expected = source_root.joinpath(*name.split('.')).with_suffix('.py')
            digest = sha256(actual.read_bytes()).hexdigest()
            if digest != sha256(expected.read_bytes()).hexdigest():
                raise ValueError('Application source identity differs: ' + name)
            if options.require_installed and (not actual.is_relative_to(installed) or actual.is_relative_to(source_root)):
                raise ValueError('The Office probe did not import installed code: ' + name)
            rows[name] = {'path': str(actual), 'sha256': digest, 'installed': actual.is_relative_to(installed)}
        return rows
    before = origins()
    checks, completed, baseline = [], [], None
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS: ' + name, flush=True)

    with TemporaryDirectory(prefix='resonance-disposable-office-') as temporary:
        app = LibraryApp(Path(temporary) / 'library')
        workspace = app.create_workspace('Disposable Office qualification')
        controller = AcademicController(app, workspace)
        reference = controller.create(dict(title='Synthetic Office qualification reference',
            authors=('Smith, Jane',), year='2020', publisher='Example Press'))
        workbench = BibliographyWorkbench(controller)
        preview = workbench.preview(identity=reference.id, style=options.style, name_mode='family-given')
        original = controller.export(reference.id, format='csl-json', include_notes=False)
        service = WordBridge(workbench, preview)
        first = service.run(timeout=45)
        current = first.capture
        check('actual already-open Word attaches without opening or saving a document',
              first.status == 'INSPECTED' and current['document']['name'] == options.document_name
              and current['document']['text'] == MARKER and current['metadata'] is None
              and current['controls'] == [] and current['fields'] == []
              and current['endnotes'] == [] and current['legacy_parts'] == [])
        baseline = {key: current['document'][key] for key in ('hwnd', 'pid', 'creation_time', 'name', 'full_name', 'word_version')}
        outside = sha256(MARKER.encode('utf-8')).hexdigest()
        check('disposable document starts at its exact unprotected main-text caret',
              current['selection'] == {'start': 0, 'end': 0, 'story': 1}
              and not current['document']['readonly'] and current['document']['protection'] == -1
              and not current['document']['track_revisions'])
        last = [None]
        def apply(intent):
            decision = WordDecision()
            def approved(view):
                captured = parse_json(view._capture_json)
                if any(captured['document'][key] != value for key, value in baseline.items()):
                    raise ValueError('The already-open disposable Word identity changed.')
                if captured['document']['outside_main_sha256'] != outside:
                    raise ValueError('Unmanaged disposable text changed.')
                if last[0] is None:
                    if captured['metadata'] is not None or captured['document']['text'] != MARKER:
                        raise ValueError('This is no longer the original disposable document.')
                elif (captured['metadata'] is None
                      or fingerprint(captured['metadata']) != last[0]['metadata_sha256']):
                    raise ValueError('The exact preceding completed metadata changed.')
                # Consent is explicit in the command line and limited above to
                # this known disposable document and these three named changes.
                print('Applying reviewed disposable phase: ' + intent['action'], flush=True)
                decision.approve(view.token)
            outcome = service.run(intent, decision=decision, on_preview=approved, timeout=120)
            if outcome.status != 'COMPLETED':
                raise RuntimeError('Actual Word outcome is uncertain. Inspect Word; this probe will not retry.')
            last[0] = outcome.receipt
            completed.append(intent['action'])
            if ResourceBroker.for_app(app.root).snapshot()['leases']:
                raise RuntimeError('A Word resource owner remains live after the phase.')
            return outcome

        first_change = apply({'action': 'insert_citation', 'citation_id': None, 'items': [
            dict(reference_id=reference.id, locator=None, label='page', locator_verified=False,
                 prefix='', suffix='', suppress_author=False)]})
        citation_id = next(row['tag'].split(':')[-1] for row in first_change.receipt['bindings']
                           if row['tag'].startswith('resonance:cite:'))
        check('actual Word insertion returns an exact completed stable citation receipt',
              first_change.receipt['saved_by_bridge'] is False)
        edited = apply({'action': 'edit_citation', 'citation_id': citation_id, 'items': [
            dict(reference_id=reference.id, locator=None, label='page', locator_verified=False,
                 prefix='see ', suffix='', suppress_author=False)]})
        check('actual Word edit preserves the stable citation and document identity',
              edited.receipt['document_id'] == first_change.receipt['document_id']
              and any(row['tag'].endswith(citation_id) for row in edited.receipt['bindings']))
        # Every owned process has returned and its lease is released before
        # this operator pause. There is no held preview waiting on stdin.
        if ResourceBroker.for_app(app.root).snapshot()['leases']:
            raise RuntimeError('Cannot request operator input while resources remain held.')
        print('In the same disposable Word document, press Ctrl+End to place the caret after the marker text. '
              'Do not type, save, close or switch documents. Return here and press Enter.', flush=True)
        input()
        bibliography = apply({'action': 'regenerate_bibliography', 'citation_id': None, 'items': []})
        check('actual Word bibliography is a separate managed completed range',
              any(row['tag'].startswith('resonance:biblio:') for row in bibliography.receipt['bindings']))
        final = service.run(timeout=45)
        check('actual Word remains open with the original unmanaged text and latest complete metadata',
              final.status == 'INSPECTED' and final.capture['document']['outside_main_sha256'] == outside
              and fingerprint(final.capture['metadata']) == bibliography.receipt['metadata_sha256'])
        check('the source library and all native resource reservations remain unchanged or released',
              controller.export(reference.id, format='csl-json', include_notes=False) == original
              and ResourceBroker.for_app(app.root).snapshot()['leases'] == [])
    check('application module bytes remain unchanged after actual Office work', origins() == before)
    report = {'status': 'PASS', 'actual_word': 'EXECUTED', 'checks': checks, 'installed_modules': before,
        'office_version': baseline['word_version'], 'style': options.style, 'completed_phases': completed,
        'saved_or_closed_by_bridge': False, 'operator_steps': 'Required disposable document and caret placement',
        'manual_nvda': 'NOT_RUN', 'visual_layout_review': 'NOT_RUN', 'all_office_versions': 'NOT_QUALIFIED',
        'foreign_document_conversion': 'NOT_RUN'}
    with new_output(options.output) as target:
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    print('The disposable Word document is still open. This bridge did not request Save or Close. Review or discard it manually.', flush=True)
    return 0


if __name__ == '__main__':
    finished = threading.Event()
    def watchdog():
        if not finished.wait(600):
            os.write(2, b'Opt-in disposable Office probe exceeded its operator/work/cleanup deadline.\n')
            os._exit(86)
    threading.Thread(target=watchdog, daemon=True).start()
    native = None
    try:
        import faulthandler
        faulthandler.enable(all_threads=True)
        from carr_library.native_runtime import activate_bundled_runtime
        native = activate_bundled_runtime()
        raise SystemExit(main())
    finally:
        if native is not None:
            native.close()
        finished.set()
