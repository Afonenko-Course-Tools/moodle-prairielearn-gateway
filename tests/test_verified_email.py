"""Проверенный email привязан к исходной сессии и не изменяет identity UID."""
import copy
import json
from pathlib import Path
import sqlite3
import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from gateway.app import create_app
from gateway.errors import GatewayError
from gateway.launch import LTI, INSTRUCTOR
from gateway.pl import PLClient
from gateway.store import Store, digest
from test_launch import env, attempt
from test_login_only import login_config
from legacy_store import legacy_schema, legacy_person

MISSING = object()


@pytest.mark.parametrize('email,verified,expected', [
    (' Owner@Example.Invalid ', MISSING, 'owner@example.invalid'),
    ('owner@example.invalid', True, 'owner@example.invalid'),
    ('owner+alias@example.invalid', MISSING, 'owner+alias@example.invalid'),
    (MISSING, MISSING, None), (None, MISSING, None), (42, MISSING, None),
    ('owner@example.invalid', False, None), ('owner@example.invalid', 'true', None),
    ('owner@example.invalid', 1, None), ('owner@example.invalid', None, None),
    ('owner@example.invalid\n', MISSING, None), ('\towner@example.invalid', MISSING, None),
    ('ownér@example.invalid', MISSING, None), ('owner@exаmple.invalid', MISSING, None),
    ('owner@@example.invalid', MISSING, None), ('owner..name@example.invalid', MISSING, None),
    ('.owner@example.invalid', MISSING, None), ('owner@example..invalid', MISSING, None),
    ('x'*65+'@example.invalid', MISSING, None), ('owner@'+'x'*64+'.invalid', MISSING, None),
    ('"owner"@example.invalid', MISSING, None), ('owner@example.invalid/extra', MISSING, None),
    ('x'*64+'@'+'.'.join(['x'*63]*3)+'.invalid', MISSING, None),
])
def test_signed_email_normalization_and_verification_policy(env,email,verified,expected):
    env[1].login_only=True
    def mutate(claims):
        if email is not MISSING: claims['email']=email
        if verified is not MISSING: claims['email_verified']=verified
    who,_,_=attempt(env,mutate)
    assert who['email']==expected and who['role']=='student'
    stored=Store(env[0].path).session(who['token'],1001)
    assert stored['email']==expected and stored['uid']==who['uid']


def test_email_changes_neither_existing_uid_nor_other_session_email(env):
    env[1].login_only=True
    first,_,_=attempt(env,lambda c:c.update(email='owner@example.invalid'))
    second,_,_=attempt(env,lambda c:c.update(email='other@example.invalid'))
    assert first['uid']==second['uid']
    reopened=Store(env[0].path)
    assert reopened.session(first['token'],1001)['email']=='owner@example.invalid'
    assert reopened.session(second['token'],1001)['email']=='other@example.invalid'
    with reopened.connect() as db:
        assert [r[1] for r in db.execute('PRAGMA table_info(users)')]==['id','issuer','client','deployment','subject','uid','name']


def test_email_is_never_used_before_signed_registration_guards(env):
    with pytest.raises(GatewayError):attempt(env,lambda c:c.update(email='owner@example.invalid',iss='https://evil.example'))
    with env[0].connect() as db:
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM users').fetchone()[0]==0


@pytest.mark.anyio
@pytest.mark.parametrize('role,email',[('teacher','owner@example.invalid'),('student','owner@example.invalid'),('student','student@example.invalid'),('teacher',None)])
async def test_private_login_enrollment_transfers_five_fields_and_keeps_six_field_response(role,email):
    scope={'userUid':'gw-fake@example.invalid','instance':'pilot','courseKey':'demo'}
    seen=[]
    def endpoint(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'schemaVersion':1,**scope,'enrolled':True,'path':'/pl/course_instance/42/assessments'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        answer=await PLClient(http,'http://private-pl:3001','s'*32).enroll('gw-fake@example.invalid','pilot','demo',role=role,email=email)
    assert seen==[{**scope,'role':role,'email':email}]
    assert set(answer)=={'schemaVersion','userUid','instance','courseKey','enrolled','path'}


@pytest.mark.anyio
@pytest.mark.parametrize('role,email',[(None,'owner@example.invalid'),('Administrator',None),(True,None),('teacher',42),('teacher',' Owner@Example.Invalid ')])
async def test_private_client_rejects_untrusted_role_or_noncanonical_email(role,email):
    def forbidden(request):raise AssertionError('Invalid trusted context must not reach private service')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(GatewayError):await PLClient(http,'http://private-pl:3001','s'*32).enroll('uid','pilot','demo',role=role,email=email)


@pytest.mark.anyio
@pytest.mark.parametrize('first_email,second_email',[(None,'owner@example.invalid'),('owner@example.invalid','other@example.invalid')])
async def test_bootstrap_uses_original_session_email_despite_new_same_user_cookie(tmp_path,first_email,second_email):
    key=RSAKey.generate_key(parameters={'kid':'moodle-fixture'})
    seen=[]
    def endpoint(request):
        payload=json.loads(request.content);seen.append(payload)
        return httpx.Response(200,json={'schemaVersion':1,**{k:payload[k] for k in ('userUid','instance','courseKey')},'enrolled':True,'path':'/pl/course_instance/42/assessments'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as private:
        class Clients:
            bridge=PLClient(private,'http://private-pl:3001','s'*32)
            async def jwks(self):return {'keys':[key.as_dict(private=False)]}
        cfg=login_config(tmp_path)
        app=create_app(cfg,clients=Clients(),clock=lambda:1000)
        app.state.handoff_secret='s'*32
        prefix='/gateway/courses/demo';target='https://course.example'+prefix+'/lti/launch'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://course.example') as browser:
            base={'iss':'https://moodle.example','target_link_uri':target,'login_hint':'hint','lti_message_hint':'message'}
            assert (await browser.get(prefix+'/lti/login',params={**base,'email':'owner@example.invalid'})).status_code==403
            launches=[]
            for email in (first_email,second_email):
                opened=await browser.get(prefix+'/lti/login',params=base)
                oidc=httpx.URL(opened.headers['location']).params
                claims={'iss':'https://moodle.example','aud':'client','sub':'same-person','iat':990,'exp':1100,'nonce':oidc['nonce'],
                        LTI+'deployment_id':'deploy',LTI+'context':{'id':'context'},LTI+'resource_link':{'id':'resource'},
                        LTI+'roles':[INSTRUCTOR],LTI+'message_type':'LtiResourceLinkRequest',LTI+'version':'1.3.0',LTI+'target_link_uri':target}
                if email is not None:claims['email']=email
                body={'state':oidc['state'],'id_token':jwt.encode({'alg':'RS256','kid':key.kid},claims,key,algorithms=['RS256'])}
                assert (await browser.post(prefix+'/lti/launch',data={**body,'email':'owner@example.invalid'})).status_code==403
                launched=await browser.post(prefix+'/lti/launch',data=body,headers={'X-Gateway-Email':'owner@example.invalid','X-Gateway-Role':'Administrator'})
                assert launched.status_code==303
                launches.append(launched.headers['location'].rsplit('/',1)[-1])
            first=launches[0]
            handoff=await browser.get('/internal/handoff/courses/demo/'+first,headers={'Authorization':'Bearer '+'s'*32})
            assert handoff.status_code==200
            assert handoff.json()['email']==handoff.json()['uid']
            assert first_email not in handoff.text if first_email else True
            response=await browser.get(handoff.json()['target']+'?email=owner@example.invalid&role=Administrator',headers={'X-Gateway-Email':'owner@example.invalid'})
            assert response.status_code==303
            assert seen==[{'userUid':handoff.json()['uid'],'instance':'pilot','courseKey':'demo','role':'teacher','email':first_email}]
            assert (await browser.get(handoff.json()['target'])).status_code==403



@pytest.mark.parametrize('version',[3,4])
def test_true_legacy_migration_adds_null_email_and_preserves_session_handoff(version,tmp_path):
    path=tmp_path/'legacy.sqlite'
    with sqlite3.connect(path) as db:db.executescript(legacy_schema(version))
    legacy=object.__new__(Store);legacy.path=path;legacy.gateway_prefix='/gateway'
    who=legacy_person(legacy)
    token=legacy.handoff(who['token'],'/gateway/bootstrap',1000)
    pending=legacy.state('old-browser',1000)
    with legacy.connect() as db:
        before={t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in ('users','sessions','handoffs','login_states','activity_bindings')}
    reopened=Store(path)
    assert reopened.session(who['token'],1001)['email'] is None
    assert reopened.session(who['token'],1001)['uid']==who['uid']
    assert reopened.pending(pending['state'],'old-browser',1001)
    with reopened.connect() as db:
        assert [r[0] for r in db.execute('SELECT version FROM schema_versions')]==[5]
        for table,rows in before.items():
            actual=[tuple(r) for r in db.execute('SELECT * FROM '+table)]
            if table=='sessions':
                assert [r[:-1] for r in actual]==rows and all(r[-1] is None for r in actual)
            else:assert actual==rows
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert Store(path).consume_handoff(token,1001)['uid']==who['uid']


@pytest.mark.parametrize('version',[3,4])
def test_email_migration_failure_rolls_back_both_schema_and_rows(version,tmp_path,monkeypatch):
    import gateway.store as module
    path=tmp_path/'legacy-failure.sqlite'
    with sqlite3.connect(path) as db:db.executescript(legacy_schema(version))
    legacy=object.__new__(Store);legacy.path=path;legacy.gateway_prefix='/gateway'
    who=legacy_person(legacy)
    original=sqlite3.connect
    class Connection(sqlite3.Connection):
        def execute(self,sql,*args):
            if sql=='ALTER TABLE sessions ADD COLUMN email TEXT':raise sqlite3.OperationalError('synthetic email migration I/O failure')
            return super().execute(sql,*args)
    monkeypatch.setattr(module.sqlite3,'connect',lambda *a,**kw:original(*a,factory=Connection,**kw))
    with pytest.raises(sqlite3.OperationalError,match='synthetic email migration'):Store(path)
    with original(path) as db:
        assert db.execute('SELECT version FROM schema_versions').fetchone()==(version,)
        columns=db.execute('PRAGMA table_info(sessions)').fetchall()
        assert len(columns)==9 and columns[6][3]==(1 if version==3 else 0)
        assert db.execute('SELECT uid FROM users').fetchone()==(who['uid'],)
        assert db.execute('SELECT count(*) FROM sessions').fetchone()==(1,)
