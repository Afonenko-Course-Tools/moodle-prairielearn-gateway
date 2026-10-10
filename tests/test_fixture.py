from pathlib import Path
import re

def test_sixty_distinct_owner_qmd_defenses_and_three_ready_java_projects():
    root=Path(__file__).parent/"fixtures/course-gateway/tasks"
    files=sorted((root/"defenses").glob("*.qmd"))
    assert len(files)==60
    relations=[]
    for source in files:
        body=source.read_text()
        relations.append(re.search(r"related-exercise: (exr-essay-\d+)",body).group(1))
        assert "pass: {at-least: 2}" in body
        assert all("@exr-fixture-"+s in body for s in ("one","two","three"))
    assert len(set(relations))==60 and all("#"+r in (root/"essays.qmd").read_text() for r in relations)
    projects=list((root/"projects").iterdir())
    assert len(projects)==3
    for project in projects:
        assert len(list((project/"student").glob("*.java")))==1
        assert len(list((project/"reference").glob("*.java")))==1
        assert len(list((project/"tests/junit").glob("*Test.java")))==1
    assert not list(root.rglob("infoAssessment.json")) and not list(root.rglob("delivery.json"))
