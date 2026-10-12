"""Source fixture must be exported by the installed owner CLI before this gate."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import pytest
from gateway.assignments import Assignments
from gateway.config import load_delivery
from gateway.errors import GatewayError
from gateway.grades import Grades
from gateway.store import Store
from test_assignment import Bridge,person
from test_gradebook import AGS

@pytest.mark.anyio
async def test_installed_native_60_defenses_capacity_two_students_and_two_of_three(tmp_path):
    native=os.environ.get("GATEWAY_NATIVE_FIXTURE")
    if not native:pytest.skip("Set GATEWAY_NATIVE_FIXTURE to standard installed exporter output")
    delivery=load_delivery(native,"pilot")
    assert len(delivery["works"])==61 and len(delivery["questions"])==3
    store=Store(tmp_path/"runtime.sqlite");bridge=Bridge();ags=AGS()
    service=Assignments(store,bridge,delivery,"pilot",{"mode":"immediate","capacity":{}},clock=lambda:1000)
    grades=Grades(store,service,ags,clock=lambda:1000)
    a,b,teacher=person(store),person(store,"student-b"),person(store,"teacher","teacher")
    assert len(service.essays)==60 and len(service.my_works(teacher))==61
    assert [w["id"] for w in service.my_works(a)]==["sec-gateway-lab"]
    essays=list(service.essays)
    ra=await service.request(a,essays[0],"choose");rb=await service.request(b,essays[1],"choose")
    assert ra["work"]!=rb["work"] and len(service.my_works(a))==len(service.my_works(b))==2
    with pytest.raises(GatewayError):service.launch_work(a,rb["work"])
    for who,row in ((a,ra),(b,rb)):
        policy=service.works[row["work"]]["completion"]
        assert len(policy["requiredSlotIds"])==3 and policy["atLeast"]==2
        evidence={"schemaVersion":2,"source":"per-slot-results-v2","state":"started","assignmentId":row["id"],"version":row["version"],"userUid":who["uid"],"instance":"pilot","deliveryHash":delivery["deliveryHash"],"workId":row["work"],"currentAttempt":{"id":"synthetic-"+who["subject"],"number":1},"slots":[{"slotId":q,"qualifiedId":next(s["candidates"][0] for s in assignments.works[row["work"]]["slots"] if s["id"]==q),"score":s,"points":s*3,"maxPoints":3,"status":"complete"} for q,s in zip(policy["slotIds"],(1,1,.8),strict=True)],"completion":{"completedRequired":2,"requiredCount":3,"atLeast":2,"passed":True}}
        assert grades.observe(row,evidence)["scoreGiven"]==2
    await grades.drain()
    assert len(ags.sent)==2 and {r[1]["userId"] for r in ags.sent}=={"student-a","student-b"}
    assert all(r[1]["scoreGiven"]==2 and r[1]["scoreMaximum"]==3 for r in ags.sent)
    restarted=Store(store.path)
    continued=Assignments(restarted,bridge,delivery,"pilot",{"mode":"immediate","capacity":{}},clock=lambda:1001)
    assert continued.current(a)["id"]==ra["id"] and continued.current(a)["lineitem"]==ra["lineitem"]
    await Grades(restarted,continued,ags,clock=lambda:2000).drain();assert len(ags.sent)==2
    c,d=person(store,"student-c"),person(store,"student-d")
    limited=Assignments(store,bridge,delivery,"pilot",{"mode":"immediate","capacity":{essays[2]:1}},clock=lambda:1000)
    def choose(who):
        try:return asyncio.run(limited.request(who,essays[2],"concurrent"))["state"]
        except GatewayError:return "conflict"
    with ThreadPoolExecutor(2) as pool:assert sorted(pool.map(choose,(c,d)))==["assigned","conflict"]
