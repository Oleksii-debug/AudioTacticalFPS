-- Test-only schema proposal; CARR-05/01 own production migration.
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
