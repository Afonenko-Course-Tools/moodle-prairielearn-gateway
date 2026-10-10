import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import Field
from joserfc.jwk import RSAKey
from .config import Closed, Configuration, load_delivery
from .store import Store
from .launch import LaunchService, LaunchError
from .errors import GatewayError
from .assignments import Assignments
from .grades import Grades
from .pl import PLClient
from .ags import AGSClient

class Selection(Closed):
    essayId: str = Field(min_length=1,max_length=256)
    idempotencyKey: str = Field(min_length=1,max_length=128)

class TeacherSelection(Selection):
    userId: str = Field(min_length=1,max_length=128)

class Empty(Closed): pass

def secret(path):
    value=Path(path).read_text().strip()
    if len(value)<32 or any(ord(c)<33 or ord(c)>126 for c in value): raise ValueError("Private service secret needs at least 32 printable characters")
    return value

class Clients:
    def __init__(self,config):
        self.registration=config.registration.model_dump()
        tls=ssl.create_default_context(cafile=config.caFile)
        self.http=httpx.AsyncClient(verify=tls,follow_redirects=False,timeout=15,limits=httpx.Limits(max_connections=8))
        self.key=RSAKey.import_key(json.loads(Path(config.privateKeyFile).read_text()))
        if not self.key.kid or "d" not in self.key.as_dict(private=True): raise ValueError("Private RS256 key with kid required")
        self.bridge=PLClient(self.http,config.bridge.url,secret(config.bridge.tokenFile))
        self.ags=AGSClient(self.http,self.registration,self.key)

    async def jwks(self):
        try:
            mapping=self.registration.get("privateJwksUrl")
            headers={"Host":__import__("urllib.parse",fromlist=["urlsplit"]).urlsplit(self.registration["issuer"]).netloc,"X-Forwarded-Proto":"https"} if mapping else {}
            response=await self.http.get(mapping or self.registration["jwksUrl"],headers=headers,follow_redirects=False,timeout=15)
            if response.status_code!=200 or len(response.content)>65536: raise LaunchError("Moodle JWKS unavailable")
            value=response.json()
            if set(value)!={"keys"} or not isinstance(value["keys"],list) or not 1<=len(value["keys"])<=32: raise LaunchError("Invalid Moodle JWKS")
            return value
        except (httpx.HTTPError,ValueError) as error: raise LaunchError("Moodle JWKS unavailable") from error

PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Мои работы</title>
<style>body{max-width:60em;margin:2em auto;padding:1em;font:18px system-ui}button,select,input{font:inherit;margin:.3em;padding:.4em}li{margin:1em 0}#status{white-space:pre-wrap}</style>
<h1>Мои работы</h1><p id="status" role="status"></p><ul id="works"></ul><section id="selection"><h2>Выбор реферата</h2><select id="essay" aria-label="Постановка реферата"></select><button id="choose">Запросить назначение</button></section><section id="teacher" hidden><h2>Назначения преподавателя</h2><select id="student" aria-label="Студент"></select><button id="assign">Назначить</button><ul id="requests"></ul></section>
<script>
let state;
const status=document.getElementById('status');
async function send(path,body){const r=await fetch('/gateway/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':state.csrfToken},body:JSON.stringify(body)});const value=await r.json();if(!r.ok)throw Error(value.detail||'Операция не выполнена');await load();return value;}
function button(label,action){const b=document.createElement('button');b.textContent=label;b.onclick=()=>action().catch(e=>status.textContent=e.message);return b;}
async function load(){const r=await fetch('/gateway/my/works');state=await r.json();if(!r.ok)throw Error(state.detail||'Требуется вход из Moodle');status.textContent=state.assignment?'Назначение: '+state.assignment.state:'Назначение отсутствует';const works=document.getElementById('works');works.replaceChildren();for(const w of state.works){const li=document.createElement('li');li.textContent=w.id+' ';if(state.role==='student')li.append(button('Открыть PrairieLearn',async()=>{const value=await send('launch/pl/'+encodeURIComponent(w.id),{});location.assign(value.url);}));works.append(li);}const essays=document.getElementById('essay');essays.replaceChildren();for(const e of state.essays){const o=document.createElement('option');o.value=e;o.textContent=e;essays.append(o);}document.getElementById('selection').hidden=state.activityKind!=='defense';document.getElementById('choose').hidden=state.role!=='student'||state.mode==='teacher';if(state.role==='teacher'){document.getElementById('teacher').hidden=false;const rr=await fetch('/gateway/teacher/roster');const roster=await rr.json();const students=document.getElementById('student');students.replaceChildren();const requests=document.getElementById('requests');requests.replaceChildren();for(const s of roster.students){const o=document.createElement('option');o.value=s.userId;o.textContent=s.name;students.append(o);if(s.assignment){const li=document.createElement('li');li.textContent=s.name+' — '+s.assignment.state+' '+(s.assignment.essay||'');const id=encodeURIComponent(s.assignment.id);if(s.assignment.state==='pending'){li.append(button('Подтвердить',()=>send('teacher/assignments/'+id+'/approve',{})),button('Отклонить',()=>send('teacher/assignments/'+id+'/reject',{})));}if(['assigned','approved','completed'].includes(s.assignment.state)){li.append(button('Заменить выбранным рефератом',()=>send('teacher/assignments/'+id+'/reassign',{essayId:essays.value,idempotencyKey:crypto.randomUUID()})));}requests.append(li);}}}}
document.getElementById('choose').onclick=()=>send('assignments/request',{essayId:document.getElementById('essay').value,idempotencyKey:crypto.randomUUID()}).catch(e=>status.textContent=e.message);
document.getElementById('assign').onclick=()=>send('teacher/assignments',{userId:document.getElementById('student').value,essayId:document.getElementById('essay').value,idempotencyKey:crypto.randomUUID()}).catch(e=>status.textContent=e.message);
load().catch(e=>status.textContent=e.message);
</script></html>"""

def create_app(config, *, store=None, delivery=None, clients=None, clock=time.time, start_worker=True):
    if not isinstance(config,Configuration): config=Configuration.model_validate(config)
    store=store or Store(config.database)
    delivery=delivery or load_delivery(config.deliveryPath,config.instance)
    own_clients=clients is None
    clients=clients or Clients(config)
    registration=config.registration.model_dump()
    registration["targetLinkUri"]=config.publicOrigin.rstrip("/")+"/gateway/lti/launch"
    launch=LaunchService(store,registration,clock)
    defense_key=next(k for k,v in config.registration.activities.items() if v.kind=="defense")
    defense=config.registration.activities[defense_key]
    flow={**config.flow.model_dump(),"contextId":registration["contextId"],"resourceLinkId":defense.resourceLinkId,"lineitem":defense.lineitem}
    assignments=Assignments(store,clients.bridge,delivery,config.instance,flow,clock)
    grades=Grades(store,assignments,clients.ags,clock,activities={k:v.model_dump() for k,v in config.registration.activities.items()})

    async def worker():
        while True:
            try:
                await assignments.reconcile_all()
                await grades.poll()
            except GatewayError:
                with store.transaction() as db: store.audit(db,"poll-retry","worker",{},int(clock()))
            await asyncio.sleep(config.pollSeconds)

    @asynccontextmanager
    async def lifespan(app):
        task=asyncio.create_task(worker()) if start_worker else None
        try: yield
        finally:
            if task:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
            if own_clients: await clients.http.aclose()

    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.store,app.state.assignments,app.state.grades=store,assignments,grades
    app.state.handoff_secret=secret(config.handoffTokenFile) if own_clients else None

    @app.exception_handler(GatewayError)
    async def rejected(request,error): return JSONResponse({"detail":str(error)},status_code=error.status,headers={"Cache-Control":"no-store"})

    @app.middleware("http")
    async def response_security(request,call_next):
        response=await call_next(request)
        response.headers["Cache-Control"]="no-store"
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        return response

    def session(request,write=False):
        value=store.session(request.cookies.get("GatewaySession",""),int(clock()))
        if write and (request.headers.get("origin")!=config.publicOrigin.rstrip("/") or not secrets.compare_digest(request.headers.get("x-csrf-token",""),value["csrf"])): raise LaunchError("CSRF origin/token rejected")
        return value

    @app.api_route("/gateway/lti/login",methods=["GET","POST"])
    async def login(request:Request):
        params=dict(await request.form()) if request.method=="POST" else dict(request.query_params)
        allowed={"iss","client_id","target_link_uri","login_hint","lti_message_hint","lti_deployment_id"}
        if set(params)-allowed or params.get("lti_deployment_id",registration["deploymentId"])!=registration["deploymentId"]: raise LaunchError("Login fields rejected")
        binding=secrets.token_urlsafe(32)
        pending=launch.begin(params.get("iss"),params.get("client_id"),params.get("target_link_uri"),params.get("login_hint"),params.get("lti_message_hint"),binding)
        query={"scope":"openid","response_type":"id_token","response_mode":"form_post","prompt":"none","client_id":registration["clientId"],"redirect_uri":registration["targetLinkUri"],"login_hint":params["login_hint"],"lti_message_hint":params["lti_message_hint"],**pending}
        response=RedirectResponse(registration["authUrl"]+"?"+urlencode(query),status_code=303)
        response.set_cookie("GatewayBinding",binding,secure=True,httponly=True,samesite="none",path="/gateway",max_age=300)
        return response

    @app.post("/gateway/lti/launch")
    async def verified_launch(request:Request):
        if len(await request.body())>65536: raise LaunchError("Launch too large")
        params=dict(await request.form())
        if set(params)!={"state","id_token"}: raise LaunchError("Launch fields rejected")
        value=launch.verify(params["state"],request.cookies.get("GatewayBinding",""),params["id_token"],await clients.jwks())
        if value["role"]=="student":
            token=store.handoff(value["token"],"/gateway/bootstrap",int(clock()))
            response=RedirectResponse("/gateway/pl/"+token,status_code=303)
            response.set_cookie("pl2_pre_auth_url","/gateway/bootstrap",secure=True,httponly=True,samesite="lax",path="/")
        else: response=RedirectResponse("/gateway/",status_code=303)
        response.set_cookie("GatewaySession",value["token"],secure=True,httponly=True,samesite="lax",path="/gateway",max_age=3600)
        response.delete_cookie("GatewayBinding",path="/gateway",secure=True,httponly=True,samesite="none")
        return response

    @app.get("/internal/handoff/{token}")
    async def consume(token:str,request:Request):
        expected="Bearer "+(app.state.handoff_secret or secrets.token_urlsafe(32))
        if not secrets.compare_digest(request.headers.get("authorization",""),expected): raise LaunchError("Private service authentication required")
        value=store.consume_handoff(token,int(clock()))
        if any(ord(c)<32 or ord(c)>126 for c in value["uid"]+value["target"]): raise LaunchError("Invalid handoff header")
        name=value["displayName"]
        if any(ord(c)<32 or ord(c)>126 for c in name): name="Moodle participant"
        return JSONResponse(value,headers={"X-Verified-Uid":value["uid"],"X-Verified-Name":name,"X-Verified-Target":value["target"]})

    @app.get("/gateway/.well-known/jwks.json")
    async def tool_keys():
        if not hasattr(clients,"key"): raise GatewayError("Tool key unavailable",503)
        return {"keys":[clients.key.as_dict(private=False)]}

    @app.get("/gateway/")
    async def home(request:Request):
        session(request)
        return HTMLResponse(PAGE)

    @app.get("/gateway/bootstrap")
    async def bootstrap(request:Request):
        who=session(request)
        if who["role"]!="student": raise LaunchError("Native Student enrollment required")
        await clients.bridge.enroll(who["uid"],config.instance,delivery["deliveryHash"])
        await assignments.reconcile_all()
        return RedirectResponse("/gateway/",status_code=303)

    @app.get("/gateway/my/works")
    async def works(request:Request):
        who=session(request)
        activity=config.registration.activities[who["activity"]]
        return {"works":[{"id":w["id"],"essayId":w.get("relatedExercise")} for w in assignments.my_works(who)],"assignment":assignments.current(who),"essays":list(assignments.essays),"role":who["role"],"mode":config.flow.mode,"activityKind":activity.kind,"csrfToken":who["csrf"]}

    @app.get("/gateway/teacher/roster")
    async def roster(request:Request):
        who=session(request);assignments._teacher(who)
        with store.connect() as db:
            students=[{"userId":r["id"],"name":r["name"],"assignment":assignments.current({"userId":r["id"]})} for r in db.execute("SELECT DISTINCT u.id,u.name FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.role='student' AND s.context=? AND s.resource=?",(registration["contextId"],defense.resourceLinkId))]
        return {"students":students}

    @app.post("/gateway/assignments/request")
    async def choose(body:Selection,request:Request): return await assignments.request(session(request,True),body.essayId,body.idempotencyKey)

    @app.post("/gateway/teacher/assignments")
    async def assign(body:TeacherSelection,request:Request): return await assignments.assign(session(request,True),body.userId,body.essayId,body.idempotencyKey)

    @app.post("/gateway/teacher/assignments/{identifier}/approve")
    async def approve(identifier:str,body:Empty,request:Request): return await assignments.approve(session(request,True),identifier)

    @app.post("/gateway/teacher/assignments/{identifier}/reject")
    async def reject(identifier:str,body:Empty,request:Request): return await assignments.reject(session(request,True),identifier)

    @app.post("/gateway/teacher/assignments/{identifier}/reassign")
    async def reassign(identifier:str,body:Selection,request:Request): return await assignments.reassign(session(request,True),identifier,body.essayId,body.idempotencyKey)

    @app.post("/gateway/launch/pl/{work_id}")
    async def native_launch(work_id:str,body:Empty,request:Request):
        who=session(request,True);work=assignments.launch_work(who,work_id)
        activity=config.registration.activities[who["activity"]]
        if activity.kind=="lab" and activity.workId!=work_id or activity.kind=="defense" and "relatedExercise" not in work: raise LaunchError("Launch from the matching Moodle activity")
        path=await clients.bridge.work_launch(who["uid"],config.instance,delivery["deliveryHash"],work_id)
        token=store.handoff(request.cookies["GatewaySession"],path,int(clock()))
        response=JSONResponse({"url":"/gateway/pl/"+token})
        response.set_cookie("pl2_pre_auth_url",path,secure=True,httponly=True,samesite="lax",path="/")
        return response

    return app
