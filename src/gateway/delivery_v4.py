"""Закрытые bindings v5, согласованные с Platform bridge/delivery.mjs."""
import re
from .errors import GatewayError

def require(condition):
    if not condition:
        raise GatewayError('Invalid native delivery v5 contract')

def closed(value, required, optional=()):
    require(isinstance(value,dict) and set(required)<=set(value) and not set(value)-set(required)-set(optional))

def text(value):
    return isinstance(value,str) and bool(value)

def relative(value):
    return text(value) and not value.startswith('/') and not re.match(r'^[A-Za-z]:',value) and not re.search(r'[\x00-\x1f\x7f]',value) and '\\' not in value and not any(p in ('','.','..') for p in value.split('/'))

def ids(value):
    return isinstance(value,list) and bool(value) and all(text(v) for v in value) and len(set(value))==len(value)

def validate_base(d):
    require(isinstance(d['courseId'],str) and re.fullmatch(r'[a-z][a-z0-9-]*',d['courseId']) is not None)
    require(isinstance(d['bookRoot'],str))
    require('candidate' not in d or type(d['candidate']) is bool)
    require(isinstance(d['files'],list) and len(set(d['files']))==len(d['files']) and all(relative(f) for f in d['files']) and isinstance(d['instances'],dict) and isinstance(d['works'],list) and bool(d['works']))
    work_ids=[]
    for work in d['works']:
        closed(work,{'id','slots','policy','completion'},{'relatedExercise'})
        require(isinstance(work['id'],str) and re.fullmatch(r'[a-z][a-z0-9-]*',work['id']) is not None and isinstance(work['policy'],dict))
        require('relatedExercise' not in work or isinstance(work['relatedExercise'],str))
        require(isinstance(work['slots'],list) and bool(work['slots']))
        for slot in work['slots']:
            closed(slot,{'id','selection','candidates','assignment'})
            require(ids(slot['candidates']))
        closed(work['completion'],{'schemaVersion','mode','source','slotIds','requiredSlotIds','atLeast','fullyCompletedScore'})
        require(ids(work['completion']['slotIds']) and ids(work['completion']['requiredSlotIds']))
        work_ids.append(work['id'])
    require(len(set(work_ids))==len(work_ids))
    for instance in d['instances'].values():
        closed(instance,{'works','selfEnrollment'})
        require(ids(instance['works']) and type(instance['selfEnrollment']) is bool and all(w in work_ids for w in instance['works']))

def native_answer(b):
    closed(b,{'kind','uuid','grading','answerType','responseSlots','upstreamGradingMethod'})
    slots=b['responseSlots'];kind=b['answerType']
    require(isinstance(slots,list) and 1<=len(slots)<=4096)
    for slot in slots:
        closed(slot,{'name','type','anchor'},{'labelAnchor','ordinal','options'})
        require(all(text(slot[k]) for k in ('name','anchor')) and slot['type'] in ('manual','numeric','single-choice','matching'))
        require('labelAnchor' not in slot or text(slot['labelAnchor']))
        require('ordinal' not in slot or type(slot['ordinal']) is int and slot['ordinal']>=0)
        if 'options' in slot:
            require(isinstance(slot['options'],list) and bool(slot['options']))
            for option in slot['options']:
                closed(option,{'anchor','ordinal'})
                require(text(option['anchor']) and type(option['ordinal']) is int and option['ordinal']>=0)
    require(len({s['anchor'] for s in slots})==len(slots))
    simple=lambda t:len(slots)==1 and slots[0]['name']=='answer' and slots[0]['type']==t and set(slots[0])=={'anchor','name','type'}
    if kind in ('manual','numeric'):
        require(simple(kind))
    elif kind=='single-choice':
        require(len(slots)>=2 and all(set(s)=={'anchor','labelAnchor','name','ordinal','type'} and s['name']=='answer' and s['type']==kind and s['ordinal']==i for i,s in enumerate(slots)))
    elif kind=='matching':
        require(all(set(s)=={'anchor','labelAnchor','name','options','ordinal','type'} and s['name']=='match-'+str(i) and s['type']==kind and s['ordinal']==i and s['options']==slots[0]['options'] and all(o['ordinal']==j for j,o in enumerate(s['options'])) and len({o['anchor'] for o in s['options']})==len(s['options']) for i,s in enumerate(slots)))
    elif kind=='multipart':
        require(len({s['name'] for s in slots})==len(slots) and all(set(s)=={'anchor','labelAnchor','name','type'} and s['type'] in ('manual','numeric') for s in slots))
    else:
        require(False)
    types={s['type'] for s in slots}
    method=('manual' if len(types)==1 else 'mixed') if 'manual' in types else 'external'
    require(b['grading']['method']==method and b['upstreamGradingMethod']==('Manual' if method=='manual' else 'Internal'))

def workspace(b):
    closed(b,{'kind','uuid','grading','sourceRoot','submission','seed','upstreamGradingMethod','singleVariant'})
    require(b['grading']['method']=='manual' and relative(b['sourceRoot']) and b['upstreamGradingMethod']=='Manual' and b['singleVariant'] is True)
    closed(b['submission'],{'mode','workspaceOptions'})
    require(b['submission']['mode']=='workspace')
    options=b['submission']['workspaceOptions']
    closed(options,{'image','home','port','gradedFiles'},{'rewriteUrl','enableNetworking'})
    require(isinstance(options['image'],str) and re.fullmatch(r'[^\s]+@sha256:[a-f0-9]{64}',options['image']) is not None)
    require(isinstance(options['home'],str) and re.fullmatch(r'/(?:[a-zA-Z0-9_-]+/?)+',options['home']) is not None)
    require(type(options['port']) is int and 1024<=options['port']<=65535)
    files=options['gradedFiles']
    require(ids(files) and len(files)<=128 and all(relative(f) for f in files) and all(f in files for f in ('work/**','work/.git/**','work/.*','explanation.m[d]','.pl-seed/identity.jso[n]')))
    require(all(k not in options or type(options[k]) is bool for k in ('rewriteUrl','enableNetworking')))
    seed=b['seed']
    closed(seed,{'schema','archive','manifest'})
    require(seed['schema']=='pl-git-workspace-seed-v1' and seed['archive']=='workspace/.pl-seed/starter.zip' and seed['manifest']=='workspace/.pl-seed/manifest.json')

def validate_bindings(d, questions, project_validator):
    bindings=d['gradingPayloads']
    require(isinstance(bindings,dict) and set(bindings)==set(questions))
    uuids=set()
    for q,b in bindings.items():
        require(isinstance(b,dict) and {'kind','uuid','grading'}<=set(b))
        require(isinstance(b['uuid'],str) and re.fullmatch(r'(?:[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[1-8][a-fA-F0-9]{3}-[89abAB][a-fA-F0-9]{3}-[a-fA-F0-9]{12}|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)',b['uuid']) is not None and b['uuid'] not in uuids)
        uuids.add(b['uuid'])
        closed(b['grading'],{'method'})
        require(b['grading']['method'] in ('manual','mixed','external'))
        if b['kind']=='project':
            project_validator({'gradingPayloads':{q:{k:v for k,v in b.items() if k!='kind'}}},[q])
        elif b['kind']=='native-answer':
            native_answer(b)
        elif b['kind']=='workspace':
            workspace(b)
        else:
            require(False)
