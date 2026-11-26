-- TEST ONLY: workspace(id TEXT PRIMARY KEY) is provided by caller, as in PR49.
-- No user_version change or automatic migration; CARR-01/05 must approve.
CREATE TABLE catalog_asset(
    digest TEXT PRIMARY KEY CHECK(length(digest)=64),
    mime TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(typeof(size)='integer' AND size>0)
);
CREATE TABLE catalog_work(
    workspace TEXT NOT NULL REFERENCES workspace(id),
    id TEXT NOT NULL,
    title TEXT,
    PRIMARY KEY(workspace,id)
);
CREATE TABLE catalog_author(
    workspace TEXT NOT NULL,
    work TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position>=0),
    name TEXT NOT NULL,
    PRIMARY KEY(workspace,work,position),
    FOREIGN KEY(workspace,work) REFERENCES catalog_work(workspace,id)
);
CREATE TABLE catalog_edition(
    workspace TEXT NOT NULL,
    id TEXT NOT NULL,
    work TEXT NOT NULL,
    asset TEXT NOT NULL REFERENCES catalog_asset(digest),
    language TEXT,
    edition TEXT,
    volume TEXT,
    PRIMARY KEY(workspace,id),
    FOREIGN KEY(workspace,work) REFERENCES catalog_work(workspace,id)
);
CREATE TABLE catalog_collection(
    workspace TEXT NOT NULL REFERENCES workspace(id),
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    PRIMARY KEY(workspace,id)
);
CREATE UNIQUE INDEX catalog_collection_name ON catalog_collection(workspace,name) WHERE archived=0;
CREATE TABLE catalog_membership(
    workspace TEXT NOT NULL,
    collection TEXT NOT NULL,
    edition TEXT NOT NULL,
    PRIMARY KEY(workspace,collection,edition),
    FOREIGN KEY(workspace,collection) REFERENCES catalog_collection(workspace,id),
    FOREIGN KEY(workspace,edition) REFERENCES catalog_edition(workspace,id)
);
CREATE TRIGGER catalog_edition_identity_immutable BEFORE UPDATE OF workspace,id,work,asset ON catalog_edition
BEGIN SELECT RAISE(ABORT,'immutable edition identity'); END;
CREATE TRIGGER catalog_edition_no_delete BEFORE DELETE ON catalog_edition
BEGIN SELECT RAISE(ABORT,'destructive deletion not supported'); END;
CREATE TRIGGER catalog_asset_no_update BEFORE UPDATE ON catalog_asset
BEGIN SELECT RAISE(ABORT,'immutable asset manifest'); END;
CREATE TRIGGER catalog_asset_no_delete BEFORE DELETE ON catalog_asset
BEGIN SELECT RAISE(ABORT,'destructive deletion not supported'); END;
CREATE TRIGGER catalog_work_identity_immutable BEFORE UPDATE OF workspace,id ON catalog_work
BEGIN SELECT RAISE(ABORT,'immutable work identity'); END;
