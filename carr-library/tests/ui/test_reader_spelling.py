"""Spelling review uses the actual reading-copy history and shared edit transaction."""
from hashlib import sha256
from types import SimpleNamespace
import threading

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.reading_tools import ReadingSnapshot
from carr_library.speech.spelling import SpellingIssue, SpellingPreview
from carr_library.ui.reader_spelling import ReaderSpellingController


class Host:
    def __init__(self, text='😀 wrng word'):
        self.document = ReadingDocument(text)
        self.range = (0, 0)
        self.events = []
        self.pending = None
        self.working = self.stopped = self.dead = self.revoked = False
        self.gui_thread = threading.get_ident()

    def _gui(self):
        assert threading.get_ident() == self.gui_thread, 'Native edit callback used in worker'

    def guard(self):
        self._gui()
        if self.revoked or self.dead:
            raise ValueError('Current source access was revoked.')

    def capture(self):
        self.guard()
        return ReadingSnapshot.capture(self.document)

    def selection(self):
        self._gui()
        return self.range

    def select(self, start, end):
        self._gui()
        self.range = (start, end)

    def verify(self, snapshot):
        self.guard()
        snapshot.check(self.document)

    def apply(self, snapshot, start, end, replacement, *, label):
        self.verify(snapshot)
        self.events.append(('apply', start, end))
        return self.document.replace(start, end, replacement, label=label)

    def submit(self, work, done, *, on_error=None):
        self._gui()
        if self.working:
            return False
        self.working = True
        self.stopped = False
        self.pending = work, done, on_error
        return True

    def finish(self, *, threaded=False):
        work, done, error = self.pending
        self.pending = None
        box = []

        def execute():
            try:
                box.append((True, work()))
            except Exception as exc:
                box.append((False, exc))

        if threaded:
            thread = threading.Thread(target=execute)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive()
        else:
            execute()
        self.working = False
        if box[0][0]:
            done(box[0][1])
        else:
            error(box[0][1])

    def report(self, text, *, focus=False):
        self._gui()
        self.events.append(('report', text))

    def alive(self):
        return not self.dead

    def busy(self):
        return self.working

    def cancelled(self):
        return self.stopped

    def voice(self):
        pytest.fail('A voice selection must never implicitly choose a spelling dictionary.')


class Service:
    def __init__(self):
        self.calls = []
        self.languages_result = ('en-US', 'uk-UA')
        self.bad_hash = False

    def languages(self, *, cancelled):
        if cancelled():
            raise InterruptedError('Cancelled language enumeration')
        self.calls.append(('languages',))
        return self.languages_result

    def check(self, text, language, *, cancelled):
        if cancelled():
            raise InterruptedError('Cancelled check')
        self.calls.append(('check', text, language))
        start = text.find('wrng')
        issues = () if start < 0 else (SpellingIssue(start, start + 4, 'wrng', 1, ('wrong',)),)
        return SpellingPreview(language,
            '0' * 64 if self.bad_hash else sha256(text.encode()).hexdigest(), issues)


def ready(text='😀 wrng word'):
    host, service = Host(text), Service()
    controller = ReaderSpellingController(host, service=service)
    assert controller.load_languages()
    assert controller.languages == ()
    host.finish(threaded=True)
    return host, service, controller


def checked(text='😀 wrng word'):
    host, service, controller = ready(text)
    assert controller.check('en-US')
    host.finish(threaded=True)
    assert controller.result is not None
    return host, service, controller


def test_check_is_async_explicit_and_never_edits_until_reviewed_acceptance():
    host, service, controller = ready()
    assert controller.languages == ('en-US', 'uk-UA')
    assert controller.check('uk-UA')
    assert service.calls == [('languages',)] and controller.result is None
    host.finish(threaded=True)
    assert service.calls[-1] == ('check', '😀 wrng word', 'uk-UA')
    preview = controller.preview(0, 'wrong')
    assert preview.start == 2 and preview.end == 6
    assert host.document.text == host.document.source_text == '😀 wrng word'
    assert controller.apply()
    assert host.document.text == '😀 wrong word'
    assert host.document.source_text == '😀 wrng word'
    assert host.document.source_sha256 == sha256('😀 wrng word'.encode()).hexdigest()
    assert host.document.source_span(2, 7).exact is False
    assert controller.result is None and controller.session.preview is None
    assert host.document.undo() and host.document.text == '😀 wrng word'


def test_selected_passage_offsets_are_applied_to_the_correct_document_location():
    host, service, controller = ready('Keep wrng untouched.\n😀 wrng end')
    first = host.document.text.rindex('wrng')
    host.range = (first, first + 4)
    assert controller.check('en-US')
    host.finish(threaded=True)
    assert service.calls[-1] == ('check', 'wrng', 'en-US')
    preview = controller.preview(0, 'wrong')
    assert preview.start == first and host.range == (first, first + 4)
    controller.apply()
    assert host.document.text == 'Keep wrng untouched.\n😀 wrong end'


@pytest.mark.parametrize('language', ['', 'auto', 'en-GB', None])
def test_no_dictionary_fallback_or_implicit_voice_language(language):
    host, service, controller = ready()
    with pytest.raises(ValueError):
        controller.check(language)
    assert service.calls == [('languages',)] and host.pending is None


@pytest.mark.parametrize('boundary', ['close', 'cancel', 'document-change', 'source-revoked', 'parent-close'])
def test_delayed_completion_cannot_publish_a_stale_or_cancelled_preview(boundary):
    host, service, controller = ready()
    assert controller.check('en-US')
    if boundary == 'close':
        controller.close()
    elif boundary == 'cancel':
        controller.cancel()
    elif boundary == 'document-change':
        host.document.replace(0, 1, 'Changed')
    elif boundary == 'source-revoked':
        host.revoked = True
    else:
        host.dead = True
    before = host.document.text
    host.finish(threaded=True)
    assert controller.result is None and controller.session.preview is None
    assert host.document.text == before
    assert not any(row[0] == 'apply' for row in host.events)


@pytest.mark.parametrize('boundary', ['document-change', 'source-revoked', 'cancel', 'parent-close', 'busy'])
def test_acceptance_rechecks_current_source_and_exact_document_after_preview(boundary):
    host, service, controller = checked()
    controller.preview(0, 'wrong')
    if boundary == 'document-change':
        host.document.replace(0, 1, 'Changed')
    elif boundary == 'source-revoked':
        host.revoked = True
    elif boundary == 'cancel':
        host.stopped = True
    elif boundary == 'parent-close':
        host.dead = True
    else:
        host.working = True
    before = host.document.text
    with pytest.raises((ValueError, InterruptedError)):
        controller.apply()
    assert host.document.text == before
    assert not any(row[0] == 'apply' for row in host.events)


def test_arbitrary_unoffered_replacement_cannot_be_smuggled_through_spelling():
    host, service, controller = checked()
    with pytest.raises(ValueError, match='suggestions'):
        controller.preview(0, 'unrelated rewrite')
    assert host.document.text == host.document.source_text


def test_changed_language_invalidates_existing_preview():
    host, service, controller = checked()
    controller.preview(0, 'wrong')
    controller.invalidate()
    with pytest.raises(ValueError):
        controller.apply()
    assert host.document.text == host.document.source_text


def test_foreign_result_hash_never_becomes_an_applicable_preview():
    host, service, controller = ready()
    service.bad_hash = True
    assert controller.check('en-US')
    host.finish()
    assert controller.result is None
    assert any('does not match' in row[1] for row in host.events if row[0] == 'report')


def test_large_document_requires_a_bounded_explicit_selection():
    host, service, controller = ready('wrng ' * 12000)
    assert controller.check('en-US')
    host.finish()
    assert service.calls == [('languages',)] and controller.result is None
    host.range = (0, 4)
    assert controller.check('en-US')
    host.finish()
    assert service.calls[-1] == ('check', 'wrng', 'en-US')


def test_empty_dictionary_inventory_is_a_reviewable_unavailable_state():
    host, service = Host(), Service()
    service.languages_result = ()
    controller = ReaderSpellingController(host, service=service)
    controller.load_languages()
    host.finish()
    assert controller.languages == ()
    with pytest.raises(ValueError):
        controller.check('en-US')
    assert any('No installed' in row[1] for row in host.events if row[0] == 'report')
