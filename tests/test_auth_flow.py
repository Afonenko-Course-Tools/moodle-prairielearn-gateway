"""Проверки одноразового входа на реальных подписанных LTI сообщениях."""
import asyncio
import copy
import json
import sqlite3
from contextlib import asynccontextmanager
import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from gateway.app import create_app
from gateway.store import Store
from gateway.errors import GatewayError
from gateway.launch import LTI, LEARNER, INSTRUCTOR

PREFIX='/gateway/courses/demo'
UID='gw-9ad555084e0e7c714ab30ad1f034099644cc0b334cd655fc26740205e39e818f@example.invalid'


def configuration(tmp_path):
    return {'publicOrigin':'https://course.example','routePrefix':PREFIX,'database':str(tmp_path/'auth.sqlite'),
            'handoffTokenFile':'/private/handoff',
            'bridge':{'url':'http://prairielearn:3001','tokenFile':'/private/bridge'},'courseKey':'demo','instance':'pilot',
            'registration':{'issuer':'https://moodle.example','clientId':'client','deploymentId':'deploy','contextId':'context',
                            'authUrl':'https://moodle.example/auth','jwksUrl':'https://moodle.example/certs',
                            'activities':{'catalog':{'resourceLinkId':'resource'}}}}


class Platform:
    def __init__(self):
        self.key=RSAKey.generate_key(parameters={'kid':'moodle-test'})
        self.calls=[]
        self.failure=None
        self.entered=None
        self.release=None
        self.bridge=self
    async def jwks(self):return {'keys':[self.key.as_dict(private=False)]}
    async def enroll(self,uid,instance,course,*,role,email):
        self.calls.append((uid,instance,course,role,email))
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.failure:raise self.failure
        return {'schemaVersion':1,'userUid':uid,'instance':instance,'courseKey':course,'enrolled':True,'path':'/pl/course_instance/42/assessments'}


@asynccontextmanager
async def browser_for(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://course.example') as browser:yield browser


async def begin(browser,platform,*,prefix=PREFIX,role=LEARNER,email=None,name='Участник',sub='student-a',login_path=None):
    target='https://course.example'+prefix+'/lti/launch'
    opened=await browser.get(login_path or prefix+'/lti/login',params={'iss':'https://moodle.example','target_link_uri':target,'login_hint':'hint','lti_message_hint':'message'})
    assert opened.status_code==303
    oidc=httpx.URL(opened.headers['location']).params
    claims={'iss':'https://moodle.example','aud':'client','sub':sub,'iat':990,'exp':1100,'nonce':oidc['nonce'],
            LTI+'deployment_id':'deploy',LTI+'context':{'id':'context'},LTI+'resource_link':{'id':'resource'},
            LTI+'roles':[role],LTI+'message_type':'LtiResourceLinkRequest',LTI+'version':'1.3.0',LTI+'target_link_uri':target,'name':name}
    if email is not None:claims['email']=email
    body={'state':oidc['state'],'id_token':jwt.encode({'alg':'RS256','kid':platform.key.kid},claims,platform.key,algorithms=['RS256'])}
    return opened,body


async def activate(browser,platform,**kwargs):
    opened,body=await begin(browser,platform,**kwargs)
    launched=await browser.post(kwargs.get('prefix',PREFIX)+'/lti/launch',data=body)
    assert launched.status_code==303
    flow=launched.headers['location'].rsplit('/',1)[1]
    return opened,launched,flow


async def handoff(browser,flow,prefix=PREFIX):
    return await browser.get('/internal/handoff'+prefix.removeprefix('/gateway')+'/'+flow,headers={'Authorization':'Bearer '+'h'*32})


@pytest.mark.anyio
async def test_only_login_sets_one_shared_nonce_and_callbacks_never_mutate_it(tmp_path):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        opened,launched,flow=await activate(browser,platform)
        assert 'GatewayLogin=' in opened.headers['set-cookie']
        assert 'Path=/gateway;' in opened.headers['set-cookie']
        assert all(x in opened.headers['set-cookie'] for x in ('Secure','HttpOnly','SameSite=none','Max-Age=300'))
        assert 'set-cookie' not in launched.headers
        private=await handoff(browser,flow)
        assert private.status_code==200 and 'set-cookie' not in private.headers
        assert private.json()['uid']==UID and private.json()['email']==UID
        boot=await browser.get(private.json()['target'])
        assert boot.status_code==303 and 'set-cookie' not in boot.headers
        assert (await browser.get(private.json()['target'])).status_code==403
        assert (await browser.get(PREFIX+'/')).status_code==404
        assert len(platform.calls)==1


@pytest.mark.anyio
async def test_success_or_failed_bridge_cannot_reissue_handoff_from_home(tmp_path):
    platform=Platform();platform.failure=GatewayError('Тестовая ошибка',503)
    app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,_,flow=await activate(browser,platform)
        private=await handoff(browser,flow)
        target=private.json()['target']
        assert (await browser.get(target)).status_code==503
        assert (await browser.get(target)).status_code==403
        assert (await browser.get(PREFIX+'/')).status_code==404
        assert len(platform.calls)==1


@pytest.mark.anyio
@pytest.mark.parametrize('field',['instance','courseKey'])
async def test_restart_mapping_change_rejects_saved_launch_before_bridge(tmp_path,field):
    cfg=configuration(tmp_path);platform=Platform()
    app=create_app(cfg,clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,_,flow=await activate(browser,platform)
        private=await handoff(browser,flow)
        cookies=browser.cookies
    changed=copy.deepcopy(cfg);changed[field]='replacement'
    restarted=create_app(changed,clients=platform,clock=lambda:1001)
    async with browser_for(restarted) as browser:
        browser.cookies.update(cookies)
        assert (await browser.get(private.json()['target'])).status_code==403
    assert platform.calls==[]


def test_auth_database_has_no_registry_or_legacy_tables(tmp_path):
    store=Store(tmp_path/'fresh.sqlite')
    with store.connect() as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'users' not in tables and 'sessions' not in tables and 'assignments' not in tables


def test_legacy_database_is_refused_without_mutation(tmp_path):
    path=tmp_path/'old.sqlite'
    with sqlite3.connect(path) as db:db.executescript("CREATE TABLE schema_versions(version INTEGER PRIMARY KEY); INSERT INTO schema_versions VALUES(5); CREATE TABLE users(id TEXT PRIMARY KEY,uid TEXT); INSERT INTO users VALUES('old','preserved');")
    path.chmod(0o640)
    before=path.read_bytes()
    with pytest.raises(GatewayError):Store(path)
    assert path.read_bytes()==before and path.stat().st_mode&0o777==0o640
    assert list(tmp_path.iterdir())==[path]


@pytest.mark.anyio
async def test_new_login_invalidates_exact_old_flow_even_for_same_user(tmp_path):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,_,old=await activate(browser,platform,email='old@example.invalid')
        old_target=(await handoff(browser,old)).json()['target']
        _,_,new=await activate(browser,platform,email='new@example.invalid')
        assert (await browser.get(old_target)).status_code==403
        private=await handoff(browser,new)
        assert (await browser.get(private.json()['target'])).status_code==303
        assert platform.calls[0][-1]=='new@example.invalid' and len(platform.calls)==1


@pytest.mark.anyio
async def test_late_launch_callback_cannot_replace_current_browser_credential(tmp_path):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,old_body=await begin(browser,platform)
        _,_,new=await activate(browser,platform)
        late=await browser.post(PREFIX+'/lti/launch',data=old_body)
        assert late.status_code==403 and 'set-cookie' not in late.headers
        private=await handoff(browser,new)
        assert (await browser.get(private.json()['target'])).status_code==303


@pytest.mark.anyio
async def test_bridge_await_consumes_old_attempt_before_new_login(tmp_path):
    platform=Platform();platform.entered=asyncio.Event();platform.release=asyncio.Event()
    app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,_,old=await activate(browser,platform)
        target=(await handoff(browser,old)).json()['target']
        pending=asyncio.create_task(browser.get(target))
        await platform.entered.wait()
        assert (await browser.get(target)).status_code==403
        _,_,new=await activate(browser,platform)
        platform.release.set()
        late=await pending
        assert 'set-cookie' not in late.headers
        private=await handoff(browser,new)
        assert private.status_code==200
        assert (await browser.get(private.json()['target'])).status_code==303


@pytest.mark.anyio
async def test_duplicate_cookie_cannot_consume_private_handoff(tmp_path):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,_,flow=await activate(browser,platform)
        credential=browser.cookies['GatewayLogin']
        bad=await browser.get('/internal/handoff/courses/demo/'+flow,headers={'Authorization':'Bearer '+'h'*32,'Cookie':'GatewayLogin='+credential+'; GatewayLogin='+credential})
        assert bad.status_code==403 and 'set-cookie' not in bad.headers
        assert (await handoff(browser,flow)).status_code==200


@pytest.mark.anyio
@pytest.mark.parametrize('field',['issuer','clientId','deploymentId','contextId','resource','activity','target'])
async def test_restart_registration_change_rejects_saved_launch(tmp_path,field):
    cfg=configuration(tmp_path);platform=Platform()
    app=create_app(cfg,clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,_,flow=await activate(browser,platform)
        target=(await handoff(browser,flow)).json()['target'];cookies=browser.cookies
    changed=copy.deepcopy(cfg)
    if field=='issuer':changed['registration'].update(issuer='https://other.example',authUrl='https://other.example/auth',jwksUrl='https://other.example/certs')
    elif field=='resource':changed['registration']['activities']['catalog']['resourceLinkId']='replacement'
    elif field=='activity':changed['registration']['activities']={'replacement':{'resourceLinkId':'resource'}}
    elif field=='target':changed['publicOrigin']='https://other.example'
    else:changed['registration'][field]='replacement'
    restarted=create_app(changed,clients=platform,clock=lambda:1001)
    async with browser_for(restarted) as browser:
        browser.cookies.update(cookies)
        denied=await browser.get(target)
        assert denied.status_code==403 and 'set-cookie' not in denied.headers
    assert platform.calls==[]


@pytest.mark.anyio
@pytest.mark.parametrize('stage',['launch','bootstrap','login'])
async def test_duplicate_cookie_is_rejected_without_mutating_current_flow(tmp_path,stage):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,body=await begin(browser,platform)
        nonce=browser.cookies['GatewayLogin']
        headers={'Cookie':'GatewayLogin='+nonce+'; GatewayLogin='+nonce}
        if stage=='launch':
            denied=await browser.post(PREFIX+'/lti/launch',data=body,headers=headers)
            valid=await browser.post(PREFIX+'/lti/launch',data=body)
        else:
            valid=await browser.post(PREFIX+'/lti/launch',data=body)
            flow=valid.headers['location'].rsplit('/',1)[1]
            target=(await handoff(browser,flow)).json()['target']
            if stage=='bootstrap':denied=await browser.get(target,headers=headers)
            else:denied=await browser.get(PREFIX+'/lti/login',headers=headers)
        assert denied.status_code==403 and 'set-cookie' not in denied.headers
        assert valid.status_code==303 and platform.calls==[]
        if stage!='launch':assert (await browser.get(target)).status_code==303


@pytest.mark.anyio
async def test_first_parallel_logins_require_the_effective_browser_nonce(tmp_path):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000)
    async with browser_for(app) as first, browser_for(app) as second:
        _,old=await begin(first,platform)
        _,current=await begin(second,platform)
        denied=await second.post(PREFIX+'/lti/launch',data=old)
        assert denied.status_code==403 and 'set-cookie' not in denied.headers
        assert (await second.post(PREFIX+'/lti/launch',data=current)).status_code==303
        assert platform.calls==[]


@pytest.mark.anyio
async def test_wrong_browser_and_unconsumed_handoff_cannot_bootstrap(tmp_path):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser, browser_for(app) as foreign:
        _,_,flow=await activate(browser,platform)
        target=PREFIX+'/bootstrap/'+flow
        assert (await browser.get(target)).status_code==403
        private=await handoff(browser,flow)
        assert (await foreign.get(target,headers={'Cookie':'GatewayLogin='+'a'*43})).status_code==403
        assert platform.calls==[]
        assert (await browser.get(private.json()['target'])).status_code==303


@pytest.mark.anyio
@pytest.mark.parametrize('now',[1060,1300])
async def test_handoff_never_outlives_attempt_or_sixty_seconds(tmp_path,now):
    platform=Platform();clock=[1000]
    app=create_app(configuration(tmp_path),clients=platform,clock=lambda:clock[0]);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,_,flow=await activate(browser,platform)
        target=(await handoff(browser,flow)).json()['target']
        clock[0]=now
        assert (await browser.get(target)).status_code==403 and platform.calls==[]


@pytest.mark.anyio
async def test_late_launch_does_not_extend_original_attempt_ttl(tmp_path):
    platform=Platform();clock=[1000]
    app=create_app(configuration(tmp_path),clients=platform,clock=lambda:clock[0]);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,body=await begin(browser,platform)
        clock[0]=1290
        claims=jwt.decode(body['id_token'],platform.key,algorithms=['RS256']).claims
        claims.update(iat=1290,exp=1400)
        body['id_token']=jwt.encode({'alg':'RS256','kid':platform.key.kid},claims,platform.key,algorithms=['RS256'])
        launched=await browser.post(PREFIX+'/lti/launch',data=body)
        flow=launched.headers['location'].rsplit('/',1)[1]
        target=(await handoff(browser,flow)).json()['target']
        clock[0]=1300
        assert (await browser.get(target)).status_code==403 and platform.calls==[]


@pytest.mark.anyio
async def test_cancellation_leaves_attempt_consumed_and_new_explicit_launch_works(tmp_path):
    platform=Platform();platform.entered=asyncio.Event();platform.release=asyncio.Event()
    app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,_,flow=await activate(browser,platform)
        target=(await handoff(browser,flow)).json()['target']
        pending=asyncio.create_task(browser.get(target))
        await platform.entered.wait();pending.cancel()
        with pytest.raises(asyncio.CancelledError):await pending
        assert (await browser.get(target)).status_code==403
        assert len(platform.calls)==1
        platform.entered=None
        _,_,fresh=await activate(browser,platform)
        fresh_target=(await handoff(browser,fresh)).json()['target']
        assert (await browser.get(fresh_target)).status_code==303
        assert len(platform.calls)==2


@pytest.mark.anyio
@pytest.mark.parametrize('role',[LEARNER,INSTRUCTOR])
async def test_saved_signed_identity_role_email_only_reach_bridge(tmp_path,role):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
    async with browser_for(app) as browser:
        _,_,flow=await activate(browser,platform,role=role,email=' Owner@Example.Invalid ')
        private=await handoff(browser,flow)
        assert private.json()=={'uid':UID,'displayName':'Участник','email':UID,'target':PREFIX+'/bootstrap/'+flow}
        answer=await browser.get(private.json()['target']+'?email=forged@example.invalid&role=Administrator',headers={'X-Gateway-Email':'forged@example.invalid','X-Gateway-Role':'Administrator'})
        assert answer.status_code==303 and answer.headers['location']=='/pl/course_instance/42/assessments'
        assert platform.calls==[(UID,'pilot','demo','teacher' if role==INSTRUCTOR else 'student','owner@example.invalid')]
        for path in ('/','/my/works','/teacher/roster','/administrator-bootstrap','/bootstrap','/.well-known/jwks.json'):
            assert (await browser.get(PREFIX+path)).status_code==404
        assert (await browser.post(PREFIX+'/launch/administrator',json={})).status_code==404
        assert (await browser.get(PREFIX+'/bootstrap/'+flow)).status_code==403
        assert (await handoff(browser,flow)).status_code==403


@pytest.mark.anyio
async def test_malformed_duplicate_cookie_does_not_silently_choose_valid_value(tmp_path):
    platform=Platform();app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000)
    async with browser_for(app) as browser:
        _,body=await begin(browser,platform)
        nonce=browser.cookies['GatewayLogin']
        denied=await browser.post(PREFIX+'/lti/launch',data=body,headers={'Cookie':'GatewayLogin; GatewayLogin='+nonce})
        assert denied.status_code==403 and 'set-cookie' not in denied.headers
        assert (await browser.post(PREFIX+'/lti/launch',data=body)).status_code==303


@pytest.mark.anyio
async def test_restart_keeps_only_exact_transient_flow_until_native_success(tmp_path):
    cfg=configuration(tmp_path);platform=Platform()
    first=create_app(cfg,clients=platform,clock=lambda:1000);first.state.handoff_secret='h'*32
    async with browser_for(first) as browser:
        _,_,flow=await activate(browser,platform)
        cookies=browser.cookies
    restarted=create_app(cfg,clients=platform,clock=lambda:1001);restarted.state.handoff_secret='h'*32
    async with browser_for(restarted) as browser:
        browser.cookies.update(cookies)
        forged=await browser.get('/internal/handoff/courses/demo/'+flow,headers={'X-Verified-Uid':UID,'X-Trust-Auth-Uid':UID,'X-Gateway-Role':'teacher'})
        assert forged.status_code==403 and 'set-cookie' not in forged.headers
        private=await handoff(browser,flow)
        assert private.status_code==200
        assert (await handoff(browser,flow)).status_code==403
        assert (await browser.get(private.json()['target'])).status_code==303
    after_success=create_app(cfg,clients=platform,clock=lambda:1002)
    async with browser_for(after_success) as browser:
        browser.cookies.update(cookies)
        assert (await browser.get(private.json()['target'])).status_code==403
        assert (await browser.get(PREFIX+'/')).status_code==404
    assert len(platform.calls)==1


def test_expired_attempt_cleanup_is_bounded_and_valid_attempt_survives(tmp_path):
    store=Store(tmp_path/'auth.sqlite')
    for index in range(205):store.state('expired-'+str(index),0)
    valid=store.state('valid',200)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM auth_attempts').fetchone()[0]==206
    store.pending(valid['state'],'valid',301)
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM auth_attempts WHERE expires<=301').fetchone()[0]==105
    assert store.pending(valid['state'],'valid',302)['phase']==0


@pytest.mark.anyio
async def test_expired_pending_state_and_old_cookies_cannot_launch(tmp_path):
    platform=Platform();clock=[1000]
    app=create_app(configuration(tmp_path),clients=platform,clock=lambda:clock[0])
    async with browser_for(app) as browser:
        _,body=await begin(browser,platform)
        old=browser.cookies['GatewayLogin'];browser.cookies.clear()
        oldcookies={'Cookie':'GatewayBinding='+old+'; GatewaySession='+old}
        assert (await browser.post(PREFIX+'/lti/launch',data=body,headers=oldcookies)).status_code==403
        clock[0]=1300
        assert (await browser.post(PREFIX+'/lti/launch',data=body,headers={'Cookie':'GatewayLogin='+old})).status_code==403
    assert platform.calls==[]


def sqlite_files(directory):
    return {p.name:(p.read_bytes(),p.stat().st_mode&0o777) for p in directory.iterdir()}


def test_closed_legacy_wal_database_refused_without_creating_sidecars(tmp_path):
    path=tmp_path/'old.sqlite'
    db=sqlite3.connect(path)
    try:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()==('wal',)
        db.execute('CREATE TABLE users(uid TEXT)')
        db.execute("INSERT INTO users VALUES('synthetic')")
        db.commit()
    finally:db.close()
    path.chmod(0o640)
    assert set(sqlite_files(tmp_path))=={'old.sqlite'}
    before=sqlite_files(tmp_path)
    with pytest.raises(GatewayError):Store(path)
    assert sqlite_files(tmp_path)==before


def test_active_legacy_wal_database_refused_without_changing_existing_sidecars(tmp_path):
    path=tmp_path/'old.sqlite'
    db=sqlite3.connect(path)
    try:
        assert db.execute('PRAGMA journal_mode=WAL').fetchone()==('wal',)
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('CREATE TABLE users(uid TEXT)')
        db.commit()
        db.execute("INSERT INTO users VALUES('synthetic')")
        db.commit()
        for p in tmp_path.iterdir():p.chmod(0o640)
        before=sqlite_files(tmp_path)
        assert set(before)=={'old.sqlite','old.sqlite-wal','old.sqlite-shm'}
        assert before['old.sqlite-wal'][0] and before['old.sqlite-shm'][0]
        with pytest.raises(GatewayError):Store(path)
        assert sqlite_files(tmp_path)==before
    finally:db.close()


def test_auth_database_reopen_retains_pending_state_with_active_wal(tmp_path):
    path=tmp_path/'auth.sqlite';store=Store(path)
    db=store.connect()
    try:
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('SELECT count(*) FROM auth_attempts').fetchone()
        pending=store.state('browser',1000)
        assert (tmp_path/'auth.sqlite-wal').stat().st_size>0
        reopened=Store(path)
        assert reopened.pending(pending['state'],'browser',1001)['nonce']==pending['nonce']
    finally:db.close()
