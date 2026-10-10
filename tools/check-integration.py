#!/usr/bin/env python3
"""Inspect Gateway durable audit/outbox evidence; live acceptance is separate."""
import argparse
import json
from pathlib import Path
import sqlite3

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--database",type=Path,required=True)
    args=p.parse_args()
    if not args.database.is_file():p.error("Existing private runtime database required")
    with sqlite3.connect(args.database.resolve().as_uri()+"?mode=ro",uri=True) as db:
        result={"schemaVersion":1,"gatewayRuntimeOnly":True,"liveMoodleAcceptance":False,"assignments":dict(db.execute("SELECT state,count(*) FROM assignments GROUP BY state")),"outbox":dict(db.execute("SELECT state,count(*) FROM outbox GROUP BY state")),"auditEvents":db.execute("SELECT count(*) FROM audit").fetchone()[0]}
    print(json.dumps(result,sort_keys=True))
    return 1 if result["assignments"].get("reconciling") or result["outbox"].get("pending") or result["outbox"].get("sending") else 0
if __name__=="__main__":raise SystemExit(main())
