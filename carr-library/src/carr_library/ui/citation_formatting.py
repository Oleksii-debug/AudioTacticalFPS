"""Native citation documents using the canonical local QuickJS formatter.

Controllers bind to one LibraryApp and workspace. Native wx classes are lazy
so actual SQLite/CSL/DOCX integration can be tested without a desktop.
Reference records, saved quotations and source approvals remain immutable.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
from pathlib import Path
import threading
from uuid import uuid4

from ..academic_integration import (
    CitationCancelled, CitationCluster, CitationRequest,
    available_styles, citation_docx_bytes, render_citations,
)
from ..durable_state import local_path, regular
from ..research.export_file import _filename, capture_export_destination, write_binary_export
from ..research.academic_library import AcademicLibrary
from .academic_library_dialog import AcademicController


LOCALES = (("en-US", "English (United States)"), ("en-GB", "English (United Kingdom)"),
           ("uk-UA", "Українська"), ("sk-SK", "Slovenčina"))
LOCATORS = (("page", "Page"), ("chapter", "Chapter"), ("section", "Section"),
            ("paragraph", "Paragraph"), ("line", "Line"), ("volume", "Volume"))


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError("Citation operation cancelled.")


@dataclass(frozen=True)
class CitationSnapshot:
    workspace: str
    sha256: str
    _json: str = field(repr=False)
    _binding: str = field(repr=False)

    @property
    def references(self):
        return tuple(json.loads(self._json))


class CitationFormattingController:
    """Actual active-workspace data, guarded against stale or cross-library use."""

    def __init__(self, library, workspace):
        if not isinstance(workspace, str) or not workspace:
            raise ValueError("Open one library before formatting its academic references.")
        self.library, self.workspace = library, workspace
        self.academic = AcademicController(library, workspace)
        self._binding = uuid4().hex
        self._result = None
        self._result_snapshot = None

    def require_access(self):
        # Reopening a scoped read also catches revoked/archived/deleted scopes.
        self.academic.statistics()

    def load(self, *, cancelled=lambda: False):
        _cancel(cancelled)
        with self.library.read(self.workspace) as db:
            service = AcademicLibrary(db, self.workspace)
            # Bound reference count before constructing a complete export.
            # Linked editions are part of the locator identity even when their
            # bibliographic fields happen to be identical.
            bindings = tuple(tuple(row) for row in db.execute('''SELECT a.id,a.edition_id,e.asset,e.edition,e.volume,e.language
                FROM academic_reference a LEFT JOIN catalog_edition e
                ON e.workspace=a.workspace AND e.id=a.edition_id
                WHERE a.workspace=? AND a.archived=0 ORDER BY a.id LIMIT 1001''', (self.workspace,)))
            if not 1 <= len(bindings) <= 1000:
                raise ValueError("Formatting needs 1 to 1000 active academic references in this library. Add references through Academic library first.")
            content = service.export(format="csl-json", include_notes=False)
        if len(content.encode("utf-8")) > 8_000_000:
            raise ValueError("This bibliography exceeds the local formatting byte limit.")
        references = json.loads(content)
        if not isinstance(references, list) or not 1 <= len(references) <= 1000:
            raise ValueError("Formatting needs 1 to 1000 active academic references in this library. Add references through Academic library first.")
        # Academic-library exchange also carries abstracts. They are not needed
        # for formatting, preview, source hashes, or embedded DOCX metadata.
        for reference in references:
            if not isinstance(reference, dict):
                raise ValueError("The academic library returned invalid bibliography data.")
            reference.pop("abstract", None)
            reference.pop("note", None)
        encoded = json.dumps(references, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        _cancel(cancelled)
        binding_text = json.dumps(bindings, ensure_ascii=False, separators=(",", ":"))
        digest = sha256((encoded + "\n" + binding_text).encode("utf-8")).hexdigest()
        return CitationSnapshot(self.workspace, digest, encoded, self._binding)

    def _check(self, snapshot, cancelled):
        if (not isinstance(snapshot, CitationSnapshot) or snapshot.workspace != self.workspace
                or snapshot._binding != self._binding):
            raise ValueError("The bibliography snapshot belongs to a different library.")
        fresh = self.load(cancelled=cancelled)
        if fresh.sha256 != snapshot.sha256 or fresh._json != snapshot._json:
            raise ValueError("Academic references changed. Reload them and format the bibliography again.")

    def render(self, snapshot, *, reference_ids=None, style="apa", locale="en-US", citations=(),
               node_path=None, cancelled=lambda: False):
        self._check(snapshot, cancelled)
        references = snapshot.references
        if reference_ids is not None:
            if not isinstance(reference_ids, (list, tuple)) or not reference_ids:
                raise ValueError("Check at least one reference for the bibliography.")
            wanted = set(reference_ids)
            present = {row["id"] for row in references}
            if len(wanted) != len(reference_ids) or not wanted <= present:
                raise ValueError("A selected reference is unavailable in this library.")
            references = tuple(row for row in references if row["id"] in wanted)
        result = render_citations(references, style=style, locale=locale, citations=citations,
                                  node_path=node_path, cancelled=cancelled)
        self._check(snapshot, cancelled)
        self._result, self._result_snapshot = result, snapshot.sha256
        return result

    def export_docx(self, snapshot, result, destination, *, title="Citations and bibliography",
                    cancelled=lambda: False, approval=None):
        if result is not self._result or self._result_snapshot != getattr(snapshot, "sha256", None):
            raise ValueError("Format this library's bibliography before exporting the document.")
        self._check(snapshot, cancelled)
        target = _new_destination(destination, ".docx", self.library.root)
        approved = approval if approval is not None else capture_export_destination(
            target, private_root=self.library.root)
        content = citation_docx_bytes(result, title=title, cancelled=cancelled)
        def authorized():
            # Publication rechecks the actual source snapshot, including
            # edition/source bindings, during the shared atomic writer.
            self._check(snapshot, cancelled)
            return False
        return write_binary_export(target, content, private_root=self.library.root,
            cancelled=authorized, require_absent=True, approval=approved)


def _new_destination(destination, suffix, private_root):
    target = local_path(destination)
    _filename(target.name)
    if target.suffix.lower() != suffix:
        raise ValueError("Choose a new " + suffix + " file.")
    regular(target.parent, directory=True)
    if target.exists():
        raise FileExistsError("That export file already exists. Choose another filename.")
    if target.is_relative_to(Path(private_root).resolve()):
        raise ValueError("Choose an export folder outside the application's library data folder.")
    return target


def reference_label(reference, number):
    names = []
    for author in reference.get("author", ()):
        names.append(author.get("literal") or " ".join(author.get(part, "") for part in ("given", "family")).strip())
    issued = reference.get("issued", {})
    rows = issued.get("date-parts", ())
    year = str(rows[0][0]) if rows and rows[0] else issued.get("literal", "Unknown year")
    title = " ".join(str(reference.get("title", "Untitled")).split())
    return f'{number}. {title[:160]} — {"; ".join(names)[:120] or "Unknown author"}; {year}'


def reference_summary(reference):
    values = [("Title", reference.get("title", "Not entered"))]
    names = []
    literal = False
    for author in reference.get("author", ()):
        literal = literal or bool(author.get("literal"))
        names.append(author.get("literal") or " ".join(author.get(part, "") for part in ("given", "family")).strip())
    values.append(("Authors", "; ".join(names) or "Not entered"))
    for key, label in (("type", "Publication type"), ("container-title", "Journal or container"),
                       ("publisher", "Publisher"), ("volume", "Volume"), ("issue", "Issue"),
                       ("page", "Publication extent; not a verified citation locator"),
                       ("DOI", "DOI"), ("ISBN", "ISBN")):
        if reference.get(key):
            values.append((label, str(reference[key])))
    text = "\n\n".join(label + ":\n" + value for label, value in values)
    if literal:
        text += "\n\nAuthor names stored as one text value remain literal. Review their spelling and citation form before export."
    return text


def formatted_preview_text(result):
    sections = [f"Style: {result.style.title}\nLocale: {result.locale}"]
    if result.citations:
        sections.append("Citations\n\n" + "\n\n".join(
            f"Citation {number}\n{citation.text}" for number, citation in enumerate(result.citations, 1)))
    sections.append("Bibliography\n\n" + "\n\n".join(entry.text for entry in result.bibliography))
    if result.warnings:
        sections.append("Review notes\n" + "\n".join(result.warnings))
    sections.append(result.attribution)
    return "\n\n".join(sections)


def reconcile_citations(previous_snapshot, snapshot, clusters):
    """Retain drafts while withdrawing pinpoint claims after a source change."""
    available = {row["id"] for row in snapshot.references}
    changed = previous_snapshot is not None and previous_snapshot.sha256 != snapshot.sha256
    retained = []
    for cluster in clusters:
        if all(item.reference_id in available for item in cluster.items):
            items = tuple(replace(item, locator_verified=False) if changed and item.locator is not None else item
                          for item in cluster.items)
            retained.append(replace(cluster, items=items))
    return retained, changed


def _scope(frame):
    if getattr(frame, "_closing", False):
        raise ValueError("The library is closing.")
    if getattr(frame, "working", False) or getattr(frame, "busy", False):
        raise ValueError("Wait for the current operation or cancel it first.")
    workspace = frame._workspace()
    if not workspace or frame.active is None or frame.active[0] != workspace:
        raise ValueError("Open one library first.")
    return frame.library, workspace


def _guard(frame, library, workspace):
    if (getattr(frame, "_closing", False) or frame.library is not library
            or frame.active is None or frame.active[0] != workspace):
        raise ValueError("The active library changed. Open this command again in the intended library.")


def _native_types():
    import wx

    def labelled(parent, sizer, label, control, *, proportion=0):
        sizer.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 7)
        control.SetName(label.replace("&", ""))
        sizer.Add(control, proportion, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        return control

    class TaskDialog(wx.Dialog):
        def initialize_tasks(self, controller):
            self.controller = controller
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="academic-formatting")
            self.cancel_event = threading.Event()
            self.pending = False
            self._alive = True
            self.controls = []
            self.Bind(wx.EVT_CLOSE, self.close)

        def add_footer(self, layout, buttons):
            self.status = labelled(self, layout, "Operation status", wx.TextCtrl(self,
                style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 62)))
            row = wx.BoxSizer(wx.HORIZONTAL)
            for button in buttons:
                row.Add(button, 0, wx.ALL, 5)
            self.stop = wx.Button(self, label="&Cancel operation")
            self.stop.SetName("Cancel the running operation")
            self.stop.Bind(wx.EVT_BUTTON, self.cancel)
            self.stop.Disable()
            row.Add(self.stop, 0, wx.ALL, 5)
            close = wx.Button(self, wx.ID_CANCEL, "C&lose")
            close.Bind(wx.EVT_BUTTON, self.close)
            row.Add(close, 0, wx.ALL, 5)
            layout.Add(row, 0, wx.ALIGN_RIGHT)
            self.SetEscapeId(wx.ID_CANCEL)

        def report(self, text, *, focus=True):
            if self._alive:
                self.status.ChangeValue(text)
                self.status.SetInsertionPoint(0)
                if focus:
                    self.status.SetFocus()

        def update_controls(self):
            for control in self.controls:
                control.Enable(not self.pending)
            self.stop.Enable(self.pending)

        def run(self, title, operation, completed):
            if not self._alive:
                return
            try:
                _guard(self.GetParent(), self.controller.library, self.controller.workspace)
            except ValueError as error:
                self.report(str(error))
                return
            if self.pending:
                self.report("An operation is already running. Use Cancel operation first.")
                return
            self.pending = True
            self.cancel_event.clear()
            self.update_controls()
            self.report(title + "…", focus=False)

            def finish(future):
                if not self._alive:
                    return
                self.pending = False
                self.update_controls()
                try:
                    result = future.result()
                    _guard(self.GetParent(), self.controller.library, self.controller.workspace)
                    completed(result)
                except (InterruptedError, CitationCancelled):
                    self.report("Operation cancelled.")
                except Exception as error:
                    self.report("Operation did not complete. " + str(error))
                self.update_controls()

            def queue(future):
                if self._alive:
                    try:
                        wx.CallAfter(finish, future)
                    except RuntimeError:
                        pass

            try:
                self.executor.submit(operation).add_done_callback(queue)
            except Exception as error:
                self.pending = False
                self.update_controls()
                self.report("Could not start the operation. " + str(error))

        def cancel(self, event=None):
            self.cancel_event.set()
            self.report("Cancelling the current operation…")

        def close(self, event=None):
            self.shutdown()
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)

        def shutdown(self):
            if self._alive:
                self._alive = False
                self.cancel_event.set()
                self.executor.shutdown(wait=False, cancel_futures=True)

    class CitationDialog(TaskDialog):
        def __init__(self, parent, controller):
            super().__init__(parent, title="Citation document with endnotes", size=(940, 760),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.initialize_tasks(controller)
            self.snapshot = None
            self.references = ()
            self.clusters = []
            self.result = None
            self.styles = available_styles()
            layout = wx.BoxSizer(wx.VERTICAL)
            self.tabs = wx.Notebook(self)
            reference_page = wx.Panel(self.tabs)
            self.tabs.AddPage(reference_page, "References and style")
            first = wx.BoxSizer(wx.VERTICAL)
            help_text = wx.StaticText(reference_page, label="Check references from the active academic library. Ctrl+Tab changes sections. "
                "Bibliography-only export is available without adding citation locators.")
            help_text.Wrap(820)
            first.Add(help_text, 0, wx.ALL, 7)
            self.references_box = labelled(reference_page, first, "&References to include", wx.CheckListBox(reference_page), proportion=1)
            self.references_box.Bind(wx.EVT_CHECKLISTBOX, self.invalidate)
            self.references_box.Bind(wx.EVT_LISTBOX, self.inspect_reference)
            row = wx.BoxSizer(wx.HORIZONTAL)
            self.all_button = wx.Button(reference_page, label="Check &all")
            self.none_button = wx.Button(reference_page, label="&Clear checks")
            self.reload_button = wx.Button(reference_page, label="&Reload references")
            for button in (self.all_button, self.none_button, self.reload_button):
                row.Add(button, 0, wx.ALL, 5)
            first.Add(row, 0)
            self.all_button.Bind(wx.EVT_BUTTON, lambda event: self.check_all(True))
            self.none_button.Bind(wx.EVT_BUTTON, lambda event: self.check_all(False))
            self.reload_button.Bind(wx.EVT_BUTTON, self.reload)
            self.details = labelled(reference_page, first, "Selected reference details", wx.TextCtrl(reference_page,
                style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 115)))
            style_row = wx.BoxSizer(wx.HORIZONTAL)
            style_row.Add(wx.StaticText(reference_page, label="Citation &style"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 7)
            self.style = wx.Choice(reference_page, choices=[row.title for row in self.styles])
            self.style.SetName("Citation style")
            self.style.SetSelection(0)
            style_row.Add(self.style, 1, wx.ALL, 5)
            style_row.Add(wx.StaticText(reference_page, label="&Language"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 7)
            self.locale = wx.Choice(reference_page, choices=[label for _, label in LOCALES])
            self.locale.SetName("Citation language")
            self.locale.SetSelection(0)
            style_row.Add(self.locale, 0, wx.ALL, 5)
            first.Add(style_row, 0, wx.EXPAND)
            self.style.Bind(wx.EVT_CHOICE, self.invalidate)
            self.locale.Bind(wx.EVT_CHOICE, self.invalidate)
            reference_page.SetSizer(first)

            citation_page = wx.Panel(self.tabs)
            self.tabs.AddPage(citation_page, "Citations and locators")
            second = wx.BoxSizer(wx.VERTICAL)
            tip = wx.StaticText(citation_page, label="Add citations in document order. Leave the locator empty for a general reference. "
                "A checked locator records your comparison with the source; it does not approve any stored quotation.")
            tip.Wrap(820)
            second.Add(tip, 0, wx.ALL, 7)
            self.citation_reference = labelled(citation_page, second, "Citation &reference", wx.Choice(citation_page))
            locator_row = wx.BoxSizer(wx.HORIZONTAL)
            locator_row.Add(wx.StaticText(citation_page, label="Locator &type"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 7)
            self.locator_kind = wx.Choice(citation_page, choices=[label for _, label in LOCATORS])
            self.locator_kind.SetName("Citation locator type")
            self.locator_kind.SetSelection(0)
            locator_row.Add(self.locator_kind, 0, wx.ALL, 5)
            locator_row.Add(wx.StaticText(citation_page, label="&Locator; optional"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 7)
            self.locator = wx.TextCtrl(citation_page)
            self.locator.SetName("Citation locator, optional")
            locator_row.Add(self.locator, 1, wx.ALL, 5)
            second.Add(locator_row, 0, wx.EXPAND)
            self.verified = wx.CheckBox(citation_page, label="I &checked this locator in this source")
            self.verified.SetName("I checked this exact locator in the selected source")
            second.Add(self.verified, 0, wx.ALL, 7)
            self.add_citation = wx.Button(citation_page, label="&Add citation")
            self.add_citation.Bind(wx.EVT_BUTTON, self.add_cluster)
            second.Add(self.add_citation, 0, wx.ALL, 7)
            self.citation_list = labelled(citation_page, second, "Citations in document order", wx.ListBox(citation_page), proportion=1)
            row = wx.BoxSizer(wx.HORIZONTAL)
            self.remove_citation = wx.Button(citation_page, label="&Remove citation")
            self.move_up = wx.Button(citation_page, label="Move &up")
            self.move_down = wx.Button(citation_page, label="Move &down")
            for button in (self.remove_citation, self.move_up, self.move_down):
                row.Add(button, 0, wx.ALL, 5)
            second.Add(row, 0)
            self.remove_citation.Bind(wx.EVT_BUTTON, self.remove_cluster)
            self.move_up.Bind(wx.EVT_BUTTON, lambda event: self.move_cluster(-1))
            self.move_down.Bind(wx.EVT_BUTTON, lambda event: self.move_cluster(1))
            self.citation_reference.Bind(wx.EVT_CHOICE, self.reset_verification)
            self.locator_kind.Bind(wx.EVT_CHOICE, self.reset_verification)
            self.locator.Bind(wx.EVT_TEXT, self.reset_verification)
            citation_page.SetSizer(second)

            preview_page = wx.Panel(self.tabs)
            self.tabs.AddPage(preview_page, "Preview and export")
            third = wx.BoxSizer(wx.VERTICAL)
            self.document_title = labelled(preview_page, third, "Document &title", wx.TextCtrl(preview_page, value="Citations and bibliography"))
            self.attribution = wx.StaticText(preview_page, label="Citations use citeproc-js by Frank Bennett and Citation Style Language styles. https://citationstyles.org/")
            self.attribution.Wrap(820)
            third.Add(self.attribution, 0, wx.ALL, 7)
            self.preview = labelled(preview_page, third, "Formatted citations and bibliography", wx.TextCtrl(preview_page,
                style=wx.TE_READONLY | wx.TE_MULTILINE), proportion=1)
            preview_page.SetSizer(third)
            layout.Add(self.tabs, 1, wx.EXPAND | wx.ALL, 6)
            self.format_button = wx.Button(self, label="&Format locally (F6)")
            self.format_button.SetDefault()
            self.format_button.Bind(wx.EVT_BUTTON, self.format)
            self.export_button = wx.Button(self, label="Save &DOCX")
            self.export_button.Bind(wx.EVT_BUTTON, self.export)
            self.add_footer(layout, (self.format_button, self.export_button))
            self.controls.extend((self.references_box, self.all_button, self.none_button, self.reload_button, self.style,
                self.locale, self.citation_reference, self.locator_kind, self.locator,
                self.verified, self.add_citation, self.citation_list, self.remove_citation, self.move_up, self.move_down,
                self.document_title, self.format_button, self.export_button))
            command = int(wx.NewIdRef())
            self.Bind(wx.EVT_MENU, self.format, id=command)
            self.SetAcceleratorTable(wx.AcceleratorTable([(wx.ACCEL_NORMAL, wx.WXK_F6, command)]))
            self.SetSizer(layout)
            self.SetMinSize((700, 550))
            self.update_controls()
            wx.CallAfter(self.reload)

        def update_controls(self):
            super().update_controls()
            self.format_button.Enable(bool(self.references) and not self.pending)
            self.export_button.Enable(self.result is not None and not self.pending)
            for control in (self.add_citation, self.all_button, self.none_button):
                control.Enable(bool(self.references) and not self.pending)
            for control in (self.remove_citation, self.move_up, self.move_down):
                control.Enable(bool(self.clusters) and not self.pending)

        def invalidate(self, event=None):
            self.result = None
            if hasattr(self, "export_button"):
                self.update_controls()
                self.report("Selection or settings changed. Format again before exporting.", focus=False)
            if event is not None:
                event.Skip()

        def reload(self, event=None):
            selected = {self.references[index]["id"] for index in self.references_box.GetCheckedItems()} if self.references else None

            def loaded(snapshot):
                self.clusters, changed = reconcile_citations(self.snapshot, snapshot, self.clusters)
                self.snapshot = snapshot
                self.references = snapshot.references
                labels = [reference_label(row, index) for index, row in enumerate(self.references, 1)]
                self.references_box.Set(labels)
                self.citation_reference.Set(labels)
                for index, reference in enumerate(self.references):
                    self.references_box.Check(index, selected is None or reference["id"] in selected)
                self.references_box.SetSelection(0)
                self.citation_reference.SetSelection(0)
                self.refresh_clusters()
                self.result = None
                self.inspect_reference()
                self.report(f"Loaded {len(self.references)} active academic references. Choose a style and format locally. "
                    + ("References or linked sources changed. Existing locators need a new source check; remove and add those citations after reviewing them. " if changed and self.clusters else "")
                    + "Literal author names remain as entered; review them before export.", focus=False)
                self.references_box.SetFocus()

            self.run("Loading this library's academic references", lambda: self.controller.load(cancelled=self.cancel_event.is_set), loaded)

        def inspect_reference(self, event=None):
            index = self.references_box.GetSelection()
            if 0 <= index < len(self.references):
                self.details.ChangeValue(reference_summary(self.references[index]))
                self.details.SetInsertionPoint(0)

        def check_all(self, checked):
            for index in range(len(self.references)):
                self.references_box.Check(index, checked)
            self.invalidate()

        def reset_verification(self, event=None):
            self.verified.SetValue(False)
            if event is not None:
                event.Skip()

        def add_cluster(self, event=None):
            if self.pending:
                return
            index = self.citation_reference.GetSelection()
            if not 0 <= index < len(self.references):
                self.report("Choose a reference for the citation.")
                return
            locator = self.locator.GetValue().strip() or None
            if locator and not self.verified.GetValue():
                self.report("Compare this exact locator with the source and check the verification box before adding it.")
                return
            reference = self.references[index]
            request = CitationRequest(reference["id"], locator=locator,
                label=LOCATORS[self.locator_kind.GetSelection()][0], locator_verified=bool(locator and self.verified.GetValue()))
            self.clusters.append(CitationCluster((request,), id="citation-" + uuid4().hex))
            self.references_box.Check(index, True)
            self.locator.ChangeValue("")
            self.verified.SetValue(False)
            self.refresh_clusters(len(self.clusters) - 1)
            self.invalidate()
            self.report("Citation added in document order.", focus=False)
            self.citation_list.SetFocus()

        def refresh_clusters(self, selected=0):
            by_id = {row["id"]: row for row in self.references}
            labels = []
            for number, cluster in enumerate(self.clusters, 1):
                item = cluster.items[0]
                label = f'{number}. {by_id[item.reference_id].get("title", "Untitled")}'
                if item.locator:
                    label += f"; {item.label}: {item.locator}; " + ("checked against source" if item.locator_verified else "needs a new source check")
                labels.append(label)
            self.citation_list.Set(labels)
            if labels:
                self.citation_list.SetSelection(min(max(0, selected), len(labels) - 1))

        def remove_cluster(self, event=None):
            index = self.citation_list.GetSelection()
            if 0 <= index < len(self.clusters):
                self.clusters.pop(index)
                self.refresh_clusters(index)
                self.invalidate()

        def move_cluster(self, step):
            index = self.citation_list.GetSelection()
            target = index + step
            if 0 <= index < len(self.clusters) and 0 <= target < len(self.clusters):
                self.clusters[index], self.clusters[target] = self.clusters[target], self.clusters[index]
                self.refresh_clusters(target)
                self.invalidate()

        def format(self, event=None):
            if self.pending or self.snapshot is None:
                return
            selected = tuple(self.references[index]["id"] for index in self.references_box.GetCheckedItems())
            if not selected:
                self.report("Check at least one reference to include.")
                return
            if any(item.reference_id not in selected for cluster in self.clusters for item in cluster.items):
                self.report("A citation refers to an unchecked reference. Check that reference or remove its citation.")
                return
            if any(item.locator is not None and not item.locator_verified for cluster in self.clusters for item in cluster.items):
                self.report("A retained locator needs a new source check. Remove that citation and add it again after reviewing its source.")
                return
            style = self.styles[self.style.GetSelection()].id
            locale = LOCALES[self.locale.GetSelection()][0]
            clusters = tuple(replace(cluster, note_index=index) for index, cluster in enumerate(self.clusters, 1))
            snapshot = self.snapshot
            self.result = None

            def ready(result):
                self.result = result
                self.preview.ChangeValue(formatted_preview_text(result))
                self.preview.SetInsertionPoint(0)
                self.attribution.SetLabel(result.attribution)
                self.attribution.Wrap(820)
                self.tabs.SetSelection(2)
                self.report(f"Formatted {len(result.bibliography)} bibliography entries and {len(result.citations)} citations. "
                    "Review the result, then save a new DOCX.", focus=False)
                self.preview.SetFocus()

            self.run("Formatting citations locally", lambda: self.controller.render(snapshot, reference_ids=selected,
                style=style, locale=locale, citations=clusters, cancelled=self.cancel_event.is_set), ready)

        def export(self, event=None):
            if self.pending or self.result is None:
                return
            with wx.FileDialog(self, "Save a new citation document", defaultFile="citations-and-bibliography.docx",
                               wildcard="Word document (*.docx)|*.docx", style=wx.FD_SAVE) as dialog:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                destination = dialog.GetPath()
            parent = self.GetParent()
            try:
                _guard(parent, self.controller.library, self.controller.workspace)
                target = _new_destination(destination, ".docx", self.controller.library.root)
                approval = capture_export_destination(target, private_root=self.controller.library.root)
            except Exception as error:
                self.report("The export destination was not approved. " + str(error))
                return
            title = self.document_title.GetValue()
            result, snapshot = self.result, self.snapshot
            def authorized_cancel():
                if not self._alive:
                    raise InterruptedError("Citation document window closed.")
                _guard(parent, self.controller.library, self.controller.workspace)
                return self.cancel_event.is_set()
            def completed(receipt):
                message = f"Saved the editable citation document: {receipt.path}"
                if receipt.cleanup_warning:
                    message += "\n" + receipt.cleanup_warning
                self.report(message)
            self.run("Saving the citation document", lambda: self.controller.export_docx(snapshot, result, target,
                title=title, cancelled=authorized_cancel, approval=approval), completed)

    return CitationDialog


def show_citation_formatter(frame):
    dialog = None
    try:
        library, workspace = _scope(frame)
        controller = CitationFormattingController(library, workspace)
        CitationDialog = _native_types()
        dialog = CitationDialog(frame, controller)
        dialog.ShowModal()
    except Exception as error:
        frame.report("Citation formatting could not open. " + str(error))
    finally:
        if dialog is not None:
            dialog.shutdown()
            dialog.Destroy()


def show_doi_preview(frame):
    """Compatibility entry point into the one current acquisition workflow."""
    from .acquisition_dialog import show_acquisition_dialog
    return show_acquisition_dialog(frame)


def install_citation_actions(frame):
    """Register one document action with the existing semantic command manager."""
    if getattr(frame, "_carr_citation_actions_installed", False):
        return
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    bar = frame.GetMenuBar()
    if bar is None:
        raise ValueError("Install the native menu bar before citation actions.")
    menu = None
    for index in range(bar.GetMenuCount()):
        if bar.GetMenuLabel(index).replace("&", "") == "Academic":
            menu = bar.GetMenu(index)
            break
    if menu is None:
        menu = wx.Menu()
        bar.Append(menu, "&Academic")
    elif menu.GetMenuItemCount():
        menu.AppendSeparator()
    item = menu.Append(wx.ID_ANY, "Citation document with endnotes...\tCtrl+Alt+C")
    frame.Bind(wx.EVT_MENU, lambda event: show_citation_formatter(frame), item)
    register_menu_command(frame, CommandSpec("academic.citation_document",
        "Citation document with endnotes", "main", "Ctrl+Alt+C"), item)
    frame._carr_citation_actions_installed = True
