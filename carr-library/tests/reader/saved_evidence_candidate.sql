-- Test-only proposal. CARR-05/01 own production migrations.
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
