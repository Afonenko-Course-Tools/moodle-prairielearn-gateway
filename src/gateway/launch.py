import time
import re
from joserfc import jwt
from joserfc.jwk import KeySet
from pydantic import BaseModel, ConfigDict, Field
from .contracts import Clock, Identity, Jwks, LaunchRegistration, Pending
from .store import Store, VerifiedLaunch
from .errors import LaunchError
from .store import digest

LTI = "https://purl.imsglobal.org/spec/lti/claim/"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"

def normalize_email(value: object) -> str | None:
    """Консервативный ASCII mailbox; адрес не является identity или правилом выдачи прав."""
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) > 126 for c in value):
        return None
    email = value.strip(' ').lower()
    if not 3 <= len(email) <= 254 or email.count('@') != 1:
        return None
    local, domain = email.split('@')
    if not 1 <= len(local) <= 64 or local.startswith('.') or local.endswith('.') or '..' in local or not re.fullmatch(r"[a-z0-9!#$%&'*+/=?^_`{|}~.\-]+", local):
        return None
    labels = domain.split('.')
    if len(labels) < 2 or any(not 1 <= len(label) <= 63 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', label) for label in labels):
        return None
    return email


def verified_email(claims: "SignedClaims") -> str | None:
    if 'email_verified' in claims.model_fields_set and claims.email_verified is not True:
        return None
    return normalize_email(claims.email)


class TokenHeader(BaseModel):
    model_config = ConfigDict(strict=True, extra='allow')
    alg: str
    kid: str = Field(min_length=1)


class ClaimLink(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str


class SignedClaims(BaseModel):
    model_config = ConfigDict(strict=True)
    sub: str = Field(min_length=1, max_length=512)
    aud: str | list[str]
    iat: int
    exp: int
    azp: str | None = None
    deployment: str = Field(alias=LTI+'deployment_id')
    context: ClaimLink = Field(alias=LTI+'context')
    message_type: str = Field(alias=LTI+'message_type')
    version: str = Field(alias=LTI+'version')
    target: str = Field(alias=LTI+'target_link_uri')
    resource: ClaimLink = Field(alias=LTI+'resource_link')
    roles: list[str] = Field(alias=LTI+'roles')
    name: object = "Участник Moodle"
    email: object = None
    email_verified: object = None


class LaunchClaimsRegistry(jwt.JWTClaimsRegistry):
    def validate_iat(self, value: int) -> None:
        if type(value) is not int or value>self.now+5: raise LaunchError('Отклонено расхождение часов Moodle')
        self.check_value('iat',value)

def platform_key_set(jwks: Jwks) -> KeySet:
    keys: list[dict[str, str | list[str]]] = []
    for original in jwks.keys:
        key = dict(original)
        if key.get("kty") == "RSA":
            for name in ("n", "e"):
                value = key[name]
                if isinstance(value, str) and value.endswith("="):
                    encoded = value.rstrip("=")
                    padding = -len(encoded) % 4
                    if padding not in (1, 2) or value != encoded + "=" * padding:
                        raise LaunchError("Некорректное RSA кодирование Moodle")
                    key[name] = encoded
        keys.append(key)
    return KeySet.import_key_set({"keys": keys})


class LaunchService:
    def __init__(self, store: Store, registration: object, clock: Clock = time.time, *, course_key: str, instance: str, prefix: str) -> None:
        self.store, self.registration, self.clock = store, LaunchRegistration.model_validate(registration), clock
        self.course_key, self.instance, self.prefix = course_key, instance, prefix

    def begin(self, issuer: object, client: object, target: object, hint: object, message: object, binding: str) -> Pending:
        r = self.registration
        if issuer != r.issuer or (client is not None and client != r.clientId) or target != r.targetLinkUri or not hint or not message or not binding:
            raise LaunchError("Отклонена регистрация или привязка входа")
        return self.store.state(binding, int(self.clock()))

    def verify(self, state: str, binding: str, encoded: str, jwks: object) -> tuple[str, VerifiedLaunch]:
        now, r = int(self.clock()), self.registration
        pending = self.store.pending(state, binding, now)
        try:
            keys = Jwks.model_validate(jwks)
            token = jwt.decode(encoded, platform_key_set(keys), algorithms=["RS256"])
            header = TokenHeader.model_validate(token.header)
            if header.alg != "RS256" or sum(k.get("kid")==header.kid for k in keys.keys) != 1 or any(k in header.model_fields_set for k in ("jku","jwk","x5u","x5c")):
                raise LaunchError()
            LaunchClaimsRegistry(now=now, leeway=0, iss={"essential":True,"value":r.issuer}, aud={"essential":True,"value":r.clientId}, sub={"essential":True}, exp={"essential":True}, iat={"essential":True}, nonce={"essential":True,"value":pending.nonce}).validate(token.claims)
            c = SignedClaims.model_validate(token.claims)
            if c.iat > now+5 or c.iat < now-300 or c.exp > c.iat+600 or c.exp <= now or c.exp <= c.iat:
                raise LaunchError()
            aud = c.aud
            if isinstance(aud,list) and (not aud or len(aud)>1 and c.azp!=r.clientId): raise LaunchError()
            if c.azp is not None and c.azp != r.clientId: raise LaunchError()
            if c.deployment != r.deploymentId or c.context.id != r.contextId or c.message_type != "LtiResourceLinkRequest" or c.version != "1.3.0" or c.target != r.targetLinkUri:
                raise LaunchError()
            resource = c.resource.id
            matches = [(key,value) for key,value in r.activities.items() if value.resourceLinkId==resource]
            if len(matches) != 1: raise LaunchError()
            activity, _ = matches[0]
            roles = c.roles
            if not roles or any(role not in (LEARNER,INSTRUCTOR) for role in roles): raise LaunchError("Требуется роль участника курса")
            sub = c.sub
            identity_key = digest([r.issuer,r.clientId,r.deploymentId,sub])
            name = c.name
            if not isinstance(name,str) or len(name)>256 or any(ord(v)<32 or 0xD800<=ord(v)<=0xDFFF for v in name): name="Участник Moodle"
            identity: Identity = {"uid":"gw-"+identity_key+"@example.invalid","subject":sub,"name":name,"role":"teacher" if INSTRUCTOR in roles else "student","context":r.contextId,"resource":resource,"activity":activity,"email":verified_email(c)}
        except LaunchError: raise
        except Exception as error: raise LaunchError("Отклонены подпись или LTI claims") from error
        return self.store.activate(state, binding, now, identity, r, self.course_key, self.instance, self.prefix)
