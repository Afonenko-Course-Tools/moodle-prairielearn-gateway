"""Catalog transport must preserve history without becoming an assignment/AGS route."""
import copy
import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from gateway.app import create_app
from gateway.config import Configuration
from gateway.launch import LTI, LEARNER, SCORE_SCOPE
from gateway.store import Store, digest
from test_assignment import Bridge, delivery, Assignments
from test_config import example
from test_gradebook import AGS, Grades, result

WORKS = ['sec-lab-work-environment', 'sec-lab-data-processing', 'sec-lab-version-control',
         'sec-unit-version-control-restore-selected-state', 'sec-unit-project-maintenance-release']
PREFIX = '/gateway/courses/demo'
LINE = 'https://moodle.example/lineitems/defense'


def catalog_config(tmp_path):
    value = example()
    value['database'] = str(tmp_path / 'runtime.sqlite')
    value['registration']['activities'] = {'streams': {'kind': 'catalog', 'resourceLinkId': 'resource', 'lineitem': LINE}}
    return value


def current_delivery():
    value = delivery()
    groups = [
        ['exr-work-environment-file-state', 'exr-work-environment-launch-counter'],
        ['exr-data-processing-two-result-pipeline'],
        ['exr-version-control-local-history', 'exr-version-control-github-ssh'],
        ['exr-version-control-restore-selected-state'],
        ['exr-project-maintenance-release'],
    ]
    ordinary = []
    for name, questions in zip(WORKS, groups):
        slots = [{'id':'demo/'+name+'/'+q,'selection':'fixed','candidates':['demo/'+q],
                  'assignment':{'requirement':'required','workMode':'individual'}} for q in questions]
        ids = [s['id'] for s in slots]
        ordinary.append({'id':name,'slots':slots,'policy':{},'completion':{
            'schemaVersion':2,'mode':'required-slot-completion','source':'per-slot-results-v2',
            'slotIds':ids,'requiredSlotIds':ids,'atLeast':len(ids),'fullyCompletedScore':1}})
    value['works'] = ordinary + value['works'][:2]
    for work in value['works']: work.setdefault('policy', {'assignment':{'student-label':'fictional defense'}})
    value['works'].append({**copy.deepcopy(ordinary[0]), 'id': 'foreign-instance-work'})
    for slot in value['works'][-1]['slots']:
        slot['id'] = slot['id'].replace(WORKS[0], 'foreign-instance-work')
    policy = value['works'][-1]['completion']
    policy['slotIds'] = policy['requiredSlotIds'] = [s['id'] for s in value['works'][-1]['slots']]
    value['instances']['other'] = {'works': ['foreign-instance-work']}
    value['instances']['pilot']['works'] = WORKS + ['defense-a']
    value['questions'] += ['demo/'+q for group in groups for q in group]
    import uuid
    value['gradingPayloads'] = {q:{'uuid':str(uuid.UUID(int=i+1)),'grading':{'method':'manual'},
        'submission':{'mode':'upload'},'sourceRoot':'projects/fictional-'+str(i)} for i,q in enumerate(value['questions'])}
    value.update(schemaVersion=3,bookRoot='tasks',sourceSnapshotHash='b'*64,inventoryHash='c'*64,
                 verificationInventoryHash='d'*64,files={})
    return value


def test_catalog_uses_valid_manual_v3_selected_delivery(tmp_path):
    import json
    from gateway.config import load_delivery
    from gateway.store import native_digest
    value = current_delivery()
    del value['deliveryHash']
    value['deliveryHash'] = native_digest(value)
    (tmp_path/'delivery.json').write_text(json.dumps(value))
    actual = load_delivery(tmp_path, 'pilot')
    ordinary = [w for w in actual['works'] if w['id'] in WORKS]
    questions = [q for w in ordinary for s in w['slots'] for q in s['candidates']]
    assert len(ordinary) == 5 and len(set(questions)) == 7
    assert all(actual['gradingPayloads'][q]['grading'] == {'method':'manual'} for q in questions)


def test_catalog_is_closed_and_has_no_work_binding(tmp_path):
    value = catalog_config(tmp_path)
    assert Configuration.model_validate(value).registration.activities['streams'].workId is None
    for change in ({'workId': 'sec-pl-streams-pilot'}, {'kind': 'bank'}, {'lineitem': 'https://foreign.example/line'}):
        bad = copy.deepcopy(value)
        bad['registration']['activities']['streams'].update(change)
        with pytest.raises(ValueError): Configuration.model_validate(bad)


@pytest.fixture
def catalog(tmp_path):
    store = Store(tmp_path / 'runtime.sqlite')
    def historical_person(subject, role='student'):
        key = digest(['https://moodle.example', 'client', 'deploy', subject])
        pending = store.state('browser', 1000)
        return store.activate(pending['state'], 'browser', 1000, {'userId':key,'issuer':'https://moodle.example','client':'client','deployment':'deploy','subject':subject,'uid':'gw-'+key+'@example.invalid','name':'Одинаковое имя','role':role,'context':'context','resource':'defense','activity':'streams','lineitem':'https://moodle.example/lineitem'})
    a = historical_person('subject-a'); b = historical_person('subject-b'); teacher = historical_person('teacher', 'teacher')
    bridge = Bridge(); ags = AGS(); key = RSAKey.generate_key(parameters={'kid': 'fictional-moodle'})
    calls = []
    async def enroll(*args): calls.append(('enroll', args))
    async def work_launch(uid, instance, hash_, work):
        calls.append(('work', (uid, instance, hash_, work)))
        return '/pl/course_instance/2/assessment/' + str(WORKS.index(work) + 1) + '/'
    async def results(*args): raise AssertionError('Catalog must not fetch assignment grades')
    bridge.enroll = enroll; bridge.work_launch = work_launch; bridge.results = results
    class Clients:
        async def jwks(self): return {'keys': [key.as_dict(private=False)]}
    clients = Clients(); clients.bridge = bridge; clients.ags = ags
    # Seed real gradebook history using the existing defense flow, including pending/sent.
    old = Assignments(store, bridge, delivery(), 'pilot', {'mode': 'immediate', 'capacity': {}}, clock=lambda:1000)
    value = catalog_config(tmp_path)
    return store, a, b, teacher, old, clients, key, calls, value


@pytest.mark.anyio
async def test_catalog_signed_relogin_five_works_and_history_never_post_scores(catalog):
    store, a, b, teacher, old, clients, key, calls, value = catalog
    for who in (a, b):
        row = await old.request(who, 'exr-essay-a', 'old')
        Grades(store, old, clients.ags, clock=lambda:1000).observe(row, result(row, who['uid']))
    with store.transaction() as db:
        db.execute("UPDATE outbox SET state='sent' WHERE user_id=?", (a['userId'],))
        # Historical identity and immutable external binding match signed registration.
        db.execute("UPDATE users SET issuer='https://moodle.example',name='Одинаковое имя'")
        for table in ('assignments', 'outbox'):
            db.execute('UPDATE '+table+' SET resource=?,lineitem=?', ('resource', LINE))
    tables = ('users', 'assignments', 'requests', 'native_attempts', 'observations', 'outbox')
    def snapshot():
        with store.connect() as db: return {t: [tuple(r) for r in db.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables}
    before = snapshot()
    app = create_app(value, store=store, delivery=current_delivery(), clients=clients, clock=lambda:1000, start_worker=False)
    app.state.handoff_secret = 's'*32
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://course.example') as c:
        assert (await c.get(PREFIX+'/lti/login', params={'iss':'https://moodle.example','client_id':'foreign','target_link_uri':'https://course.example'+PREFIX+'/lti/launch','login_hint':'hint','lti_message_hint':'message'})).status_code == 403
        for who in (a, b, a):
            login = await c.get(PREFIX+'/lti/login', params={'iss':'https://moodle.example','client_id':'client','target_link_uri':'https://course.example'+PREFIX+'/lti/launch','login_hint':'hint','lti_message_hint':'message'})
            oidc = httpx.URL(login.headers['location']).params
            claims = {'iss':'https://moodle.example','aud':'client','sub':who['subject'],'iat':990,'exp':1100,'nonce':oidc['nonce'],'name':'Одинаковое имя',LTI+'deployment_id':'deploy',LTI+'context':{'id':'context'},LTI+'resource_link':{'id':'resource'},LTI+'roles':[LEARNER],LTI+'message_type':'LtiResourceLinkRequest',LTI+'version':'1.3.0',LTI+'target_link_uri':'https://course.example'+PREFIX+'/lti/launch','https://purl.imsglobal.org/spec/lti-ags/claim/endpoint':{'lineitem':LINE,'scope':[SCORE_SCOPE]}}
            # Existing identities are keyed by the verified issuer tuple, not names.
            from gateway.store import digest
            expected_id = digest(['https://moodle.example','client','deploy',who['subject']])
            # Fixture's historical IDs must already be the signed tuple's IDs.
            assert who['userId'] == expected_id
            body = {'state':oidc['state'],'id_token':jwt.encode({'alg':'RS256','kid':key.kid},claims,key,algorithms=['RS256'])}
            launch = await c.post(PREFIX+'/lti/launch', data=body)
            assert launch.status_code == 303
            assert (await c.post(PREFIX+'/lti/launch',data=body)).status_code == 403
            token = launch.headers['location'].split('/')[-1]
            assert (await c.get('/internal/handoff/courses/demo/'+token)).status_code == 403
            handoff = await c.get('/internal/handoff/courses/demo/'+token,headers={'Authorization':'Bearer '+'s'*32})
            assert handoff.json()['uid'] == who['uid']
            assert (await c.get('/internal/handoff/courses/demo/'+token,headers={'Authorization':'Bearer '+'s'*32})).status_code == 403
            bootstrap = await c.get(handoff.json()['target'])
            assert bootstrap.headers['location'] == PREFIX+'/'
            if '/bootstrap/' in handoff.json()['target']:
                assert (await c.get(handoff.json()['target'])).status_code == 403
            state = (await c.get(PREFIX+'/my/works')).json()
            assert [w['id'] for w in state['works']] == WORKS
            assert state['assignment'] is None and state['essays'] == []
            headers = {'Origin':'https://course.example','X-CSRF-Token':state['csrfToken']}
            for work in WORKS:
                assert (await c.post(PREFIX+'/launch/pl/'+work,json={},headers=headers)).status_code == 200
            for work in ('defense-a','defense-b','foreign','foreign-instance-work','sec-pl-streams-pilot'):
                assert (await c.post(PREFIX+'/launch/pl/'+work,json={},headers=headers)).status_code == 403
            assert (await c.post(PREFIX+'/launch/pl/'+WORKS[0],json={})).status_code == 403
            for forged in ({'deliveryHash':'forged'}, {'instance':'other'}, {'userUid':b['uid']}, {'role':'teacher'}):
                assert (await c.post(PREFIX+'/launch/pl/'+WORKS[0],json=forged,headers=headers)).status_code == 422
            assert (await c.post(PREFIX+'/launch/pl/defense-a',json={},headers={**headers,'X-Gateway-Role':'teacher'})).status_code == 403
            for path, data in [('assignments/request',{'essayId':'exr-essay-a','idempotencyKey':'new'}), ('teacher/assignments',{'userId':a['userId'],'essayId':'exr-essay-a','idempotencyKey':'new'}), ('teacher/assignments/old/approve',{}),('teacher/assignments/old/reject',{}),('teacher/assignments/old/reassign',{'essayId':'exr-essay-a','idempotencyKey':'new'})]:
                assert (await c.post(PREFIX+'/'+path,json=data,headers=headers)).status_code == 403
            assert (await c.get(PREFIX+'/teacher/roster')).status_code == 403
            with store.connect() as db:
                old_row = dict(db.execute('SELECT * FROM assignments WHERE user_id=?',(who['userId'],)).fetchone())
            assert app.state.grades.observe(old_row, result(old_row,who['uid'])) is None
            await app.state.grades.poll(); await app.state.grades.drain()
            await app.state.assignments.reconcile_all()
    assert snapshot() == before
    assert clients.ags.sent == []
    assert len([c for c in calls if c[0]=='enroll']) == 3


@pytest.mark.anyio
async def test_catalog_teacher_handoff_is_not_student_or_assignment_authority(catalog):
    store, a, _, teacher, _, clients, key, calls, value = catalog
    # Keep a real separate defense activity: catalog must not inherit its authority.
    value['registration']['activities']['defense'] = {'kind':'defense','resourceLinkId':'defense-resource','lineitem':'https://moodle.example/defense'}
    app = create_app(value, store=store, delivery=current_delivery(), clients=clients, clock=lambda:1000, start_worker=False)
    app.state.handoff_secret = 's'*32
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://course.example') as c:
        login = await c.get(PREFIX+'/lti/login',params={'iss':'https://moodle.example','client_id':'client','target_link_uri':'https://course.example'+PREFIX+'/lti/launch','login_hint':'hint','lti_message_hint':'message'})
        oidc = httpx.URL(login.headers['location']).params
        claims = {'iss':'https://moodle.example','aud':'client','sub':'teacher','iat':990,'exp':1100,'nonce':oidc['nonce'],'name':'Одинаковое имя',LTI+'deployment_id':'deploy',LTI+'context':{'id':'context'},LTI+'resource_link':{'id':'resource'},LTI+'roles':['http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor'],LTI+'message_type':'LtiResourceLinkRequest',LTI+'version':'1.3.0',LTI+'target_link_uri':'https://course.example'+PREFIX+'/lti/launch','https://purl.imsglobal.org/spec/lti-ags/claim/endpoint':{'lineitem':LINE,'scope':[SCORE_SCOPE]}}
        launched = await c.post(PREFIX+'/lti/launch',data={'state':oidc['state'],'id_token':jwt.encode({'alg':'RS256','kid':key.kid},claims,key,algorithms=['RS256'])})
        assert launched.status_code == 303 and '/pl/' in launched.headers['location']
        token = launched.headers['location'].split('/')[-1]
        handoff = await c.get('/internal/handoff/courses/demo/'+token,headers={'Authorization':'Bearer '+'s'*32})
        bootstrap = await c.get(handoff.json()['target'])
        assert bootstrap.status_code == 303 and bootstrap.headers['location'] == '/pl/'
        state = (await c.get(PREFIX+'/my/works')).json()
        assert [w['id'] for w in state['works']] == WORKS and state['role'] == 'teacher' and state['administrator'] is False
        headers = {'Origin':'https://course.example','X-CSRF-Token':state['csrfToken']}
        assert (await c.get(PREFIX+'/teacher/roster')).status_code == 403
        for path, body in [('teacher/assignments',{'userId':a['userId'],'essayId':'exr-essay-a','idempotencyKey':'new'}),('teacher/assignments/old/approve',{}),('teacher/assignments/old/reject',{}),('teacher/assignments/old/reassign',{'essayId':'exr-essay-a','idempotencyKey':'new'}),('launch/pl/'+WORKS[0],{}),('launch/administrator',{})]:
            assert (await c.post(PREFIX+'/'+path,json=body,headers=headers)).status_code == 403
        assert (await c.get(PREFIX+'/administrator-bootstrap')).status_code == 403
    assert calls == [] and clients.ags.sent == []
