import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from gateway.launch import LTI, LEARNER, SCORE_SCOPE
from gateway.app import create_app
from gateway.config import Configuration
from gateway.store import Store
from test_assignment import Bridge, person, delivery
from test_config import example

@pytest.fixture
def setup(tmp_path):
    value=example();value["database"]=str(tmp_path/"runtime.sqlite")
    cfg=Configuration.model_validate(value);store=Store(cfg.database)
    student=person(store);teacher=person(store,"teacher","teacher")
    # HTTP fixture uses the exact configured resource binding.
    with store.transaction() as db:
        db.execute("UPDATE sessions SET resource='resource',lineitem='https://moodle.example/lineitems/defense'");db.execute("UPDATE activity_bindings SET resource='resource',lineitem='https://moodle.example/lineitems/defense'")
    class Clients:
        bridge=Bridge()
        async def jwks(self): return {"keys":[]}
        class AGS:
            async def score(self,*args): pass
        ags=AGS()
    app=create_app(cfg,store=store,delivery=delivery(),clients=Clients(),clock=lambda:1000,start_worker=False)
    return app,store,student,teacher

@pytest.mark.anyio
async def test_session_required_no_supplied_uid_or_grade_api(setup):
    app,store,student,teacher=setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="https://course.example") as c:
        assert (await c.get("/gateway/courses/demo/my/works",headers={"X-Gateway-User":student["uid"]})).status_code==403
        assert (await c.post("/gateway/courses/demo/grades",json={"score":1})).status_code==404
        c.cookies.set("GatewaySession",student["token"])
        response=await c.get("/gateway/courses/demo/my/works")
        assert response.status_code==200 and [w["id"] for w in response.json()["works"]]==["lab"]
        headers={"Origin":"https://course.example","X-CSRF-Token":student["csrf"]}
        assert (await c.post("/gateway/courses/demo/assignments/request",headers=headers,json={"essayId":"exr-essay-a","idempotencyKey":"a","userUid":"foreign"})).status_code==422

@pytest.mark.anyio
async def test_csrf_teacher_scope_and_api_assignment(setup):
    app,store,student,teacher=setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="https://course.example",cookies={"GatewaySession":student["token"]}) as c:
        body={"essayId":"exr-essay-a","idempotencyKey":"a"}
        assert (await c.post("/gateway/courses/demo/assignments/request",json=body)).status_code==403
        headers={"Origin":"https://evil.example","X-CSRF-Token":student["csrf"]}
        assert (await c.post("/gateway/courses/demo/assignments/request",headers=headers,json=body)).status_code==403
        headers["Origin"]="https://course.example"
        created=await c.post("/gateway/courses/demo/assignments/request",headers=headers,json=body)
        assert created.status_code==200 and created.json()["state"]=="assigned"
        assert (await c.post("/gateway/courses/demo/teacher/assignments/"+created.json()["id"]+"/approve",headers=headers,json={})).status_code==403

@pytest.mark.anyio
async def test_private_oneuse_handoff_requires_service_auth(setup):
    app,store,student,_=setup
    token=store.handoff(student["token"],"/gateway/courses/demo/bootstrap",1000)
    app.state.handoff_secret="s"*32
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="https://course.example") as c:
        path="/internal/handoff/courses/demo/"+token
        assert (await c.get(path,headers={"X-Gateway-User":"forged"})).status_code==403
        first=await c.get(path,headers={"Authorization":"Bearer "+"s"*32})
        assert first.status_code==200 and first.json()["uid"]==student["uid"] and "role" not in first.json()
        assert first.headers["x-verified-uid"]==student["uid"] and first.headers["x-verified-target"]=="/gateway/courses/demo/bootstrap"
        assert (await c.get(path,headers={"Authorization":"Bearer "+"s"*32})).status_code==403

@pytest.mark.anyio
async def test_safe_work_ui_no_unready_defense(setup):
    app,_,student,_=setup
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="https://course.example",cookies={"GatewaySession":student["token"]}) as c:
        response=await c.get("/gateway/courses/demo/")
        assert response.status_code==200 and "Мои работы" in response.text and "innerHTML" not in response.text

@pytest.mark.anyio
@pytest.mark.parametrize("client_id", ["client", None])
@pytest.mark.parametrize("padded_platform_key", [False, True])
async def test_real_signed_http_launch_cookie_binding_bootstrap_and_replay(tmp_path, client_id, padded_platform_key):
    config=example();config["database"]=str(tmp_path/"http.sqlite")
    key=RSAKey.generate_key(parameters={"kid":"synthetic-moodle"})
    class Clients:
        bridge=Bridge()
        async def jwks(self):
            public=key.as_dict(private=False)
            if padded_platform_key:
                public["n"] += "=="
            return {"keys":[public]}
        class AGS:
            async def score(self,*args):pass
        ags=AGS()
    enrolled=[]
    async def enroll(uid,instance,delivery_hash):enrolled.append((uid,instance,delivery_hash))
    clients=Clients();clients.bridge.enroll=enroll
    app=create_app(config,delivery=delivery(),clients=clients,clock=lambda:1000,start_worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="https://course.example") as http:
        params={"iss":"https://moodle.example","target_link_uri":"https://course.example/gateway/courses/demo/lti/launch","login_hint":"signed-hint","lti_message_hint":"fixed-message"}
        if client_id is not None: params["client_id"]=client_id
        response=await http.get("/gateway/courses/demo/lti/login",params=params)
        assert response.status_code==303 and "Secure" in response.headers["set-cookie"] and "SameSite=none" in response.headers["set-cookie"]
        oidc=httpx.URL(response.headers["location"]).params
        assert oidc["client_id"] == "client"
        claims={"iss":"https://moodle.example","aud":"client","sub":"synthetic-student","iat":990,"exp":1100,"nonce":oidc["nonce"],"name":"Студент","https://purl.imsglobal.org/spec/lti-ags/claim/endpoint":{"lineitem":"https://moodle.example/lineitems/defense","scope":[SCORE_SCOPE]},LTI+"deployment_id":"deploy",LTI+"context":{"id":"context"},LTI+"resource_link":{"id":"resource"},LTI+"roles":[LEARNER],LTI+"message_type":"LtiResourceLinkRequest",LTI+"version":"1.3.0",LTI+"target_link_uri":"https://course.example/gateway/courses/demo/lti/launch"}
        body={"state":oidc["state"],"id_token":jwt.encode({"alg":"RS256","kid":key.kid},claims,key,algorithms=["RS256"])}
        launched=await http.post("/gateway/courses/demo/lti/launch",data=body,headers={"X-Gateway-User":"administrator@example.invalid"})
        assert launched.status_code==303 and launched.headers["location"].startswith("/gateway/courses/demo/pl/")
        cookies=launched.headers.get_list("set-cookie")
        assert any("GatewaySession=" in cookie and "Secure" in cookie and "HttpOnly" in cookie for cookie in cookies)
        assert http.cookies.get("GatewayBinding") is None
        who=app.state.store.session(http.cookies["GatewaySession"],1000)
        assert who["subject"]=="synthetic-student" and who["uid"].endswith("@example.invalid")
        assert (await http.get("/gateway/courses/demo/bootstrap")).status_code==303
        assert enrolled==[(who["uid"],"pilot","a"*64)]
        app.state.handoff_secret="s"*32
        handoff=launched.headers["location"].split("/")[-1]
        reply=await http.get("/internal/handoff/courses/demo/"+handoff,headers={"Authorization":"Bearer "+"s"*32})
        assert reply.headers["x-verified-target"]=="/gateway/courses/demo/bootstrap" and reply.headers["x-verified-name-utf8-b64"]=="v1.0KHRgtGD0LTQtdC90YI"
        assert reply.json()["displayName"]=="Студент" and "role" not in reply.json()
        assert (await http.post("/gateway/courses/demo/lti/launch",data=body)).status_code==403
