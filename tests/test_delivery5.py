"""Gateway читает выбранный Native пакет и маршрутизирует по курсу, без proof."""
import copy,json
import pytest
from gateway.config import load_delivery
from gateway.errors import GatewayError

def fixture(tmp_path):
 q='demo/exr-a';slot='demo/lab/exr-a';(tmp_path/'public.txt').write_text('Открытые данные')
 return {'schemaVersion':5,'courseId':'demo','bookRoot':'tasks','questions':[q],'gradingPayloads':{q:{'kind':'project','uuid':'37d79ee7-fd7d-44db-bd45-6587711862a5','grading':{'method':'manual'},'submission':{'mode':'upload'},'sourceRoot':'projects/a'}},'works':[{'id':'lab','slots':[{'id':slot,'selection':'fixed','candidates':[q],'assignment':{'requirement':'required','workMode':'individual'}}],'policy':{},'completion':{'schemaVersion':2,'mode':'required-slot-completion','source':'per-slot-results-v2','slotIds':[slot],'requiredSlotIds':[slot],'atLeast':1,'fullyCompletedScore':1}}],'instances':{'fixture':{'works':['lab'],'selfEnrollment':False}},'files':['public.txt']}
def save(tmp_path,d):
 (tmp_path/'delivery.json').write_text(json.dumps(d))
def test_current_files_without_proof_or_hash_suffix(tmp_path):
 d=fixture(tmp_path);save(tmp_path,d);assert load_delivery(tmp_path,'fixture')['courseId']=='demo';(tmp_path/'public.txt').write_text('Обновлено');assert load_delivery(tmp_path,'fixture')==d
@pytest.mark.parametrize('name',['../escape','C:/evil','a\\b','/absolute','a//b'])
def test_path_escape_rejected(tmp_path,name):
 d=fixture(tmp_path);d['files']=[name];save(tmp_path,d)
 with pytest.raises(GatewayError):load_delivery(tmp_path,'fixture')
def test_symlink_rejected(tmp_path):
 d=fixture(tmp_path);(tmp_path/'link').symlink_to('/etc/passwd');d['files']=['link'];save(tmp_path,d)
 with pytest.raises(GatewayError):load_delivery(tmp_path,'fixture')
def test_native_uuid_and_slot_binding_remain_closed(tmp_path):
 d=fixture(tmp_path);d['gradingPayloads']['demo/exr-a']['uuid']='not-uuid';save(tmp_path,d)
 with pytest.raises(GatewayError):load_delivery(tmp_path,'fixture')
@pytest.mark.parametrize('layout',['files','zip','files-or-zip'])
def test_upload_layout_preserved(tmp_path,layout):
 d=fixture(tmp_path);d['gradingPayloads']['demo/exr-a']['submission']['layout']=layout;save(tmp_path,d);assert load_delivery(tmp_path,'fixture')['gradingPayloads']['demo/exr-a']['submission']['layout']==layout
@pytest.mark.parametrize('submission',[{'mode':'editor','layout':'zip'},{'mode':'upload','layout':'unknown'}])
def test_invalid_layout_refused(tmp_path,submission):
 d=fixture(tmp_path);d['gradingPayloads']['demo/exr-a']['submission']=submission;save(tmp_path,d)
 with pytest.raises(GatewayError):load_delivery(tmp_path,'fixture')
