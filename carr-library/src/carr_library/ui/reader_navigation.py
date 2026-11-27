"""Current and adjacent reading spans on the reader's existing worker."""
from ..speech.navigation import UNITS, unit_range
from .reading_tools import ReaderToolSession


class ReaderNavigation:
    """Select an authorized reading-copy span; explicit Read reuses its narrator."""
    def __init__(self, host, *, speak, focus=lambda: None):
        self.host, self.speak, self.focus = host, speak, focus
        self.session = ReaderToolSession(host, report=lambda message:
            host.report(message, focus=False))

    def navigate(self, unit, *, direction=0, read_aloud=False):
        if self.session.closed or not self.host.alive():
            return False
        if unit not in UNITS or type(read_aloud) is not bool:
            self.host.report('Choose word, logical line, sentence or paragraph navigation.')
            return False
        if self.session.busy or self.host.busy():
            self.host.report('Wait for the current reader operation or cancel it first.')
            return False
        try:
            self.host.guard()
            captured_selection = self.host.selection()
            if (type(captured_selection) is not tuple or len(captured_selection) != 2
                    or any(type(value) is not int for value in captured_selection)
                    or not 0 <= captured_selection[0] <= captured_selection[1]):
                raise ValueError('The native reading selection changed. Select it again.')
        except Exception as exc:
            self.host.report(str(exc), focus=False)
            return False

        def locate(snapshot, cancelled):
            if captured_selection[1] > len(snapshot.text):
                raise ValueError('The captured selection is outside the reading copy.')
            return unit_range(snapshot.text, captured_selection[0], unit,
                              direction=direction, cancelled=cancelled)

        def located(snapshot, span):
            if self.host.selection() != captured_selection:
                raise ValueError('The reading cursor moved. Request the current text unit again.')
            if span is None:
                self.host.report('No further ' + unit + ' in this direction. The selection is unchanged.')
                return
            if (type(span) is not tuple or len(span) != 2
                    or any(type(value) is not int for value in span)
                    or not 0 <= span[0] < span[1] <= len(snapshot.text)):
                raise ValueError('The reading-unit result is outside its captured copy.')
            self.host.select(*span)
            self.focus()
            # Selection/focus callbacks cannot transfer this request to another
            # tab, source, revision or cancellation lifetime.
            self.host.verify(snapshot)
            if (self.host.cancelled() or self.session.cancelled.is_set()
                    or not self.host.alive() or self.session.closed):
                raise InterruptedError('Reading-unit selection was cancelled before speech.')
            if self.host.selection() != span:
                raise ValueError('The selected text unit changed before speech. Select it again.')
            self.host.report(f'Selected {unit}, characters {span[0] + 1} to {span[1]}. '
                'Text boundaries are navigation aids; source quotations are unchanged.')
            if read_aloud:
                self.speak()

        return self.session.start(locate, located)

    def cancel(self):
        self.session.cancel()

    def close(self):
        self.session.close()
