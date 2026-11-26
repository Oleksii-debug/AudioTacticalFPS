"""Version-four academic reference records, separate from source quotations.

The host appends this DDL to the unchanged V3 schema. A reference can exist
without a document; an optional edition link must belong to the same workspace.
Archiving retains identity and never rewrites an original, revision or evidence.
"""

ACADEMIC_SCHEMA = '''
CREATE TABLE academic_reference(
    workspace TEXT NOT NULL REFERENCES workspace(id),
    id TEXT NOT NULL CHECK(length(id) BETWEEN 1 AND 200),
    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 4000),
    authors TEXT NOT NULL CHECK(length(authors)<=500000),
    item_type TEXT NOT NULL CHECK(item_type IN ('book','article','thesis','report','webpage','other')),
    year TEXT NOT NULL CHECK(length(year)<=32),
    container_title TEXT NOT NULL CHECK(length(container_title)<=4000),
    publisher TEXT NOT NULL CHECK(length(publisher)<=4000),
    volume TEXT NOT NULL CHECK(length(volume)<=4000),
    issue TEXT NOT NULL CHECK(length(issue)<=4000),
    pages TEXT NOT NULL CHECK(length(pages)<=4000),
    doi TEXT NOT NULL CHECK(length(doi)<=2000),
    isbn TEXT NOT NULL CHECK(length(isbn)<=13),
    url TEXT NOT NULL CHECK(length(url)<=4000),
    abstract TEXT NOT NULL CHECK(length(abstract)<=100000),
    notes TEXT NOT NULL CHECK(length(notes)<=100000),
    tags TEXT NOT NULL CHECK(length(tags)<=100000),
    reading_status TEXT NOT NULL CHECK(reading_status IN ('unread','reading','read','on_hold')),
    priority TEXT NOT NULL CHECK(priority IN ('low','normal','high')),
    rating INTEGER NOT NULL CHECK(typeof(rating)='integer' AND rating BETWEEN 0 AND 5),
    progress INTEGER NOT NULL CHECK(typeof(progress)='integer' AND progress BETWEEN 0 AND 100),
    archived INTEGER NOT NULL CHECK(typeof(archived)='integer' AND archived IN (0,1)),
    edition_id TEXT,
    created_utc TEXT NOT NULL CHECK(length(created_utc)=27),
    updated_utc TEXT NOT NULL CHECK(length(updated_utc)=27),
    search_text TEXT NOT NULL CHECK(length(search_text)<=1000000),
    PRIMARY KEY(workspace,id),
    FOREIGN KEY(workspace,edition_id) REFERENCES catalog_edition(workspace,id)
);
CREATE INDEX academic_reference_reading ON academic_reference(workspace,archived,reading_status,priority);
CREATE INDEX academic_reference_doi ON academic_reference(workspace,doi) WHERE doi<>'';
CREATE INDEX academic_reference_isbn ON academic_reference(workspace,isbn) WHERE isbn<>'';
CREATE TRIGGER academic_reference_identity_immutable BEFORE UPDATE OF workspace,id,created_utc ON academic_reference
BEGIN SELECT RAISE(ABORT,'immutable academic reference identity'); END;
CREATE TRIGGER academic_reference_no_replace BEFORE INSERT ON academic_reference
WHEN EXISTS(SELECT 1 FROM academic_reference WHERE workspace=NEW.workspace AND id=NEW.id)
BEGIN SELECT RAISE(ABORT,'academic reference already exists'); END;
CREATE TRIGGER academic_reference_no_delete BEFORE DELETE ON academic_reference
BEGIN SELECT RAISE(ABORT,'archive academic reference instead'); END;
'''
