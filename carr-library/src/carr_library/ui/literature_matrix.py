"""Keyboard-native comparison of already verified saved evidence.

The dialog filters an immutable view for browsing. Export and evidence editing
are commands returned to the host, which obtains a fresh authorized snapshot.
wx is imported only when a native dialog is constructed.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class MatrixCommand:
    kind: str
    include_descendants: bool = True
    tag_filter: str | None = None
    evidence: object | None = None


def visible_sources(matrix, *, include_descendants=True, tag_filter=None):
    """Keep exact edition/revision grouping and separate topic/evidence IDs."""
    result = []
    for source in matrix.sources:
        rows = tuple(row for row in source.evidence
                     if (include_descendants or row.topic_id == matrix.topic_id)
                     and (tag_filter is None or tag_filter in row.tags))
        if rows:
            result.append((source, rows))
    return tuple(result)


def theme_summary(rows):
    themes = {}
    for source, evidence in rows:
        key = source.edition_id, source.revision_id
        for item in evidence:
            for tag in item.tags:
                sources, items = themes.setdefault(tag, (set(), set()))
                sources.add(key)
                items.add((item.topic_id, item.evidence_id))
    if not themes:
        return 'No tags in these saved evidence items. Add themes through Research, Edit selected evidence tags.'
    return '\n'.join(f'{tag}: {len(sources)} source versions; {len(items)} evidence items.'
                     for tag, (sources, items) in sorted(themes.items()))


def comparison_tags(matrix, requested=None):
    """A vanished tag keeps its exact scope instead of becoming All themes."""
    names = (None, *matrix.themes)
    if requested is not None and requested not in names:
        names = (*names, requested)
    labels = ['All themes']
    labels.extend(f'Tag: {name}' + (' (no current matches)' if name not in matrix.themes else '')
                  for name in names[1:])
    return names, labels


def evidence_text(source, evidence, *, topic_path=None):
    parts = [f'Source: {source.title or "Unknown title"}',
             f'Author: {source.author or "Unknown author"}',
             f'Edition: {source.edition or "unknown"}; volume: {source.volume or "unknown"}',
             f'Topic: {topic_path or evidence.topic_name}',
             'Tags: ' + ('; '.join(evidence.tags) or '(none)')]
    for number, (quote, citation) in enumerate(zip(evidence.quotes, evidence.citations), 1):
        parts.extend((f'Source part {number}. Citation:', citation, 'Exact quotation:', quote))
    parts.extend(('Research note, separate from the source:', evidence.note or '(No saved note)',
                  f'Edition identity: {source.edition_id}',
                  f'Source revision: {source.revision_id}',
                  f'Evidence identity: {evidence.evidence_id}'))
    return '\n\n'.join(parts)


def make_matrix_dialog(parent, matrix, *, include_descendants=True, tag_filter=None):
    import wx

    class LiteratureMatrixDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Compare sources in this topic', size=(900, 790))
            self.command = None
            self.topic_paths = {section.topic_id: ' / '.join(section.path) for section in matrix.sections}
            self.source_rows = ()
            self.evidence_rows = ()
            layout = wx.BoxSizer(wx.VERTICAL)
            intro = wx.StaticText(self, label=(
                f'Topic: {matrix.topic_name}\n'
                'Compare saved source versions, quotations, research notes and themes. '
                'Each source version remains separate. Exports recheck the source and saved notes.'))
            intro.Wrap(840)
            layout.Add(intro, 0, wx.EXPAND | wx.ALL, 8)
            self.descendants = wx.CheckBox(self, label='Include &subtopics')
            self.descendants.SetName('Include subtopics in source comparison')
            self.descendants.SetValue(include_descendants)
            layout.Add(self.descendants, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            layout.Add(wx.StaticText(self, label='Theme or tag'), 0, wx.LEFT | wx.RIGHT, 8)
            self.tag_names, tag_labels = comparison_tags(matrix, tag_filter)
            self.tags = wx.Choice(self, choices=tag_labels)
            self.tags.SetName('Filter source comparison by theme or tag')
            self.tags.SetSelection(self.tag_names.index(tag_filter) if tag_filter in self.tag_names else 0)
            layout.Add(self.tags, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.summary = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.summary.SetName('Source comparison summary')
            self.summary.SetMinSize((-1, 56))
            layout.Add(self.summary, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(self, label='Sources and versions'), 0, wx.LEFT | wx.RIGHT, 8)
            self.sources = wx.ListBox(self, style=wx.LB_SINGLE)
            self.sources.SetName('Sources and versions in the literature review')
            self.sources.SetMinSize((-1, 110))
            layout.Add(self.sources, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            layout.Add(wx.StaticText(self, label='Saved evidence in the selected source'), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            self.evidence = wx.ListBox(self, style=wx.LB_SINGLE)
            self.evidence.SetName('Saved evidence in the selected comparison source')
            self.evidence.SetMinSize((-1, 85))
            layout.Add(self.evidence, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
            self.details = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.details.SetName('Exact source quotations, citations and separate research note')
            self.details.SetMinSize((-1, 150))
            layout.Add(self.details, 2, wx.EXPAND | wx.ALL, 8)
            self.themes = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.themes.SetName('Themes compared across source versions')
            self.themes.SetMinSize((-1, 60))
            layout.Add(self.themes, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            buttons = wx.WrapSizer(wx.HORIZONTAL)
            self.open_button = wx.Button(self, label='&Open selected evidence and notes')
            self.open_button.Bind(wx.EVT_BUTTON, lambda event: self.finish('open_evidence'))
            buttons.Add(self.open_button, 0, wx.ALL, 4)
            for label, action in (
                    ('Export comparison as &CSV', 'matrix_csv'),
                    ('Export comparison as &Markdown', 'matrix_markdown'),
                    ('&Refresh comparison', 'refresh')):
                button = wx.Button(self, label=label)
                button.Bind(wx.EVT_BUTTON, lambda event, kind=action: self.finish(kind))
                buttons.Add(button, 0, wx.ALL, 4)
            buttons.Add(wx.Button(self, wx.ID_CANCEL, '&Close'), 0, wx.ALL, 4)
            layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)
            self.SetSizer(layout)
            self.SetMinSize((740, 700))
            self.SetEscapeId(wx.ID_CANCEL)
            self.sources.Bind(wx.EVT_LISTBOX, self.on_source)
            self.evidence.Bind(wx.EVT_LISTBOX, self.on_evidence)
            self.descendants.Bind(wx.EVT_CHECKBOX, self.refilter)
            self.tags.Bind(wx.EVT_CHOICE, self.refilter)
            self.refilter()
            (self.sources if self.source_rows else self.summary).SetFocus()

        def chosen_tag(self):
            selected = self.tags.GetSelection()
            return self.tag_names[selected] if 0 <= selected < len(self.tag_names) else None

        def refilter(self, event=None):
            self.source_rows = visible_sources(matrix,
                include_descendants=self.descendants.GetValue(), tag_filter=self.chosen_tag())
            self.sources.Clear()
            for number, (source, rows) in enumerate(self.source_rows, 1):
                self.sources.Append(f'{number}. {source.title or "Unknown title"}; '
                    f'{source.author or "Unknown author"}; edition {source.edition or "unknown"}; '
                    f'volume {source.volume or "unknown"}; {len(rows)} saved evidence items')
            count = sum(len(rows) for source, rows in self.source_rows)
            self.summary.ChangeValue(f'{len(self.source_rows)} source versions; {count} saved evidence items.'
                                     if count else 'No saved evidence matches these options.')
            self.themes.ChangeValue(theme_summary(self.source_rows))
            if self.source_rows:
                self.sources.SetSelection(0)
            self.on_source()

        def on_source(self, event=None):
            selected = self.sources.GetSelection()
            self.evidence_rows = self.source_rows[selected][1] if 0 <= selected < len(self.source_rows) else ()
            self.evidence.Clear()
            for number, row in enumerate(self.evidence_rows, 1):
                self.evidence.Append(f'{number}. {self.topic_paths.get(row.topic_id, row.topic_name)}; {"; ".join(row.locations)}; '
                                     f'tags: {"; ".join(row.tags) or "none"}')
            if self.evidence_rows:
                self.evidence.SetSelection(0)
            self.on_evidence()

        def on_evidence(self, event=None):
            selected, source_index = self.evidence.GetSelection(), self.sources.GetSelection()
            valid = 0 <= selected < len(self.evidence_rows) and 0 <= source_index < len(self.source_rows)
            self.open_button.Enable(valid)
            self.details.ChangeValue(evidence_text(self.source_rows[source_index][0], self.evidence_rows[selected],
                                      topic_path=self.topic_paths.get(self.evidence_rows[selected].topic_id))
                                     if valid else 'Choose a source and a saved evidence item.')
            self.details.SetInsertionPoint(0)

        def finish(self, kind):
            selected = self.evidence.GetSelection()
            evidence = self.evidence_rows[selected] if 0 <= selected < len(self.evidence_rows) else None
            if kind == 'open_evidence' and evidence is None:
                self.summary.ChangeValue('Select an evidence item first.')
                self.summary.SetFocus()
                return
            self.command = MatrixCommand(kind, self.descendants.GetValue(), self.chosen_tag(), evidence)
            self.EndModal(wx.ID_OK)

    return LiteratureMatrixDialog()
