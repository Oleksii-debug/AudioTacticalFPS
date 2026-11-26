"""Synthetic-only vertical-slice demonstration. Not production OCR or a GUI."""
import re
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Hit:
    page_id: int
    edition: str
    physical: int
    label: str | None
    title: str
    author: str
    volume: str
    start: int
    end: int
    quote: str

    @property
    def citation(self):
        page = f"p. {self.label}" if self.label else f"PDF page {self.physical} (printed page unverified)"
        return f"{self.author}, {self.title}, vol. {self.volume}, edition {self.edition}, {page}"


class Library:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE page (id INTEGER PRIMARY KEY, edition TEXT, physical INTEGER, label TEXT, title TEXT, author TEXT, volume TEXT, raw TEXT, UNIQUE(edition, physical))")

    def add(self, edition, physical, label, title, author, volume, raw):
        if physical < 1 or not raw:
            raise ValueError("invalid page")
        self.db.execute("INSERT INTO page(edition, physical, label, title, author, volume, raw) VALUES (?, ?, ?, ?, ?, ?, ?)", (edition, physical, label, title, author, volume, raw))
        self.db.commit()

    def search(self, query, thematic=False, expanded=False):
        query = query.strip()
        if not query or len(query) > 500:
            return []
        terms = re.findall(r"\w+", query)
        if not terms:
            return []
        pattern = re.compile(r"(?<!\w)" + re.escape(query) + r"(?!\w)", re.IGNORECASE)
        term_patterns = [re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE) for term in terms]
        ranked = []
        for page_id, edition, physical, label, title, author, volume, raw in self.db.execute("SELECT * FROM page ORDER BY id"):
            matches = [m for p in term_patterns if (m := p.search(raw))] if thematic else [m for m in [pattern.search(raw)] if m]
            if not matches:
                continue
            m = min(matches, key=lambda x: x.start())
            start, end = (0, len(raw)) if expanded else (max(0, m.start() - 30), min(len(raw), m.end() + 30))
            hit = Hit(page_id, edition, physical, label, title, author, volume, start, end, raw[start:end])
            ranked.append((-len(matches), page_id, hit))
        return [hit for _, _, hit in sorted(ranked)]

    def more(self, hit):
        row = self.db.execute("SELECT edition, physical, raw FROM page WHERE id=?", (hit.page_id,)).fetchone()
        if not row or row[:2] != (hit.edition, hit.physical) or row[2][hit.start:hit.end] != hit.quote:
            raise ValueError("source mismatch")
        return row[2]


def smoke():
    lib = Library()
    assert lib.search("railway") == []
    lib.add("synthetic-A", 1, "iv", "Synthetic book", "Fixture author", "I", "Synthetic fixture only. A railway and a bridge connect two towns.")
    lib.add("synthetic-A", 2, None, "Synthetic book", "Fixture author", "I", "A bridge crosses a river in this fabricated test page.")
    lib.add("synthetic-B", 1, "99", "Synthetic book", "Other fixture author", "II", "A railway connects a market in this fabricated test page.")
    hits = lib.search("railway and a bridge")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.edition == "synthetic-A" and hit.label == "iv"
    assert lib.more(hit)[hit.start:hit.end] == hit.quote
    assert "p. iv" in hit.citation
    assert len(lib.search("railway")) == 2
    assert len(lib.search("railway river", thematic=True)) == 3
    assert len(lib.search("rail", thematic=False)) == 0
    assert len(lib.search("' OR 1=1 --")) == 0
    assert lib.search("") == []
    wide = lib.search("river", expanded=True)[0]
    assert wide.quote == lib.more(wide)
    assert "PDF page 2 (printed page unverified)" in wide.citation
    try:
        lib.add("synthetic-A", 1, "999", "x", "x", "x", "duplicate")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate page allowed")
    print("PASS: synthetic import, literal/thematic search, source-exact spans, context, edition/page citation, duplicate and invalid query checks")
    print(hit.quote)
    print(hit.citation)


if __name__ == "__main__":
    smoke()
