#!/usr/bin/env python3
"""Run the private Gateway ASGI service from closed server-owned configuration."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import uvicorn
from gateway.courses import CoursesConfiguration, create_courses_app

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",type=Path,required=True)
    p.add_argument("--host",default="0.0.0.0")
    p.add_argument("--port",type=int,default=18080)
    args=p.parse_args()
    os.umask(0o077)
    value=json.loads(args.config.read_text())
    config=CoursesConfiguration.model_validate(value)
    repo=Path(__file__).resolve().parents[1]
    for course in config.courses.values():
        for path in (course.database,course.privateKeyFile,course.handoffTokenFile,course.bridge.tokenFile):
            if Path(path).resolve().is_relative_to(repo): p.error("Private runtime DB/keys/tokens must remain outside owner source")
    uvicorn.run(create_courses_app(config),host=args.host,port=args.port,proxy_headers=False,access_log=False,server_header=False)

if __name__=="__main__":main()
