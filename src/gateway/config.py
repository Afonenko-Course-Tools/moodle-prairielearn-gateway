from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
import json
import re
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .errors import GatewayError
from .store import native_digest

class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

def origin(value):
    parsed=urlsplit(value)
    if parsed.scheme!="https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment: raise ValueError("Fixed HTTPS origin required")
    return parsed.scheme,parsed.netloc

class Activity(Closed):
    kind: Literal["defense","lab"]
    resourceLinkId: str = Field(min_length=1,max_length=256)
    lineitem: str
    workId: str | None = None

class Registration(Closed):
    issuer: str
    clientId: str = Field(min_length=1,max_length=256)
    deploymentId: str = Field(min_length=1,max_length=256)
    contextId: str = Field(min_length=1,max_length=256)
    authUrl: str
    jwksUrl: str
    privateJwksUrl: str | None = None
    tokenUrl: str
    activities: dict[str, Activity]
    @model_validator(mode="after")
    def trusted_urls(self):
        expected=origin(self.issuer)
        if any(origin(v)!=expected for v in (self.authUrl,self.jwksUrl,self.tokenUrl)): raise ValueError("Registration URL origin mismatch")
        if self.privateJwksUrl is not None:
            mapped=urlsplit(self.privateJwksUrl);public=urlsplit(self.jwksUrl)
            if mapped.scheme!="http" or mapped.hostname!="moodle" or mapped.port not in (None,80) or mapped.username or mapped.password or mapped.path!=public.path or mapped.query or mapped.fragment:
                raise ValueError("Only fixed private Moodle JWKS transport mapping is allowed")
        if not self.activities or len({v.resourceLinkId for v in self.activities.values()})!=len(self.activities): raise ValueError("Unique activity bindings required")
        if sum(v.kind=="defense" for v in self.activities.values())>1: raise ValueError("At most one defense lineitem required")
        for activity in self.activities.values():
            if origin(activity.lineitem)!=expected or activity.kind=="lab" and not activity.workId or activity.kind=="defense" and activity.workId: raise ValueError("Fixed AGS activity/work binding required")
        return self

class Flow(Closed):
    mode: Literal["teacher","immediate","approval"]
    capacity: dict[str,int] = Field(default_factory=dict)
    @model_validator(mode="after")
    def limits(self):
        if any(type(v) is not int or v<1 for v in self.capacity.values()): raise ValueError("Positive explicit capacity required")
        return self

class Bridge(Closed):
    url: str
    tokenFile: str
    @model_validator(mode="after")
    def private_url(self):
        u=urlsplit(self.url)
        if u.scheme not in ("http","https") or not u.netloc or u.port!=3001 or u.username or u.password or u.path not in ("","/") or u.query or u.fragment: raise ValueError("Fixed private bridge :3001 origin required")
        return self

class Configuration(Closed):
    publicOrigin: str
    routePrefix: str = Field(pattern=r'^/gateway/courses/[a-z][a-z0-9-]*$')
    database: str
    privateKeyFile: str
    handoffTokenFile: str
    bridge: Bridge
    deliveryPath: str
    instance: str = Field(min_length=1)
    registration: Registration
    flow: Flow
    caFile: str | None = None
    pollSeconds: int = Field(default=15,ge=1,le=3600)
    administratorSubject: str | None = Field(default=None,min_length=1,max_length=512)
    @model_validator(mode="after")
    def paths(self):
        parsed=urlsplit(self.publicOrigin);origin(self.publicOrigin)
        if parsed.path not in ("","/") or parsed.query: raise ValueError("Public origin must not contain a path/query")
        for p in (self.database,self.privateKeyFile,self.handoffTokenFile,self.bridge.tokenFile,self.deliveryPath):
            if not Path(p).is_absolute(): raise ValueError("Absolute private runtime paths required")
        return self

def _delivery_bindings(delivery,questions):
    bindings=delivery['gradingPayloads'];uuids=set()
    if not isinstance(bindings,dict) or set(bindings)!=set(questions):raise GatewayError('Exact canonical question bindings required')
    def relative(value):
        return isinstance(value,str) and bool(value) and not value.startswith('/') and not re.match(r'^[A-Za-z]:',value) and not re.search(r'[\x00-\x1f\x7f]',value) and '\\' not in value and not any(p in ('','.','..') for p in value.split('/'))
    for binding in bindings.values():
        if not isinstance(binding,dict) or not {'uuid','grading','submission','sourceRoot'}<=set(binding):raise GatewayError('Canonical grading/submission binding required')
        grading=binding['grading'];submission=binding['submission']
        if not isinstance(grading,dict) or set(grading)!={'method'} or grading['method'] not in ('external','mixed','manual'):raise GatewayError('Explicit canonical grading method required')
        manual=grading['method']=='manual';fields={'uuid','grading','submission','sourceRoot'}|(set() if manual else {'projectCheck','runtime','sources'})
        if set(binding)!=fields or not relative(binding['sourceRoot']):raise GatewayError('Closed method-specific question binding required')
        if not isinstance(binding['uuid'],str) or not re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}',binding['uuid']) or binding['uuid'] in uuids:raise GatewayError('Unique native UUIDs required')
        uuids.add(binding['uuid'])
        if not isinstance(submission,dict) or not {'mode'}<=set(submission) or set(submission)-{'mode','aceMode'} or submission['mode'] not in ('editor','upload') or submission['mode']=='upload' and 'aceMode' in submission or manual and submission['mode']!='upload':raise GatewayError('Closed native input contract required')
        if 'aceMode' in submission and (not isinstance(submission['aceMode'],str) or not re.fullmatch(r'ace/mode/[a-z][a-z0-9_]*',submission['aceMode'])):raise GatewayError('Invalid editor language')
        if not manual:
            if any(not isinstance(binding[k],str) or not binding[k] for k in ('runtime','projectCheck')) or not isinstance(binding['sources'],list) or not binding['sources']:raise GatewayError('Automatic runtime/source binding required')
            for source in binding['sources']:
                if not isinstance(source,dict) or set(source)!={'projectRelativePath','submissionRelativePath','sha256'} or not relative(source['projectRelativePath']) or not relative(source['submissionRelativePath']) or not isinstance(source['sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',source['sha256']):raise GatewayError('Invalid automatic source binding')

def load_delivery(path, instance):
    root=Path(path).resolve(strict=True)
    delivery=json.loads((root/"delivery.json").read_text())
    required={"schemaVersion","courseId","bookRoot","sourceSnapshotHash","inventoryHash","verificationInventoryHash","questions","gradingPayloads","works","instances","files","deliveryHash"}
    if not isinstance(delivery,dict) or not required<=delivery.keys() or set(delivery)-required-{"candidate"} or type(delivery["schemaVersion"]) is not int or delivery["schemaVersion"]!=3 or native_digest({k:v for k,v in delivery.items() if k!="deliveryHash"})!=delivery["deliveryHash"]: raise GatewayError("Unsupported native delivery v3 identity/schema")
    questions=delivery['questions']
    if not isinstance(questions,list) or not questions or any(not isinstance(q,str) or not re.fullmatch(re.escape(delivery['courseId'])+r'/exr-[a-z0-9][a-z0-9-]*',q) for q in questions) or len(set(questions))!=len(questions): raise GatewayError('Invalid native question catalog')
    _delivery_bindings(delivery,questions)
    for name,sha in delivery["files"].items():
        if not isinstance(name,str) or name.startswith("/") or any(s in ("",".","..") for s in name.split("/")) or not isinstance(sha,str) or not re.fullmatch("[a-f0-9]{64}",sha): raise GatewayError("Unsafe native delivery inventory")
        file=root/name
        if file.resolve(strict=True)!=file.absolute() or not file.is_file() or __import__("hashlib").sha256(file.read_bytes()).hexdigest()!=sha: raise GatewayError("Native delivery file mismatch")
    if instance not in delivery["instances"]: raise GatewayError("Instance absent from delivery")
    works={w["id"]:w for w in delivery["works"]}
    if len(works)!=len(delivery["works"]) or any(w not in works for w in delivery["instances"][instance]["works"]): raise GatewayError("Ambiguous selected works")
    for work in works.values():
        if set(work)-{'id','slots','policy','completion','relatedExercise'} or not {'id','slots','policy','completion'}<=set(work):raise GatewayError('Unsupported v3 work fields')
        slots=work['slots']
        if not isinstance(slots,list) or not slots:raise GatewayError('Required native slots')
        slot_ids=[]; candidates=[]; required_ids=[]
        for slot in slots:
            if not isinstance(slot,dict) or set(slot)!={'id','selection','candidates','assignment'}:raise GatewayError('Closed native slots required')
            prefix=delivery['courseId']+'/'+work['id']+'/'
            if not isinstance(slot['id'],str) or not slot['id'].startswith(prefix) or slot['id'] in slot_ids:raise GatewayError('Invalid native slot identity')
            local=slot['id'][len(prefix):];pool=slot['candidates'];assignment=slot['assignment']
            if not isinstance(pool,list) or not pool or len(set(pool))!=len(pool) or any(q not in questions or q in candidates for q in pool):raise GatewayError('Invalid or repeated native candidate')
            if slot['selection']=='fixed':
                if len(pool)!=1 or local!=pool[0].split('/')[-1]:raise GatewayError('Invalid fixed slot')
            elif slot['selection'] in ('random','choice'):
                if len(pool)<2 or not re.fullmatch(r'alt-[a-z0-9][a-z0-9-]*',local):raise GatewayError('Invalid random slot')
            else:raise GatewayError('Unsupported slot selection')
            if not isinstance(assignment,dict) or not {'requirement','workMode'}<=set(assignment) or set(assignment)-{'requirement','workMode','stage'} or assignment['requirement'] not in ('required','optional') or assignment['workMode'] not in ('individual','pair','group') or 'stage' in assignment and assignment['stage'] not in ('demonstration','classroom','homework'):raise GatewayError('Invalid slot assignment')
            if slot['selection']=='choice' and assignment['requirement']!='required':raise GatewayError('Choice must be required')
            slot_ids.append(slot['id']);candidates.extend(pool)
            if assignment['requirement']=='required':required_ids.append(slot['id'])
        policy=work['completion']
        fields={"schemaVersion","mode","source","slotIds","requiredSlotIds","atLeast","fullyCompletedScore"}
        if set(policy)!=fields or type(policy['schemaVersion']) is not int or policy['schemaVersion']!=2 or policy['mode']!='required-slot-completion' or policy['source']!='per-slot-results-v2' or type(policy['fullyCompletedScore']) is not int or policy['fullyCompletedScore']!=1 or policy['slotIds']!=slot_ids or policy['requiredSlotIds']!=required_ids or type(policy['atLeast']) is not int or not 1<=policy['atLeast']<=len(required_ids):raise GatewayError('Unsupported or unsatisfiable native completion v2')
        if "relatedExercise" in work and not work.get("policy",{}).get("assignment",{}).get("student-label"): raise GatewayError("Defense lacks native assignment ACL")
    return delivery
