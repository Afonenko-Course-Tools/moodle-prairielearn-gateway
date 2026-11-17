BEGIN IMMEDIATE;
CREATE TABLE exam_schema(version INTEGER PRIMARY KEY CHECK(version=1));
INSERT INTO exam_schema VALUES(1);
CREATE TABLE exam_launches(
    id INTEGER PRIMARY KEY,
    occurred_at INTEGER NOT NULL,
    event TEXT NOT NULL CHECK(event='verified_launch'),
    exam_id TEXT NOT NULL,
    course TEXT NOT NULL,
    activity TEXT NOT NULL,
    uid TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source_ip TEXT,
    evidence_status TEXT NOT NULL,
    gate_decision TEXT NOT NULL CHECK(gate_decision IN ('monitor','allowed','denied')),
    ip_multiple_students INTEGER NOT NULL CHECK(ip_multiple_students IN (0,1)),
    student_multiple_ips INTEGER NOT NULL CHECK(student_multiple_ips IN (0,1))
);
CREATE INDEX exam_ip ON exam_launches(course,exam_id,source_ip,uid);
CREATE INDEX exam_student ON exam_launches(course,exam_id,uid,source_ip);
COMMIT;
