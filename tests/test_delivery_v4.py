"""Reader v4 сохраняет типизированные Native-контракты без понижения версии."""
import copy
import pytest
from gateway.config import load_delivery
from gateway.errors import GatewayError
from test_delivery_v3 import fixture, save

UUID = '37d79ee7-fd7d-44db-bd45-6587711862a5'

def binding(kind):
    if kind == 'project':
        return dict(kind=kind, uuid=UUID, grading={'method':'manual'},
                    submission={'mode':'upload'}, sourceRoot='projects/a')
    if kind == 'workspace':
        return dict(kind=kind, uuid=UUID, grading={'method':'manual'}, sourceRoot='projects/a',
            submission={'mode':'workspace','workspaceOptions':{
                'image':'example.invalid/workspace@sha256:'+'a'*64,'home':'/home/pl','port':8080,
                'gradedFiles':['work/**','work/.git/**','work/.*','explanation.m[d]','.pl-seed/identity.jso[n]']}},
            seed={'schema':'pl-git-workspace-seed-v1','archive':'workspace/.pl-seed/starter.zip',
                'archiveSha256':'b'*64,'manifest':'workspace/.pl-seed/manifest.json','manifestSha256':'c'*64},
            upstreamGradingMethod='Manual',singleVariant=True)
    slots=[{'name':'answer','type':kind,'anchor':'answer'}]
    if kind == 'single-choice':
        slots=[dict(name='answer',type=kind,anchor='a'+str(i),labelAnchor='l'+str(i),ordinal=i) for i in range(2)]
    if kind == 'matching':
        slots=[dict(name='match-'+str(i),type=kind,anchor='a'+str(i),labelAnchor='l'+str(i),ordinal=i,
                    options=[dict(anchor='o'+str(j),ordinal=j) for j in range(2)]) for i in range(2)]
    if kind == 'multipart':
        slots=[dict(name='part-'+str(i),type=t,anchor='a'+str(i),labelAnchor='l'+str(i))
               for i,t in enumerate(['manual','numeric'])]
    method='manual' if kind=='manual' else 'mixed' if kind=='multipart' else 'external'
    return dict(kind='native-answer',uuid=UUID,grading={'method':method},answerType=kind,
                responseSlots=slots,upstreamGradingMethod='Manual' if method=='manual' else 'Internal')

def delivery(tmp_path, kind):
    d=fixture(tmp_path);d['schemaVersion']=4;d['gradingPayloads']['demo/exr-a']=binding(kind)
    return d

@pytest.mark.parametrize('kind',['project','workspace','manual','numeric','single-choice','matching','multipart'])
def test_exact_v4_preserved(tmp_path,kind):
    d=delivery(tmp_path,kind);saved=save(tmp_path,d)
    assert load_delivery(tmp_path,'fixture')==saved

def invalid_cases(tmp_path):
    cases=[]
    def add(kind,label,change):
        d=delivery(tmp_path,kind);change(d,d['gradingPayloads']['demo/exr-a']);cases.append((label,d))
    add('manual','downgrade',lambda d,b:d.update(schemaVersion=3))
    add('project','kindless',lambda d,b:b.pop('kind'))
    add('manual','unknown-kind',lambda d,b:b.update(kind='unknown'))
    add('manual','non-rfc-uuid',lambda d,b:b.update(uuid='37d79ee7-fd7d-04db-0d45-6587711862a5'))
    add('manual','fake-project',lambda d,b:b.update(runtime='fake'))
    add('manual','grading-mismatch',lambda d,b:b.update(grading={'method':'external'}))
    add('manual','upstream-mismatch',lambda d,b:b.update(upstreamGradingMethod='Internal'))
    add('numeric','duplicate-anchor',lambda d,b:b['responseSlots'].append(copy.deepcopy(b['responseSlots'][0])))
    add('numeric','empty-anchor',lambda d,b:b['responseSlots'][0].update(anchor=''))
    add('numeric','null-optional',lambda d,b:b['responseSlots'][0].update(ordinal=None))
    add('single-choice','ordinal-bool',lambda d,b:b['responseSlots'][0].update(ordinal=False))
    add('single-choice','missing-label',lambda d,b:b['responseSlots'][0].pop('labelAnchor'))
    add('matching','different-options',lambda d,b:b['responseSlots'][1]['options'].reverse())
    add('matching','duplicate-options',lambda d,b:b['responseSlots'][0]['options'][1].update(anchor='o0'))
    add('multipart','duplicate-name',lambda d,b:b['responseSlots'][1].update(name='part-0'))
    add('workspace','workspace-tag',lambda d,b:b['submission']['workspaceOptions'].update(image='example.invalid/ws:latest'))
    add('workspace','workspace-path',lambda d,b:b.update(sourceRoot='../secret'))
    add('workspace','workspace-port-bool',lambda d,b:b['submission']['workspaceOptions'].update(port=True))
    add('workspace','missing-hidden-files',lambda d,b:b['submission']['workspaceOptions']['gradedFiles'].pop())
    add('workspace','duplicate-files',lambda d,b:b['submission']['workspaceOptions']['gradedFiles'].append('work/**'))
    add('workspace','workspace-singlevariant',lambda d,b:b.update(singleVariant=1))
    add('workspace','workspace-seed',lambda d,b:b['seed'].update(archive='../starter.zip'))
    add('workspace','workspace-null-network',lambda d,b:b['submission']['workspaceOptions'].update(enableNetworking=None))
    add('project','project-extra',lambda d,b:b.update(answerType='manual'))
    add('manual','sourcehash',lambda d,b:d.update(sourceSnapshotHash='bad'))
    add('manual','candidate-null',lambda d,b:d.update(candidate=None))
    add('manual','instance-extra',lambda d,b:d['instances']['fixture'].update(fake=True))
    add('manual','unselected-instance-missing-work',lambda d,b:d['instances'].update(other={'works':['absent'],'selfEnrollment':False}))
    return cases

def test_v4_rejects_invalid_bindings(tmp_path):
    for label,d in invalid_cases(tmp_path):
        save(tmp_path,d)
        with pytest.raises(GatewayError,match='.+'):
            load_delivery(tmp_path,'fixture')

def test_v4_same_uuid_across_kinds_refused(tmp_path):
    d=delivery(tmp_path,'manual');d['questions'].append('demo/exr-b')
    d['gradingPayloads']['demo/exr-b']=binding('workspace');save(tmp_path,d)
    with pytest.raises(GatewayError):load_delivery(tmp_path,'fixture')

def test_v4_hash_and_payload_still_required(tmp_path):
    d=delivery(tmp_path,'numeric');save(tmp_path,d)
    (tmp_path/'public.txt').write_text('CHANGED')
    with pytest.raises(GatewayError):load_delivery(tmp_path,'fixture')
