import copy
import pytest
from gateway.assignments import Assignments
from gateway.grades import Grades
from gateway.store import Store
from gateway.errors import GatewayError
from test_assignment import Bridge,person,delivery
from test_gradebook import AGS,result

@pytest.mark.anyio
@pytest.mark.parametrize('selection',['choice','random','fixed'])
async def test_representative_changes_only_for_canonical_choice_pool(tmp_path,selection):
 store=Store(tmp_path/'runtime.sqlite');student=person(store);d=delivery()
 slot=d['works'][0]['slots'][0];slot['selection']=selection;slot['candidates'].append('demo/exr-other')
 assignments=Assignments(store,Bridge(),d,'pilot',{'mode':'immediate','capacity':{}},clock=lambda:1000)
 grades=Grades(store,assignments,AGS(),clock=lambda:1000)
 row=await assignments.request(student,'exr-essay-a','a');a=result(row,student['uid']);grades.observe(row,a)
 b=copy.deepcopy(a);b['slots'][0]['qualifiedId']='demo/exr-other'
 if selection!='choice':
  with pytest.raises(GatewayError,match='changed'):grades.observe(row,b)
  return
 event=grades.observe(row,b);assert event['scoreGiven']==2 and event['scoreMaximum']==3
 assert grades.observe(row,b)==event
 with store.connect() as db:
  assert db.execute('SELECT count(*) FROM native_attempts').fetchone()[0]==1
  assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==2
 slot['candidates'].append('demo/exr-new')
 with pytest.raises(GatewayError,match='changed'):grades.observe(row,b)
 slot['candidates'].pop()
 bad=copy.deepcopy(b);bad['slots'][0]['qualifiedId']='demo/foreign'
 with pytest.raises(GatewayError):grades.observe(row,bad)
 for field,value in [('deliveryHash','b'*64),('instance','other')]:
  bad=copy.deepcopy(b);bad[field]=value
  with pytest.raises(GatewayError):grades.observe(row,bad)
 bad=copy.deepcopy(b);bad['currentAttempt']['id']='conflicting-same-number'
 with pytest.raises(GatewayError):grades.observe(row,bad)

@pytest.mark.anyio
async def test_pending_teacher_preserves_prior_outbox_without_fully_graded_event(tmp_path):
 store=Store(tmp_path/'runtime.sqlite');student=person(store)
 assignments=Assignments(store,Bridge(),delivery(),'pilot',{'mode':'immediate','capacity':{}},clock=lambda:1000)
 grades=Grades(store,assignments,AGS(),clock=lambda:1000)
 row=await assignments.request(student,'exr-essay-a','a');pending=result(row,student['uid'])
 pending['slots'][2].update(status='grading',score=1,points=10)
 assert grades.observe(row,pending) is None
 with store.connect() as db:assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
 complete=result(row,student['uid']);old=grades.observe(row,complete)
 with store.connect() as db:before=[tuple(r) for r in db.execute('SELECT * FROM outbox')]
 assert grades.observe(row,pending) is None
 with store.connect() as db:assert [tuple(r) for r in db.execute('SELECT * FROM outbox')]==before
 assert grades.observe(row,complete)==old

@pytest.mark.anyio
async def test_choice_partial_winner_and_pending_winner_keep_existing_ags_scales(tmp_path):
 store=Store(tmp_path/'runtime.sqlite');student=person(store);d=delivery();slot=d['works'][0]['slots'][0];slot['selection']='choice';slot['candidates'].append('demo/exr-other')
 assignments=Assignments(store,Bridge(),d,'pilot',{'mode':'immediate','capacity':{}},clock=lambda:1000);grades=Grades(store,assignments,AGS(),clock=lambda:1000)
 row=await assignments.request(student,'exr-essay-a','a');partial=result(row,student['uid'],(.6,1,0))
 event=grades.observe(row,partial);assert event['gradingProgress']=='FullyGraded' and event['scoreGiven']==1 and event['scoreMaximum']==3
 pending=copy.deepcopy(partial);pending['slots'][0].update(qualifiedId='demo/exr-other',status='grading',score=.8,points=8)
 with store.connect() as db:before=[tuple(r) for r in db.execute('SELECT * FROM outbox')]
 assert grades.observe(row,pending) is None
 with store.connect() as db:assert [tuple(r) for r in db.execute('SELECT * FROM outbox')]==before
 finished=copy.deepcopy(pending);finished['slots'][0].update(status='complete',score=1,points=10);finished['completion'].update(completedRequired=2,passed=True)
 event=grades.observe(row,finished);assert event['scoreGiven']==2 and event['scoreMaximum']==3
 assert grades.observe(row,finished)==event
