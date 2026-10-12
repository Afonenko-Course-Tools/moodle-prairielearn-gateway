import copy
import pytest
from gateway.assignments import Assignments
from gateway.grades import Grades
from gateway.store import Store
from gateway.errors import GatewayError
from test_assignment import Bridge, person, delivery
from test_gradebook import AGS, result

@pytest.mark.anyio
@pytest.mark.parametrize('kind',['defense','lab'])
@pytest.mark.parametrize('status',['unanswered','grading'])
async def test_first_started_selection_is_durable_before_score(tmp_path,kind,status):
 store=Store(tmp_path/'runtime.sqlite');student=person(store);d=delivery()
 work=next(w for w in d['works'] if w['id']==('defense-a' if kind=='defense' else 'lab'))
 work['slots'][0]['selection']='random';work['slots'][0]['candidates'].append('demo/exr-other')
 assignments=Assignments(store,Bridge(),d,'pilot',{'mode':'immediate','capacity':{}},clock=lambda:1000)
 activities={};ags=AGS()
 if kind=='defense':
  binding=await assignments.request(student,'exr-essay-a','first')
  evidence=result(binding,student['uid'])
 else:
  binding={'user_id':student['userId'],'activity':'lab','context':'context','resource':'lab-resource','lineitem':'https://moodle.example/lab-lineitem'}
  with store.transaction() as db:db.execute('INSERT INTO activity_bindings VALUES(?,?,?,?,?)',tuple(binding.values()))
  activities={'lab':{'kind':'lab','workId':'lab','resourceLinkId':binding['resource'],'lineitem':binding['lineitem']}}
  evidence=result({'id':'unused','version':1,'work':'lab'},student['uid'])
  evidence.pop('assignmentId');evidence.pop('version');evidence.update(scoreGiven=None,scoreMaximum=None)
 for s in evidence['slots']:s.update(score=None,points=None,maxPoints=None,status=status)
 evidence['completion'].update(completedRequired=0,passed=False)
 grades=Grades(store,assignments,ags,clock=lambda:1000,activities=activities)
 observe=lambda g,e:g.observe(binding,e) if kind=='defense' else g.observe_lab(binding,e)
 not_started={**evidence,'state':'not-started','currentAttempt':None,'slots':[]}
 assert observe(grades,not_started) is None
 with store.connect() as db:assert db.execute('SELECT count(*) FROM native_attempts').fetchone()[0]==0
 assert observe(grades,evidence) is None
 with store.connect() as db:assert db.execute('SELECT count(*) FROM native_attempts').fetchone()[0]==1
 with store.connect() as db:
  assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==0
  assert db.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
 resumed=Grades(Store(store.path),assignments,ags,clock=lambda:1001,activities=activities)
 changed=copy.deepcopy(evidence);changed['slots'][0]['qualifiedId']='demo/exr-other'
 with pytest.raises(GatewayError,match='changed'):observe(resumed,changed)
 changed=copy.deepcopy(evidence);changed['currentAttempt']['id']='replacement'
 with pytest.raises(GatewayError,match='attempt'):observe(resumed,changed)
 ready=copy.deepcopy(evidence)
 for s in ready['slots']:s.update(score=1,points=1,maxPoints=1,status='complete')
 ready['completion'].update(completedRequired=3,passed=True)
 if kind=='lab':ready.update(scoreGiven=3,scoreMaximum=3)
 assert observe(resumed,ready)['scoreGiven']==3
 assert observe(resumed,ready)['scoreGiven']==3
 changed=copy.deepcopy(evidence);changed['currentAttempt']['number']=2
 with pytest.raises(GatewayError,match='attempt'):observe(resumed,changed)
 newer=copy.deepcopy(evidence);newer['currentAttempt']={'id':'second-attempt','number':2}
 assert observe(resumed,newer) is None
 await resumed.drain()
 assert ags.sent==[]
 assert observe(resumed,ready) is None
 ready['currentAttempt']=newer['currentAttempt']
 assert observe(resumed,ready)['scoreGiven']==3
 await resumed.drain()
 assert len(ags.sent)==1

def test_database_without_current_binding_table_rejected_without_mutation(tmp_path):
 import sqlite3
 p=tmp_path/'prior-candidate.sqlite'
 with sqlite3.connect(p) as db:
  db.execute('CREATE TABLE schema_versions(version INTEGER PRIMARY KEY)')
  db.execute('INSERT INTO schema_versions VALUES(3)')
 before=p.read_bytes()
 with pytest.raises(GatewayError,match='schema'):Store(p)
 assert p.read_bytes()==before and not p.with_suffix('.sqlite-wal').exists()
