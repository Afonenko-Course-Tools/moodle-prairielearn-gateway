import copy
import httpx
import pytest
from gateway.courses import CoursesConfiguration, create_courses_app
from test_config import example
from test_assignment import Bridge, delivery, person
from gateway.store import Store
from joserfc import jwt
from joserfc.jwk import RSAKey
from gateway.launch import LTI, INSTRUCTOR, LEARNER, SCORE_SCOPE

def configs(tmp_path):
    courses={}
    for name in ['java','system-practice']:
        value=example();value.update(routePrefix='/gateway/courses/'+name,database=str(tmp_path/(name+'.sqlite')),deliveryPath='/native/'+name)
        value['registration']['activities']={'lab':{'kind':'lab','resourceLinkId':name,'lineitem':'https://moodle.example/lineitems/'+name,'workId':'lab'}}
        courses[name]=value
    return {'schemaVersion':2,'courses':courses}

def test_course_configs_allow_one_moodle_context_but_reject_state_and_route_collisions(tmp_path):
    value=configs(tmp_path); cfg=CoursesConfiguration.model_validate(value)
    assert cfg.courses['java'].registration.contextId==cfg.courses['system-practice'].registration.contextId
    for field in ['database','routePrefix']:
        broken=copy.deepcopy(value);broken['courses']['system-practice'][field]=broken['courses']['java'][field]
        with pytest.raises(ValueError):CoursesConfiguration.model_validate(broken)
    broken=copy.deepcopy(value);broken['courses']['system-practice']['privateKeyFile']='/private/different-client-key.json'
    with pytest.raises(ValueError):CoursesConfiguration.model_validate(broken)

@pytest.mark.anyio
async def test_two_courses_reject_foreign_sessions_and_restart_without_losing_other_course(tmp_path):
    cfg=CoursesConfiguration.model_validate(configs(tmp_path)); stores={};people={}
    class Clients:
        bridge=Bridge()
        class AGS:
            async def score(self,*args):pass
        ags=AGS()
    def factory(config):
        from gateway.app import create_app
        store=Store(config.database);stores[config.routePrefix]=store
        if config.routePrefix not in people:
            people[config.routePrefix]=person(store,'same-moodle-student')
            with store.transaction() as db:
                db.execute("UPDATE sessions SET activity='lab'")
        return create_app(config,store=store,delivery=delivery(),clients=Clients(),clock=lambda:1000,start_worker=False)
    app=create_courses_app(cfg,factory=factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://course.example') as c:
        for prefix,who in people.items():
            c.cookies.clear();c.cookies.set('GatewaySession',who['token'],domain='course.example',path=prefix)
            assert (await c.get(prefix+'/my/works')).status_code==200
            other=next(p for p in people if p!=prefix)
            assert (await c.get(other+'/my/works')).status_code==403
            home=await c.get(prefix+'/'); assert prefix+'/my/works' in home.text
            assert (await c.get('/gateway/my/works')).status_code==404
    # Each DB remains authoritative when all app objects are recreated.
    restarted=create_courses_app(cfg,factory=factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(restarted),base_url='https://course.example') as c:
        for prefix,who in people.items():
            c.cookies.clear();c.cookies.set('GatewaySession',who['token'],domain='course.example',path=prefix)
            assert (await c.get(prefix+'/my/works')).status_code==200

@pytest.mark.anyio
@pytest.mark.parametrize("client_id", ["client", None])
@pytest.mark.parametrize("role", ["student", "teacher", "administrator"])
async def test_one_lti_registration_routes_two_signed_resources_in_shared_context(tmp_path, client_id, role):
    values=configs(tmp_path)
    for course in values['courses'].values(): course['administratorSubject']='owner'
    cfg=CoursesConfiguration.model_validate(values);key=RSAKey.generate_key(parameters={'kid':'shared-moodle'})
    class Clients:
        class NativeBridge(Bridge):
            async def enroll(self, uid, instance, delivery_hash): pass
            async def work_launch(self, uid, instance, delivery_hash, work_id):
                assert work_id=='lab'
                return '/pl/course_instance/1/assessment/2/'
            async def administrator(self, uid):
                return {'path':'/pl/administrator/courses'}
        bridge=NativeBridge()
        async def jwks(self): return {'keys':[key.as_dict(private=False)]}
        class AGS:
            async def score(self,*args):pass
        ags=AGS()
    def factory(config):
        from gateway.app import create_app
        return create_app(config,delivery=delivery(),clients=Clients(),clock=lambda:1000,start_worker=False)
    app=create_courses_app(cfg,factory=factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://course.example') as c:
        for name,course in cfg.courses.items():
            target=course.publicOrigin+course.routePrefix+'/lti/launch'
            params={'iss':'https://moodle.example','target_link_uri':target,'login_hint':'hint','lti_message_hint':'message'}
            if client_id is not None: params['client_id']=client_id
            response=await c.post('/gateway/lti/login',data=params)
            assert response.status_code==303
            assert 'Path='+course.routePrefix in response.headers['set-cookie']
            oidc=httpx.URL(response.headers['location']).params
            assert oidc['client_id']=='client'
            claims={'iss':'https://moodle.example','aud':'client','sub':'owner' if role=='administrator' else 'same-person','iat':990,'exp':1100,'nonce':oidc['nonce'],LTI+'deployment_id':'deploy',LTI+'context':{'id':'context'},LTI+'resource_link':{'id':name},LTI+'roles':[LEARNER if role=='student' else INSTRUCTOR],LTI+'message_type':'LtiResourceLinkRequest',LTI+'version':'1.3.0',LTI+'target_link_uri':target,'https://purl.imsglobal.org/spec/lti-ags/claim/endpoint':{'lineitem':'https://moodle.example/lineitems/'+name,'scope':[SCORE_SCOPE]}}
            token=jwt.encode({'alg':'RS256','kid':key.kid},claims,key,algorithms=['RS256'])
            launch=await c.post(course.routePrefix+'/lti/launch',data={'state':oidc['state'],'id_token':token})
            assert launch.status_code==303
            assert launch.headers['location'].startswith(course.routePrefix+'/pl/')
            child=app.state.courses[name]
            child.state.handoff_secret='s'*32
            opaque=launch.headers['location'].rsplit('/',1)[-1]
            internal='/internal/handoff/courses/'+name+'/'+opaque
            headers={'Authorization':'Bearer '+'s'*32}
            handoff=await c.get(internal,headers=headers)
            assert handoff.status_code==200
            target=course.routePrefix+('/administrator-bootstrap' if role=='administrator' else '/bootstrap')+'/'+opaque
            assert handoff.json()['target']==target
            assert (await c.get(internal,headers=headers)).status_code==403
            bootstrap=await c.get(target)
            assert bootstrap.status_code==303
            expected={'student':'/pl/course_instance/1/assessment/2/', 'teacher':'/pl/', 'administrator':'/pl/administrator/courses'}
            assert bootstrap.headers['location']==expected[role]
            assert (await c.get(target)).status_code==403
            assert ('pl2_access_as_administrator=active' in bootstrap.headers.get('set-cookie','')) == (role=='administrator')
        uids=[]
        for child in app.state.courses.values():
            with child.state.store.connect() as db:uids.append(db.execute('SELECT uid FROM users').fetchone()[0])
        assert len(set(uids))==1
        assert (await c.get('/gateway/lti/login',params={'target_link_uri':'https://evil.example'})).status_code==403
