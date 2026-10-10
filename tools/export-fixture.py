#!/usr/bin/env python3
"""Export the synthetic 60-defense owner QMD fixture through installed owners."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--core",type=Path,required=True)
    p.add_argument("--exporter",type=Path,required=True)
    p.add_argument("--runtime-registry",type=Path,required=True)
    p.add_argument("--candidate-image",required=True)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    repo=Path(__file__).resolve().parents[1]
    output=args.output.resolve()
    if output.is_relative_to(repo):p.error("Native delivery/checks/evidence remain outside owner source")
    source=Path(tempfile.mkdtemp(prefix="gateway-native-fixture-"))
    shutil.copytree(repo/"tests/fixtures/course-gateway",source,dirs_exist_ok=True)
    try:
        book=source/"tasks"
        for owner in (args.core,args.exporter):subprocess.run(["quarto","add",str(owner.resolve()),"--no-prompt"],cwd=book,check=True)
        entry=next(book.glob("_extensions/**/course-prairielearn/entrypoints/export-course.ts"),None)
        if entry is None:p.error("Installed PrairieLearn exporter not found")
        output.parent.mkdir(parents=True,exist_ok=True)
        checks=output.parent/(output.name+"-checks.json")
        subprocess.run(["quarto","run",str(entry),str(source),str(output),"--instance","pilot","--checks-output",str(checks),"--runtime-registry",str(args.runtime_registry.resolve()),"--candidate-image",args.candidate_image],cwd=source,check=True)
        manifest=json.loads((output/"delivery.json").read_text())
        assert len(manifest["questions"])==3 and len(manifest["works"])==61
        defenses=[w for w in manifest["works"] if "relatedExercise" in w]
        assert len(defenses)==60 and len({w["relatedExercise"] for w in defenses})==60
        assert all(w["completion"]["atLeast"]==2 and len(w["completion"]["requiredQuestionIds"])==3 for w in defenses)
        lab=next(w for w in manifest["works"] if w["id"]=="sec-gateway-lab")
        assert not lab["policy"].get("assignment",{}).get("student-label"),"Ordinary lab must not require a defense label"
        print(json.dumps({"source":"installed-native-owner-export","synthetic":True,"questions":3,"defenses":60,"labs":1,"deliveryHash":manifest["deliveryHash"],"sourceRoot":str(source)},sort_keys=True))
    except BaseException:
        print("Retained failed synthetic source: "+str(source))
        raise
    else:
        shutil.rmtree(source)

if __name__=="__main__":main()
