import httpx
import pytest
from gateway.app import create_app
from gateway.config import Configuration
from gateway.errors import LaunchError
from gateway.store import Store
from test_assignment import Bridge, delivery, person
from test_config import example

@pytest.fixture
def admin_setup(tmp_path):
    value=example(); value['database']=str(tmp_path/'runtime.sqlite'); value['administratorSubject']='owner'
    config=Configuration.model_validate(value); store=Store(config.database)
    owner=person(store,'owner','teacher'); other=person(store,'other','teacher'); student=person(store,'student')
    with store.transaction() as db: db.execute("UPDATE users SET issuer='https://moodle.example'")
    granted=[]
    class Clients:
        bridge=Bridge()
        class AGS:
            async def score(self,*args): pass
        ags=AGS()
    async def administrator(uid):
        granted.append(uid)
        return {'schemaVersion':1,'userUid':uid,'administrator':True,'path':'/pl/administrator/courses'}
    clients=Clients();clients.bridge.administrator=administrator
    app=create_app(config,store=store,delivery=delivery(),clients=clients,clock=lambda:1000,start_worker=False)
    app.state.handoff_secret='s'*32
    return app,store,owner,other,student,granted

@pytest.mark.anyio
async def test_owner_admin_handoff_and_bootstrap(admin_setup):
    app,store,owner,_,_,granted=admin_setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://course.example',cookies={'GatewaySession':owner['token']}) as c:
        assert (await c.get('/gateway/courses/demo/my/works')).json()['administrator'] is True
        headers={'Origin':'https://course.example','X-CSRF-Token':owner['csrf']}
        assert (await c.post('/gateway/courses/demo/launch/administrator',json={})).status_code==403
        response=await c.post('/gateway/courses/demo/launch/administrator',json={},headers=headers)
        assert response.status_code==200
        target=response.json()['url'];assert target.startswith('/gateway/courses/demo/pl/') and not granted
        handoff=await c.get('/internal/handoff/courses/demo/'+target.split('/')[-1],headers={'Authorization':'Bearer '+'s'*32})
        assert handoff.json()['uid']==owner['uid'] and handoff.json()['target']=='/gateway/courses/demo/administrator-bootstrap'
        assert (await c.get('/internal/handoff/courses/demo/'+target.split('/')[-1],headers={'Authorization':'Bearer '+'s'*32})).status_code==403
        bootstrap=await c.get('/gateway/courses/demo/administrator-bootstrap')
        assert bootstrap.headers['location']=='/pl/administrator/courses'
        assert 'pl2_access_as_administrator=active' in bootstrap.headers['set-cookie']
        assert 'Secure' in bootstrap.headers['set-cookie'] and 'HttpOnly' in bootstrap.headers['set-cookie']
        assert granted==[owner['uid']]
        assert (await c.post('/gateway/courses/demo/launch/administrator',json={'userUid':'forged'},headers=headers)).status_code==422

@pytest.mark.anyio
async def test_only_owner_verified_teacher_can_get_admin(admin_setup):
    app,store,owner,other,student,granted=admin_setup
    for who in [other,student]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://course.example',cookies={'GatewaySession':who['token']}) as c:
            assert (await c.get('/gateway/courses/demo/my/works')).json()['administrator'] is False
            headers={'Origin':'https://course.example','X-CSRF-Token':who['csrf']}
            assert (await c.post('/gateway/courses/demo/launch/administrator',json={},headers=headers)).status_code==403
            assert (await c.get('/gateway/courses/demo/administrator-bootstrap')).status_code==403
    assert not granted
    with pytest.raises(LaunchError):store.handoff(student['token'],'/gateway/courses/demo/administrator-bootstrap',1000)

@pytest.mark.anyio
async def test_changed_registration_or_expired_session_never_grants_admin(admin_setup):
    app,store,owner,_,_,granted=admin_setup
    for column in ['issuer','client','deployment','subject']:
        with store.transaction() as db:
            old=db.execute('SELECT '+column+' FROM users WHERE id=?',(owner['userId'],)).fetchone()[0]
            db.execute('UPDATE users SET '+column+'=? WHERE id=?',('different',owner['userId']))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://course.example',cookies={'GatewaySession':owner['token']}) as c:
            assert (await c.get('/gateway/courses/demo/administrator-bootstrap')).status_code==403
        with store.transaction() as db:db.execute('UPDATE users SET '+column+'=? WHERE id=?',(old,owner['userId']))
    with store.transaction() as db:db.execute('UPDATE sessions SET expires=999')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://course.example',cookies={'GatewaySession':owner['token']}) as c:
        assert (await c.get('/gateway/courses/demo/administrator-bootstrap')).status_code==403
    assert not granted
