from collections.abc import Callable
from pathlib import Path
from json import JSONEncoder
from gateway.contracts import Pending
from gateway.store import VerifiedLaunch
from payloads import Payload, mutations
"""Сохранённая криптографическая проверка LTI без AGS и постоянной сессии."""
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from gateway.launch import LaunchService, LaunchError, LTI, LEARNER, INSTRUCTOR
from gateway.store import Store


Environment = tuple[Store, LaunchService, RSAKey]


def launch_registration() -> Payload:
    return Payload({'issuer':'https://moodle.example','clientId':'client','deploymentId':'deploy','contextId':'context',
                    'authUrl':'https://moodle.example/auth','jwksUrl':'https://moodle.example/certs',
                    'targetLinkUri':'https://course.example/gateway/courses/demo/lti/launch',
                    'activities':{'catalog':{'resourceLinkId':'resource'}}})


@pytest.fixture
def env(tmp_path: Path) -> Environment:
    key=RSAKey.generate_key(parameters={'kid':'moodle-test'})
    registration=launch_registration()
    store=Store(tmp_path/'auth.sqlite')
    return store,LaunchService(store,registration,clock=lambda:1000,course_key='demo',instance='pilot',prefix='/gateway/courses/demo'),key


def attempt(env: Environment, mutate: Callable[[Payload], object] | None = None, *, state: Pending | None = None, binding: str = 'browser', mutate_key: Callable[[dict[str, str | list[str]]], object] | None = None, header: dict[str, object] | None = None, key: RSAKey | None = None, encoder: type[JSONEncoder] | None = None, duplicate_kid: bool = False) -> tuple[VerifiedLaunch, Pending, str, str]:
    _,service,registered_key=env
    pending=state or service.begin('https://moodle.example','client',service.registration.targetLinkUri,'hint','message',binding)
    claims=Payload({'iss':'https://moodle.example','aud':'client','sub':'student-a','iat':990,'exp':1100,'nonce':pending['nonce'],
            LTI+'deployment_id':'deploy',LTI+'context':{'id':'context'},LTI+'resource_link':{'id':'resource'},
            LTI+'roles':[LEARNER],LTI+'message_type':'LtiResourceLinkRequest',LTI+'version':'1.3.0',
            LTI+'target_link_uri':service.registration.targetLinkUri})
    if mutate:mutate(claims)
    encoded=jwt.encode(header or {'alg':'RS256','kid':'moodle-test'},claims,key or registered_key,algorithms=['RS256'],encoder_cls=encoder)
    public=registered_key.as_dict(private=False)
    if mutate_key:mutate_key(public)
    flow,who=service.verify(pending['state'],binding,encoded,{'keys':[public,public] if duplicate_kid else [public]})
    return who,pending,encoded,flow


def test_literal_stable_uid_and_replay(env: Environment) -> None:
    who,pending,encoded,_=attempt(env)
    assert who.uid=='gw-9ad555084e0e7c714ab30ad1f034099644cc0b334cd655fc26740205e39e818f@example.invalid'
    with pytest.raises(LaunchError):env[1].verify(pending['state'],'browser',encoded,{'keys':[env[2].as_dict(private=False)]})


@pytest.mark.parametrize('offset',[1,5])
def test_small_clock_skew_is_preserved(env: Environment,offset: int) -> None:
    assert attempt(env,lambda c:c.update(iat=1000+offset))[0].role=='student'


@pytest.mark.parametrize('mutate',mutations([
    lambda c:c.update(iss='https://evil.example'),lambda c:c.update(aud='other'),
    lambda c:c.update(aud=['client','other'],azp='other'),lambda c:c.update(azp='other'),
    lambda c:c.update(aud=[]),lambda c:c.update(aud=['client',42]),
    lambda c:c.update(exp=1000),lambda c:c.update(exp=900),lambda c:c.update(exp=True),
    lambda c:c.update(exp=1700),lambda c:c.update(iat=1006),lambda c:c.update(iat=500),lambda c:c.update(iat=True),
    lambda c:c.update(nonce='wrong'),lambda c:c.update(sub=''),lambda c:c.update(sub='x'*513),
    lambda c:c.pop('iat'),lambda c:c.pop('exp'),lambda c:c.pop('nonce'),
    lambda c:c.update({LTI+'deployment_id':'other'}),lambda c:c.update({LTI+'context':{'id':'other'}}),
    lambda c:c.update({LTI+'resource_link':{'id':'other'}}),lambda c:c.update({LTI+'roles':['Administrator']}),
    lambda c:c.update({LTI+'roles':[]}),lambda c:c.pop(LTI+'roles'),
    lambda c:c.update({LTI+'target_link_uri':'https://evil.example'}),
    lambda c:c.update({LTI+'message_type':'LtiDeepLinkingRequest'}),lambda c:c.update({LTI+'version':'1.2.0'}),
]))
def test_signed_invalid_claims_never_activate(env: Environment,mutate: Callable[[Payload], object]) -> None:
    with pytest.raises(LaunchError):attempt(env,mutate)
    with env[0].connect() as db:
        assert db.execute('SELECT count(*) FROM auth_attempts WHERE phase>0').fetchone()[0]==0


@pytest.mark.parametrize('header',[
    {'alg':'RS256'}, {'alg':'RS256','kid':'unknown'},
    {'alg':'RS256','kid':'moodle-test','jku':'https://evil.example'},
    {'alg':'RS256','kid':'moodle-test','jwk':{}},
    {'alg':'RS256','kid':'moodle-test','x5u':'https://evil.example'},
    {'alg':'RS256','kid':'moodle-test','x5c':[]},
])
def test_untrusted_key_headers_rejected(env: Environment,header: dict[str, object]) -> None:
    with pytest.raises(LaunchError):attempt(env,header=header)


def test_foreign_signature_rejected(env: Environment) -> None:
    with pytest.raises(LaunchError):attempt(env,key=RSAKey.generate_key(parameters={'kid':'moodle-test'}))


def test_browser_binding_and_new_login_reject_old_state(env: Environment) -> None:
    pending=env[1].begin('https://moodle.example',None,env[1].registration.targetLinkUri,'hint','message','a')
    with pytest.raises(LaunchError):attempt(env,state=pending,binding='b')
    other=env[1].begin('https://moodle.example',None,env[1].registration.targetLinkUri,'hint','message','a')
    with pytest.raises(LaunchError):attempt(env,state=pending,binding='a')
    assert attempt(env,state=other,binding='a')[0].subject=='student-a'


@pytest.mark.parametrize('client',['','unknown'])
def test_explicit_unregistered_client_rejected(env: Environment,client: str) -> None:
    with pytest.raises(LaunchError):env[1].begin('https://moodle.example',client,env[1].registration.targetLinkUri,'hint','message','browser')


@pytest.mark.parametrize('issuer,target',[('https://evil.example',None),(None,'https://evil.example')])
def test_optional_client_does_not_relax_issuer_or_target(env: Environment,issuer: str | None,target: str | None) -> None:
    with pytest.raises(LaunchError):env[1].begin(issuer or 'https://moodle.example',None,target or env[1].registration.targetLinkUri,'hint','message','browser')


def rsa_string(key: dict[str, str | list[str]], name: str) -> str:
    value = key[name]
    assert isinstance(value, str)
    return value


def test_padded_rsa_modulus_preserves_signature_validation(env: Environment) -> None:
    assert attempt(env,mutate_key=lambda k:k.update(n=rsa_string(k, 'n')+'=='))[0].subject=='student-a'
    foreign=RSAKey.generate_key(parameters={'kid':'moodle-test'})
    with pytest.raises(LaunchError):attempt(env,mutate_key=lambda k:k.update(n=rsa_string(foreign.as_dict(private=False), 'n')+'=='))


@pytest.mark.parametrize('suffix',['=','===','==\n'])
def test_malformed_rsa_padding_rejected(env: Environment,suffix: str) -> None:
    with pytest.raises(LaunchError):attempt(env,mutate_key=lambda k:k.update(n=rsa_string(k, 'n')+suffix))


def test_instructor_and_owner_email_learner_keep_uid_and_signed_role(env: Environment) -> None:
    student=attempt(env,lambda c:c.update(email=' Owner@Example.Invalid '))[0]
    teacher=attempt(env,lambda c:c.update({LTI+'roles':[INSTRUCTOR]}))[0]
    assert student.uid==teacher.uid and student.role=='student' and teacher.role=='teacher'
    assert student.email=='owner@example.invalid'


def test_duplicate_kid_rejected(env: Environment) -> None:
    with pytest.raises(LaunchError):attempt(env,duplicate_kid=True)


@pytest.mark.parametrize('algorithm',['none','HS256'])
def test_non_rs256_token_cannot_activate(env: Environment,algorithm: str) -> None:
    import base64
    import json
    _,_,encoded,_=attempt(env)
    fresh=env[1].begin('https://moodle.example','client',env[1].registration.targetLinkUri,'hint','message','browser')
    header=base64.urlsafe_b64encode(json.dumps({'alg':algorithm,'kid':'moodle-test'}).encode()).decode().rstrip('=')
    forged=header+'.'+encoded.split('.')[1]+'.'+encoded.split('.')[2]
    with pytest.raises(LaunchError):env[1].verify(fresh['state'],'browser',forged,{'keys':[env[2].as_dict(private=False)]})


@pytest.mark.parametrize('mutate', mutations([
    lambda value: value.pop('authUrl'),
    lambda value: value.update(jwksUrl='https://evil.example/certs'),
    lambda value: value.update(clientId=''),
    lambda value: value.child('activities').update(duplicate={'resourceLinkId':'resource'}),
    lambda value: value.child('activities').child('catalog').update(resourceLinkId=''),
]))
def test_direct_launch_service_rejects_invalid_canonical_registration(tmp_path: Path, mutate: Callable[[Payload], object]) -> None:
    registration = launch_registration()
    mutate(registration)
    with pytest.raises(ValueError):
        LaunchService(Store(tmp_path/'auth.sqlite'), registration, clock=lambda:1000,
                      course_key='demo', instance='pilot', prefix='/gateway/courses/demo')
