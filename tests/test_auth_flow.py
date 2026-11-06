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
UID='gw-1a2a1b8aac1e98801cd059e3a12b68408140654198d592e8a9d68173911b7ff9@example.invalid'


def configuration(tmp_path):
    return {'publicOrigin':'https://course.example','routePrefix':PREFIX,'database':str(tmp_path/'auth.sqlite'),
            'privateKeyFile':'/private/unused-key','handoffTokenFile':'/private/handoff','mode':'login-only',
            'bridge':{'url':'http://prairielearn:3001','tokenFile':'/private/bridge'},'courseKey':'demo','instance':'pilot',
            'registration':{'issuer':'https://moodle.example','clientId':'client','deploymentId':'deploy','contextId':'context',
                            'authUrl':'https://moodle.example/auth','jwksUrl':'https://moodle.example/certs',
                            'activities':{'catalog':{'kind':'catalog','resourceLinkId':'resource'}}}}


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
        return {'path':'/pl/course_instance/42/assessments'}


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
    with sqlite3.connect(path) as db:db.executescript(open('schema/runtime.sql').read())
    before=path.read_bytes()
    with pytest.raises(GatewayError):Store(path)
    assert path.read_bytes()==before


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
