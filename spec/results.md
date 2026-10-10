# Trusted results and AGS outbox

The background worker initially polls every 15 seconds, configurable in private
settings. It reconciles durable assignment intents, reads current scoped native
results, then drains the SQLite outbox. Gateway reads no PrairieLearn SQL and
accepts no public grade callback or browser-supplied score.

Defense reads use `GET /internal/gateway/results?assignmentId=ID&version=N` with
private service bearer. The closed reply binds schema/source, assignment/version,
server UID, instance, delivery, work, current attempt and exact qualified question
set. Gateway independently checks all these values against its current active
slot and installed completion policy. Unknown/duplicate/missing questions, invalid
status, non-finite or out-of-range scores and inconsistent aggregate completion fail.

Only required questions whose status is complete and score is exactly 1 count.
Optional questions never replace required questions; weighted partial credit does
not become two completed tasks. Defense AGS scoreGiven is completedRequired and
scoreMaximum is requiredCount. The manifest atLeast threshold controls completed
assignment state. A wholly ungraded attempt produces no event and preserves the
previous Moodle grade. Current attempts cannot move backwards or change ID for
the same number.

Ordinary lab reads use `/internal/gateway/work-results` with the exact server UID,
instance, deliveryHash and workId. They require a previously verified fixed Moodle
lab activity/context/lineitem and an ordinary native work. The reply omits defense
assignment/version and includes native scoreGiven/scoreMaximum from the current
attempt. These declared aggregate points preserve native weighting (for example
6/8), while per-question scope/completion are still independently validated.

Events bind canonical trusted evidence, sorted qualified questions, the fixed
assignment/activity scope and the preceding persistent revision. Observation and
outbox insertion share a SQLite transaction. Consecutive identical polls reuse the
committed event and payload; a legitimate A→B→A regrade creates a new revision even
though its final content appeared earlier. Each outbox row
persists user, activity/context, lineitem, delivery/version, payload and timestamp.
New payload timestamps are monotonic per user/lineitem even within one polling
second and remain identical on transport retry.

Before claiming an event, the worker rechecks the current assignment/delivery and
latest observed attempt/revision. Old assignment/delivery/attempt events become
obsolete, with audit, and cannot overwrite newer scores after a delayed retry.
Both persisted rows and evidence must match the service's currently configured
delivery and instance; a restart with a different delivery cannot send old queued
grades before explicit migration. Successful regrades below the pass threshold
restore the assignment's assigned/approved state instead of retaining completed.
An expiring persistent send lease permits restart recovery and serializes sends
per user/lineitem. Failed delivery returns to pending with bounded exponential
backoff; successful delivery becomes sent. Replacement never clears the existing
Moodle score. It remains until a valid grade from the new activated defense arrives.

AGS obtains an OAuth token through the fixed registered token endpoint using a
short-lived RS256 client assertion signed by the private tool key, with the score
scope. Score POSTs target only registered lineitems, append /scores while preserving
Moodle's type_id query, use the prescribed content type and a stable Idempotency-Key.
No redirects are followed. TLS uses the configured trusted CA, with no verification
bypass. OAuth/transport failures are retryable; a 401 invalidates the token cache.

`tools/check-integration.py` reads state counts/audit from a private existing SQLite
database in read-only mode and returns failure for reconciling or undelivered rows.
Its output explicitly says liveMoodleAcceptance=false. Actual Moodle AGS readback,
two-user isolation, native direct-URL checks and course-upgrade preservation require
the separate private integration receipts.
