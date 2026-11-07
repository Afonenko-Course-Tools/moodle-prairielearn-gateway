"""Проверяемые значения на границах LTI, SQLite и private Bridge."""
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol, TypedDict
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .config import Registration

Role = Literal['student', 'teacher']
Clock = Callable[[], float]


class FrozenValue(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra='forbid')


class LaunchRegistration(Registration):
    targetLinkUri: str

    def canonical(self) -> str:
        import json
        return json.dumps(self.model_dump(exclude_unset=True), sort_keys=True, separators=(',', ':'))


class Jwks(FrozenValue):
    keys: list[dict[str, str | list[str]]] = Field(min_length=1, max_length=32)


class Identity(TypedDict):
    uid: str
    name: str
    role: Role
    email: str | None
    subject: str
    context: str
    resource: str
    activity: str


class Pending(TypedDict):
    state: str
    nonce: str


class Handoff(TypedDict):
    uid: str
    displayName: str
    email: str
    target: str


class Enrollment(FrozenValue):
    schemaVersion: int
    userUid: str
    instance: str
    courseKey: str
    enrolled: bool
    path: str

    @model_validator(mode='after')
    def exact_evidence(self) -> 'Enrollment':
        import re
        if self.schemaVersion != 1 or self.enrolled is not True or re.fullmatch(r'/pl/course_instance/[1-9][0-9]*/assessments', self.path) is None:
            raise ValueError('Некорректное подтверждение зачисления Bridge')
        return self


class EnrollmentClient(Protocol):
    def enroll(self, uid: str, instance: str, course: str, *, role: object, email: object) -> Awaitable[Enrollment]: ...


class PlatformClients(Protocol):
    @property
    def bridge(self) -> EnrollmentClient: ...
    def jwks(self) -> Awaitable[Jwks]: ...
