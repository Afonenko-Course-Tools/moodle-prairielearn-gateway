import copy
import httpx
import pytest
from gateway.app import create_app
from gateway.config import Configuration
from gateway.store import Store, digest
from test_config import example
from test_assignment import Bridge, delivery, person

@pytest.mark.anyio
async def test_parallel_lab_bootstraps_keep_original_activity_and_reject_foreign_identity(tmp_path):
    value=example();value['database']=str(tmp_path/'state.sqlite')
    value['registration']['activities']={key:{'kind':'lab','resourceLinkId':key,'lineitem':'https://moodle.example/lineitems/'+key,'workId':key} for key in ('lab','lab-other')}
    config=Configuration.model_validate(value);store=Store(config.database)
    payload=delivery();other_work=copy.deepcopy(payload['works'][-1]);other_work['id']='lab-other'
    payload['works'].append(other_work);payload['instances']['pilot']['works'].append('lab-other')
    first=person(store,'same-student');second=person(store,'same-student');foreign=person(store,'other-student')
    for who,activity in ((first,'lab'),(second,'lab-other'),(foreign,'lab')):
        with store.transaction() as db: db.execute('UPDATE sessions SET activity=? WHERE token_hash=?',(activity,digest(who['token'])))
    class NativeBridge(Bridge):
        async def enroll(self,*args): pass
        async def work_launch(self,uid,instance,delivery_hash,work):
            return '/pl/course_instance/1/assessment/'+('2' if work=='lab' else '3')+'/'
    class Clients:
        bridge=NativeBridge()
        class AGS:
            async def score(self,*args): pass
        ags=AGS()
    app=create_app(config,store=store,delivery=payload,clients=Clients(),clock=lambda:1000,start_worker=False)
    app.state.handoff_secret='s'*32
    first_token,first_target=store.bootstrap_handoff(first['token'],False,1000)
    second_token,second_target=store.bootstrap_handoff(second['token'],False,1000)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://course.example',cookies={'GatewaySession':second['token']}) as c:
        # Native authentication must consume the handoff before bootstrap.
        assert (await c.get(first_target)).status_code==403
        for token in (first_token,second_token):
            assert (await c.get('/internal/handoff/courses/demo/'+token,headers={'Authorization':'Bearer '+'s'*32})).status_code==200
        # The shared cookie now points to B, while tab A returns from authentication.
        c.cookies.clear();c.cookies.set('GatewaySession',foreign['token'])
        assert (await c.get(first_target)).status_code==403
        c.cookies.clear();c.cookies.set('GatewaySession',second['token'])
        first_response=await c.get(first_target)
        second_response=await c.get(second_target)
        assert first_response.status_code==second_response.status_code==303
        assert first_response.headers['location']=='/pl/course_instance/1/assessment/2/'
        assert second_response.headers['location']=='/pl/course_instance/1/assessment/3/'
        assert (await c.get(first_target)).status_code==403
        assert (await c.get(second_target)).status_code==403
