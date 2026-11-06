"""Закрытая серверная конфигурация адаптера входа."""
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


def origin(value):
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
    def trusted_urls(self):
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
    def private_url(self):
        u = urlsplit(self.url)
        if (u.scheme not in ('http', 'https') or not u.netloc or u.port != 3001 or u.username
                or u.password or u.path not in ('', '/') or u.query or u.fragment):
            raise ValueError('Требуется фиксированный private Bridge origin :3001')
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

    @model_validator(mode='after')
    def paths(self):
        parsed = urlsplit(self.publicOrigin)
        origin(self.publicOrigin)
        if parsed.path not in ('', '/') or parsed.query:
            raise ValueError('Public origin не должен содержать path/query')
        for p in (self.database, self.handoffTokenFile, self.bridge.tokenFile, self.caFile):
            if p is not None and not Path(p).is_absolute():
                raise ValueError('Требуются абсолютные private runtime пути')
        return self
