import asyncio
import pytest
from gateway.assignments import Assignments
from gateway.errors import GatewayError
from gateway.store import Store, digest

class Bridge:
    def __init__(self): self.calls=[]; self.fail=False
    async def apply(self, assignment_id, payload):
        self.calls.append((assignment_id, payload.copy()))
        if self.fail: raise GatewayError("Bridge unavailable", 503)
        return {"schemaVersion":1,"id":assignment_id,**payload,"reconciled":True,"duplicate":False}

def person(store, subject="student-a", role="student"):
    pending = store.state("browser", 1000)
    key = digest(["issuer","client","deploy",subject])
    identity={"userId":key,"issuer":"issuer","client":"client","deployment":"deploy","subject":subject,"uid":"gw-"+key+"@example.invalid","name":subject,"role":role,"context":"context","resource":"defense","activity":"defense","lineitem":"https://moodle.example/lineitem"}
    return store.activate(pending["state"],"browser",1000,identity)

def delivery():
    policy={"schemaVersion":1,"mode":"required-question-completion","source":"per-question-results-v1","questionIds":["demo/exr-1","demo/exr-2","demo/exr-3"],"requiredQuestionIds":["demo/exr-1","demo/exr-2","demo/exr-3"],"atLeast":2,"fullyCompletedScore":1}
    return {"deliveryHash":"a"*64,"courseId":"demo","questions":policy["questionIds"],"instances":{"pilot":{"works":["defense-a","defense-b","lab"]}},"works":[{"id":"defense-a","relatedExercise":"exr-essay-a","completion":policy},{"id":"defense-b","relatedExercise":"exr-essay-b","completion":policy},{"id":"lab","completion":policy}]}

@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/"runtime.sqlite"); bridge=Bridge()
    student=person(store); teacher=person(store,"teacher", "teacher")
    def service(mode="immediate", capacity=None):
        return Assignments(store,bridge,delivery(),"pilot",{"mode":mode,"capacity":capacity or {}},clock=lambda:1000)
    return store,bridge,student,teacher,service

@pytest.mark.anyio
async def test_pending_has_no_acl(setup):
    _, bridge, student, teacher, factory=setup
    service=factory("approval")
    row=await service.request(student,"exr-essay-a","request-a")
    assert row["state"]=="pending" and not bridge.calls
    with pytest.raises(GatewayError): service.launch_work(student,"defense-a")
    approved=await service.approve(teacher,row["id"])
    assert approved["state"]=="approved" and bridge.calls[-1][1]["newWork"]=="defense-a"

@pytest.mark.anyio
async def test_teacher_assignment_and_reject_new_request_audit(setup):
    store,bridge,student,teacher,factory=setup
    service=factory("teacher")
    with pytest.raises(GatewayError): await service.request(student,"exr-essay-a","not-allowed")
    row=await service.assign(teacher,student["userId"],"exr-essay-a","teacher-a")
    assert row["state"]=="assigned"
    other=person(store,"student-b"); pending=factory("approval")
    row=await pending.request(other,"exr-essay-a","pending")
    await pending.reject(teacher,row["id"])
    assert (await pending.request(other,"exr-essay-b","new"))["state"]=="pending"
    with store.connect() as db: assert db.execute("SELECT count(*) FROM requests WHERE user_id=?",(other["userId"],)).fetchone()[0]==2

@pytest.mark.anyio
async def test_duplicate_concurrent_selection_and_capacity(setup):
    store,bridge,student,teacher,factory=setup
    service=factory(capacity={"exr-essay-a":1})
    row=await service.request(student,"exr-essay-a","a")
    assert (await service.request(student,"exr-essay-a","a"))["id"]==row["id"]
    with pytest.raises(GatewayError): await service.request(student,"exr-essay-b","b")
    other=person(store,"student-b")
    with pytest.raises(GatewayError): await service.request(other,"exr-essay-a","full")
    unlimited=factory(); third=person(store,"student-c")
    assert (await unlimited.request(third,"exr-essay-a","unlimited"))["state"]=="assigned"

@pytest.mark.anyio
async def test_reassignment_revokes_existing_session(setup):
    _,bridge,student,teacher,factory=setup
    service=factory(); row=await service.request(student,"exr-essay-a","a")
    assert service.launch_work(student,"defense-a")["id"]=="defense-a"
    replaced=await service.reassign(teacher,row["id"],"exr-essay-b","replacement")
    assert replaced["version"]==2 and bridge.calls[-1][1]["oldWork"]=="defense-a" and bridge.calls[-1][1]["newWork"]=="defense-b"
    with pytest.raises(GatewayError): service.launch_work(student,"defense-a")
    assert service.launch_work(student,"defense-b")["id"]=="defense-b"

@pytest.mark.anyio
async def test_acl_reconciled_after_restart_and_failure_closed(setup):
    store,bridge,student,teacher,factory=setup
    bridge.fail=True; service=factory()
    with pytest.raises(GatewayError): await service.request(student,"exr-essay-a","a")
    assert service.current(student)["state"]=="reconciling"
    with pytest.raises(GatewayError): service.launch_work(student,"defense-a")
    bridge.fail=False
    restarted=Assignments(Store(store.path),bridge,delivery(),"pilot",{"mode":"immediate","capacity":{}},clock=lambda:1001)
    await restarted.reconcile_all()
    assert restarted.current(student)["state"]=="assigned" and bridge.calls[0]==bridge.calls[1]

@pytest.mark.anyio
async def test_foreign_defense_direct_url_denied_and_roster_scope(setup):
    _,_,student,teacher,factory=setup; service=factory()
    assert [w["id"] for w in service.my_works(student)]==["lab"]
    assert len(service.my_works(teacher))==3
    await service.request(student,"exr-essay-a","a")
    assert {w["id"] for w in service.my_works(student)}=={"lab","defense-a"}
    with pytest.raises(GatewayError): service.launch_work(student,"defense-b")
    with pytest.raises(GatewayError): await service.approve(student,service.current(student)["id"])

@pytest.mark.anyio
async def test_upgrade_or_changed_moodle_binding_cannot_mutate_pending_request(setup):
    store,_,student,teacher,factory=setup;old=factory("approval")
    row=await old.request(student,"exr-essay-a","pending")
    upgraded=delivery();upgraded["deliveryHash"]="b"*64
    service=Assignments(store,Bridge(),upgraded,"pilot",{"mode":"approval","capacity":{}},clock=lambda:1000)
    for operation in (service.approve,service.reject):
        with pytest.raises(GatewayError):await operation(teacher,row["id"])
    assert old.current(student)["state"]=="pending" and old.current(student)["version"]==0
    changed=Assignments(store,Bridge(),delivery(),"pilot",{"mode":"approval","capacity":{},"contextId":"other","resourceLinkId":"defense"},clock=lambda:1000)
    foreign_teacher={**teacher,"context":"other"}
    with pytest.raises(GatewayError):await changed.approve(foreign_teacher,row["id"])
    assert old.current(student)["state"]=="pending"

@pytest.mark.anyio
async def test_closed_typed_bridge_confirmation_required_before_activation(setup):
    _,bridge,student,_,factory=setup
    async def invalid(identifier,payload):return {"schemaVersion":True,"id":identifier,**payload,"reconciled":1,"duplicate":False}
    bridge.apply=invalid;service=factory()
    with pytest.raises(GatewayError):await service.request(student,"exr-essay-a","typed")
    assert service.current(student)["state"]=="reconciling"
