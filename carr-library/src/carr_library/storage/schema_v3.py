"""Shared research schema, extending the exact V2 catalog/revision schema.

The topic, evidence and saved-query definitions are promoted from the existing
PR49/54/68 fixtures. V1/V2 databases retain their original signatures. This DDL
is used for explicit fresh V3 creation, never as an implicit live migration.
"""

RESEARCH_V3 = '''
CREATE TABLE workspace_profile(
    workspace TEXT PRIMARY KEY REFERENCES workspace(id),
    name TEXT NOT NULL CHECK(length(trim(name))>0),
    description TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))
);
CREATE TRIGGER workspace_identity_immutable BEFORE UPDATE OF id ON workspace
BEGIN SELECT RAISE(ABORT,'immutable workspace identity'); END;
CREATE TRIGGER workspace_no_delete BEFORE DELETE ON workspace
BEGIN SELECT RAISE(ABORT,'archive workspace instead'); END;
CREATE TRIGGER workspace_no_replace BEFORE INSERT ON workspace
WHEN EXISTS(SELECT 1 FROM workspace WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT,'workspace identity already exists'); END;
CREATE TRIGGER workspace_profile_identity_immutable BEFORE UPDATE OF workspace ON workspace_profile
BEGIN SELECT RAISE(ABORT,'immutable workspace profile identity'); END;
CREATE TRIGGER workspace_profile_no_delete BEFORE DELETE ON workspace_profile
BEGIN SELECT RAISE(ABORT,'archive workspace instead'); END;
CREATE TRIGGER workspace_profile_no_replace BEFORE INSERT ON workspace_profile
WHEN EXISTS(SELECT 1 FROM workspace_profile WHERE workspace=NEW.workspace)
BEGIN SELECT RAISE(ABORT,'workspace profile already exists'); END;

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
CREATE TRIGGER research_topic_identity_immutable BEFORE UPDATE OF workspace,id ON research_topic
BEGIN SELECT RAISE(ABORT,'immutable topic identity'); END;
CREATE TRIGGER research_topic_no_delete BEFORE DELETE ON research_topic
BEGIN SELECT RAISE(ABORT,'archive topic instead'); END;
CREATE TRIGGER research_topic_no_replace BEFORE INSERT ON research_topic
WHEN EXISTS(SELECT 1 FROM research_topic WHERE workspace=NEW.workspace AND id=NEW.id)
BEGIN SELECT RAISE(ABORT,'topic identity already exists'); END;
CREATE TRIGGER research_topic_no_cycle BEFORE UPDATE OF parent ON research_topic
WHEN NEW.parent IS NOT NULL AND EXISTS(
    WITH RECURSIVE ancestors(id) AS (
        SELECT NEW.parent UNION
        SELECT t.parent FROM research_topic t JOIN ancestors a ON t.id=a.id
        WHERE t.workspace=NEW.workspace AND t.parent IS NOT NULL
    ) SELECT 1 FROM ancestors WHERE id=NEW.id
)
BEGIN SELECT RAISE(ABORT,'topic cycle'); END;

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

CREATE UNIQUE INDEX revision_identity_v3 ON revision(workspace,edition,asset,id);
CREATE TABLE metadata_snapshot(
    workspace TEXT NOT NULL,
    edition TEXT NOT NULL,
    asset TEXT NOT NULL,
    revision TEXT NOT NULL,
    payload TEXT NOT NULL,
    checksum TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    PRIMARY KEY(workspace,edition,asset,revision),
    FOREIGN KEY(workspace,edition,asset,revision) REFERENCES revision(workspace,edition,asset,id),
    FOREIGN KEY(revision) REFERENCES sealed(revision)
);
CREATE TRIGGER metadata_snapshot_immutable BEFORE UPDATE OF workspace,edition,asset,revision,payload,checksum ON metadata_snapshot
BEGIN SELECT RAISE(ABORT,'immutable metadata snapshot'); END;
CREATE TRIGGER metadata_snapshot_no_delete BEFORE DELETE ON metadata_snapshot
BEGIN SELECT RAISE(ABORT,'revoke metadata snapshot instead'); END;
CREATE TRIGGER metadata_snapshot_no_replace BEFORE INSERT ON metadata_snapshot
WHEN EXISTS(SELECT 1 FROM metadata_snapshot WHERE workspace=NEW.workspace AND edition=NEW.edition AND asset=NEW.asset AND revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'metadata snapshot already exists'); END;
CREATE TRIGGER metadata_snapshot_no_reactivate BEFORE UPDATE OF active ON metadata_snapshot
WHEN OLD.active=0 AND NEW.active<>0
BEGIN SELECT RAISE(ABORT,'revoked metadata snapshot'); END;
'''
