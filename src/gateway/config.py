"""Закрытая серверная конфигурация адаптера входа."""
from pathlib import Path
import ipaddress
from urllib.parse import SplitResult, urlsplit
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


def origin(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Требуется фиксированный HTTPS origin')
    return parsed.scheme, parsed.netloc


class Activity(Closed):
    resourceLinkId: str = Field(min_length=1, max_length=256)


class Registration(Closed):
    issuer: str
    clientId: str = Field(min_length=1, max_length=256)
    deploymentId: str = Field(min_length=1, max_length=256)
    contextId: str = Field(min_length=1, max_length=256)
    authUrl: str
    jwksUrl: str
    privateJwksUrl: str | None = None
    activities: dict[str, Activity] = Field(min_length=1)

    @model_validator(mode='after')
    def trusted_urls(self) -> "Registration":
        expected = origin(self.issuer)
        if any(origin(v) != expected for v in (self.authUrl, self.jwksUrl)):
            raise ValueError('Origin регистрации не совпадает')
        if self.privateJwksUrl is not None:
            mapped, public = urlsplit(self.privateJwksUrl), urlsplit(self.jwksUrl)
            if (mapped.scheme != 'http' or mapped.hostname != 'moodle' or mapped.port not in (None, 80)
                    or mapped.username or mapped.password or mapped.path != public.path or mapped.query or mapped.fragment):
                raise ValueError('Разрешён только фиксированный private transport Moodle JWKS')
        if len({v.resourceLinkId for v in self.activities.values()}) != len(self.activities):
            raise ValueError('Привязки LTI activity должны быть уникальны')
        return self


class Bridge(Closed):
    url: str
    tokenFile: str

    @model_validator(mode='after')
    def private_url(self) -> "Bridge":
        u = urlsplit(self.url)
        if (u.scheme not in ('http', 'https') or not u.netloc or u.port != 3001 or u.username
                or u.password or u.path not in ('', '/') or u.query or u.fragment):
            raise ValueError('Требуется фиксированный private Bridge origin :3001')
        return self


def same_path(first: str, second: str) -> bool:
    a, b = Path(first), Path(second)
    if a.resolve() == b.resolve():
        return True
    try:
        return a.samefile(b)
    except OSError:
        return False


def sqlite_paths(path: str, *, auth: bool = False) -> list[str]:
    suffixes = ['', '-journal', '-wal', '-shm']
    if auth:
        suffixes.append('.startup-lock')
    return [base + suffix for base in {path, str(Path(path).resolve())} for suffix in suffixes]


class ExamAudit(Closed):
    examId: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
    activities: list[str] = Field(min_length=1)
    database: str
    proxyTokenFile: str
    classroomCidrs: list[str] = Field(min_length=1)
    restrictLaunchToClassroom: bool = False

    @model_validator(mode='after')
    def closed_scope(self) -> "ExamAudit":
        if len(set(self.activities)) != len(self.activities):
            raise ValueError('Экзаменационные activity должны быть уникальны')
        if len(set(self.classroomCidrs)) != len(self.classroomCidrs):
            raise ValueError('Диапазоны экзамена должны быть уникальны')
        for network in self.classroomCidrs:
            if '%' in network or '/' not in network:
                raise ValueError('Требуется CIDR без IPv6 zone')
            ipaddress.ip_network(network, strict=True)
        if any(not Path(p).is_absolute() for p in (self.database, self.proxyTokenFile)):
            raise ValueError('Требуются абсолютные private runtime пути аудита')
        return self


def binding_origin(value: str) -> SplitResult:
    uri = urlsplit(value)
    if (uri.scheme not in ('http', 'https') or uri.hostname is None or uri.username is not None
            or uri.password is not None or uri.path not in ('', '/') or not uri.port
            or any(ord(char) < 33 or ord(char) > 126 or char in '\\?#' for char in value)):
        raise ValueError('Требуется origin привязки с явным портом')
    return uri


PRIVATE_CONTROLLER_NETWORKS = tuple(ipaddress.ip_network(network) for network in
                                    ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                                     '127.0.0.0/8', 'fc00::/7', '::1/128'))


class ExamBinding(Closed):
    controllerOrigin: str
    tokenFile: str
    handoffOrigin: str | None = None

    @model_validator(mode='after')
    def private_receiver(self) -> 'ExamBinding':
        endpoint = binding_origin(self.controllerOrigin)
        if endpoint.scheme == 'http' and endpoint.hostname != 'controller':
            address = ipaddress.ip_address(endpoint.hostname or '')
            if not any(address in network for network in PRIVATE_CONTROLLER_NETWORKS):
                raise ValueError('HTTP привязки допускается только в закрытой сети')
        if not Path(self.tokenFile).is_absolute():
            raise ValueError('Для привязки требуется абсолютный путь private token')
        return self


class Configuration(Closed):
    publicOrigin: str
    routePrefix: str = Field(pattern=r'^/gateway/courses/[a-z][a-z0-9-]*$')
    database: str
    handoffTokenFile: str
    bridge: Bridge
    courseKey: str = Field(pattern=r'^[a-z][a-z0-9-]*$')
    instance: str = Field(min_length=1, max_length=256)
    registration: Registration
    caFile: str | None = None
    examAudit: ExamAudit | None = None
    examBinding: ExamBinding | None = None

    @model_validator(mode='after')
    def paths(self) -> "Configuration":
        parsed = urlsplit(self.publicOrigin)
        origin(self.publicOrigin)
        if parsed.path not in ('', '/') or parsed.query:
            raise ValueError('Public origin не должен содержать path/query')
        for p in (self.database, self.handoffTokenFile, self.bridge.tokenFile, self.caFile):
            if p is not None and not Path(p).is_absolute():
                raise ValueError('Требуются абсолютные private runtime пути')
        if self.examBinding is not None:
            if self.examAudit is None:
                raise ValueError('Привязка требует область examAudit')
            if self.examBinding.handoffOrigin is not None:
                handoff = binding_origin(self.examBinding.handoffOrigin)
                if (handoff.scheme != 'https' or handoff.hostname != parsed.hostname
                        or handoff.port == (parsed.port or 443)):
                    raise ValueError('Для handoff требуется HTTPS на прежнем узле и отдельном порту')
        if self.examAudit is not None:
            exam = self.examAudit
            if not set(exam.activities) <= self.registration.activities.keys():
                raise ValueError('Экзамен использует только зарегистрированные серверные activity')
            validate_exam_paths([self])
        return self


def validate_exam_paths(courses: list[Configuration]) -> None:
    auth_paths = [p for course in courses for p in sqlite_paths(course.database, auth=True)]
    private_files = [p for course in courses for p in
                     (course.handoffTokenFile, course.bridge.tokenFile, course.caFile) if p is not None]
    exams = [course.examAudit for course in courses if course.examAudit is not None]
    all_tokens = [exam.proxyTokenFile for exam in exams]
    groups = [sqlite_paths(exam.database) for exam in exams]
    binding_tokens = [course.examBinding.tokenFile for course in courses if course.examBinding is not None]
    reserved_for_tokens = auth_paths + private_files + all_tokens + [path for group in groups for path in group]
    if any(same_path(token, reserved) for token in binding_tokens for reserved in reserved_for_tokens):
        raise ValueError('Токен привязки пересекается с private state')
    for i, exam in enumerate(exams):
        reserved = auth_paths + private_files + all_tokens + [p for j, group in enumerate(groups) if i != j for p in group]
        if any(same_path(path, other) for path in groups[i] for other in reserved):
            raise ValueError('Audit DB и SQLite sidecar пересекаются с private state')
        if any(same_path(exam.proxyTokenFile, other) for other in auth_paths + private_files):
            raise ValueError('Proxy token аудита пересекается с private state')
