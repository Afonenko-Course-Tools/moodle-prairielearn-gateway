ALTER TABLE outbox ADD COLUMN lease_until INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS activity_bindings(
 user_id TEXT NOT NULL REFERENCES users(id), activity TEXT NOT NULL,
 context TEXT NOT NULL, resource TEXT NOT NULL, lineitem TEXT NOT NULL,
 PRIMARY KEY(user_id,activity)
);
INSERT INTO schema_versions VALUES(2);
