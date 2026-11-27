"""Deterministic Word object-model stand-in, never a native Office claim.

Fields have explicit range markers so tests can prove that selective writes do
not erase unrelated content. Native Word's actual range semantics are reserved
for the opt-in running-Word probe.
"""
from types import SimpleNamespace
import threading

from carr_library.application import LibraryApp
from carr_library.ui.academic_library_dialog import AcademicController
from carr_library.ui.citation_formatting import CitationFormattingController
from carr_library.academic_integration.word_com import WordComBridge
from carr_library.academic_integration.word_fields import WordCitationService


class Story:
    def __init__(self, document, text, kind=1):
        self.document, self.kind, self.parts = document, kind, [text]

    def positions(self):
        at = 0
        for part in self.parts:
            text = part if isinstance(part, str) else part.serial()
            yield at, at + len(text), part
            at += len(text)

    def text(self):
        return "".join(part if isinstance(part, str) else part.serial() for part in self.parts)

    def position(self, selected):
        for first, last, part in self.positions():
            if part is selected:
                return first, last
        raise RuntimeError("This modeled native object was removed.")

    def insert(self, at, value):
        for number, (first, last, part) in enumerate(self.positions()):
            if first <= at <= last and isinstance(part, str):
                offset = at - first
                self.parts[number:number + 1] = [part[:offset], value, part[offset:]]
                return
            if at == first:
                self.parts.insert(number, value)
                return
            if at == last:
                self.parts.insert(number + 1, value)
                return
        if at == len(self.text()):
            self.parts.append(value)
            return
        raise ValueError("Insertion would replace an existing native field.")

    def range(self):
        return Range(self, 0, len(self.text()), full=True)


class Range:
    def __init__(self, story, start, end, *, full=False, field=None, role=None):
        self.story, self.start, self.end = story, start, end
        self.full, self.field, self.role = full, field, role

    @property
    def Document(self):
        return self.story.document

    @property
    def StoryType(self):
        return self.story.kind

    @property
    def Start(self):
        if self.field is not None:
            base = self.story.position(self.field)[0]
            return base + 1 if self.role == "code" else base + len(self.field.code) + 2
        return self.start

    @property
    def End(self):
        if self.field is not None:
            return self.Start + len(self.field.code if self.role == "code" else self.field.result)
        return len(self.story.text()) if self.full else self.end

    @property
    def Text(self):
        if self.field is not None:
            return self.field.code if self.role == "code" else self.field.result
        return self.story.text()[self.Start:self.End]

    @Text.setter
    def Text(self, value):
        if self.field is None or self.role != "result":
            raise AssertionError("Production may write only an owned field result.")
        self.Document.record("result", (self.field, value))
        self.field.result = value

    @property
    def Duplicate(self):
        return Range(self.story, self.Start, self.End)

    @property
    def Fields(self):
        return Fields(self.story, self.Start, self.End)

    def SetRange(self, start, end):
        assert 0 <= start <= end <= len(self.story.text())
        self.start, self.end, self.full = start, end, False

    def Collapse(self, direction):
        assert direction == 1
        self.end = self.Start
        self.start = self.Start
        self.full = False


class NativeField:
    def __init__(self, story, code, result="", data="", kind=81):
        self.story, self.code, self.result, self.data, self.Type = story, code, result, data, kind

    def serial(self):
        return "\x13" + self.code + "\x14" + self.result + "\x15"

    @property
    def Code(self):
        return Range(self.story, 0, 0, field=self, role="code")

    @property
    def Result(self):
        return Range(self.story, 0, 0, field=self, role="result")

    @property
    def Data(self):
        if not self.code.strip().startswith("ADDIN CARR_"):
            raise AssertionError("Foreign field metadata must never be read.")
        return self.data

    @Data.setter
    def Data(self, value):
        self.story.document.record("data", (self, value))
        self.data = value

    def Update(self):
        raise AssertionError("The bridge must never invoke Word's field evaluator.")


class Fields:
    def __init__(self, story, start=0, end=None):
        self.story, self.start, self.end = story, start, end

    def rows(self):
        end = len(self.story.text()) if self.end is None else self.end
        return [part for first, last, part in self.story.positions()
                if isinstance(part, NativeField) and self.start <= first and last <= end]

    @property
    def Count(self):
        return len(self.rows())

    def Item(self, index):
        return self.rows()[index - 1]

    def Add(self, *, Range, Type, Text, PreserveFormatting):
        assert Type == 81 and PreserveFormatting is False and Range.Start == Range.End
        self.story.document.record("add", (Range.story.kind, Range.Start, Text))
        field = NativeField(Range.story, " ADDIN " + Text + " ")
        Range.story.insert(Range.Start, field)
        return field

    def Update(self):
        raise AssertionError("No complete field collection may be updated.")


class Note:
    def __init__(self, document, text):
        self.document = document
        self.body = Story(document, text + "\r", kind=3)

    def serial(self):
        return "\x02"

    @property
    def Index(self):
        return self.document.Endnotes.rows().index(self) + 1

    @property
    def Reference(self):
        first, end = self.document.main.position(self)
        return Range(self.document.main, first, end)

    @property
    def Range(self):
        return self.body.range()


class Notes:
    NumberingRule = 0
    StartingNumber = 1
    NumberStyle = 0

    def __init__(self, document):
        self.document = document

    def rows(self):
        return [part for _, _, part in self.document.main.positions() if isinstance(part, Note)]

    @property
    def Count(self):
        return len(self.rows())

    def Item(self, index):
        return self.rows()[index - 1]

    def Add(self, *, Range, Text):
        assert Range.Start == Range.End and Range.story is self.document.main
        self.document.record("note", Range.Start)
        value = Note(self.document, Text)
        self.document.main.insert(Range.Start, value)
        return value


class Document:
    ReadOnly = False
    ProtectionType = -1
    TrackRevisions = False

    def __init__(self, text="Unrelated opening prose. Original closing prose.\r"):
        self.identity = object()
        self.FullName = "CARR synthetic Word fixture.docx"
        self.main = Story(self, text)
        self.Endnotes = Notes(self)
        self.mutations = []
        self.before_mutation = lambda kind, value: None
        self.cursor = 0
        self.selection_end = 0

    @property
    def Content(self):
        return self.main.range()

    @property
    def Fields(self):
        return Fields(self.main)

    def Range(self, start, end):
        return Range(self.main, start, end)

    def record(self, kind, value):
        self.before_mutation(kind, value)
        self.mutations.append((kind, value))

    def move_end(self):
        self.cursor = self.selection_end = len(self.main.text()) - 1

    def foreign_field(self):
        value = NativeField(self.main, " DATE ", "foreign field result", kind=31)
        self.main.insert(0, value)
        self.move_end()
        return value

    def foreign_note(self, text="Foreign endnote; never rewrite."):
        value = Note(self, text)
        self.main.insert(0, value)
        self.move_end()
        return value


class App:
    def __init__(self, document=None):
        self.document = document or Document()
        self.ActiveWindow = SimpleNamespace(Hwnd=12345)
        self.read_threads = []

    @property
    def ActiveDocument(self):
        self.read_threads.append(threading.get_ident())
        if self.document is None:
            raise RuntimeError("The selected Word document closed.")
        return self.document

    @property
    def Selection(self):
        return SimpleNamespace(Range=Range(self.document.main, self.document.cursor, self.document.selection_end))


def library_fixture(path):
    library = LibraryApp(path / "library")
    workspace = library.create_workspace("Word source library")
    other = library.create_workspace("Other source library")
    source = library.add_text(workspace, "Retained original", "Exact immutable spectroscopy source.", authors=("Source Author",))
    academic = AcademicController(library, workspace)
    reference = academic.create(dict(title="Reviewed synthetic methods", authors=("Research Group",),
        item_type="book", year="2024", publisher="Example Press", pages="1-400",
        abstract="PRIVATE ABSTRACT", notes="PRIVATE NOTE", edition_id=source["edition"]))
    second = academic.create(dict(title="Second reviewed source", authors=("Another Author",),
                                 item_type="book", year="2023"))
    AcademicController(library, other).create(dict(title="Excluded foreign library", year="2025"))
    return library, workspace, other, academic, reference, second, source


def service_fixture(path, *, application=None):
    values = library_fixture(path)
    library, workspace = values[:2]
    app = application or App()
    bridge = WordComBridge(connect=lambda: app, identity=lambda doc: doc.identity)
    service = WordCitationService(CitationFormattingController(library, workspace), bridge)
    return values, app, bridge, service


def insert(service, app, reference, *, style="apa", locator=None, verified=False):
    app.document.move_end()
    context = service.capture()
    plan = service.preview(context, operation="insert", reference_ids=(reference.id,),
                           style=style, locator=locator, locator_verified=verified)
    receipt = service.apply(plan)
    return plan, receipt
