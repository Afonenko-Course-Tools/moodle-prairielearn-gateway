from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import asyncio
import copy
import httpx
import pytest
from gateway.app import create_app
from gateway.store import Store
from gateway.assignments import Assignments
from gateway.errors import GatewayError
from test_assignment import Bridge, person, delivery
from test_config import example

def test_atomic_concurrent_capacity_and_duplicate_selection(tmp_path):
    store=Store(tmp_path/"runtime.sqlite"); a=person(store);b=person(store,"student-b")
    service=Assignments(store,Bridge(),delivery(),"pilot",{"mode":"immediate","capacity":{"exr-essay-a":1}},clock=lambda:1000)
    def choose(who):
        try:return asyncio.run(service.request(who,"exr-essay-a","concurrent"))["state"]
        except GatewayError:return "conflict"
    with ThreadPoolExecutor(2) as pool: outcomes=list(pool.map(choose,(a,b)))
    assert sorted(outcomes)==["assigned","conflict"]
    with Store(store.path).connect() as db:assert db.execute("SELECT count(*) FROM assignments").fetchone()[0]==1

def test_current_schema_and_expired_auth_after_restart(tmp_path):
    store=Store(tmp_path/"runtime.sqlite");who=person(store)
    restarted=Store(store.path)
    assert restarted.session(who["token"],1001)["subject"]=="student-a"
    with pytest.raises(GatewayError):restarted.session(who["token"],4600)
    with restarted.connect() as db:assert [r[0] for r in db.execute("SELECT version FROM schema_versions ORDER BY version")]==[3]

def test_concurrent_fresh_schema_creations_are_serialized(tmp_path):
    path=tmp_path/"fresh.sqlite";barrier=Barrier(8)
    def start(_):
        barrier.wait()
        return Store(path)
    with ThreadPoolExecutor(8) as pool: stores=list(pool.map(start,range(8)))
    with stores[0].connect() as db:
        assert [r[0] for r in db.execute("SELECT version FROM schema_versions ORDER BY version")]==[3]

@pytest.mark.anyio
@pytest.mark.parametrize("retain_database", [True, False])
async def test_new_instance_preserves_session_csrf_and_old_assignment_only_with_same_database(tmp_path, retain_database):
    store=Store(tmp_path/"runtime.sqlite")
    config=example();config.update(database=str(store.path),routePrefix="/gateway/courses/java")
    config["registration"]["activities"]["lab"]={"kind":"lab","resourceLinkId":"lab-resource","lineitem":"https://moodle.example/lineitems/lab","workId":"lab"}
    pending=store.state("browser",1000)
    identity={"userId":"synthetic-student","uid":"student@example.invalid","issuer":"https://moodle.example","client":"client","deployment":"deploy","subject":"student","name":"Студент","role":"student","context":"context","resource":"resource","activity":"defense","lineitem":"https://moodle.example/lineitems/defense"}
    defense=store.activate(pending["state"],"browser",1000,identity)
    old=Assignments(store,Bridge(),delivery(),"pilot",{"mode":"immediate","capacity":{}},clock=lambda:1000)
    legacy=await old.request(defense,"exr-essay-a","pilot-request")
    pending=store.state("browser",1000)
    lab=store.activate(pending["state"],"browser",1000,{**identity,"activity":"lab","resource":"lab-resource","lineitem":"https://moodle.example/lineitems/lab"})
    with store.connect() as db:
        old_requests=[dict(row) for row in db.execute("SELECT * FROM requests WHERE assignment_id=?",(legacy["id"],))]

    next_delivery=copy.deepcopy(delivery());next_delivery["deliveryHash"]="b"*64
    next_delivery["instances"]={"2026-2027-s1":{"works":["defense-a","defense-b","lab"]}}
    config.update(instance="2026-2027-s1",deliveryPath="/private/new-native-course")
    restarted=Store(store.path if retain_database else tmp_path/"fresh.sqlite")
    config["database"]=str(restarted.path)
    class Native(Bridge):
        async def enroll(self,uid,instance,delivery_hash):
            assert (uid,instance,delivery_hash)==("student@example.invalid","2026-2027-s1","b"*64)
        async def work_launch(self,uid,instance,delivery_hash,work):
            assert (uid,instance,delivery_hash,work)==("student@example.invalid","2026-2027-s1","b"*64,"lab")
            return "/pl/course_instance/999/assessment/999/"
    class Clients:
        bridge=Native()
        class AGS:
            async def score(self,*args):raise AssertionError("AGS is outside the local restart test")
        ags=AGS()
    app=create_app(config,store=restarted,delivery=next_delivery,clients=Clients(),clock=lambda:1001,start_worker=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="https://course.example") as client:
        client.cookies.set("GatewaySession",lab["token"],domain="course.example",path="/gateway/courses/java")
        response=await client.get("/gateway/courses/java/my/works")
        assert response.status_code==(200 if retain_database else 403)
        if retain_database:
            assert response.json()["assignment"] is None
            assert [work["id"] for work in response.json()["works"]]==["lab"]
            assert response.json()["csrfToken"]==lab["csrf"]
            bootstrap=await client.get("/gateway/courses/java/bootstrap")
            assert bootstrap.status_code==303 and bootstrap.headers["location"]=="/pl/course_instance/999/assessment/999/"
            headers={"Origin":"https://course.example","X-CSRF-Token":"wrong"}
            assert (await client.post("/gateway/courses/java/launch/pl/lab",headers=headers,json={})).status_code==403
            headers["X-CSRF-Token"]=lab["csrf"]
            launch=await client.post("/gateway/courses/java/launch/pl/lab",headers=headers,json={})
            assert launch.status_code==200
            handoff=launch.json()["url"].rsplit("/",1)[1]
            assert restarted.consume_handoff(handoff,1001)["target"]=="/pl/course_instance/999/assessment/999/"
            client.cookies.set("GatewaySession",defense["token"],domain="course.example",path="/gateway/courses/java")
            headers["X-CSRF-Token"]=defense["csrf"]
            selected=await client.post("/gateway/courses/java/assignments/request",headers=headers,json={"essayId":"exr-essay-b","idempotencyKey":"new-semester-request"})
            assert selected.status_code==200
            assert selected.json()["instance"]=="2026-2027-s1" and selected.json()["delivery"]=="b"*64
            assert selected.json()["id"]!=legacy["id"] and selected.json()["state"]=="assigned"
    with store.connect() as db:
        assert dict(db.execute("SELECT * FROM assignments WHERE id=?",(legacy["id"],)).fetchone())==legacy
        assert [dict(row) for row in db.execute("SELECT * FROM requests WHERE assignment_id=?",(legacy["id"],))]==old_requests
