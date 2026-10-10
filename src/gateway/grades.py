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

    def _questions(self, policy, evidence):
        ids=policy["questionIds"]; required=policy["requiredQuestionIds"]
        if not isinstance(evidence,list) or len(evidence)!=len(ids): raise GatewayError("Exact scoped questions required",503)
        rows={}
        for row in evidence:
            closed(row,{"qualifiedId","score","points","maxPoints","status"})
            key=row["qualifiedId"]
            if key not in ids or key in rows or row["status"] not in ("complete","grading","unanswered") or row["score"] is not None and (not number(row["score"]) or not 0<=row["score"]<=1): raise GatewayError("Invalid scoped question",503)
            for field in ("points","maxPoints"):
                if row[field] is not None and not number(row[field]): raise GatewayError("Invalid native points",503)
            rows[key]=row
        completed=sum(rows[q]["status"]=="complete" and rows[q]["score"]==1 for q in required)
        return {"completedRequired":completed,"requiredCount":len(required),"atLeast":policy["atLeast"],"passed":completed>=policy["atLeast"]}

    def observe(self, assignment, evidence):
        closed(evidence,{"schemaVersion","source","assignmentId","version","userUid","instance","deliveryHash","workId","currentAttempt","questions","completion"})
        now=int(self.clock())
        with self.store.transaction() as db:
            current=db.execute("SELECT a.*,u.uid,u.subject FROM assignments a JOIN users u ON u.id=a.user_id WHERE a.id=?",(assignment["id"],)).fetchone()
            if not current or current["state"] not in ACTIVE or any(current[k]!=assignment[k] for k in ("version","delivery","work","user_id","context","resource","lineitem")):
                self.store.audit(db,"old-result-ignored",assignment["id"],{"version":assignment["version"]},now)
                return None
            self.assignments._binding(current)
            expected={"schemaVersion":1,"source":"per-question-results-v1","assignmentId":current["id"],"version":current["version"],"userUid":current["uid"],"instance":current["instance"],"deliveryHash":current["delivery"],"workId":current["work"]}
            if any(evidence[k]!=v or type(evidence[k]) is not type(v) for k,v in expected.items()): raise GatewayError("Result identity/version binding rejected",503)
            policy=self.assignments.works[current["work"]]["completion"]
            computed=self._questions(policy,evidence["questions"])
            closed(evidence["completion"],computed)
            if any(evidence["completion"][k]!=v or type(evidence["completion"][k]) is not type(v) for k,v in computed.items()): raise GatewayError("Untrusted aggregate completion mismatch",503)
            attempt=evidence["currentAttempt"]
            if attempt is None: return None
            closed(attempt,{"id","number"})
            if not isinstance(attempt["id"],str) or not attempt["id"] or type(attempt["number"]) is not int or attempt["number"]<1: raise GatewayError("Invalid current attempt",503)
            latest=db.execute("SELECT attempt_number,data FROM observations WHERE assignment_id=? AND version=? ORDER BY attempt_number DESC LIMIT 1",(current["id"],current["version"])).fetchone()
            if latest and attempt["number"]<latest["attempt_number"]:
                self.store.audit(db,"old-attempt-ignored",current["id"],attempt,now);return None
            if latest and attempt["number"]==latest["attempt_number"] and json.loads(latest["data"])["currentAttempt"]["id"]!=attempt["id"]: raise GatewayError("Conflicting current attempt",503)
            if not any(q["status"]=="complete" and q["score"] is not None for q in evidence["questions"]): return None
            stable={**evidence,"questions":sorted(evidence["questions"],key=lambda q:q["qualifiedId"])}
            event_id=digest(stable)
            timestamp=self._timestamp(db,current["user_id"],current["lineitem"],now)
            payload={"userId":current["subject"],"scoreGiven":computed["completedRequired"],"scoreMaximum":computed["requiredCount"],"activityProgress":"Completed","gradingProgress":"FullyGraded","timestamp":timestamp}
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)",(event_id,current["id"],current["version"],attempt["number"],json.dumps(stable,sort_keys=True),now))
            db.execute("INSERT OR IGNORE INTO outbox(id,assignment_id,version,user_id,context,resource,delivery,lineitem,payload,created) VALUES(?,?,?,?,?,?,?,?,?,?)",(event_id,current["id"],current["version"],current["user_id"],current["context"],current["resource"],current["delivery"],current["lineitem"],json.dumps(payload,sort_keys=True),now))
            return payload

    def _lab_id(self,user_id,activity):
        return "lab-"+digest([user_id,activity,self.assignments.delivery["deliveryHash"]])

    def observe_lab(self,binding,evidence):
        fields={"schemaVersion","source","userUid","instance","deliveryHash","workId","currentAttempt","questions","completion","scoreGiven","scoreMaximum"}
        closed(evidence,fields)
        activity=self.activities.get(binding["activity"])
        if not activity or activity["kind"]!="lab": raise GatewayError("Registered lab activity required",503)
        work=self.assignments.works.get(activity["workId"])
        if not work or "relatedExercise" in work: raise GatewayError("Ordinary native lab required",503)
        now=int(self.clock())
        with self.store.transaction() as db:
            current=db.execute("SELECT b.*,u.uid,u.subject FROM activity_bindings b JOIN users u ON u.id=b.user_id WHERE b.user_id=? AND b.activity=?",(binding["user_id"],binding["activity"])).fetchone()
            if not current or current["resource"]!=activity["resourceLinkId"] or current["lineitem"]!=activity["lineitem"] or current["context"]!=self.assignments.flow.get("contextId",current["context"]) or any(current[k]!=binding[k] for k in ("context","resource","lineitem")): raise GatewayError("Lab activity/context binding rejected",503)
            expected={"schemaVersion":1,"source":"per-question-results-v1","userUid":current["uid"],"instance":self.assignments.instance,"deliveryHash":self.assignments.delivery["deliveryHash"],"workId":work["id"]}
            if any(evidence[k]!=v or type(evidence[k]) is not type(v) for k,v in expected.items()): raise GatewayError("Native lab identity/version mismatch",503)
            completion=self._questions(work["completion"],evidence["questions"])
            closed(evidence["completion"],completion)
            if any(evidence["completion"][k]!=v or type(evidence["completion"][k]) is not type(v) for k,v in completion.items()): raise GatewayError("Native lab completion mismatch",503)
            attempt=evidence["currentAttempt"]
            if attempt is None: return None
            closed(attempt,{"id","number"})
            if not isinstance(attempt["id"],str) or not attempt["id"] or type(attempt["number"]) is not int or attempt["number"]<1: raise GatewayError("Invalid native lab attempt",503)
            given,maximum=evidence["scoreGiven"],evidence["scoreMaximum"]
            if given is None or maximum is None: return None
            if not number(given) or not number(maximum) or not 0<=given<=maximum or maximum<=0: raise GatewayError("Invalid declared native lab score",503)
            if not any(q["status"]=="complete" and q["score"] is not None for q in evidence["questions"]): return None
            identifier=self._lab_id(current["user_id"],current["activity"])
            latest=db.execute("SELECT attempt_number,data FROM observations WHERE assignment_id=? ORDER BY attempt_number DESC LIMIT 1",(identifier,)).fetchone()
            if latest and attempt["number"]<latest["attempt_number"]: return None
            if latest and attempt["number"]==latest["attempt_number"] and json.loads(latest["data"])["currentAttempt"]["id"]!=attempt["id"]: raise GatewayError("Conflicting native lab attempt",503)
            stable={**evidence,"questions":sorted(evidence["questions"],key=lambda q:q["qualifiedId"])}
            event_id=digest({"activity":current["activity"],**stable})
            payload={"userId":current["subject"],"scoreGiven":given,"scoreMaximum":maximum,"activityProgress":"Completed","gradingProgress":"FullyGraded","timestamp":self._timestamp(db,current["user_id"],current["lineitem"],now)}
            db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)",(event_id,identifier,1,attempt["number"],json.dumps(stable,sort_keys=True),now))
            db.execute("INSERT OR IGNORE INTO outbox(id,assignment_id,version,user_id,context,resource,delivery,lineitem,payload,created) VALUES(?,?,?,?,?,?,?,?,?,?)",(event_id,identifier,1,current["user_id"],current["context"],current["resource"],self.assignments.delivery["deliveryHash"],current["lineitem"],json.dumps(payload,sort_keys=True),now))
            return payload

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
                if row["state"] not in ("pending","sending") or row["state"]=="sending" and row["lease_until"]>now: continue
                current=db.execute("SELECT * FROM assignments WHERE id=?",(row["assignment_id"],)).fetchone()
                valid=bool(current and current["state"] in ACTIVE and all(current[k]==row[k] for k in ("version","user_id","context","resource","delivery","lineitem"))) or self._current_lab(row,db)
                if current:
                    try:self.assignments._binding(current)
                    except GatewayError:valid=False
                latest=db.execute("SELECT event_id FROM observations WHERE assignment_id=? AND version=? ORDER BY attempt_number DESC,rowid DESC LIMIT 1",(row["assignment_id"],row["version"])).fetchone()
                valid=valid and latest and latest["event_id"]==event_id
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
                if threshold is not None and payload["scoreGiven"]>=threshold:
                    db.execute("UPDATE assignments SET state='completed' WHERE id=? AND version=? AND state IN ('assigned','approved','completed')",(row["assignment_id"],row["version"]))
                self.store.audit(db,"ags-score-sent",event_id,{"assignment":row["assignment_id"],"version":row["version"]},now)

    async def poll(self):
        with self.store.connect() as db: rows=[dict(r) for r in db.execute("SELECT * FROM assignments WHERE state IN ('assigned','approved','completed') AND instance=? AND delivery=?",(self.assignments.instance,self.assignments.delivery["deliveryHash"]))]
        for row in rows:
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
