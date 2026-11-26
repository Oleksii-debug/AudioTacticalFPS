-- Test-only migration proposal; CARR-05/01 must approve catalog FK and schema version.
-- Never run automatically against a production database.
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
