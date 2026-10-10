import copy
import pytest
from gateway.grades import Grades
from gateway.errors import GatewayError
from gateway.assignments import Assignments
from gateway.store import Store
from test_assignment import Bridge, person, delivery

class AGS:
    def __init__(self): self.sent=[]; self.fail=False
    async def score(self, lineitem, payload, key):
        if self.fail: raise GatewayError("AGS unavailable",503)
        self.sent.append((lineitem,copy.deepcopy(payload),key))

def result(row, uid, scores=(1,1,0)):
    return {"schemaVersion":1,"source":"per-question-results-v1","assignmentId":row["id"],"version":row["version"],"userUid":uid,"instance":"pilot","deliveryHash":"a"*64,"workId":row["work"],"currentAttempt":{"id":"attempt-a","number":1},"questions":[{"qualifiedId":"demo/exr-"+str(i+1),"score":score,"points":score*10,"maxPoints":10,"status":"complete"} for i,score in enumerate(scores)],"completion":{"completedRequired":sum(s==1 for s in scores),"requiredCount":3,"atLeast":2,"passed":sum(s==1 for s in scores)>=2}}

@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/"runtime.sqlite"); bridge=Bridge(); ags=AGS()
    student=person(store); teacher=person(store,"teacher","teacher")
    assignments=Assignments(store,bridge,delivery(),"pilot",{"mode":"immediate","capacity":{}},clock=lambda:1000)
    grades=Grades(store,assignments,ags,clock=lambda:1000)
    return store,student,teacher,assignments,grades,ags

@pytest.mark.anyio
async def test_weighted_partial_does_not_count_as_two(setup):
    _,student,_,assignments,grades,ags=setup
    row=await assignments.request(student,"exr-essay-a","a")
    evidence=result(row,student["uid"],(1,0.8,0.7))
    event=grades.observe(row,evidence)
    assert event["scoreGiven"]==1 and event["scoreMaximum"]==3
    await grades.drain()
    assert ags.sent[0][1]["scoreGiven"]==1 and assignments.current(student)["state"]=="assigned"

@pytest.mark.anyio
async def test_optional_full_score_does_not_replace_required(setup):
    _,student,_,assignments,grades,_=setup
    assignments.works["defense-a"]["completion"]["requiredQuestionIds"]=["demo/exr-1","demo/exr-2"]
    row=await assignments.request(student,"exr-essay-a","a")
    evidence=result(row,student["uid"],(1,0,1));evidence["completion"]={"completedRequired":1,"requiredCount":2,"atLeast":2,"passed":False}
    assert grades.observe(row,evidence)["scoreGiven"]==1

@pytest.mark.anyio
async def test_old_assignment_grade_ignored_and_prior_grade_preserved(setup):
    store,student,teacher,assignments,grades,ags=setup
    old=await assignments.request(student,"exr-essay-a","a")
    grades.observe(old,result(old,student["uid"]));await grades.drain()
    await assignments.reassign(teacher,old["id"],"exr-essay-b","replace")
    assert grades.observe(old,result(old,student["uid"])) is None
    await grades.drain();assert len(ags.sent)==1
    with store.connect() as db: assert db.execute("SELECT count(*) FROM audit WHERE kind='old-result-ignored'").fetchone()[0]>=1

@pytest.mark.anyio
async def test_two_students_one_moodle_lineitem(setup):
    store,a,_,assignments,grades,ags=setup;b=person(store,"student-b")
    for student in (a,b):
        row=await assignments.request(student,"exr-essay-a","key")
        grades.observe(row,result(row,student["uid"]))
    await grades.drain()
    assert len(ags.sent)==2 and {s[1]["userId"] for s in ags.sent}=={"student-a","student-b"}
    assert len({s[0] for s in ags.sent})==1

@pytest.mark.anyio
async def test_ags_outbox_retry_idempotent_and_restart(setup):
    store,student,_,assignments,grades,ags=setup
    row=await assignments.request(student,"exr-essay-a","a");evidence=result(row,student["uid"])
    grades.observe(row,evidence);grades.observe(row,evidence)
    ags.fail=True;await grades.drain()
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0]==1
        assert db.execute("SELECT state FROM outbox").fetchone()[0]=="pending"
    ags.fail=False
    restarted=Grades(Store(store.path),assignments,ags,clock=lambda:2000)
    await restarted.drain();await restarted.drain()
    assert len(ags.sent)==1 and ags.sent[0][1]["scoreGiven"]==2 and assignments.current(student)["state"]=="completed"

@pytest.mark.anyio
@pytest.mark.parametrize("field,value",[("userUid","foreign"),("version",3),("deliveryHash","b"*64),("workId","foreign"),("assignmentId","foreign"),("instance","foreign")])
async def test_wrong_user_activity_context_version_rejected(setup,field,value):
    _,student,_,assignments,grades,_=setup;row=await assignments.request(student,"exr-essay-a","a")
    evidence=result(row,student["uid"]);evidence[field]=value
    with pytest.raises(GatewayError): grades.observe(row,evidence)

@pytest.mark.anyio
async def test_closed_questions_completion_and_attempt_scope(setup):
    _,student,_,assignments,grades,_=setup;row=await assignments.request(student,"exr-essay-a","a")
    evidence=result(row,student["uid"]);evidence["completion"]["completedRequired"]=3
    with pytest.raises(GatewayError): grades.observe(row,evidence)
    evidence=result(row,student["uid"]);evidence["questions"][1]=evidence["questions"][0]
    with pytest.raises(GatewayError): grades.observe(row,evidence)
    evidence=result(row,student["uid"]);evidence["currentAttempt"]["number"]=2;grades.observe(row,evidence)
    assert grades.observe(row,result(row,student["uid"])) is None

@pytest.mark.anyio
async def test_failed_old_attempt_cannot_overwrite_new_attempt(setup):
    store,student,_,assignments,grades,ags=setup
    row=await assignments.request(student,"exr-essay-a","a")
    old=result(row,student["uid"],(1,0,0))
    first=grades.observe(row,old);ags.fail=True;await grades.drain()
    new=result(row,student["uid"],(1,1,0));new["currentAttempt"]={"id":"attempt-b","number":2}
    second=grades.observe(row,new)
    assert second["timestamp"]>first["timestamp"]
    ags.fail=False;await grades.drain()
    grades.clock=lambda:2000
    await grades.drain()
    assert [sent[1]["scoreGiven"] for sent in ags.sent]==[2]
    with store.connect() as db:
        assert sorted(r[0] for r in db.execute("SELECT state FROM outbox"))==["obsolete","sent"]

@pytest.mark.anyio
async def test_latest_regrade_in_same_attempt_supersedes_pending_score(setup):
    _,student,_,assignments,grades,ags=setup
    row=await assignments.request(student,"exr-essay-a","a")
    grades.observe(row,result(row,student["uid"],(1,0,0)))
    grades.observe(row,result(row,student["uid"],(1,1,0)))
    await grades.drain()
    assert [sent[1]["scoreGiven"] for sent in ags.sent]==[2]

@pytest.mark.anyio
@pytest.mark.parametrize("field",["contextId","resourceLinkId","lineitem"])
async def test_changed_runtime_activity_cannot_deliver_prior_pending_grade(setup,field):
    store,student,_,assignments,grades,ags=setup
    row=await assignments.request(student,"exr-essay-a","a");evidence=result(row,student["uid"])
    grades.observe(row,evidence)
    assignments.flow[field]="changed-runtime-binding"
    with pytest.raises(GatewayError):grades.observe(row,evidence)
    await grades.drain();assert not ags.sent
    with store.connect() as db:assert db.execute("SELECT state FROM outbox").fetchone()[0]=="obsolete"

@pytest.mark.anyio
@pytest.mark.parametrize("queued_state",["pending","sending"])
@pytest.mark.parametrize("change",["delivery","instance"])
async def test_restart_changed_delivery_or_instance_fences_old_observation_and_outbox(setup,queued_state,change):
    store,student,_,assignments,grades,ags=setup
    row=await assignments.request(student,"exr-essay-a","a")
    evidence=result(row,student["uid"]);grades.observe(row,evidence)
    with store.transaction() as db:
        db.execute("UPDATE outbox SET state=?,lease_until=900",(queued_state,))
    upgraded=delivery();instance="pilot"
    if change=="delivery":upgraded["deliveryHash"]="b"*64
    else:
        instance="other";upgraded["instances"][instance]=upgraded["instances"]["pilot"]
    reopened=Store(store.path)
    restarted=Assignments(reopened,Bridge(),upgraded,instance,{"mode":"immediate","capacity":{}},clock=lambda:2000)
    resumed=Grades(reopened,restarted,ags,clock=lambda:2000)
    accepted=resumed.observe(row,evidence)
    await resumed.drain()
    assert not ags.sent  # No replacement/zero score; preserve the prior Moodle grade.
    assert accepted is None
    with reopened.connect() as db:
        current=db.execute("SELECT * FROM assignments WHERE id=?",(row["id"],)).fetchone()
        assert current["state"]=="assigned" and current["version"]==row["version"]
        assert current["delivery"]==row["delivery"] and current["lineitem"]==row["lineitem"]
        assert db.execute("SELECT state FROM outbox").fetchone()[0]=="obsolete"
        assert db.execute("SELECT count(*) FROM observations").fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM audit WHERE kind='old-result-ignored'").fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM audit WHERE kind='old-outbox-ignored'").fetchone()[0]==1
