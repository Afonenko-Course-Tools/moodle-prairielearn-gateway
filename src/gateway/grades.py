import datetime
import json
import math
import time
from .assignments import ACTIVE
from .errors import GatewayError
from .store import digest

def closed(value, fields):
    if not isinstance(value,dict) or set(value)!=set(fields): raise GatewayError("Closed result fields required",503)

def number(value):
    return type(value) in (int,float) and math.isfinite(value)

class Grades:
    def __init__(self, store, assignments, ags, clock=time.time, activities=None):
        self.store, self.assignments, self.ags, self.clock = store, assignments, ags, clock
        self.activities=activities or {}

    def _timestamp(self,db,user_id,lineitem,now):
        # Moodle compares timestamps. Persist a distinct time even when two native
        # revisions are observed during the same polling second.
        current=datetime.datetime.fromtimestamp(now,datetime.UTC)
        prior=db.execute("SELECT payload FROM outbox WHERE user_id=? AND lineitem=? ORDER BY rowid DESC LIMIT 1",(user_id,lineitem)).fetchone()
        if prior:
            previous=datetime.datetime.fromisoformat(json.loads(prior["payload"])["timestamp"])
            current=max(current,previous+datetime.timedelta(microseconds=1))
        return current.isoformat(timespec="microseconds")

    def _record(self,db,stable,latest,identifier,version,binding,given,maximum,now):
        if latest and json.loads(latest["data"])==stable:
            # Consecutive identical polls retry the committed event, including its
            # original timestamp. A later return to old content is a new revision.
            prior=db.execute("SELECT payload FROM outbox WHERE id=?",(latest["event_id"],)).fetchone()
            if prior:return json.loads(prior["payload"])
        event_id=digest({"scope":[identifier,version],"evidence":stable,"previousEvent":latest["event_id"] if latest else None})
        payload={"userId":binding["subject"],"scoreGiven":given,"scoreMaximum":maximum,"activityProgress":"Completed","gradingProgress":"FullyGraded","timestamp":self._timestamp(db,binding["user_id"],binding["lineitem"],now)}
        db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?)",(event_id,identifier,version,stable["currentAttempt"]["number"],json.dumps(stable,sort_keys=True),now))
        db.execute("INSERT INTO outbox(id,assignment_id,version,user_id,context,resource,delivery,lineitem,payload,created) VALUES(?,?,?,?,?,?,?,?,?,?)",(event_id,identifier,version,binding["user_id"],binding["context"],binding["resource"],self.assignments.delivery["deliveryHash"],binding["lineitem"],json.dumps(payload,sort_keys=True),now))
        return payload

    def _slots(self, work, evidence, *, not_started=False):
        policy=work['completion'];ids=policy['slotIds'];required=policy['requiredSlotIds']
        if not isinstance(evidence,list) or len(evidence)!=(0 if not_started else len(ids)):raise GatewayError('Exact assigned slots required',503)
        rows={};chosen=set()
        for row in evidence:
            closed(row,{'slotId','qualifiedId','score','points','maxPoints','status'})
            key=row['slotId'];slot=next((s for s in work['slots'] if s['id']==key),None)
            if not slot or key in rows or row['qualifiedId'] not in slot['candidates'] or row['qualifiedId'] in chosen or row['status'] not in ('complete','grading','unanswered') or row['score'] is not None and (not number(row['score']) or not 0<=row['score']<=1):raise GatewayError('Invalid selected slot question',503)
            for field in ('points','maxPoints'):
                if row[field] is not None and (not number(row[field]) or row[field]<0):raise GatewayError('Invalid native points',503)
            if row['points'] is not None and row['maxPoints'] is not None and row['points']>row['maxPoints']:raise GatewayError('Native points exceed maximum',503)
            if row['status']=='complete' and row['score']==1 and (row['points'] is None or row['maxPoints'] is None or row['maxPoints']<=0 or row['points']!=row['maxPoints']):raise GatewayError('Incomplete full native result',503)
            rows[key]=row;chosen.add(row['qualifiedId'])
        completed=sum(rows.get(q,{}).get('status')=='complete' and rows[q]['score']==1 for q in required if q in rows)
        return {'completedRequired':completed,'requiredCount':len(required),'atLeast':policy['atLeast'],'passed':completed>=policy['atLeast']}

    def _state(self,evidence):
        if evidence['state'] not in ('started','not-started') or (evidence['state']=='not-started')!=(evidence['currentAttempt'] is None):raise GatewayError('Native state/attempt mismatch',503)
        return evidence['state']=='not-started'

    def _bind_attempt(self,db,identifier,version,evidence):
        attempt=evidence['currentAttempt']
        # Choice binds its canonical assignment pool, not the changing best result.
        work=self.assignments.works[evidence['workId']]
        slots={slot['id']:slot for slot in work['slots']}
        selection=json.dumps(sorted((s['slotId'],{'choice':sorted(slots[s['slotId']]['candidates'])} if slots[s['slotId']]['selection']=='choice' else s['qualifiedId']) for s in evidence['slots']),sort_keys=True)
        rows=db.execute('SELECT * FROM native_attempts WHERE scope=? AND version=? ORDER BY attempt_number DESC',(identifier,version)).fetchall()
        prior=next((row for row in rows if row['attempt_number']==attempt['number'] or row['attempt_id']==attempt['id']),None)
        if prior:
            if prior['attempt_id']!=attempt['id'] or prior['attempt_number']!=attempt['number']:raise GatewayError('Conflicting current attempt',503)
            if prior['selection']!=selection:raise GatewayError('Selected question changed within native attempt',503)
            if any(prior[key]!=evidence[field] for key,field in [('instance','instance'),('delivery','deliveryHash'),('work','workId')]):raise GatewayError('Conflicting native attempt scope',503)
        if rows and attempt['number']<rows[0]['attempt_number']:return False
        if not prior:
            db.execute('INSERT INTO native_attempts VALUES(?,?,?,?,?,?,?,?)',(identifier,version,evidence['instance'],evidence['deliveryHash'],evidence['workId'],attempt['id'],attempt['number'],selection))
        return True

    def _catalog_binding(self, row):
        return any(a["kind"]=="catalog" and a["resourceLinkId"]==row["resource"] and a["lineitem"]==row["lineitem"] for a in self.activities.values())

    def observe(self, assignment, evidence):
        if self._catalog_binding(assignment): return None
        closed(evidence,{"schemaVersion","source","assignmentId","version","userUid","instance","deliveryHash","workId","currentAttempt","slots","completion","state"})
        now=int(self.clock())
        with self.store.transaction() as db:
            current=db.execute("SELECT a.*,u.uid,u.subject FROM assignments a JOIN users u ON u.id=a.user_id WHERE a.id=?",(assignment["id"],)).fetchone()
            if not current or current["state"] not in ACTIVE or current["delivery"]!=self.assignments.delivery["deliveryHash"] or current["instance"]!=self.assignments.instance or any(current[k]!=assignment[k] for k in ("version","delivery","work","user_id","context","resource","lineitem")):
                self.store.audit(db,"old-result-ignored",assignment["id"],{"version":assignment["version"]},now)
                return None
            self.assignments._binding(current)
            expected={"schemaVersion":2,"source":"per-slot-results-v2","assignmentId":current["id"],"version":current["version"],"userUid":current["uid"],"instance":current["instance"],"deliveryHash":current["delivery"],"workId":current["work"]}
            if any(evidence[k]!=v or type(evidence[k]) is not type(v) for k,v in expected.items()): raise GatewayError("Result identity/version binding rejected",503)
            work=self.assignments.works[current["work"]]
            computed=self._slots(work,evidence["slots"],not_started=self._state(evidence))
            closed(evidence["completion"],computed)
            if any(evidence["completion"][k]!=v or type(evidence["completion"][k]) is not type(v) for k,v in computed.items()): raise GatewayError("Untrusted aggregate completion mismatch",503)
            attempt=evidence["currentAttempt"]
            if attempt is None: return None
            closed(attempt,{"id","number"})
            if not isinstance(attempt["id"],str) or not attempt["id"] or type(attempt["number"]) is not int or attempt["number"]<1: raise GatewayError("Invalid current attempt",503)
            if not self._bind_attempt(db,current["id"],current["version"],evidence):return None
            latest=db.execute("SELECT event_id,attempt_number,data FROM observations WHERE assignment_id=? AND version=? ORDER BY attempt_number DESC,rowid DESC LIMIT 1",(current["id"],current["version"])).fetchone()
            if latest and attempt["number"]<latest["attempt_number"]:
                self.store.audit(db,"old-attempt-ignored",current["id"],attempt,now);return None
            if any(q["status"]=="grading" for q in evidence["slots"]): return None
            if not any(q["status"]=="complete" and q["score"] is not None for q in evidence["slots"]): return None
            stable={**evidence,"slots":sorted(evidence["slots"],key=lambda q:q["slotId"])}
            return self._record(db,stable,latest,current["id"],current["version"],current,computed["completedRequired"],computed["requiredCount"],now)

    def _lab_id(self,user_id,activity):
        return "lab-"+digest([user_id,activity,self.assignments.delivery["deliveryHash"]])

    def observe_lab(self,binding,evidence):
        fields={"schemaVersion","source","userUid","instance","deliveryHash","workId","currentAttempt","slots","completion","state","scoreGiven","scoreMaximum"}
        closed(evidence,fields)
        activity=self.activities.get(binding["activity"])
        if not activity or activity["kind"]!="lab": raise GatewayError("Registered lab activity required",503)
        work=self.assignments.works.get(activity["workId"])
        if not work or "relatedExercise" in work: raise GatewayError("Ordinary native lab required",503)
        now=int(self.clock())
        with self.store.transaction() as db:
            current=db.execute("SELECT b.*,u.uid,u.subject FROM activity_bindings b JOIN users u ON u.id=b.user_id WHERE b.user_id=? AND b.activity=?",(binding["user_id"],binding["activity"])).fetchone()
            if not current or current["resource"]!=activity["resourceLinkId"] or current["lineitem"]!=activity["lineitem"] or current["context"]!=self.assignments.flow.get("contextId",current["context"]) or any(current[k]!=binding[k] for k in ("context","resource","lineitem")): raise GatewayError("Lab activity/context binding rejected",503)
            expected={"schemaVersion":2,"source":"per-slot-results-v2","userUid":current["uid"],"instance":self.assignments.instance,"deliveryHash":self.assignments.delivery["deliveryHash"],"workId":work["id"]}
            if any(evidence[k]!=v or type(evidence[k]) is not type(v) for k,v in expected.items()): raise GatewayError("Native lab identity/version mismatch",503)
            completion=self._slots(work,evidence["slots"],not_started=self._state(evidence))
            closed(evidence["completion"],completion)
            if any(evidence["completion"][k]!=v or type(evidence["completion"][k]) is not type(v) for k,v in completion.items()): raise GatewayError("Native lab completion mismatch",503)
            attempt=evidence["currentAttempt"]
            if attempt is None: return None
            closed(attempt,{"id","number"})
            if not isinstance(attempt["id"],str) or not attempt["id"] or type(attempt["number"]) is not int or attempt["number"]<1: raise GatewayError("Invalid native lab attempt",503)
            identifier=self._lab_id(current["user_id"],current["activity"])
            given,maximum=evidence["scoreGiven"],evidence["scoreMaximum"]
            if given is not None and maximum is not None and (not number(given) or not number(maximum) or not 0<=given<=maximum or maximum<=0):raise GatewayError("Invalid declared native lab score",503)
            if not self._bind_attempt(db,identifier,1,evidence):return None
            if given is None or maximum is None: return None
            if not number(given) or not number(maximum) or not 0<=given<=maximum or maximum<=0: raise GatewayError("Invalid declared native lab score",503)
            if any(q["status"]=="grading" for q in evidence["slots"]): return None
            if not any(q["status"]=="complete" and q["score"] is not None for q in evidence["slots"]): return None
            latest=db.execute("SELECT event_id,attempt_number,data FROM observations WHERE assignment_id=? ORDER BY attempt_number DESC,rowid DESC LIMIT 1",(identifier,)).fetchone()
            if latest and attempt["number"]<latest["attempt_number"]: return None
            stable={**evidence,"slots":sorted(evidence["slots"],key=lambda q:q["slotId"])}
            return self._record(db,stable,latest,identifier,1,current,given,maximum,now)

    def _current_lab(self,row,db):
        for activity,config in self.activities.items():
            if config["kind"]=="lab" and self._lab_id(row["user_id"],activity)==row["assignment_id"] and row["delivery"]==self.assignments.delivery["deliveryHash"] and row["version"]==1:
                binding=db.execute("SELECT * FROM activity_bindings WHERE user_id=? AND activity=?",(row["user_id"],activity)).fetchone()
                return bool(binding and binding["context"]==self.assignments.flow.get("contextId",binding["context"]) and binding["resource"]==config["resourceLinkId"] and binding["lineitem"]==config["lineitem"] and all(binding[k]==row[k] for k in ("context","resource","lineitem")))
        return False

    async def drain(self):
        now=int(self.clock())
        with self.store.connect() as db:
            ids=[r[0] for r in db.execute("SELECT id FROM outbox WHERE (state='pending' AND next_attempt<=?) OR (state='sending' AND lease_until<=?) ORDER BY created,id",(now,now))]
        for event_id in ids:
            with self.store.transaction() as db:
                row=db.execute("SELECT * FROM outbox WHERE id=?",(event_id,)).fetchone()
                if self._catalog_binding(row): continue
                if row["state"] not in ("pending","sending") or row["state"]=="sending" and row["lease_until"]>now: continue
                current=db.execute("SELECT * FROM assignments WHERE id=?",(row["assignment_id"],)).fetchone()
                valid=bool(current and current["state"] in ACTIVE and current["instance"]==self.assignments.instance and current["delivery"]==self.assignments.delivery["deliveryHash"] and all(current[k]==row[k] for k in ("version","user_id","context","resource","delivery","lineitem"))) or self._current_lab(row,db)
                if current:
                    try:self.assignments._binding(current)
                    except GatewayError:valid=False
                latest=db.execute("SELECT event_id,attempt_number,data FROM observations WHERE assignment_id=? AND version=? ORDER BY attempt_number DESC,rowid DESC LIMIT 1",(row["assignment_id"],row["version"])).fetchone()
                valid=valid and row["delivery"]==self.assignments.delivery["deliveryHash"] and latest and latest["event_id"]==event_id and json.loads(latest["data"])["instance"]==self.assignments.instance
                bound=db.execute('SELECT attempt_number FROM native_attempts WHERE scope=? AND version=? ORDER BY attempt_number DESC LIMIT 1',(row['assignment_id'],row['version'])).fetchone()
                valid=valid and bound and latest['attempt_number']==bound['attempt_number']
                if not valid:
                    db.execute("UPDATE outbox SET state='obsolete' WHERE id=?",(event_id,));self.store.audit(db,"old-outbox-ignored",event_id,{},now);continue
                if db.execute("SELECT 1 FROM outbox WHERE id!=? AND user_id=? AND lineitem=? AND state='sending' AND lease_until>?",(event_id,row["user_id"],row["lineitem"],now)).fetchone(): continue
                db.execute("UPDATE outbox SET state='sending',tries=tries+1,lease_until=? WHERE id=?",(now+60,event_id))
                payload=json.loads(row["payload"])
            try:
                await self.ags.score(row["lineitem"],payload,event_id)
            except GatewayError:
                with self.store.transaction() as db:
                    db.execute("UPDATE outbox SET state='pending',next_attempt=?,lease_until=0 WHERE id=?",(now+min(300,2**min(row["tries"]+1,8)),event_id))
                continue
            with self.store.transaction() as db:
                db.execute("UPDATE outbox SET state='sent',lease_until=0 WHERE id=?",(event_id,))
                assignment=db.execute("SELECT * FROM assignments WHERE id=?",(row["assignment_id"],)).fetchone()
                threshold=self.assignments.works[assignment["work"]]["completion"]["atLeast"] if assignment else None
                if threshold is not None:
                    state="completed" if payload["scoreGiven"]>=threshold else assignment["desired_state"]
                    db.execute("UPDATE assignments SET state=? WHERE id=? AND version=? AND state IN ('assigned','approved','completed')",(state,row["assignment_id"],row["version"]))
                self.store.audit(db,"ags-score-sent",event_id,{"assignment":row["assignment_id"],"version":row["version"]},now)

    async def poll(self):
        with self.store.connect() as db: rows=[dict(r) for r in db.execute("SELECT * FROM assignments WHERE state IN ('assigned','approved','completed') AND instance=? AND delivery=?",(self.assignments.instance,self.assignments.delivery["deliveryHash"]))]
        for row in rows:
            if self._catalog_binding(row): continue
            try: self.observe(row,await self.assignments.bridge.results(row["id"],row["version"]))
            except GatewayError:
                with self.store.transaction() as db: self.store.audit(db,"result-read-failed",row["id"],{"version":row["version"]},int(self.clock()))
        with self.store.connect() as db: bindings=[dict(r) for r in db.execute("SELECT b.*,u.uid,u.subject FROM activity_bindings b JOIN users u ON u.id=b.user_id")]
        for binding in bindings:
            activity=self.activities.get(binding["activity"])
            if not activity or activity["kind"]!="lab": continue
            try: self.observe_lab(binding,await self.assignments.bridge.work_results(binding["uid"],self.assignments.instance,self.assignments.delivery["deliveryHash"],activity["workId"]))
            except GatewayError:
                with self.store.transaction() as db:self.store.audit(db,"lab-read-failed",binding["user_id"],{"activity":binding["activity"]},int(self.clock()))
        await self.drain()
