from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import secrets
import sqlite3
import time
from .errors import LaunchError, GatewayError
import fcntl

def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(value.encode()).hexdigest()

class Store:
    @staticmethod
    def _enable_wal(db):
        # Concurrent first opens can fail this PRAGMA without honoring SQLite's
        # busy handler. Retry only lock contention within the normal 15s budget.
        deadline = time.monotonic() + 15
        try:
            while True:
                remaining = max(0, deadline - time.monotonic())
                db.execute(f"PRAGMA busy_timeout={int(remaining * 1000)}")
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    return
                except sqlite3.OperationalError as error:
                    code = getattr(error, "sqlite_errorcode", 0) & 0xff
                    if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                        raise
                    time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        finally:
            db.execute("PRAGMA busy_timeout=15000")

    def __init__(self, path):
        self.path = Path(path)
        self.gateway_prefix = '/gateway'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize first creation across processes. Existing databases are opened
        # read-only and validated before a writable SQLite connection or WAL.
        with self.path.with_suffix(self.path.suffix+'.startup-lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            existing=self.path.exists()
            if existing:
                try:
                    with sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True) as check:
                        versions=[row[0] for row in check.execute('SELECT version FROM schema_versions')]
                        session_columns=list(check.execute('PRAGMA table_info(sessions)'))
                        expected_sessions=['token_hash','user_id','role','context','resource','activity','lineitem','expires','csrf'] + (['email'] if versions == [5] else [])
                        if [row[1] for row in session_columns] != expected_sessions or versions in ([3],[4],[5]) and session_columns[6][3] != (1 if versions == [3] else 0) or versions == [5] and session_columns[9][3] != 0: raise GatewayError('Unsupported Gateway session schema')
                        if versions not in ([3],[4],[5]) or [row[1] for row in check.execute('PRAGMA table_info(native_attempts)')]!=['scope','version','instance','delivery','work','attempt_id','attempt_number','selection']:raise GatewayError('Unsupported Gateway database schema')
                except sqlite3.DatabaseError as error:raise GatewayError('Unsupported Gateway database schema') from error
            with self.connect() as db:
                self._enable_wal(db)
                if existing and versions in ([3],[4]):
                    # Preserve legacy/security rows; email belongs to the original session.
                    db.execute("PRAGMA foreign_keys=OFF")
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        if versions == [3]:
                            db.execute("CREATE TABLE sessions_v4(token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), role TEXT NOT NULL CHECK(role IN ('student','teacher')), context TEXT NOT NULL, resource TEXT NOT NULL, activity TEXT NOT NULL, lineitem TEXT, expires INTEGER NOT NULL, csrf TEXT NOT NULL)")
                            db.execute("INSERT INTO sessions_v4 SELECT token_hash,user_id,role,context,resource,activity,lineitem,expires,csrf FROM sessions")
                            db.execute("DROP TABLE sessions")
                            db.execute("ALTER TABLE sessions_v4 RENAME TO sessions")
                        db.execute("ALTER TABLE sessions ADD COLUMN email TEXT")
                        if db.execute("PRAGMA foreign_key_check").fetchone(): raise GatewayError("Gateway migration foreign key check rejected")
                        db.execute("UPDATE schema_versions SET version=5 WHERE version IN (3,4)")
                        db.commit()
                    except BaseException:
                        db.rollback()
                        raise
                    finally: db.execute("PRAGMA foreign_keys=ON")
                if not existing:
                    schema=Path(__file__).resolve().parents[2]/'schema/runtime.sql'
                    if not schema.exists():schema=Path(__import__('sys').prefix)/'share/moodle-prairielearn-gateway/schema/runtime.sql'
                    db.executescript(schema.read_text())

        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def state(self, binding, now):
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute("INSERT INTO login_states(token_hash,nonce,binding,expires) VALUES(?,?,?,?)", (digest(state), nonce, digest(binding), now+300))
        return {"state": state, "nonce": nonce}

    def pending(self, state, binding, now):
        with self.connect() as db:
            row = db.execute("SELECT * FROM login_states WHERE token_hash=?", (digest(state),)).fetchone()
        if not row or row["consumed"] or row["expires"] <= now or not secrets.compare_digest(row["binding"], digest(binding)):
            raise LaunchError("State binding/TTL/replay rejected")
        return dict(row)

    def activate(self, state, binding, now, identity, *, login_only=False):
        if not login_only and not identity.get("lineitem"):
            raise LaunchError("AGS activity binding required")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.transaction() as db:
            updated = db.execute("UPDATE login_states SET consumed=1 WHERE token_hash=? AND binding=? AND expires>? AND consumed=0", (digest(state), digest(binding), now)).rowcount
            if updated != 1: raise LaunchError("Launch replay rejected")
            db.execute("INSERT INTO users(id,issuer,client,deployment,subject,uid,name) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name", tuple(identity[k] for k in ("userId","issuer","client","deployment","subject","uid","name")))
            db.execute("INSERT INTO sessions(token_hash,user_id,role,context,resource,activity,lineitem,expires,csrf,email) VALUES(?,?,?,?,?,?,?,?,?,?)", (digest(token), identity["userId"], identity["role"], identity["context"], identity["resource"], identity["activity"], None if login_only else identity["lineitem"], now+3600, csrf, identity.get("email")))
            if identity["role"]=="student" and not login_only:
                db.execute("INSERT INTO activity_bindings VALUES(?,?,?,?,?) ON CONFLICT(user_id,activity) DO UPDATE SET context=excluded.context,resource=excluded.resource,lineitem=excluded.lineitem",(identity["userId"],identity["activity"],identity["context"],identity["resource"],identity["lineitem"]))
        return {**identity, "token": token, "csrf": csrf}

    def session(self, token, now):
        with self.connect() as db:
            row = db.execute("SELECT s.*,u.uid,u.subject,u.name,u.issuer,u.client,u.deployment,u.id AS userId FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=? AND expires>?", (digest(token), now)).fetchone()
        if not row: raise LaunchError("Session absent or expired")
        return dict(row)

    def handoff(self, session, target, now):
        who=self.session(session, now)
        administrator_target=target==self.gateway_prefix+"/administrator-bootstrap" and who["role"]=="teacher"
        if not (administrator_target or target==self.gateway_prefix+"/bootstrap" or __import__("re").fullmatch(r"/pl/course_instance/[1-9][0-9]*/assessment/[1-9][0-9]*/",target)): raise LaunchError("Unsafe native target")
        token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute("INSERT INTO handoffs(token_hash,session_hash,target,expires) VALUES(?,?,?,?)", (digest(token), digest(session), target, now+60))
        return token

    def bootstrap_handoff(self, session, administrator, now):
        who=self.session(session,now)
        if administrator and who["role"]!="teacher": raise LaunchError("Teacher role required")
        token=secrets.token_urlsafe(32)
        target=self.gateway_prefix+("/administrator-bootstrap/" if administrator else "/bootstrap/")+token
        with self.transaction() as db:
            db.execute("INSERT INTO handoffs(token_hash,session_hash,target,expires) VALUES(?,?,?,?)",(digest(token),digest(session),target,now+60))
        return token,target

    def consume_bootstrap(self, token, session, administrator, now):
        current=self.session(session,now)
        target=self.gateway_prefix+("/administrator-bootstrap/" if administrator else "/bootstrap/")+token
        with self.transaction() as db:
            row=db.execute("SELECT h.target,h.expires AS handoff_expires,h.consumed,s.*,u.uid,u.subject,u.name,u.issuer,u.client,u.deployment,u.id AS userId FROM handoffs h JOIN sessions s ON s.token_hash=h.session_hash JOIN users u ON u.id=s.user_id WHERE h.token_hash=?",(digest(token),)).fetchone()
            if not row or row["consumed"]!=1 or row["target"]!=target or row["handoff_expires"]<=now or row["expires"]<=now or row["userId"]!=current["userId"] or row["role"]!=current["role"]:
                raise LaunchError("Bootstrap binding/TTL/replay rejected")
            db.execute("UPDATE handoffs SET consumed=2 WHERE token_hash=?",(digest(token),))
            return dict(row)

    def consume_handoff(self, token, now):
        with self.transaction() as db:
            row = db.execute("SELECT h.*,u.uid,u.name,s.role,s.expires AS session_expires FROM handoffs h JOIN sessions s ON s.token_hash=h.session_hash JOIN users u ON u.id=s.user_id WHERE h.token_hash=?", (digest(token),)).fetchone()
            if not row or row["consumed"] or row["expires"] <= now or row["session_expires"] <= now: raise LaunchError("Handoff replay/TTL rejected")
            db.execute("UPDATE handoffs SET consumed=1 WHERE token_hash=?", (digest(token),))
            return {"uid": row["uid"], "displayName": row["name"], "email": row["uid"], "target": row["target"]}

    def audit(self, db, kind, entity, data, now):
        db.execute("INSERT INTO audit(kind,entity,data,created) VALUES(?,?,?,?)", (kind, entity, json.dumps(data, sort_keys=True), now))
