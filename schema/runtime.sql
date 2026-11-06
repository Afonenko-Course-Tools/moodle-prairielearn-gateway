CREATE TABLE auth_schema(version INTEGER PRIMARY KEY CHECK(version=1));
INSERT INTO auth_schema VALUES(1);
CREATE TABLE auth_attempts(
 state_hash TEXT PRIMARY KEY,
 nonce TEXT NOT NULL,
 browser_hash TEXT NOT NULL UNIQUE,
 expires INTEGER NOT NULL,
 -- 0: OIDC pending; 1: verified; 2: handoff consumed; 3: terminal.
 phase INTEGER NOT NULL DEFAULT 0 CHECK(phase IN (0,1,2,3)),
 launch_json TEXT,
 flow_hash TEXT UNIQUE,
 handoff_expires INTEGER
);
CREATE INDEX auth_attempts_expiry ON auth_attempts(expires);
