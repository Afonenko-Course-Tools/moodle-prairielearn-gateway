"""Exact local candidate producer -> existing Gateway consumer, with no new API."""
import json,os,subprocess
from pathlib import Path
import pytest
from gateway.assignments import Assignments
from gateway.grades import Grades
from gateway.store import Store
from test_assignment import Bridge,person,delivery
from test_gradebook import AGS,result

pytestmark=pytest.mark.skipif(not os.environ.get('PLATFORM_BRIDGE_POLICY'),reason='Set exact local Platform bridge policy path for candidate interoperability')

@pytest.mark.anyio
@pytest.mark.parametrize('route',['assignment','lab'])
async def test_projected_winner_status_and_native_scale_through_actual_bridge(tmp_path,route):
 store=Store(tmp_path/'runtime.sqlite');student=person(store);d=delivery();work=d['works'][0 if route=='assignment' else 2]
 slot=work['slots'][0];slot.update(id='demo/'+work['id']+'/alt-first',selection='choice',candidates=['demo/exr-1','demo/exr-other']);work['completion']['slotIds'][0]=slot['id'];work['completion']['requiredSlotIds'][0]=slot['id']
 assignments=Assignments(store,Bridge(),d,'pilot',{'mode':'immediate','capacity':{}},clock=lambda:1000)
 activities={'lab':{'kind':'lab','workId':'lab','resourceLinkId':'lab-resource','lineitem':'https://moodle.example/lab-lineitem'}}
 grades=Grades(store,assignments,AGS(),clock=lambda:1000,activities=activities)
 if route=='assignment':row=await assignments.request(student,'exr-essay-a','a');base=result(row,student['uid'])
 else:
  with store.transaction() as db:db.execute('INSERT INTO activity_bindings VALUES(?,?,?,?,?)',(student['userId'],'lab','context','lab-resource','https://moodle.example/lab-lineitem'))
  row={'user_id':student['userId'],'activity':'lab','context':'context','resource':'lab-resource','lineitem':'https://moodle.example/lab-lineitem'}
  base={k:v for k,v in result({'id':'unused','version':1,'work':'lab'},student['uid']).items() if k not in ('assignmentId','version')}
 groups=[{'group_id':'1','zone_id':'choice','zone_best_questions':1,'zone_number_choose':None,'number_choose':None,'question_ids':['demo/exr-1']},{'group_id':'2','zone_id':'choice','zone_best_questions':1,'zone_number_choose':None,'number_choose':None,'question_ids':['demo/exr-other']}]+[{'group_id':str(i+2),'zone_id':'regular','zone_best_questions':None,'zone_number_choose':None,'number_choose':None,'question_ids':['demo/exr-'+str(i+1)]} for i in (1,2)]
 script="import {readFileSync} from 'node:fs';const {assignedSlots,completion}=await import(process.argv[1]);const v=JSON.parse(readFileSync(0,'utf8'));const slots=assignedSlots(v.work,v.rows,v.attempt,v.groups);console.log(JSON.stringify({slots,completion:completion(v.work,slots)}));"
 def evidence(a,b,pending):
  scores=[a,b,1,0];qids=['demo/exr-1','demo/exr-other','demo/exr-2','demo/exr-3']
  rows=[{'group_id':str(i+1),'qualified_id':q,'attempt_id':base['currentAttempt']['id'],'attempt_number':1,'score':score,'points':score*10,'max_points':10,'native_status':'correct','requires_manual_grading':i==pending} for i,(q,score) in enumerate(zip(qids,scores))]
  request={'work':work,'rows':rows,'attempt':base['currentAttempt'],'groups':groups}
  command=['node','--input-type=module','-e',script,Path(os.environ['PLATFORM_BRIDGE_POLICY']).resolve().as_uri()]
  run=subprocess.run(command,input=json.dumps(request),text=True,capture_output=True);assert run.returncode==0,run.stderr
  projected=json.loads(run.stdout);request['rows'].reverse();again=subprocess.run(command,input=json.dumps(request),text=True,capture_output=True);assert again.returncode==0 and json.loads(again.stdout)==projected
  e={**base,**projected}
  if route=='lab':e.update(scoreGiven=(max(a,b)+1)*10,scoreMaximum=30)
  return e
 observe=grades.observe if route=='assignment' else grades.observe_lab
 e=evidence(.6,.2,1);event=observe(row,e);assert event['gradingProgress']=='FullyGraded' and event['scoreGiven']==(1 if route=='assignment' else 16) and event['scoreMaximum']==(3 if route=='assignment' else 30)
 with store.connect() as db:before=[tuple(r) for r in db.execute('SELECT * FROM outbox')]
 e=evidence(.6,.8,1);assert e['slots'][0]['qualifiedId']=='demo/exr-other' and e['slots'][0]['status']=='grading';assert observe(row,e) is None
 with store.connect() as db:assert [tuple(r) for r in db.execute('SELECT * FROM outbox')]==before
 e=evidence(.6,1,-1);event=observe(row,e);assert event['scoreGiven']==(2 if route=='assignment' else 20);assert observe(row,e)==event
 e=evidence(1,.8,1);event=observe(row,e);assert event['scoreGiven']==(2 if route=='assignment' else 20) and e['completion']['completedRequired']==2
