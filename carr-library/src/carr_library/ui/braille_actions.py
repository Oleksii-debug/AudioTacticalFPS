"""Plain BRF export action on the existing reader menu and host worker."""
from pathlib import Path
import re

from ..integrations.liblouis import inspect_archive
from ..reader.braille import BrailleSourceExport
from ..reader.reading_desk import ReadingDesk
from .export_destination import confirm_export_destination


def _path(frame, *, archive):
    import wx
    dialog = wx.FileDialog(frame,
        "Select the reviewed Liblouis 3.39.0 Windows x64 archive" if archive else "Export a separate plain BRF",
        defaultFile="" if archive else "source.brf",
        wildcard="ZIP archive (*.zip)|*.zip" if archive else "Braille ASCII (*.brf)|*.brf",
        style=(wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) if archive else wx.FD_SAVE)
    try:
        return Path(dialog.GetPath()) if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def export_braille(actions, event=None):
    """Keep every modal return, queue and publication bound to the same source."""
    import wx
    frame = actions.frame
    try:
        scope = actions._scope()
        actions._guard(scope)
        panel = frame.book.GetPage(1) if frame.book is not None else None
        if panel is None or panel.workspace != scope.workspace or not panel.ready:
            raise ValueError("Select a book in Books and Collections before exporting BRF.")
        edition = panel._selection()[2]
        if edition is None or edition not in panel.rows:
            raise ValueError("Select one current book in Books and Collections.")
        # Capture the root identity before any modal or delayed callback.
        service = BrailleSourceExport(scope.library, guard=lambda: actions._guard(scope))

        def current():
            service._current(frame.cancel_event.is_set)

        def chosen(total):
            try:
                current()
                span = actions.dialogs.text(frame, "Plain BRF source range",
                    "First-last reading positions (1-based; at most 100,000 characters)", "1-" + str(total))
                current()
                if span is None:
                    return
                match = re.fullmatch(r"\s*([0-9]+)(?:\s*-\s*([0-9]+))?\s*", span)
                if match is None:
                    raise ValueError("Enter a reading position or first-last range.")
                first, last = int(match[1]), int(match[2] or match[1])
                if not 1 <= first <= last <= total:
                    raise ValueError("Choose positions inside the selected original source.")
                table = actions.dialogs.choose(frame, "Plain BRF table",
                    "Table declarations are not independent tactile-quality qualification.",
                    ("English UEB grade-1", "Ukrainian"))
                current()
                if table is None:
                    return
                if type(table) is not int or table not in (0, 1):
                    raise ValueError("Choose one of the two reviewed tables.")
                table = ("en-ueb-g1", "uk")[table]
                archive = _path(frame, archive=True)
                current()
                if archive is None:
                    return
                destination = _path(frame, archive=False)
                current()
                if destination is None:
                    return
                approval = confirm_export_destination(frame, destination, private_root=service.root,
                                                       title="Replace the selected BRF file")
                current()
                if approval is None:
                    return

                def review():
                    current()
                    selection = service.review(scope.workspace, edition, first, last, table,
                        topic=scope.topic, cancelled=frame.cancel_event.is_set)
                    runtime = inspect_archive(archive, cancelled=frame.cancel_event.is_set)
                    current()
                    return selection, runtime

                def reviewed(value):
                    try:
                        current()
                        selection, runtime = value
                        accepted = wx.MessageBox(
                            f"Source: {selection.title[:240]}\nPositions: {first}-{last}\n"
                            f"Characters: {selection.characters}\nTable: {selection.table}\n"
                            f"Runtime: Liblouis {runtime['version']} (exact archive and table hashes verified)\n"
                            f"Unverified source warnings: {len(selection.warnings)}\n\n"
                            "Create a separate six-dot Braille ASCII derivative?\n"
                            "Original source bytes remain unchanged. Logical line breaks are preserved; "
                            "page layout, navigation, images and embosser settings are not reproduced. "
                            "Independent tactile/table-quality and NVDA checks have not been performed.",
                            "Create plain BRF derivative", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, frame) == wx.YES
                        current()
                        if not accepted:
                            return
                        def complete(receipt):
                            # Completion remains true after its file is committed;
                            # a stale view is suppressed without undoing the result.
                            try:
                                service._current(lambda: False)
                            except (ValueError, PermissionError, RuntimeError):
                                return
                            warning = receipt.get("cleanup_warning")
                            frame.report("Plain BRF exported: " + receipt["path"]
                                + ("\n" + warning if warning else ""))
                        frame.run_job("Exporting the reviewed BRF source",
                            lambda: service.export(selection, archive, destination,
                                approval=approval, cancelled=frame.cancel_event.is_set), complete)
                    except Exception as error:
                        frame.report("BRF export was not started: " + str(error))
                frame.run_job("Reviewing the selected BRF source and local runtime", review, reviewed)
            except Exception as error:
                frame.report("BRF export was not started: " + str(error))

        def inspect_source():
            current()
            desk = ReadingDesk(scope.library, scope.workspace, edition, topic=scope.topic,
                               cancelled=frame.cancel_event.is_set)
            current()
            return len(desk.pages)
        frame.run_job("Reading the selected book's source positions", inspect_source, chosen)
    except Exception as error:
        frame.report("BRF export was not started: " + str(error))
