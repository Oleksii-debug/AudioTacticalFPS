# Explicit clipboard reading

Choose **Voice → Read clipboard…** to open one snapshot of the current Unicode
text in the existing Voice reader. Select an installed voice and press **F6**
or **Read selection / all** when ready to listen. Opening the copy does not start
speech, install a clipboard watcher, or react to later clipboard changes.

The action accepts nonempty valid Unicode text up to the existing reader limit
of 2,000,000 characters. It preserves whitespace and line endings in the retained
source text. Empty, unavailable, unsupported, over-limit, NUL-containing or invalid
Unicode input reports an error before opening the reader. The clipboard is held
only while retrieving the snapshot and is closed before the reader opens. The
action never clears, replaces or flushes clipboard contents. The size limit is
an admission bound on retrieved text; it is not a claim that the operating
system's clipboard transfer itself streams within that bound.

The copy is independent text, with no retained-library quotation authority.
Its recovery uses the existing `ReaderDraftStore` under the separate `clipboard`
slot, leaving the manual reading draft and book-specific drafts intact. Source
preservation, edits, Save As, pronunciation review, explicit voice selection,
cancellation and playback remain the existing reader workflows.

`tests/speech/test_clipboard_reader.py` exercises actual menu dispatch, Unicode
validation, clipboard ownership/error handling and real draft isolation through
native API stand-ins. Actual Windows clipboard/device/keyboard and human NVDA
acceptance remain separate. API usage follows the official
[wx.Clipboard](https://docs.wxpython.org/wx.Clipboard.html) and
[wx.TextDataObject](https://docs.wxpython.org/wx.TextDataObject.html) documentation,
reviewed 2026-11-27.
