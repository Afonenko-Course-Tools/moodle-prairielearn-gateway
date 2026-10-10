from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import secrets
import sqlite3
import time
import math
from decimal import Decimal
from .errors import LaunchError

def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(value.encode()).hexdigest()

def native_digest(value):
    """Owner wire format: scalar-sorted keys, UTF-8, ECMAScript JSON numbers.

    Keep identity digest separate: its established compact JSON tuple uses ASCII
    escaping, as the verified Moodle identity spike does.
    """
    def canonical(item):
        if isinstance(item,dict):
            return "{"+",".join(json.dumps(key,ensure_ascii=False)+":"+canonical(item[key]) for key in sorted(item))+"}"
        if isinstance(item,list):return "["+",".join(map(canonical,item))+"]"
        if type(item) is float:
            if not math.isfinite(item):raise ValueError("Non-finite native JSON")
            if item==0:return "0"
            shortest=repr(item)
            if 1e-6<=abs(item)<1e21:
                text=format(Decimal(shortest),"f")
                return text.rstrip("0").rstrip(".") if "." in text else text
            mantissa,exponent=shortest.split("e")
            exponent=int(exponent)
            return mantissa.rstrip("0").rstrip(".")+"e"+("+" if exponent>=0 else "-")+str(abs(exponent))
        return json.dumps(item,ensure_ascii=False,allow_nan=False,separators=(",",":"))
    return hashlib.sha256(canonical(value).encode()).hexdigest()

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            self._enable_wal(db)
            migration = Path(__file__).resolve().parents[2] / "migrations/001_runtime.sql"
            # Wheel installs package data; source checkouts use owner migrations.
            if not migration.exists(): migration = Path(__import__("sys").prefix) / "share/moodle-prairielearn-gateway/migrations/001_runtime.sql"
            db.executescript(migration.read_text())
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM schema_versions WHERE version=2").fetchone():
                # executescript commits implicitly; individual statements keep the
                # migration version check and ALTERs under one write lock.
                for statement in (migration.parent / "002_delivery_leases.sql").read_text().split(";"):
                    if statement.strip(): db.execute(statement)
            db.commit()
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

    def activate(self, state, binding, now, identity):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.transaction() as db:
            updated = db.execute("UPDATE login_states SET consumed=1 WHERE token_hash=? AND binding=? AND expires>? AND consumed=0", (digest(state), digest(binding), now)).rowcount
            if updated != 1: raise LaunchError("Launch replay rejected")
            db.execute("INSERT INTO users(id,issuer,client,deployment,subject,uid,name) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name", tuple(identity[k] for k in ("userId","issuer","client","deployment","subject","uid","name")))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)", (digest(token), identity["userId"], identity["role"], identity["context"], identity["resource"], identity["activity"], identity["lineitem"], now+3600, csrf))
            if identity["role"]=="student":
                db.execute("INSERT INTO activity_bindings VALUES(?,?,?,?,?) ON CONFLICT(user_id,activity) DO UPDATE SET context=excluded.context,resource=excluded.resource,lineitem=excluded.lineitem",(identity["userId"],identity["activity"],identity["context"],identity["resource"],identity["lineitem"]))
        return {**identity, "token": token, "csrf": csrf}

    def session(self, token, now):
        with self.connect() as db:
            row = db.execute("SELECT s.*,u.uid,u.subject,u.name,u.issuer,u.client,u.deployment,u.id AS userId FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=? AND expires>?", (digest(token), now)).fetchone()
        if not row: raise LaunchError("Session absent or expired")
        return dict(row)

    def handoff(self, session, target, now):
        self.session(session, now)
        if not (target=="/gateway/bootstrap" or __import__("re").fullmatch(r"/pl/course_instance/[1-9][0-9]*/assessment/[1-9][0-9]*/",target)): raise LaunchError("Unsafe native target")
        token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute("INSERT INTO handoffs(token_hash,session_hash,target,expires) VALUES(?,?,?,?)", (digest(token), digest(session), target, now+60))
        return token

    def consume_handoff(self, token, now):
        with self.transaction() as db:
            row = db.execute("SELECT h.*,u.uid,u.name,s.role,s.expires AS session_expires FROM handoffs h JOIN sessions s ON s.token_hash=h.session_hash JOIN users u ON u.id=s.user_id WHERE h.token_hash=?", (digest(token),)).fetchone()
            if not row or row["consumed"] or row["expires"] <= now or row["session_expires"] <= now: raise LaunchError("Handoff replay/TTL rejected")
            db.execute("UPDATE handoffs SET consumed=1 WHERE token_hash=?", (digest(token),))
            return {"uid": row["uid"], "displayName": row["name"], "email": row["uid"], "target": row["target"]}

    def audit(self, db, kind, entity, data, now):
        db.execute("INSERT INTO audit(kind,entity,data,created) VALUES(?,?,?,?)", (kind, entity, json.dumps(data, sort_keys=True), now))
