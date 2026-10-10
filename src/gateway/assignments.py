import secrets
import time
from .errors import GatewayError
from .store import digest

ACTIVE = {"assigned", "approved", "completed"}

class Assignments:
    def __init__(self, store, bridge, delivery, instance, flow, clock=time.time):
        self.store, self.bridge, self.delivery, self.instance, self.flow, self.clock = store, bridge, delivery, instance, flow, clock
        ids = delivery["instances"][instance]["works"]
        self.works = {w["id"]:w for w in delivery["works"] if w["id"] in ids}
        self.essays = {}
        for work in self.works.values():
            if "relatedExercise" in work:
                if work["relatedExercise"] in self.essays: raise GatewayError("Ambiguous active essay defense")
                self.essays[work["relatedExercise"]] = work

    def current(self, session):
        with self.store.connect() as db:
            row=db.execute("SELECT * FROM assignments WHERE user_id=? AND instance=?",(session["userId"],self.instance)).fetchone()
        return dict(row) if row else None

    def _teacher(self, session):
        if session["role"] != "teacher": raise GatewayError("Teacher operation required",403)
        self._binding(session)

    def _binding(self, session):
        if self.flow.get("contextId", session["context"]) != session["context"] or self.flow.get("resourceLinkId",session["resource"]) != session["resource"] or self.flow.get("lineitem",session["lineitem"])!=session["lineitem"]:
            raise GatewayError("Assignment activity/context mismatch",403)

    def _get(self, assignment_id):
        with self.store.connect() as db: row=db.execute("SELECT * FROM assignments WHERE id=? AND instance=?",(assignment_id,self.instance)).fetchone()
        if not row: raise GatewayError("Assignment missing",404)
        if row["delivery"] != self.delivery["deliveryHash"]: raise GatewayError("Explicit delivery migration required")
        self._binding(row)
        return dict(row)

    def _reserve(self, session, essay, key, desired, replacing=False):
        self._binding(session)
        work=self.essays.get(essay)
        if not work: raise GatewayError("Essay has no ready exported defense",404)
        if not isinstance(key,str) or not 1<=len(key)<=128: raise GatewayError("Idempotency key required",400)
        body_hash=digest({"essay":essay,"desired":desired,"replacing":replacing})
        now=int(self.clock())
        with self.store.transaction() as db:
            prior=db.execute("SELECT * FROM requests WHERE user_id=? AND idempotency=?",(session["userId"],key)).fetchone()
            if prior:
                if prior["body_hash"] != body_hash: raise GatewayError("Idempotency key conflict")
                return dict(db.execute("SELECT * FROM assignments WHERE id=?",(prior["assignment_id"],)).fetchone())
            current=db.execute("SELECT * FROM assignments WHERE user_id=? AND instance=?",(session["userId"],self.instance)).fetchone()
            if current and current["delivery"] != self.delivery["deliveryHash"]: raise GatewayError("Explicit delivery migration required")
            if current and any(current[k]!=session[k] for k in ("context","resource","lineitem")): raise GatewayError("Assignment activity mapping is immutable")
            if current and db.execute("SELECT 1 FROM outbox WHERE assignment_id=? AND state='sending' AND lease_until>?",(current["id"],now)).fetchone():
                raise GatewayError("Grade delivery in progress; retry replacement")
            if current and (current["state"] in ("pending","reconciling") or current["state"] in ACTIVE and not replacing):
                raise GatewayError("Current request/assignment already exists")
            if replacing and (not current or current["state"] not in ACTIVE): raise GatewayError("Active assignment required for replacement")
            capacity=self.flow.get("capacity",{}).get(essay)
            if capacity is not None:
                occupied=db.execute("SELECT count(*) FROM assignments WHERE instance=? AND essay=? AND user_id!=? AND state IN ('pending','reconciling','assigned','approved','completed')",(self.instance,essay,session["userId"])).fetchone()[0]
                if occupied>=capacity: raise GatewayError("Essay capacity exhausted")
            state="pending" if desired=="pending" else "reconciling"
            assignment_id=current["id"] if current else secrets.token_urlsafe(24)
            version=(current["version"] if current else 0)+(state=="reconciling")
            old=current["work"] if current and current["state"] in ACTIVE else None
            if current:
                db.execute("UPDATE requests SET state='replaced' WHERE assignment_id=? AND state IN ('assigned','approved','completed')",(assignment_id,))
                db.execute("UPDATE assignments SET version=?,old_work=?,work=?,essay=?,state=?,desired_state=?,context=?,resource=?,lineitem=?,updated=? WHERE id=?",(version,old,work["id"],essay,state,desired,session["context"],session["resource"],session["lineitem"],now,assignment_id))
            else:
                db.execute("INSERT INTO assignments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(assignment_id,session["userId"],self.instance,self.delivery["deliveryHash"],version,work["id"],essay,old,state,desired,session["context"],session["resource"],session["lineitem"],now))
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)",(secrets.token_urlsafe(24),assignment_id,session["userId"],key,body_hash,work["id"],essay,state,now))
            self.store.audit(db,"assignment-reserved",assignment_id,{"version":version,"work":work["id"],"state":state},now)
            return dict(db.execute("SELECT * FROM assignments WHERE id=?",(assignment_id,)).fetchone())

    async def request(self, session, essay, key):
        if session["role"] != "student": raise GatewayError("Student request required",403)
        if self.flow["mode"]=="teacher": raise GatewayError("Teacher assignment mode",403)
        row=self._reserve(session,essay,key,"pending" if self.flow["mode"]=="approval" else "assigned")
        return await self.reconcile(row["id"]) if row["state"]=="reconciling" else row

    def _student(self, user_id):
        with self.store.connect() as db:
            row=db.execute("SELECT s.*,u.id AS userId,u.uid FROM sessions s JOIN users u ON u.id=s.user_id WHERE user_id=? AND role='student' AND expires>? ORDER BY expires DESC",(user_id,int(self.clock()))).fetchall()
        rows=[dict(r) for r in row if self.flow.get("resourceLinkId",r["resource"])==r["resource"] and self.flow.get("contextId",r["context"])==r["context"]]
        if not rows: raise GatewayError("Verified enrolled student launch required",403)
        return rows[0]

    async def assign(self, teacher, user_id, essay, key):
        self._teacher(teacher)
        row=self._reserve(self._student(user_id),essay,key,"assigned")
        return await self.reconcile(row["id"])

    async def approve(self, teacher, assignment_id):
        self._teacher(teacher)
        self._get(assignment_id)
        with self.store.transaction() as db:
            row=db.execute("SELECT * FROM assignments WHERE id=? AND instance=?",(assignment_id,self.instance)).fetchone()
            if not row: raise GatewayError("Assignment missing",404)
            if row["state"] in ("approved","completed"): return dict(row)
            if row["state"] != "pending": raise GatewayError("Pending request required")
            db.execute("UPDATE assignments SET version=version+1,state='reconciling',desired_state='approved',updated=? WHERE id=?",(int(self.clock()),assignment_id))
            db.execute("UPDATE requests SET state='reconciling' WHERE assignment_id=? AND state='pending'",(assignment_id,))
            self.store.audit(db,"request-approved",assignment_id,{"teacher":teacher["userId"]},int(self.clock()))
        return await self.reconcile(assignment_id)

    async def reject(self, teacher, assignment_id):
        self._teacher(teacher)
        self._get(assignment_id)
        with self.store.transaction() as db:
            row=db.execute("SELECT * FROM assignments WHERE id=? AND instance=?",(assignment_id,self.instance)).fetchone()
            if not row or row["state"] not in ("pending","rejected"): raise GatewayError("Pending request required")
            db.execute("UPDATE assignments SET state='rejected',desired_state='rejected',updated=? WHERE id=?",(int(self.clock()),assignment_id))
            db.execute("UPDATE requests SET state='rejected' WHERE assignment_id=? AND state='pending'",(assignment_id,))
            self.store.audit(db,"request-rejected",assignment_id,{"teacher":teacher["userId"]},int(self.clock()))
        return self._get(assignment_id)

    async def reassign(self, teacher, assignment_id, essay, key):
        self._teacher(teacher)
        row=self._get(assignment_id)
        selected=self._student(row["user_id"])
        row=self._reserve(selected,essay,key,"assigned",replacing=True)
        return await self.reconcile(row["id"])

    async def reconcile(self, assignment_id):
        row=self._get(assignment_id)
        if row["state"] != "reconciling": return row
        with self.store.connect() as db: uid=db.execute("SELECT uid FROM users WHERE id=?",(row["user_id"],)).fetchone()[0]
        payload={"version":row["version"],"userUid":uid,"instance":row["instance"],"deliveryHash":row["delivery"],"oldWork":row["old_work"],"newWork":row["work"]}
        answer=await self.bridge.apply(assignment_id,payload)
        expected={"schemaVersion":1,"id":assignment_id,**payload,"reconciled":True}
        if any(answer.get(k)!=v or type(answer.get(k)) is not type(v) for k,v in expected.items()) or type(answer.get("duplicate")) is not bool or set(answer)!=set(expected)|{"duplicate"}: raise GatewayError("Bridge reconciliation evidence mismatch",503)
        with self.store.transaction() as db:
            changed=db.execute("UPDATE assignments SET state=desired_state,updated=? WHERE id=? AND version=? AND state='reconciling'",(int(self.clock()),assignment_id,row["version"])).rowcount
            if changed:
                db.execute("UPDATE requests SET state=? WHERE assignment_id=? AND state='reconciling'",(row["desired_state"],assignment_id))
                self.store.audit(db,"acl-reconciled",assignment_id,payload,int(self.clock()))
        return self._get(assignment_id)

    async def reconcile_all(self):
        with self.store.connect() as db: rows=db.execute("SELECT id FROM assignments WHERE state='reconciling' AND instance=?",(self.instance,)).fetchall()
        for row in rows:
            try: await self.reconcile(row["id"])
            except GatewayError: continue  # Durable intent remains pending for the next retry.

    def my_works(self, session):
        current=self.current(session)
        return [work for work in self.works.values() if session["role"]=="teacher" or "relatedExercise" not in work or current and current["delivery"]==self.delivery["deliveryHash"] and current["state"] in ACTIVE and current["work"]==work["id"]]

    def launch_work(self, session, work_id):
        if session["role"]=="teacher": raise GatewayError("Teacher role does not grant native Student/admin access",403)
        work=next((work for work in self.my_works(session) if work["id"]==work_id),None)
        if not work: raise GatewayError("Work is not currently assigned",403)
        return work
