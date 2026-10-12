import sqlite3
from types import SimpleNamespace
import pytest
from gateway.grades import Grades
from gateway.store import Store
from gateway.errors import GatewayError

WORK={'id':'lab','slots':[{'id':'demo/lab/alt-x','selection':'random','candidates':['demo/exr-a','demo/exr-b'],'assignment':{'requirement':'required','workMode':'individual'}}],'completion':{'schemaVersion':2,'mode':'required-slot-completion','source':'per-slot-results-v2','slotIds':['demo/lab/alt-x'],'requiredSlotIds':['demo/lab/alt-x'],'atLeast':1,'fullyCompletedScore':1}}

def test_selected_random_slot_is_one_required_result():
 g=Grades(None,None,None)
 row={'slotId':'demo/lab/alt-x','qualifiedId':'demo/exr-b','score':1,'points':1,'maxPoints':1,'status':'complete'}
 assert g._slots(WORK,[row])=={'completedRequired':1,'requiredCount':1,'atLeast':1,'passed':True}
 for bad in [[],[row,row],[{**row,'qualifiedId':'demo/exr-c'}],[{**row,'slotId':'demo/lab/other'}],[{**row,'score':1,'points':0}]]:
  with pytest.raises(GatewayError):g._slots(WORK,bad)
 assert g._slots(WORK,[{**row,'score':.5,'points':.5}])['passed'] is False

def test_previous_database_rejected_without_mutation(tmp_path):
 p=tmp_path/'old.sqlite'
 with sqlite3.connect(p) as db:db.execute('CREATE TABLE schema_versions(version INTEGER PRIMARY KEY)');db.execute('INSERT INTO schema_versions VALUES(2)')
 before=p.read_bytes()
 with pytest.raises(GatewayError,match='schema'):Store(p)
 assert p.read_bytes()==before and not (tmp_path/'old.sqlite-wal').exists()

def test_old_standalone_config_rejected():
 from gateway.courses import CoursesConfiguration
 from test_config import example
 with pytest.raises(ValueError):CoursesConfiguration.model_validate({'schemaVersion':1,**example()})

def test_unsupported_delivery_rejected_before_database_write(tmp_path):
 import json
 from gateway.app import create_app
 from test_config import example
 native=tmp_path/'native';native.mkdir();(native/'delivery.json').write_text(json.dumps({'schemaVersion':1}))
 config=example();config['database']=str(tmp_path/'untouched/runtime.sqlite');config['deliveryPath']=str(native)
 with pytest.raises(GatewayError,match='schema'):create_app(config)
 assert not (tmp_path/'untouched').exists()

@pytest.mark.anyio
async def test_native_selection_persists_across_regrade_and_restart(tmp_path):
 from gateway.assignments import Assignments
 from test_assignment import Bridge,person,delivery
 from test_gradebook import AGS,result
 store=Store(tmp_path/'runtime.sqlite');student=person(store);d=delivery()
 work=d['works'][0];work['slots'][0]['selection']='random';work['slots'][0]['candidates'].append('demo/exr-other');work['slots'][0]['id']='demo/defense-a/alt-first'
 work['completion']['slotIds'][0]=work['slots'][0]['id'];work['completion']['requiredSlotIds'][0]=work['slots'][0]['id']
 assignments=Assignments(store,Bridge(),d,'pilot',{'mode':'immediate','capacity':{}},clock=lambda:1000)
 row=await assignments.request(student,'exr-essay-a','a');e=result(row,student['uid']);e['slots'][0]['slotId']=work['slots'][0]['id']
 Grades(store,assignments,AGS(),clock=lambda:1000).observe(row,e)
 resumed=Grades(Store(store.path),assignments,AGS(),clock=lambda:1001)
 changed={**e,'slots':[dict(s) for s in e['slots']]};changed['slots'][0]['qualifiedId']='demo/exr-other'
 with pytest.raises(GatewayError,match='changed'):resumed.observe(row,changed)
 e['slots'][0].update(score=.5,points=5)
 e['completion'].update(completedRequired=1,passed=False)
 assert resumed.observe(row,e)['scoreGiven']==1
 not_started={**e,'state':'not-started','currentAttempt':None,'slots':[],'completion':{'completedRequired':0,'requiredCount':3,'atLeast':2,'passed':False}}
 assert resumed.observe(row,not_started) is None
