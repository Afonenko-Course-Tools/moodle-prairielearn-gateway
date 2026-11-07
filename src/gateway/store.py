"""Кратковременные попытки входа; постоянной identity/session registry нет."""
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from collections.abc import Generator
from pydantic import ConfigDict, Field, TypeAdapter
from typing import ClassVar
from .contracts import FrozenValue, Handoff, Identity, LaunchRegistration, Pending, Role
from pathlib import Path
import fcntl
import hashlib
import json
import secrets
import sqlite3
import time
from .errors import LaunchError, GatewayError


def digest(value: object) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class VerifiedLaunch:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(strict=True, extra='forbid')
    uid: str
    name: str
    role: Role
    email: str | None
    subject: str
    registration: str
    context: str
    resource: str
    activity: str
    targetLinkUri: str
    courseKey: str
    instance: str
    bootstrapTarget: str


SNAPSHOT = TypeAdapter(VerifiedLaunch)


class AttemptRow(FrozenValue):
    state_hash: str
    nonce: str
    browser_hash: str
    expires: int
    phase: int = Field(ge=0, le=3)
    launch_json: str | None
    flow_hash: str | None
    handoff_expires: int | None


def attempt_row(row: sqlite3.Row) -> AttemptRow:
    return AttemptRow.model_validate(dict(row))


class Store:
    @staticmethod
    def _enable_wal(db: sqlite3.Connection) -> None:
        # При первом открытии PRAGMA может проигнорировать busy handler SQLite.
        # Повторяем только lock contention в пределах штатных 15 секунд.
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

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).absolute()
        lock_path = self.path.with_suffix(self.path.suffix + '.startup-lock')
        if self.path.exists():
            # Первый процесс может ещё создавать схему. Чужую БД проверяем
            # до writable connection/WAL/chmod и даже создания lock файла.
            if lock_path.exists():
                with lock_path.open('r') as lock:
                    fcntl.flock(lock, fcntl.LOCK_SH)
                    self._validate_existing()
            else:
                self._validate_existing()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            existing = self.path.exists()
            if existing:
                self._validate_existing()
            with closing(self.connect()) as db:
                self._enable_wal(db)
                if not existing:
                    schema = Path(__file__).resolve().parents[2] / 'schema/runtime.sql'
                    if not schema.exists():
                        schema = Path(__import__('sys').prefix) / 'share/moodle-prairielearn-gateway/schema/runtime.sql'
                    db.executescript(schema.read_text())
        self.path.chmod(0o600)

    def _validate_existing(self) -> None:
        try:
            # mode=ro всё ещё может создавать/менять WAL shared-memory файлы.
            # Проверяем только неизменяемую схему main DB без WAL/SHM. Первая
            # инициализация закрывает connection под startup lock и сохраняет
            # схему в main DB; runtime больше не меняет её. Сами попытки из WAL
            # читаются штатным connect только после этого preflight.
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                columns = [r[1] for r in db.execute('PRAGMA table_info(auth_attempts)')]
                if (tables != {'auth_schema', 'auth_attempts'}
                        or list(db.execute('SELECT version FROM auth_schema')) != [(1,)]
                        or columns != ['state_hash', 'nonce', 'browser_hash', 'expires', 'phase', 'launch_json', 'flow_hash', 'handoff_expires']):
                    raise GatewayError('Нужна новая auth DB; прежняя БД не изменена')
        except sqlite3.DatabaseError as error:
            raise GatewayError('Нужна новая auth DB; прежняя БД не изменена') from error

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        return db

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _cleanup(db: sqlite3.Connection, now: int) -> None:
        db.execute('DELETE FROM auth_attempts WHERE state_hash IN (SELECT state_hash FROM auth_attempts WHERE expires<=? LIMIT 100)', (now,))

    def state(self, binding: str, now: int) -> Pending:
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.transaction() as db:
            self._cleanup(db, now)
            db.execute('DELETE FROM auth_attempts WHERE browser_hash=?', (digest(binding),))
            db.execute('INSERT INTO auth_attempts(state_hash,nonce,browser_hash,expires) VALUES(?,?,?,?)',
                       (digest(state), nonce, digest(binding), now + 300))
        return {'state': state, 'nonce': nonce}

    def pending(self, state: str, binding: str, now: int) -> AttemptRow:
        with self.transaction() as db:
            self._cleanup(db, now)
            row = db.execute('SELECT * FROM auth_attempts WHERE state_hash=?', (digest(state),)).fetchone()
        if row is None:
            raise LaunchError('Отклонены state, browser credential, срок или повторный запуск')
        pending = attempt_row(row)
        if pending.phase != 0 or pending.expires <= now or not secrets.compare_digest(pending.browser_hash, digest(binding)):
            raise LaunchError('Отклонены state, browser credential, срок или повторный запуск')
        return pending

    def activate(self, state: str, binding: str, now: int, identity: Identity, registration: LaunchRegistration, course_key: str, instance: str, prefix: str) -> tuple[str, VerifiedLaunch]:
        flow = secrets.token_urlsafe(32)
        snapshot = VerifiedLaunch(
            uid=identity['uid'], name=identity['name'], role=identity['role'], email=identity['email'],
            subject=identity['subject'], registration=registration.canonical(),
            context=identity['context'], resource=identity['resource'], activity=identity['activity'],
            targetLinkUri=registration.targetLinkUri, courseKey=course_key, instance=instance,
            bootstrapTarget=prefix + '/bootstrap/' + flow)
        with self.transaction() as db:
            self._cleanup(db, now)
            updated = db.execute('UPDATE auth_attempts SET phase=1,launch_json=?,flow_hash=?,handoff_expires=min(expires,?) '
                                 'WHERE state_hash=? AND browser_hash=? AND expires>? AND phase=0',
                                 (json.dumps(asdict(snapshot)), digest(flow), now + 60, digest(state), digest(binding), now)).rowcount
            if updated != 1:
                raise LaunchError('Повторный или устаревший LTI запуск')
        return flow, snapshot

    def consume_handoff(self, flow: str, binding: str, now: int) -> Handoff:
        with self.transaction() as db:
            self._cleanup(db, now)
            row = db.execute('SELECT * FROM auth_attempts WHERE flow_hash=? AND browser_hash=? AND phase=1 AND expires>? AND handoff_expires>?',
                             (digest(flow), digest(binding), now, now)).fetchone()
            if not row:
                raise LaunchError('Handoff отсутствует, просрочен или уже потреблён')
            value = attempt_row(row)
            if value.launch_json is None:
                raise LaunchError('Отсутствует подтверждённый snapshot')
            snapshot = SNAPSHOT.validate_json(value.launch_json, strict=True)
            db.execute('UPDATE auth_attempts SET phase=2 WHERE state_hash=?', (value.state_hash,))
        return {'uid': snapshot.uid, 'displayName': snapshot.name, 'email': snapshot.uid, 'target': snapshot.bootstrapTarget}

    def consume_bootstrap(self, flow: str, binding: str, now: int, registration: LaunchRegistration, course_key: str, instance: str, target: str) -> VerifiedLaunch:
        with self.transaction() as db:
            self._cleanup(db, now)
            row = db.execute('SELECT * FROM auth_attempts WHERE flow_hash=? AND browser_hash=? '
                             'AND phase=2 AND expires>? AND handoff_expires>?',
                             (digest(flow), digest(binding), now, now)).fetchone()
            if not row:
                raise LaunchError('Требуется новый переход из Moodle: flow недействителен')
            value = attempt_row(row)
            if value.launch_json is None:
                raise LaunchError('Отсутствует подтверждённый snapshot')
            snapshot = SNAPSHOT.validate_json(value.launch_json, strict=True)
            # Необратимый consume фиксируется до любого обращения к Bridge.
            db.execute('UPDATE auth_attempts SET phase=3 WHERE state_hash=?', (value.state_hash,))
        current = registration.canonical()
        if (snapshot.registration != current or snapshot.courseKey != course_key or snapshot.instance != instance
                or snapshot.bootstrapTarget != target):
            raise LaunchError('Регистрация или курс изменились; повторите переход из Moodle')
        return snapshot
