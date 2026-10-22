"""Выполняем настоящие publisher-блоки на вымышленных локальных артефактах."""
import hashlib
import json
import subprocess
import textwrap
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def step(name):
    # Bounded reader for the existing workflow's literal run block; no YAML dependency.
    body = (ROOT / '.github/workflows/release.yml').read_text().split('      - name: ' + name + '\n', 1)[1]
    return body.split('\n      - ', 1)[0]


def python_block(name):
    return textwrap.dedent(step(name).split("python - <<'PY'\n", 1)[1].split('\n          PY', 1)[0])


@pytest.fixture
def release(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'dist').mkdir()
    wheel = tmp_path / 'dist/gateway-2.1.0-py3-none-any.whl'
    wheel.write_bytes(b'fictional local wheel')
    monkeypatch.setenv('IMAGE_DIGEST', 'sha256:' + 'a' * 64)
    monkeypatch.setenv('IMAGE_NAME', 'ghcr.io/example/gateway')
    monkeypatch.setenv('RELEASE_TAG', 'v2.1.0')
    monkeypatch.setenv('RELEASE_REPOSITORY', 'example/gateway')
    def source(args, **kwargs):
        assert args == ['git', 'rev-parse', 'HEAD']
        return 'b' * 40 + '\n'
    monkeypatch.setattr(subprocess, 'check_output', source)
    return tmp_path, wheel


def test_actual_publisher_receipt_matches_current_native_contract(release):
    tmp_path, wheel = release
    exec(compile(python_block('Record published digest'), 'workflow-record-receipt', 'exec'), {})
    receipt = json.loads((tmp_path / 'dist/release-images.json').read_text())
    assert receipt == {
        'schemaVersion': 2, 'versionTag': 'v2.1.0', 'sourceCommit': 'b' * 40,
        'repository': 'example/gateway', 'platform': 'linux/amd64',
        'image': 'ghcr.io/example/gateway@sha256:' + 'a' * 64,
        'databaseSchemaVersion': 3, 'deliverySchemaVersion': 3,
        'resultsSchemaVersion': 2, 'configurationSchemaVersion': 2,
        'wheel': {'name': wheel.name, 'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()},
        'previousSchemas': 'rejected; fresh database required',
    }


@pytest.mark.parametrize('digest', ['', 'latest', 'sha256:' + 'a'*63, 'sha256:' + 'G'*64])
def test_actual_publisher_refuses_missing_or_non_digest_image(release, monkeypatch, digest):
    tmp_path, _ = release
    monkeypatch.setenv('IMAGE_DIGEST', digest)
    with pytest.raises(SystemExit, match='Missing actual registry image digest'):
        exec(python_block('Record published digest'), {})
    assert not (tmp_path / 'dist/release-images.json').exists()


@pytest.mark.parametrize('count', [0, 2])
def test_actual_publisher_refuses_ambiguous_wheel_closure(release, count):
    tmp_path, wheel = release
    if count == 0: wheel.unlink()
    else: (tmp_path / 'dist/other.whl').write_bytes(b'other')
    with pytest.raises(SystemExit, match='Expected exactly one release wheel'):
        exec(python_block('Record published digest'), {})
    assert not (tmp_path / 'dist/release-images.json').exists()


@pytest.mark.parametrize('tag,head,ancestor,allowed', [
    ('v2.1.0', 'b'*40, 0, True), ('v2.0.3', 'b'*40, 0, False),
    ('v2.1.0', 'c'*40, 0, False), ('v2.1.0', 'b'*40, 1, False),
])
def test_actual_release_source_guard_requires_matching_integrated_version(tmp_path, monkeypatch, tag, head, ancestor, allowed):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion="2.1.0"\n')
    monkeypatch.setenv('RELEASE_TAG', tag)
    def git(args, **kwargs):
        assert args in (['git','rev-parse','HEAD'], ['git','rev-parse','--verify','refs/tags/v2.1.0^{commit}'])
        return (head if args[-1] == 'HEAD' else 'b'*40) + '\n'
    def ancestry(args, **kwargs):
        assert args == ['git','merge-base','--is-ancestor','HEAD','origin/main']
        return subprocess.CompletedProcess(args, ancestor)
    monkeypatch.setattr(subprocess, 'check_output', git)
    monkeypatch.setattr(subprocess, 'run', ancestry)
    if allowed: exec(python_block('Verify immutable release source'), {})
    else:
        with pytest.raises(SystemExit): exec(python_block('Verify immutable release source'), {})


@pytest.mark.parametrize('existing,view_exit,allowed', [('',0,True), ('release-images.json\n',0,False), ('',1,False)])
def test_actual_release_preflight_requires_existing_release_without_receipt(tmp_path, monkeypatch, existing, view_exit, allowed):
    # Execute the actual shell gate with only its GH transport and scratch path substituted.
    gh = tmp_path / 'gh'
    gh.write_text('#!/bin/sh\n[ "$*" = \'release view v2.1.0 --json assets --jq .assets[].name\' ] || exit 90\nprintf "%s" "$FIXTURE_ASSETS"\nexit "$FIXTURE_EXIT"\n')
    gh.chmod(0o700)
    monkeypatch.setenv('PATH',str(tmp_path)+':'+__import__('os').environ['PATH'])
    monkeypatch.setenv('FIXTURE_ASSETS',existing)
    monkeypatch.setenv('FIXTURE_EXIT',str(view_exit))
    monkeypatch.setenv('RELEASE_TAG','v2.1.0')
    script = textwrap.dedent(step('Require an unpublished release receipt').split('run: |\n',1)[1])
    script = script.replace('/tmp/gateway-release-assets', str(tmp_path/'assets'))
    actual = subprocess.run(['bash','-e','-c',script],capture_output=True,text=True)
    assert (actual.returncode == 0) is allowed


def test_actual_release_upload_attaches_both_assets_without_overwrite(release, monkeypatch):
    tmp_path, _ = release
    (tmp_path/'dist/release-images.json').write_text('{}')
    gh = tmp_path/'gh'
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FIXTURE_ARGV"\n')
    gh.chmod(0o700)
    monkeypatch.setenv('PATH',str(tmp_path)+':'+__import__('os').environ['PATH'])
    monkeypatch.setenv('FIXTURE_ARGV',str(tmp_path/'argv'))
    script = step('Attach image receipt and wheel to existing release').split('run: ',1)[1].strip()
    actual = subprocess.run(['bash','-e','-c',script],capture_output=True,text=True)
    assert actual.returncode == 0
    assert (tmp_path/'argv').read_text().splitlines() == ['release','upload','v2.1.0','dist/gateway-2.1.0-py3-none-any.whl','dist/release-images.json']
