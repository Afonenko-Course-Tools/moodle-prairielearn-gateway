"""Наблюдение подтверждённых LTI входов; не контроль присутствия или native сессий."""
from contextlib import closing
import logging
import ipaddress
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
from starlette.datastructures import Headers
from .config import ExamAudit
from .store import VerifiedLaunch
from .errors import LaunchError

WARNING = 'Exam audit degraded; launch policy unchanged'
LOGGER = logging.getLogger(__name__)
COLUMNS = ['id', 'occurred_at', 'event', 'exam_id', 'course', 'activity', 'uid', 'display_name',
           'source_ip', 'evidence_status', 'gate_decision', 'ip_multiple_students', 'student_multiple_ips']


class ExamStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path).absolute()
        existing = self.path.exists()
        if existing:
            # Никаких writable connection, журналов или chmod до проверки чужой БД.
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                if ({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        != {'exam_schema', 'exam_launches'}
                        or list(db.execute('SELECT version FROM exam_schema')) != [(1,)]
                        or [r[1] for r in db.execute('PRAGMA table_info(exam_launches)')] != COLUMNS):
                    raise ValueError('Требуется отдельная новая audit DB')
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Исключительное создание не позволяет принять конкурентно появившуюся чужую БД.
            with self.path.open('xb'):
                pass
            self.path.chmod(0o600)
            schema = Path(__file__).resolve().parents[2] / 'schema/exam.sql'
            if not schema.exists():
                schema = Path(sys.prefix) / 'share/moodle-prairielearn-gateway/schema/exam.sql'
            with closing(self.connect()) as db:
                db.executescript(schema.read_text())
        self.path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=0.1)

    def record(self, *, now: int, exam_id: str, course: str, activity: str, uid: str,
               name: str, source_ip: str | None, status: str, decision: str) -> None:
        with closing(self.connect()) as db:
            with db:
                db.execute('BEGIN IMMEDIATE')
                multiple_students = multiple_ips = False
                if source_ip is not None:
                    multiple_students = db.execute(
                        'SELECT 1 FROM exam_launches WHERE course=? AND exam_id=? AND source_ip=? AND uid<>? LIMIT 1',
                        (course, exam_id, source_ip, uid)).fetchone() is not None
                    multiple_ips = db.execute(
                        'SELECT 1 FROM exam_launches WHERE course=? AND exam_id=? AND uid=? AND source_ip IS NOT NULL AND source_ip<>? LIMIT 1',
                        (course, exam_id, uid, source_ip)).fetchone() is not None
                db.execute('INSERT INTO exam_launches(occurred_at,event,exam_id,course,activity,uid,display_name,source_ip,evidence_status,gate_decision,ip_multiple_students,student_multiple_ips) '
                           "VALUES(?,'verified_launch',?,?,?,?,?,?,?,?,?,?)",
                           (now, exam_id, course, activity, uid, name, source_ip, status, decision, int(multiple_students), int(multiple_ips)))


class ExamMonitor:
    def __init__(self, config: ExamAudit, course: str) -> None:
        self.config, self.course = config, course
        self.degraded = False
        self.token: bytes | None = None
        self.store: ExamStore | None = None
        try:
            path = Path(config.proxyTokenFile)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError('Требуется закрытый proxy token файл')
            value = path.read_bytes().strip()
            if len(value) < 32 or any(c < 33 or c > 126 for c in value):
                raise ValueError('Требуется ASCII proxy token')
            self.token = value
        except (OSError, ValueError):
            self._degrade()
        try:
            self.store = ExamStore(config.database)
        except (OSError, ValueError, sqlite3.Error):
            self._degrade()

    def _degrade(self) -> None:
        self.degraded = True
        LOGGER.warning(WARNING)

    def evidence(self, headers: Headers) -> tuple[str | None, str]:
        values = [headers.getlist(name) for name in
                  ('x-exam-proxy-token', 'x-exam-client-ip')]
        if any(len(v) > 1 for v in values):
            return None, 'duplicate_headers'
        if any(len(v) != 1 for v in values):
            return None, 'missing_headers'
        token, address = (v[0] for v in values)
        if self.token is None or not secrets.compare_digest(token.encode('utf-8'), self.token):
            return None, 'untrusted_proxy'
        try:
            if '%' in address:
                raise ValueError('IPv6 zone недопустим')
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return None, 'invalid_ip'
        inside = any(parsed in ipaddress.ip_network(network) for network in self.config.classroomCidrs)
        return str(parsed), 'inside_classroom' if inside else 'outside_classroom'

    def observe(self, who: VerifiedLaunch, headers: Headers, now: int) -> None:
        if who.role != 'student' or who.activity not in self.config.activities:
            return
        source_ip, status = self.evidence(headers)
        decision = ('allowed' if status == 'inside_classroom' else 'denied') if self.config.restrictLaunchToClassroom else 'monitor'
        if self.store is None:
            self._degrade()
        else:
            try:
                self.store.record(now=now, exam_id=self.config.examId, course=self.course,
                                  activity=who.activity, uid=who.uid, name=who.name,
                                  source_ip=source_ip, status=status, decision=decision)
            except (OSError, ValueError, sqlite3.Error):
                self._degrade()
        if decision == 'denied':
            raise LaunchError('Экзаменационный вход требует подтверждённый адрес разрешённой сети')
