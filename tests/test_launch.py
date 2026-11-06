"""Сохранённая криптографическая проверка LTI без AGS и постоянной сессии."""
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from gateway.launch import LaunchService, LaunchError, LTI, LEARNER, INSTRUCTOR
from gateway.store import Store


@pytest.fixture
def env(tmp_path):
    key=RSAKey.generate_key(parameters={'kid':'moodle-test'})
    registration={'issuer':'https://moodle.example','clientId':'client','deploymentId':'deploy','contextId':'context',
                  'targetLinkUri':'https://course.example/gateway/courses/demo/lti/launch',
                  'activities':{'catalog':{'resourceLinkId':'resource'}}}
    store=Store(tmp_path/'auth.sqlite')
    return store,LaunchService(store,registration,clock=lambda:1000,course_key='demo',instance='pilot',prefix='/gateway/courses/demo'),key


def attempt(env,mutate=None,*,state=None,binding='browser',mutate_key=None,header=None,key=None,encoder=None,duplicate_kid=False):
    store,service,registered_key=env
    pending=state or service.begin('https://moodle.example','client',service.registration['targetLinkUri'],'hint','message',binding)
    claims={'iss':'https://moodle.example','aud':'client','sub':'student-a','iat':990,'exp':1100,'nonce':pending['nonce'],
            LTI+'deployment_id':'deploy',LTI+'context':{'id':'context'},LTI+'resource_link':{'id':'resource'},
            LTI+'roles':[LEARNER],LTI+'message_type':'LtiResourceLinkRequest',LTI+'version':'1.3.0',
            LTI+'target_link_uri':service.registration['targetLinkUri']}
    if mutate:mutate(claims)
    encoded=jwt.encode(header or {'alg':'RS256','kid':'moodle-test'},claims,key or registered_key,algorithms=['RS256'],encoder_cls=encoder)
    public=registered_key.as_dict(private=False)
    if mutate_key:mutate_key(public)
    flow,who=service.verify(pending['state'],binding,encoded,{'keys':[public,public] if duplicate_kid else [public]})
    return who,pending,encoded,flow


def test_literal_stable_uid_and_replay(env):
    who,pending,encoded,_=attempt(env)
    assert who.uid=='gw-9ad555084e0e7c714ab30ad1f034099644cc0b334cd655fc26740205e39e818f@example.invalid'
    with pytest.raises(LaunchError):env[1].verify(pending['state'],'browser',encoded,{'keys':[env[2].as_dict(private=False)]})


@pytest.mark.parametrize('offset',[1,5])
def test_small_clock_skew_is_preserved(env,offset):
    assert attempt(env,lambda c:c.update(iat=1000+offset))[0].role=='student'


@pytest.mark.parametrize('mutate',[
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
])
def test_signed_invalid_claims_never_activate(env,mutate):
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
def test_untrusted_key_headers_rejected(env,header):
    with pytest.raises(LaunchError):attempt(env,header=header)


def test_foreign_signature_rejected(env):
    with pytest.raises(LaunchError):attempt(env,key=RSAKey.generate_key(parameters={'kid':'moodle-test'}))


def test_browser_binding_and_new_login_reject_old_state(env):
    pending=env[1].begin('https://moodle.example',None,env[1].registration['targetLinkUri'],'hint','message','a')
    with pytest.raises(LaunchError):attempt(env,state=pending,binding='b')
    other=env[1].begin('https://moodle.example',None,env[1].registration['targetLinkUri'],'hint','message','a')
    with pytest.raises(LaunchError):attempt(env,state=pending,binding='a')
    assert attempt(env,state=other,binding='a')[0].subject=='student-a'


@pytest.mark.parametrize('client',['','unknown'])
def test_explicit_unregistered_client_rejected(env,client):
    with pytest.raises(LaunchError):env[1].begin('https://moodle.example',client,env[1].registration['targetLinkUri'],'hint','message','browser')


@pytest.mark.parametrize('issuer,target',[('https://evil.example',None),(None,'https://evil.example')])
def test_optional_client_does_not_relax_issuer_or_target(env,issuer,target):
    with pytest.raises(LaunchError):env[1].begin(issuer or 'https://moodle.example',None,target or env[1].registration['targetLinkUri'],'hint','message','browser')


def test_padded_rsa_modulus_preserves_signature_validation(env):
    assert attempt(env,mutate_key=lambda k:k.update(n=k['n']+'=='))[0].subject=='student-a'
    foreign=RSAKey.generate_key(parameters={'kid':'moodle-test'})
    with pytest.raises(LaunchError):attempt(env,mutate_key=lambda k:k.update(n=foreign.as_dict(private=False)['n']+'=='))


@pytest.mark.parametrize('suffix',['=','===','==\n'])
def test_malformed_rsa_padding_rejected(env,suffix):
    with pytest.raises(LaunchError):attempt(env,mutate_key=lambda k:k.update(n=k['n']+suffix))


def test_instructor_and_owner_email_learner_keep_uid_and_signed_role(env):
    student=attempt(env,lambda c:c.update(email=' Owner@Example.Invalid '))[0]
    teacher=attempt(env,lambda c:c.update({LTI+'roles':[INSTRUCTOR]}))[0]
    assert student.uid==teacher.uid and student.role=='student' and teacher.role=='teacher'
    assert student.email=='owner@example.invalid'


def test_duplicate_kid_rejected(env):
    with pytest.raises(LaunchError):attempt(env,duplicate_kid=True)


@pytest.mark.parametrize('algorithm',['none','HS256'])
def test_non_rs256_token_cannot_activate(env,algorithm):
    import base64
    import json
    _,pending,encoded,_=attempt(env)
    fresh=env[1].begin('https://moodle.example','client',env[1].registration['targetLinkUri'],'hint','message','browser')
    header=base64.urlsafe_b64encode(json.dumps({'alg':algorithm,'kid':'moodle-test'}).encode()).decode().rstrip('=')
    forged=header+'.'+encoded.split('.')[1]+'.'+encoded.split('.')[2]
    with pytest.raises(LaunchError):env[1].verify(fresh['state'],'browser',forged,{'keys':[env[2].as_dict(private=False)]})
