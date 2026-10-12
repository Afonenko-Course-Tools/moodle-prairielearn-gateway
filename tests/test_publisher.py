import os
from pathlib import Path
import subprocess
import pytest

@pytest.mark.parametrize('exit_code,text,allowed',[(0,'digest',False),(1,'manifest unknown',True),(1,'no such manifest',True),(1,'unauthorized: manifest not found',False),(1,'denied: requested access',False),(1,'network timeout',False),(1,'unknown error',False)])
def test_registry_absence_must_be_proven(tmp_path,exit_code,text,allowed):
    docker=tmp_path/'docker'
    docker.write_text('#!/bin/sh\nprintf "%s\\n" '+repr(text)+' >&2\nexit '+str(exit_code)+'\n');docker.chmod(0o700)
    env={**os.environ,'PATH':str(tmp_path)+':'+os.environ['PATH']}
    result=subprocess.run(['bash',str(Path(__file__).parents[1]/'tools/require-absent-image.sh'),'ghcr.io/example/gateway:v2.0.0'],env=env,capture_output=True,text=True)
    assert (result.returncode==0)==allowed
