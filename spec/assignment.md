# Persistent assignments

The installed native delivery is hashed and inventoried at startup. Only its
selected instance works are eligible. Defense works have canonical relatedExercise
and native student-label policy; ordinary labs have no relatedExercise. The essay
mapping comes exclusively from this relation, never a resemblance between IDs.
Each eligible essay maps to one defense in the selected instance.

Flow is server-owned `teacher`, `immediate` or `approval`; quotas are positive
explicit per-essay integers. Missing quotas mean unlimited capacity. A student
can hold one current assignment in the instance. SQLite BEGIN IMMEDIATE serializes
capacity and duplicate/concurrent selection. Idempotency keys bind user and exact
request body; conflicts fail. Previous requests remain in audit after rejection
or replacement. Source QMD/native files are never edited by runtime actions.

Browser paths are prefixed `/gateway`:

| Method/path | Body/authorization |
| --- | --- |
| GET /my/works | Verified session; labs plus current active defense for Student, all selected works for teacher |
| POST /assignments/request | Student; closed essayId/idempotencyKey; immediate or approval flow |
| GET /teacher/roster | Teacher in the fixed defense context/resource |
| POST /teacher/assignments | Teacher; closed opaque Gateway userId/essayId/idempotencyKey; previously verified Student |
| POST /teacher/assignments/{id}/approve | Teacher; empty closed object |
| POST /teacher/assignments/{id}/reject | Teacher; empty closed object |
| POST /teacher/assignments/{id}/reassign | Teacher; closed essayId/idempotencyKey |
| POST /launch/pl/{workId} | Student; empty closed object; matching registered Moodle activity |

Teacher assignment and immediate selection reserve `reconciling` then activate
`assigned`. Approval selection is `pending` with no ACL call; approval reserves
`reconciling` then activates `approved`. Rejection is `rejected` and grants no
access; a later selection records a new request. A valid passing grade may mark
assigned/approved as completed. Public state exposes pending/reconciling rather
than claiming access before native reconciliation succeeds.

The private bridge receives only stable assignment ID and closed version,
server-derived userUid, instance, deliveryHash, oldWork and newWork. Its exact
response must confirm the same scope and reconciled state. Version starts at one
and increments on each native transition. Replacement preserves assignment ID,
revokes the old work and confirms the new work before activating it; the bridge
enforces existing-session/direct-URL ACL as well as list visibility. Failed calls
leave durable reconciliation intent and retry after restart/poll. Identical native
retries reconcile ACL again. A live AGS send lease temporarily prevents replacement
from racing delivery of the old score.

Gateway checks its own current assignment before asking the private manifest
work-launch API for the native path. Platform independently verifies native joined
Student access and current defense label/journal. Teachers cannot use this route
to gain native staff/admin access. UI visibility does not substitute for native ACL.

SQLite schema migrations preserve sessions, assignments, requests, outbox and audit;
startup ALTERs are serialized. A delivery change is explicitly rejected for an
existing slot. A reviewed delivery-migration operation must bind old/new manifests,
preserve identity/activity/grade mapping, revoke old native ACL and confirm new ACL
before switching. No automatic source edit, blind journal rewrite or old regrade
is supported by this version.
