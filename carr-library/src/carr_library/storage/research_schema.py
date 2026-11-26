"""Canonical research schema assembled from the existing research services.

The topic, evidence and saved-query tables reuse the reviewed candidate DDL in
tests/reader/*_candidate.sql. SQLite remains the sole persistence engine.
Additional tables persist workspace presentation, frozen citation metadata,
explicit PDF reviews and workspace-scoped tags. They grant no automatic review.
"""

RESEARCH_SCHEMA = '''
CREATE TABLE workspace_meta(
    workspace TEXT PRIMARY KEY REFERENCES workspace(id),
    name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 512),
    description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=100000),
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))
);
CREATE TRIGGER workspace_meta_identity_immutable BEFORE UPDATE OF workspace ON workspace_meta
BEGIN SELECT RAISE(ABORT,'immutable workspace identity'); END;
CREATE TABLE source_metadata(
    workspace TEXT NOT NULL,
    edition TEXT NOT NULL,
    revision TEXT NOT NULL,
    author TEXT,
    title TEXT NOT NULL,
    volume TEXT,
    edition_label TEXT,
    PRIMARY KEY(workspace,edition,revision),
    FOREIGN KEY(workspace,edition,revision) REFERENCES revision(workspace,edition,id)
);
CREATE TRIGGER source_metadata_no_update BEFORE UPDATE ON source_metadata
BEGIN SELECT RAISE(ABORT,'immutable source metadata'); END;
CREATE TRIGGER source_metadata_no_delete BEFORE DELETE ON source_metadata
BEGIN SELECT RAISE(ABORT,'immutable source metadata'); END;
CREATE TRIGGER source_metadata_no_replace BEFORE INSERT ON source_metadata
WHEN EXISTS(SELECT 1 FROM source_metadata WHERE workspace=NEW.workspace AND edition=NEW.edition AND revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'source metadata already exists'); END;
CREATE TABLE source_page_review(
    workspace TEXT NOT NULL,
    edition TEXT NOT NULL,
    revision TEXT NOT NULL,
    physical INTEGER NOT NULL CHECK(typeof(physical)='integer' AND physical>0),
    raw_sha256 TEXT NOT NULL CHECK(length(raw_sha256)=64),
    render_sha256 TEXT NOT NULL CHECK(length(render_sha256)=64),
    renderer TEXT NOT NULL CHECK(length(trim(renderer))>0),
    reviewer TEXT NOT NULL CHECK(length(trim(reviewer))>0),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    PRIMARY KEY(workspace,edition,revision,physical),
    FOREIGN KEY(workspace,edition,revision) REFERENCES revision(workspace,edition,id),
    FOREIGN KEY(revision,physical) REFERENCES page(revision,physical)
);
CREATE TRIGGER source_page_review_immutable BEFORE UPDATE OF workspace,edition,revision,physical,raw_sha256,render_sha256,renderer,reviewer ON source_page_review
BEGIN SELECT RAISE(ABORT,'immutable page review'); END;
CREATE TRIGGER source_page_review_no_replace BEFORE INSERT ON source_page_review
WHEN EXISTS(SELECT 1 FROM source_page_review WHERE workspace=NEW.workspace AND edition=NEW.edition AND revision=NEW.revision AND physical=NEW.physical)
BEGIN SELECT RAISE(ABORT,'page review already exists'); END;
CREATE TRIGGER source_page_review_no_delete BEFORE DELETE ON source_page_review
BEGIN SELECT RAISE(ABORT,'revoke page review instead'); END;
CREATE TABLE research_topic(
    workspace TEXT NOT NULL REFERENCES workspace(id),
    id TEXT NOT NULL,
    parent TEXT,
    name TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(typeof(position)='integer' AND position>=0 AND position<=2147483647),
    archived INTEGER NOT NULL CHECK(archived IN (0,1)),
    PRIMARY KEY(workspace,id),
    FOREIGN KEY(workspace,parent) REFERENCES research_topic(workspace,id),
    CHECK(parent IS NULL OR parent<>id)
);
CREATE INDEX research_topic_parent ON research_topic(workspace,parent,position,id);
CREATE TABLE research_evidence(
    workspace TEXT NOT NULL,
    topic TEXT NOT NULL,
    id TEXT NOT NULL,
    payload TEXT NOT NULL,
    checksum TEXT NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY(workspace,topic,id),
    FOREIGN KEY(workspace,topic) REFERENCES research_topic(workspace,id)
);
CREATE TRIGGER research_evidence_immutable BEFORE UPDATE OF workspace,topic,id,payload,checksum ON research_evidence
BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
CREATE TRIGGER research_evidence_no_delete BEFORE DELETE ON research_evidence
BEGIN SELECT RAISE(ABORT,'evidence deletion requires explicit migration policy'); END;
CREATE TRIGGER research_evidence_no_replace BEFORE INSERT ON research_evidence
WHEN EXISTS(SELECT 1 FROM research_evidence WHERE workspace=NEW.workspace AND topic=NEW.topic AND id=NEW.id)
BEGIN SELECT RAISE(ABORT,'evidence identity already exists'); END;
CREATE TABLE research_saved_query(
    workspace TEXT NOT NULL,
    topic TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    field TEXT NOT NULL CHECK(field IN ('quote','note')),
    result_limit INTEGER NOT NULL CHECK(typeof(result_limit)='integer' AND result_limit BETWEEN 1 AND 100),
    archived INTEGER NOT NULL CHECK(archived IN (0,1)),
    PRIMARY KEY(workspace,topic,id),
    FOREIGN KEY(workspace,topic) REFERENCES research_topic(workspace,id)
);
CREATE TRIGGER research_saved_query_no_replace BEFORE INSERT ON research_saved_query
WHEN EXISTS(SELECT 1 FROM research_saved_query WHERE workspace=NEW.workspace AND topic=NEW.topic AND id=NEW.id)
BEGIN SELECT RAISE(ABORT,'query identity already exists'); END;
CREATE TRIGGER research_saved_query_no_retarget BEFORE UPDATE OF workspace,topic,id ON research_saved_query
BEGIN SELECT RAISE(ABORT,'immutable query identity'); END;
CREATE TRIGGER research_saved_query_no_delete BEFORE DELETE ON research_saved_query
BEGIN SELECT RAISE(ABORT,'archive saved query instead'); END;
CREATE TABLE research_tag(
    workspace TEXT NOT NULL REFERENCES workspace(id),
    id TEXT NOT NULL,
    name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 512),
    PRIMARY KEY(workspace,id),
    UNIQUE(workspace,name)
);
CREATE TABLE research_evidence_tag(
    workspace TEXT NOT NULL,
    topic TEXT NOT NULL,
    evidence TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY(workspace,topic,evidence,tag),
    FOREIGN KEY(workspace,topic,evidence) REFERENCES research_evidence(workspace,topic,id),
    FOREIGN KEY(workspace,tag) REFERENCES research_tag(workspace,id)
);
'''
