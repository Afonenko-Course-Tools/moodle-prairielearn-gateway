import pytest
from gateway.assignments import Assignments
from gateway.grades import Grades
from gateway.store import Store
from gateway.errors import GatewayError
from test_assignment import Bridge,person,delivery
from test_gradebook import AGS

@pytest.mark.anyio
async def test_labs_preserve_native_declared_weighted_score_and_activity(tmp_path):
    store=Store(tmp_path/"runtime.sqlite");student=person(store)
    with store.transaction() as db:
        db.execute("INSERT INTO activity_bindings VALUES(?,?,?,?,?)",(student["userId"],"lab","context","lab-resource","https://moodle.example/lab-lineitem"))
    assignments=Assignments(store,Bridge(),delivery(),"pilot",{"mode":"immediate","capacity":{}},clock=lambda:1000)
    activities={"lab":{"kind":"lab","workId":"lab","resourceLinkId":"lab-resource","lineitem":"https://moodle.example/lab-lineitem"}}
    ags=AGS();grades=Grades(store,assignments,ags,clock=lambda:1000,activities=activities)
    binding={"user_id":student["userId"],"uid":student["uid"],"subject":"student-a","activity":"lab","context":"context","resource":"lab-resource","lineitem":"https://moodle.example/lab-lineitem"}
    evidence={"schemaVersion":1,"source":"per-question-results-v1","userUid":student["uid"],"instance":"pilot","deliveryHash":"a"*64,"workId":"lab","currentAttempt":{"id":"native-lab","number":1},"questions":[{"qualifiedId":"demo/exr-"+str(i+1),"score":s,"points":s,"maxPoints":1,"status":"complete"} for i,s in enumerate((1,.5,1))],"completion":{"completedRequired":2,"requiredCount":3,"atLeast":2,"passed":True},"scoreGiven":6,"scoreMaximum":8}
    assert grades.observe_lab(binding,evidence)["scoreGiven"]==6
    await grades.drain()
    assert ags.sent[0][0]=="https://moodle.example/lab-lineitem" and ags.sent[0][1]["scoreMaximum"]==8
    wrong={**evidence,"userUid":"foreign"}
    with pytest.raises(GatewayError): grades.observe_lab(binding,wrong)
