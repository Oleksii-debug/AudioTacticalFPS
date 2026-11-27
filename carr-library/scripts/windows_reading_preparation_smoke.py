"""Actual Windows wx/Hunspell/optional normalization qualification on public fixtures."""
import argparse
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
import time
import traceback


def run(work, normalizer, hunspell):
    if os.name != 'nt':
        raise RuntimeError('This probe requires actual Windows; there is no simulated PASS.')
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader.narration import NarrationStore, apply_preparation, encode_project, new_project
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.preparation import PreparationPreview
    from carr_library.ui.narration_preparation import PreparationDialog
    from carr_library.ui.live_reading import native_offset

    work = Path(work).absolute()
    work.mkdir(mode=0o700)
    normalizer, hunspell = Path(normalizer).absolute(), Path(hunspell).absolute()
    checks, failures = [], []
    def check(name, condition):
        checks.append({'name': name, 'passed': bool(condition)})
        if not condition:
            raise AssertionError(name)

    check('actual_windows', os.name == 'nt')
    check('isolated_probe', bool(sys.flags.isolated))
    check('explicit_optional_manifests', normalizer.is_file() and hunspell.is_file())
    app = wx.App(False)
    library = LibraryApp(work / 'library')
    workspace = library.create_workspace('Public native preparation fixture')
    text = 'Original 😀 42\r\nhelo world.'
    book = library.add_text(workspace, 'Public fixture', text)
    page = ReadingDesk(library, workspace, book['edition']).page(1)
    project = new_project(page.raw_text, source_location=page.location)
    original_raw = project.original_text
    store = NarrationStore(library.root)
    store.save(project)
    deadline = time.monotonic() + 90

    def protect(window, action):
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError('Native preparation probe exceeded its owned deadline.')
            action()
        except Exception:
            failures.append(traceback.format_exc())
            window.close()

    def await_result(window, action):
        def poll():
            if time.monotonic() >= deadline:
                protect(window, lambda: (_ for _ in ()).throw(TimeoutError('Native worker did not settle.')))
            elif window.task.busy:
                wx.CallLater(30, poll)
            else:
                protect(window, action)
        wx.CallLater(30, poll)

    first, last = original_raw.index('42'), original_raw.index('42') + 2
    def ready_number(window):
        def begin():
            check('native_selected_readonly_text', window.selection.GetValue() == '42'
                  and not window.selection.IsEditable())
            check('named_language_control', bool(window.language.GetName()))
            check('number_mode_enabled', window.number.IsEnabled())
            check('irrelevant_unit_disabled', not window.unit.IsEnabled())
            window.normal_manifest.ChangeValue(str(normalizer))
            window.changed()
            window.normal_manifest.SetFocus()
            wx.YieldIfNeeded()
            check('native_keyboard_focus', wx.Window.FindFocus() is window.normal_manifest)
            check('explicit_runtime_enables_preview', window.buttons['normalize'].IsEnabled())
            window.normalize()
            check('owned_work_disables_apply', window.task.busy and not window.buttons['apply'].IsEnabled())
            def done():
                check('real_number_preview', type(window.preview) is PreparationPreview
                      and window.preview.record.after == 'forty-two')
                check('preview_source_unchanged', window.project.original_text == original_raw
                      and window.project.current_text == original_raw)
                check('preview_exact_input_digest', window.preview.record.input_sha256
                      == sha256(original_raw.encode('utf-8')).hexdigest())
                check('preview_reviewable_manifest_identity', window.preview.record.manifest_sha256
                      == sha256(normalizer.read_bytes()).hexdigest())
                window.apply()
            await_result(window, done)
        protect(window, begin)

    number = PreparationDialog(None, project, first, last)
    selected, rule = number.show(on_ready=ready_number)
    if failures:
        raise RuntimeError('\n'.join(failures))
    check('only_explicit_apply_returns_change', selected is not None and rule is None)
    project = apply_preparation(project, selected)
    check('number_apply_is_separate_revision', len(project.revisions) == 2 and project.original_text == original_raw)

    first, last = project.current_text.index('helo'), project.current_text.index('helo') + 4
    def ready_spelling(window):
        def begin():
            window.spell_manifest.ChangeValue(str(hunspell))
            window.changed()
            check('explicit_dictionary_enables_spellcheck', window.buttons['spell'].IsEnabled())
            window.spell()
            def done():
                check('actual_hunspell_native_result', window.report is not None
                      and len(window.report.issues) == 1 and window.report.issues[0].word == 'helo'
                      and 'hello' in window.report.issues[0].suggestions)
                check('named_occurrence_list', bool(window.issues.GetName()) and window.issues.GetCount() == 1)
                window.issues.SetSelection(0)
                window.selected_issue()
                window.suggestions.SetSelection(window.report.issues[0].suggestions.index('hello'))
                window.spelling_preview()
                check('exact_occurrence_preview', window.preview.record.start == first
                      and window.preview.record.end == last and window.preview.record.before == 'helo'
                      and window.preview.record.after == 'hello')
                check('native_review_before_spelling_apply', window.buttons['apply'].IsEnabled()
                      and window.project.current_text[first:last] == 'helo')
                window.apply()
            await_result(window, done)
        protect(window, begin)

    spelling = PreparationDialog(None, project, first, last)
    selected, rule = spelling.show(on_ready=ready_spelling)
    if failures:
        raise RuntimeError('\n'.join(failures))
    check('spelling_apply_returns_bound_change', selected is not None and rule is None)
    project = apply_preparation(project, selected)
    check('both_derivatives_keep_original', project.original_text == original_raw
          and project.current_text == original_raw.replace('42', 'forty-two').replace('helo', 'hello')
          and len(project.revisions) == 3)
    saved_before, digest = store.load(project.id)
    store.save(project, expected_digest=digest)
    reopened, _ = store.load(project.id)
    check('typed_v5_durable_roundtrip', reopened == project and json.loads(encode_project(reopened))['version'] == 5)
    check('saved_tool_and_dictionary_provenance', [row.preparation.engine for row in reopened.revisions[1:]]
          == ['reading-preparation', 'hunspell'])
    check('catalog_quotation_source_unchanged', ReadingDesk(library, workspace, book['edition']).page(1).raw_text
          == original_raw)

    # Real native Cancel/Close lifecycle, including cancellation while verification
    # or the optional worker owns the request. No partial callback is accepted.
    before_cancel = encode_project(project)
    def ready_cancel(window):
        def begin():
            window.normal_manifest.ChangeValue(str(normalizer))
            window.changed()
            window.normalize()
            window.close()
            check('close_requests_owned_cancellation', window.close_pending and window.task.cancel.is_set())
        protect(window, begin)
    cancel = PreparationDialog(None, project, 0, len('Original'))
    selected, rule = cancel.show(on_ready=ready_cancel)
    if failures:
        raise RuntimeError('\n'.join(failures))
    check('close_waits_without_partial_application', selected is None and rule is None and not cancel.task.busy)
    check('cancel_keeps_saved_history_exact', encode_project(project) == before_cancel
          and store.load(project.id)[0] == project)

    modules = ('carr_library.reader.preparation', 'carr_library.reader.preparation_worker',
               'carr_library.reader.narration', 'carr_library.ui.narration_preparation',
               'carr_library.ui.narration_editor', 'carr_library.audio.model_manifest')
    origins = {}
    for name in modules:
        module = importlib.import_module(name)
        path = Path(module.__file__).absolute()
        origins[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    app.Destroy()
    return dict(schema=1, status='PASS', platform=sys.platform, isolated=bool(sys.flags.isolated),
                checks=checks, module_origins=origins,
                optional_manifests={'normalizer': sha256(normalizer.read_bytes()).hexdigest(),
                                    'hunspell': sha256(hunspell.read_bytes()).hexdigest()},
                real_hunspell=True, real_num2words=True, real_babel=True,
                public_fixture=True, native_speaker_quality='NOT_RUN', nvda='NOT_RUN',
                languages_135='NOT_RUN', physical_speech_playback='NOT_RUN')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('work')
    parser.add_argument('--normalizer', required=True)
    parser.add_argument('--hunspell', required=True)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    try:
        if not args.native:
            raise ValueError('Explicit --native is required; no source-only native PASS exists.')
        report = run(args.work, args.normalizer, args.hunspell)
    except Exception as exc:
        print(json.dumps({'schema': 1, 'status': 'FAIL', 'platform': sys.platform,
                          'isolated': bool(sys.flags.isolated), 'error': type(exc).__name__ + ': ' + str(exc)},
                         ensure_ascii=False))
        raise
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
