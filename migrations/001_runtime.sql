CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_versions VALUES(1);
CREATE TABLE IF NOT EXISTS login_states(
 token_hash TEXT PRIMARY KEY, nonce TEXT NOT NULL, binding TEXT NOT NULL,
 expires INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, issuer TEXT NOT NULL, client TEXT NOT NULL, deployment TEXT NOT NULL,
 subject TEXT NOT NULL, uid TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 UNIQUE(issuer,client,deployment,subject)
);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 role TEXT NOT NULL CHECK(role IN ('student','teacher')), context TEXT NOT NULL,
 resource TEXT NOT NULL, activity TEXT NOT NULL, lineitem TEXT NOT NULL,
 expires INTEGER NOT NULL, csrf TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs(
 token_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL REFERENCES sessions(token_hash),
 target TEXT NOT NULL, expires INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assignments(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), instance TEXT NOT NULL,
 delivery TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, work TEXT, essay TEXT,
 old_work TEXT, state TEXT NOT NULL, desired_state TEXT NOT NULL,
 context TEXT NOT NULL, resource TEXT NOT NULL, lineitem TEXT NOT NULL,
 updated INTEGER NOT NULL, UNIQUE(user_id,instance)
);
CREATE TABLE IF NOT EXISTS requests(
 id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL REFERENCES assignments(id),
 user_id TEXT NOT NULL REFERENCES users(id), idempotency TEXT NOT NULL,
 body_hash TEXT NOT NULL, work TEXT NOT NULL, essay TEXT NOT NULL, state TEXT NOT NULL,
 created INTEGER NOT NULL, UNIQUE(user_id,idempotency)
);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, entity TEXT NOT NULL,
 data TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS observations(
 event_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, version INTEGER NOT NULL,
 attempt_number INTEGER NOT NULL, data TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox(
 id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, version INTEGER NOT NULL,
 user_id TEXT NOT NULL REFERENCES users(id), context TEXT NOT NULL, resource TEXT NOT NULL,
 delivery TEXT NOT NULL, lineitem TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', tries INTEGER NOT NULL DEFAULT 0,
 next_attempt INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL
);
