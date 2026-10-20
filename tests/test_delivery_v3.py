"""Закрытый reader принимает только согласованный delivery v3."""
import copy,json,hashlib
import pytest
from gateway.config import load_delivery
from gateway.store import native_digest
from gateway.errors import GatewayError

def fixture(tmp_path):
 q='demo/exr-a';slot='demo/lab/exr-a';p=tmp_path/'public.txt';p.write_text('SYNTHETIC')
 return {'schemaVersion':3,'courseId':'demo','bookRoot':'tasks','sourceSnapshotHash':'a'*64,'inventoryHash':'b'*64,'verificationInventoryHash':'c'*64,'questions':[q],'gradingPayloads':{q:{'uuid':'37d79ee7-fd7d-44db-bd45-6587711862a5','grading':{'method':'manual'},'submission':{'mode':'upload'},'sourceRoot':'projects/a'}},'works':[{'id':'lab','slots':[{'id':slot,'selection':'fixed','candidates':[q],'assignment':{'requirement':'required','workMode':'individual'}}],'policy':{},'completion':{'schemaVersion':2,'mode':'required-slot-completion','source':'per-slot-results-v2','slotIds':[slot],'requiredSlotIds':[slot],'atLeast':1,'fullyCompletedScore':1}}],'instances':{'fixture':{'works':['lab'],'selfEnrollment':False}},'files':{'public.txt':hashlib.sha256(p.read_bytes()).hexdigest()}}
def save(tmp_path,value):
 value=copy.deepcopy(value);value['deliveryHash']=native_digest(value);(tmp_path/'delivery.json').write_text(json.dumps(value));return value

def test_exact_v3_grading_and_submission_required(tmp_path):
 d=fixture(tmp_path);save(tmp_path,d);assert load_delivery(tmp_path,'fixture')['gradingPayloads']['demo/exr-a']['grading']['method']=='manual'
 for change in ['old','missing-grading','missing-submission','manual-runtime','extra-question-binding']:
  changed=copy.deepcopy(d);b=changed['gradingPayloads']['demo/exr-a']
  if change=='old':changed['schemaVersion']=2
  if change=='missing-grading':del b['grading']
  if change=='missing-submission':del b['submission']
  if change=='manual-runtime':b['runtime']='fake'
  if change=='extra-question-binding':changed['gradingPayloads']['demo/exr-extra']=b
  save(tmp_path,changed)
  with pytest.raises(GatewayError):load_delivery(tmp_path,'fixture')

def test_choice_catalog_and_path_guards(tmp_path):
 d=fixture(tmp_path);q='demo/exr-b';d['questions'].append(q);d['gradingPayloads'][q]=copy.deepcopy(d['gradingPayloads']['demo/exr-a']);d['gradingPayloads'][q]['uuid']='8cb757ae-6f14-467a-83cf-c1cfcb65d116'
 work=d['works'][0];s=work['slots'][0];s.update(id='demo/lab/alt-pair',selection='choice',candidates=list(d['questions']));work['completion']['slotIds']=[s['id']];work['completion']['requiredSlotIds']=[s['id']]
 save(tmp_path,d);assert load_delivery(tmp_path,'fixture')['works'][0]['slots'][0]['selection']=='choice'
 for path in ['C:/private','a\x00b','../secret','a//b','/absolute','a\\b']:
  changed=copy.deepcopy(d);changed['gradingPayloads'][q]['sourceRoot']=path;save(tmp_path,changed)
  with pytest.raises(GatewayError):load_delivery(tmp_path,'fixture')
 changed=copy.deepcopy(d);changed['works'][0]['slots'][0]['assignment']['requirement']='optional';save(tmp_path,changed)
 with pytest.raises(GatewayError):load_delivery(tmp_path,'fixture')
