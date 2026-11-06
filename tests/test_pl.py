"""Закрытый wire контракт и терминальные ошибки настоящего PLClient."""
import json
import httpx
import pytest
from gateway.errors import GatewayError
from gateway.pl import PLClient
from gateway.app import create_app
from test_auth_flow import configuration, Platform, browser_for, activate, handoff, PREFIX

SCOPE={'userUid':'uid','instance':'pilot','courseKey':'demo'}
ANSWER={'schemaVersion':1,**SCOPE,'enrolled':True,'path':'/pl/course_instance/42/assessments'}


@pytest.mark.anyio
@pytest.mark.parametrize('role,email',[('teacher','owner@example.invalid'),('student','owner@example.invalid'),('student',None)])
async def test_enrollment_has_exact_five_field_request_and_six_field_answer(role,email):
    seen=[]
    def endpoint(request):
        seen.append(request)
        return httpx.Response(200,json=ANSWER)
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        answer=await PLClient(http,'http://prairielearn:3001','s'*32).enroll('uid','pilot','demo',role=role,email=email)
    assert answer==ANSWER and len(seen)==1
    assert seen[0].method=='PUT' and seen[0].url==httpx.URL('http://prairielearn:3001/internal/gateway/enrollment')
    assert json.loads(seen[0].content)=={**SCOPE,'role':role,'email':email}
    assert seen[0].headers['authorization']=='Bearer '+'s'*32


@pytest.mark.anyio
@pytest.mark.parametrize('change',[
    {'path':'//evil.example/'},{'path':'https://evil.example/pl/course_instance/42/assessments'},
    {'path':'/pl/course_instance/0/assessments'},{'path':'/pl/course_instance/01/assessments'},
    {'path':'/pl/course_instance/42/assessments?role=Administrator'},
    {'path':'/pl/course_instance/42/assessments/'},{'path':42},{'path':'/pl/course_instance/42/assessment/9/'},
    {'enrolled':1},{'enrolled':False},{'schemaVersion':True},{'schemaVersion':2},
    {'userUid':'other'},{'instance':'other'},{'courseKey':'other'},{'role':'Administrator'},
])
async def test_invalid_enrollment_evidence_rejected(change):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={**ANSWER,**change}))) as http:
        with pytest.raises(GatewayError):await PLClient(http,'http://prairielearn:3001','s'*32).enroll('uid','pilot','demo',role='student',email=None)


@pytest.mark.anyio
@pytest.mark.parametrize('missing',list(ANSWER))
async def test_missing_evidence_field_rejected(missing):
    answer={k:v for k,v in ANSWER.items() if k!=missing}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=answer))) as http:
        with pytest.raises(GatewayError):await PLClient(http,'http://prairielearn:3001','s'*32).enroll('uid','pilot','demo',role='student',email=None)


@pytest.mark.anyio
@pytest.mark.parametrize('role,email',[(None,None),('Administrator',None),(True,None),('student',42),('teacher',' Owner@Example.Invalid ')])
async def test_untrusted_role_or_noncanonical_email_never_reaches_bridge(role,email):
    def forbidden(request):raise AssertionError('Некорректная identity не должна обращаться к Bridge')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(GatewayError):await PLClient(http,'http://prairielearn:3001','s'*32).enroll('uid','pilot','demo',role=role,email=email)


@pytest.mark.anyio
@pytest.mark.parametrize('failure',['timeout','network','malformed','oversized','array','non200','redirect'])
async def test_actual_transport_failure_consumes_flow_before_request(tmp_path,failure):
    calls=[]
    def endpoint(request):
        calls.append(request)
        if failure=='timeout':raise httpx.ReadTimeout('Тестовый timeout',request=request)
        if failure=='network':raise httpx.ConnectError('Тестовый обрыв',request=request)
        if failure=='malformed':return httpx.Response(200,content=b'{')
        if failure=='oversized':return httpx.Response(200,content=b' '*65537)
        if failure=='array':return httpx.Response(200,json=[])
        return httpx.Response(302 if failure=='redirect' else 503,headers={'Location':'http://other.example'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        platform=Platform();platform.bridge=PLClient(http,'http://prairielearn:3001','s'*32)
        app=create_app(configuration(tmp_path),clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
        async with browser_for(app) as browser:
            _,_,flow=await activate(browser,platform)
            target=(await handoff(browser,flow)).json()['target']
            response=await browser.get(target)
            assert response.status_code==503 and 'set-cookie' not in response.headers
            assert (await browser.get(target)).status_code==403
            assert (await handoff(browser,flow)).status_code==403
    assert len(calls)==1


@pytest.mark.anyio
@pytest.mark.parametrize('answer',[None,42,[{'keys':[]}]])
async def test_malformed_platform_jwks_is_a_launch_rejection(answer):
    from gateway.app import Clients
    from gateway.errors import LaunchError
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=answer) if answer is not None else httpx.Response(200,content=b'null'))) as http:
        clients=object.__new__(Clients)
        clients.registration={'jwksUrl':'https://moodle.example/certs'};clients.http=http
        with pytest.raises(LaunchError):await clients.jwks()
