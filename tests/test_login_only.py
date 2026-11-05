"""Синтетическая приёмка входа в штатный список курса без оценивания."""
import asyncio
import copy
import json
import sqlite3
from pathlib import Path
import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from gateway.app import create_app
from gateway.config import Configuration
from gateway.courses import CoursesConfiguration
from gateway.errors import GatewayError
from gateway.launch import LTI, LEARNER, INSTRUCTOR
from gateway.pl import PLClient
from gateway.store import Store, digest
from test_config import example
from test_assignment import person, Bridge, delivery
from gateway.assignments import Assignments
from test_launch import env, attempt


def login_config(tmp_path):
    value=example()
    value.update(mode='login-only',courseKey='demo',database=str(tmp_path/'runtime.sqlite'))
    for key in ('deliveryPath','flow'):value.pop(key)
    value['registration'].pop('tokenUrl')
    value['registration']['activities']={'catalog':{'kind':'catalog','resourceLinkId':'resource'}}
    return value


def test_login_configuration_has_no_delivery_or_ags_prerequisites(tmp_path):
    cfg=Configuration.model_validate(login_config(tmp_path))
    assert cfg.mode=='login-only' and cfg.courseKey=='demo'
    assert cfg.deliveryPath is None and cfg.flow is None
    assert cfg.registration.tokenUrl is None
    assert cfg.registration.activities['catalog'].lineitem is None


@pytest.mark.parametrize('mutate',[
    lambda c:c.update(courseKey=None),lambda c:c.update(courseKey='../demo'),
    lambda c:c.update(deliveryPath='/private/delivery'),lambda c:c.update(flow={'mode':'immediate'}),
    lambda c:c.update(administratorSubject='owner'),
    lambda c:c['registration']['activities']['catalog'].update(kind='lab',workId='lab'),
    lambda c:c['registration'].update(tokenUrl='https://evil.example/token'),
    lambda c:c['registration']['activities']['catalog'].update(lineitem='https://evil.example/score'),
])
def test_login_configuration_rejects_ambiguous_or_unsafe_scope(tmp_path,mutate):
    value=login_config(tmp_path);mutate(value)
    with pytest.raises(ValueError):Configuration.model_validate(value)


def test_legacy_configuration_still_requires_grade_contract():
    for mutate in (lambda c:c.pop('deliveryPath'),lambda c:c.pop('flow'),lambda c:c['registration'].pop('tokenUrl'),lambda c:c['registration']['activities']['defense'].pop('lineitem')):
        value=example();mutate(value)
        with pytest.raises(ValueError):Configuration.model_validate(value)


@pytest.mark.anyio
async def test_enrollment_returns_typed_scoped_native_course_path():
    payload={'userUid':'gw-synthetic@example.invalid','instance':'pilot','courseKey':'demo'}
    answer={'schemaVersion':1,**payload,'enrolled':True,'path':'/pl/course_instance/42/assessments'}
    seen=[]
    def endpoint(request):
        seen.append(request)
        return httpx.Response(200,json=answer)
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        result=await PLClient(http,'http://private-pl:3001','s'*32).enroll(**dict(zip(('uid','instance','delivery'),payload.values())))
    assert result==answer
    assert json.loads(seen[0].content)==payload
    assert seen[0].headers['authorization']=='Bearer '+'s'*32


@pytest.mark.anyio
@pytest.mark.parametrize('change',[
    {'path':'//evil.example/'},{'path':'https://evil.example/pl/course_instance/42/assessments'},
    {'path':'/pl/course_instance/0/assessments'},{'path':'/pl/course_instance/42/assessments?role=Administrator'},
    {'path':'/pl/course_instance/42/assessments/'},{'path':42},{'path':'/pl/course_instance/42/assessment/9/'},
    {'enrolled':1},{'schemaVersion':True},{'userUid':'other'},{'instance':'other'},{'courseKey':'other'},{'role':'Administrator'},
])
async def test_enrollment_rejects_wrong_scope_shape_type_or_path(change):
    answer={'schemaVersion':1,'userUid':'uid','instance':'pilot','courseKey':'demo','enrolled':True,'path':'/pl/course_instance/42/assessments',**change}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=answer))) as http:
        with pytest.raises(GatewayError):await PLClient(http,'http://private-pl:3001','s'*32).enroll('uid','pilot','demo')


@pytest.mark.anyio
@pytest.mark.parametrize('role',[LEARNER,INSTRUCTOR])
async def test_signed_login_keeps_uid_and_uses_oneuse_native_bootstrap_without_grade_services(tmp_path,monkeypatch,role):
    import gateway.app as module
    def forbidden(*a,**kw):raise AssertionError('Login-only must not compose grading services')
    for name in ('load_delivery','Assignments','Grades','AGSClient'):monkeypatch.setattr(module,name,forbidden)
    key=RSAKey.generate_key(parameters={'kid':'synthetic-moodle'})
    calls=[]
    class Clients:
        class Bridge:
            async def enroll(self,uid,instance,delivery):
                calls.append((uid,instance,delivery))
                return {'path':'/pl/course_instance/42/assessments'}
        bridge=Bridge()
        async def jwks(self):return {'keys':[key.as_dict(private=False)]}
    app=create_app(login_config(tmp_path),clients=Clients(),clock=lambda:1000)
    assert app.state.assignments is None and app.state.grades is None
    prefix='/gateway/courses/demo'
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://course.example') as client:
            response=await client.get(prefix+'/lti/login',params={'iss':'https://moodle.example','target_link_uri':'https://course.example'+prefix+'/lti/launch','login_hint':'hint','lti_message_hint':'message'})
            oidc=httpx.URL(response.headers['location']).params
            claims={'iss':'https://moodle.example','aud':'client','sub':'same-person','iat':990,'exp':1100,'nonce':oidc['nonce'],LTI+'deployment_id':'deploy',LTI+'context':{'id':'context'},LTI+'resource_link':{'id':'resource'},LTI+'roles':[role],LTI+'message_type':'LtiResourceLinkRequest',LTI+'version':'1.3.0',LTI+'target_link_uri':'https://course.example'+prefix+'/lti/launch'}
            body={'state':oidc['state'],'id_token':jwt.encode({'alg':'RS256','kid':key.kid},claims,key,algorithms=['RS256'])}
            launched=await client.post(prefix+'/lti/launch',data=body)
            assert launched.status_code==303
            who=app.state.store.session(client.cookies['GatewaySession'],1000)
            expected='gw-'+digest(['https://moodle.example','client','deploy','same-person'])+'@example.invalid'
            assert who['uid']==expected and who['lineitem'] is None
            opaque=launched.headers['location'].rsplit('/',1)[1]
            target=prefix+'/bootstrap/'+opaque
            assert target in launched.headers['set-cookie']
            assert (await client.get(target)).status_code==403
            app.state.handoff_secret='s'*32
            endpoint='/internal/handoff/courses/demo/'+opaque
            assert (await client.get(endpoint)).status_code==403
            handoff=await client.get(endpoint,headers={'Authorization':'Bearer '+'s'*32})
            assert handoff.json()['uid']==expected and handoff.json()['target']==target
            boot=await client.get(target)
            assert boot.status_code==303 and boot.headers['location']=='/pl/course_instance/42/assessments'
            assert 'pl2_access_as_administrator' not in boot.headers.get('set-cookie','')
            assert (await client.get(target)).status_code==403
            assert (await client.post(prefix+'/lti/launch',data=body)).status_code==403
            for path in ('/my/works','/teacher/roster','/administrator-bootstrap','/bootstrap'):
                assert (await client.get(prefix+path)).status_code==404
            assert (await client.post(prefix+'/launch/administrator',json={})).status_code==404
            assert calls==[(expected,'pilot','demo')]
    with app.state.store.connect() as db:
        for table in ('activity_bindings','assignments','requests','observations','outbox'):
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0


def test_migration_v3_preserves_all_old_rows_and_allows_explicit_login_without_binding(tmp_path):
    path=tmp_path/'legacy.sqlite'
    schema=Path('schema/runtime.sql').read_text().replace('VALUES(4)','VALUES(3)').replace('lineitem TEXT,','lineitem TEXT NOT NULL,')
    with sqlite3.connect(path) as db:db.executescript(schema)
    old=object.__new__(Store);old.path=path;old.gateway_prefix="/gateway"
    who=person(old)
    handoff=old.handoff(who['token'],'/gateway/bootstrap',1000)
    state=old.state('browser2',1000)
    assignment=asyncio.run(Assignments(old,Bridge(),delivery(),'pilot',{'mode':'immediate','capacity':{}},clock=lambda:1000).request(who,'exr-essay-a','old-request'))
    with old.transaction() as db:
        db.execute("INSERT INTO observations VALUES('old-event',?,1,1,'{}',1000)",(assignment['id'],))
        db.execute("INSERT INTO native_attempts VALUES('scope',1,'pilot','demo','lab','attempt',1,'[]')")
        db.execute("INSERT INTO outbox(id,assignment_id,version,user_id,context,resource,delivery,lineitem,payload,created) VALUES('old-outbox',?,1,?,'context','resource','demo','https://moodle.example/score','{}',1000)",(assignment['id'],who['userId']))
    with old.connect() as db:
        before={t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in ('users','sessions','handoffs','login_states','activity_bindings','assignments','requests','audit','observations','outbox','native_attempts')}
    reopened=Store(path)
    assert reopened.session(who['token'],1001)['uid']==who['uid']
    with reopened.connect() as db:
        assert [r[0] for r in db.execute('SELECT version FROM schema_versions')]==[4]
        for t,rows in before.items():assert [tuple(r) for r in db.execute('SELECT * FROM '+t)]==rows
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    identity={**who,'activity':'catalog','lineitem':None}
    new=reopened.activate(state['state'],'browser2',1000,identity,login_only=True)
    assert new['uid']==who['uid']
    assert reopened.consume_handoff(handoff,1001)['uid']==who['uid']
    p=reopened.state('ordinary',1000)
    with pytest.raises(GatewayError):reopened.activate(p['state'],'ordinary',1000,identity)
    assert reopened.pending(p['state'],'ordinary',1000)


def test_named_login_courses_require_unique_database_resource_and_native_binding(tmp_path):
    first=login_config(tmp_path)
    second=copy.deepcopy(first)
    second.update(routePrefix='/gateway/courses/other',database=str(tmp_path/'other.sqlite'),courseKey='other')
    second['registration']['activities']['catalog']['resourceLinkId']='other'
    value={'schemaVersion':2,'courses':{'demo':first,'other':second}}
    assert len(CoursesConfiguration.model_validate(value).courses)==2
    second['courseKey']='demo'
    with pytest.raises(ValueError):CoursesConfiguration.model_validate(value)


@pytest.mark.parametrize('mutate',[
    lambda c:c.update(iss='https://evil.example'),lambda c:c.update(aud='other'),
    lambda c:c.update(nonce='wrong'),lambda c:c.update(exp=1000),
    lambda c:c.update({LTI+'deployment_id':'other'}),lambda c:c.update({LTI+'context':{'id':'other'}}),
    lambda c:c.update({LTI+'resource_link':{'id':'other'}}),lambda c:c.update({LTI+'roles':['Administrator']}),
    lambda c:c.update({LTI+'target_link_uri':'https://evil.example'}),
])
def test_login_only_retains_signature_claim_scope_guards(env,mutate):
    env[1].login_only=True
    def claims(c):
        c.pop('https://purl.imsglobal.org/spec/lti-ags/claim/endpoint')
        mutate(c)
    with pytest.raises(GatewayError):attempt(env,claims)


def test_login_only_uses_previous_uid_for_both_roles_and_restart(env):
    original,_,_=attempt(env)
    env[1].login_only=True
    def claims(c):
        c.pop('https://purl.imsglobal.org/spec/lti-ags/claim/endpoint')
        c[LTI+'roles']=[INSTRUCTOR]
    second,_,_=attempt(env,claims)
    assert second['uid']==original['uid']
    assert Store(env[0].path).session(second['token'],1001)['uid']==original['uid']


@pytest.mark.parametrize('version',[2,1001,5])
def test_unrecognized_database_version_is_rejected_without_write(tmp_path,version):
    path=tmp_path/'unsupported.sqlite'
    with sqlite3.connect(path) as db:db.executescript(Path('schema/runtime.sql').read_text().replace('VALUES(4)',f'VALUES({version})'))
    before=path.read_bytes()
    with pytest.raises(GatewayError):Store(path)
    assert path.read_bytes()==before


def test_v3_migration_failure_rolls_back_schema_rows_and_version(tmp_path,monkeypatch):
    import gateway.store as module
    path=tmp_path/'rollback.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript(Path('schema/runtime.sql').read_text().replace('VALUES(4)','VALUES(3)').replace('lineitem TEXT,','lineitem TEXT NOT NULL,'))
    old=object.__new__(Store);old.path=path;old.gateway_prefix='/gateway'
    who=person(old)
    original=sqlite3.connect
    class Connection(sqlite3.Connection):
        def execute(self,sql,*args):
            if sql=='ALTER TABLE sessions_v4 RENAME TO sessions':raise sqlite3.OperationalError('synthetic I/O failure')
            return super().execute(sql,*args)
    monkeypatch.setattr(module.sqlite3,'connect',lambda *a,**kw:original(*a,factory=Connection,**kw))
    with pytest.raises(sqlite3.OperationalError,match='synthetic I/O failure'):Store(path)
    with original(path) as db:
        assert db.execute('SELECT version FROM schema_versions').fetchone()==(3,)
        assert db.execute('PRAGMA table_info(sessions)').fetchall()[6][3]==1
        assert db.execute('SELECT uid FROM users').fetchone()==(who['uid'],)
        assert db.execute('SELECT count(*) FROM sessions').fetchone()==(1,)
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='sessions_v4'").fetchone()==(0,)


@pytest.mark.anyio
async def test_real_login_composition_does_not_create_ags_or_worker(tmp_path,monkeypatch):
    import gateway.app as module
    value=login_config(tmp_path)
    key=RSAKey.generate_key(parameters={'kid':'synthetic-tool'})
    for name,content in (('privateKeyFile',json.dumps(key.as_dict(private=True))),('handoffTokenFile','h'*32)):
        path=tmp_path/name;path.write_text(content);value[name]=str(path)
    token=tmp_path/'bridge-token';token.write_text('b'*32);value['bridge']['tokenFile']=str(token)
    def forbidden(*args,**kwargs):raise AssertionError('Login-only must not start grading composition')
    for name in ('AGSClient','Assignments','Grades','load_delivery'):monkeypatch.setattr(module,name,forbidden)
    monkeypatch.setattr(module.asyncio,'create_task',forbidden)
    app=create_app(value)
    async with app.router.lifespan_context(app):
        assert app.state.assignments is None and app.state.grades is None
