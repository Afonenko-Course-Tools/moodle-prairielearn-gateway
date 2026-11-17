"""Публичный LTI вход и отдельный наблюдательный журнал на вымышленных данных."""
from contextlib import closing
import copy
import ipaddress
import json
import logging
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
import httpx
import pytest
from joserfc import jwt
from gateway.app import create_app
from gateway.config import Configuration
from gateway.courses import CoursesConfiguration
from gateway.exam import ExamMonitor, ExamStore, WARNING
from gateway.launch import INSTRUCTOR, LTI
from gateway.store import Store
from payloads import Payload, mutations
from test_auth_flow import Platform, PREFIX, UID, begin, browser_for, configuration
from test_config import named

CLIENT_IP = '10.20.30.40'
TOKEN = 'p' * 32


def exam_configuration(tmp_path: Path) -> Payload:
    config = configuration(tmp_path)
    token = tmp_path / 'exam-proxy-token'
    token.write_text(TOKEN)
    token.chmod(0o600)
    config['examAudit'] = {'examId': 'synthetic-2026-11-17', 'activities': ['catalog'],
                           'database': str(tmp_path / 'exam.sqlite'), 'proxyTokenFile': str(token),
                           'classroomCidrs': ['10.0.0.0/8']}
    return config


def trusted_headers(address: str = CLIENT_IP) -> list[tuple[str, str]]:
    return [('X-Exam-Proxy-Token', TOKEN), ('X-Exam-Client-IP', address)]


def rows(config: Payload) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(str(config.child('examAudit')['database']))) as db:
        db.row_factory = sqlite3.Row
        return list(db.execute('SELECT * FROM exam_launches ORDER BY id'))


@pytest.mark.parametrize('mutate', mutations([
    lambda c: c.child('examAudit').update(examId=''),
    lambda c: c.child('examAudit').update(examId='browser?value'),
    lambda c: c.child('examAudit').update(activities=[]),
    lambda c: c.child('examAudit').update(activities=['catalog', 'catalog']),
    lambda c: c.child('examAudit').update(activities=['unregistered']),
    lambda c: c.child('examAudit').update(database='relative.sqlite'),
    lambda c: c.child('examAudit').update(proxyTokenFile='relative-token'),
    lambda c: c.child('examAudit').update(classroomCidrs=[]),
    lambda c: c.child('examAudit').update(classroomCidrs=['10.20.0.0/8']),
    lambda c: c.child('examAudit').update(classroomCidrs=['bad']),
    lambda c: c.child('examAudit').update(classroomCidrs=['10.0.0.0/8', '10.0.0.0/8']),
    lambda c: c.child('examAudit').update(classroomCidrs=['fe80::%eth0/64']),
    lambda c: c.child('examAudit').update(restrictLaunchToClassroom='false'),
    lambda c: c.child('examAudit').update(required=True),
    lambda c: c.child('examAudit').update(database=c['database']),
    lambda c: c.child('examAudit').update(database=c['handoffTokenFile']),
    lambda c: c.child('examAudit').update(proxyTokenFile=c.child('bridge')['tokenFile']),
    lambda c: c.child('examAudit').update(database=c.child('examAudit')['proxyTokenFile']),
]))
def test_exam_scope_is_closed_and_server_owned(tmp_path: Path, mutate: Callable[[Payload], object]) -> None:
    value = exam_configuration(tmp_path)
    mutate(value)
    with pytest.raises(ValueError):
        Configuration.model_validate(value)


@pytest.mark.parametrize('field', ['database', 'handoffTokenFile', 'bridge', 'caFile', 'audit', 'proxy'])
def test_named_courses_reject_cross_course_audit_path_collisions(tmp_path: Path, field: str) -> None:
    value = named(tmp_path)
    courses = value.child('courses')
    courses.child('demo')['examAudit'] = exam_configuration(tmp_path)['examAudit']
    other = courses.child('other')
    other['caFile'] = str(tmp_path / 'ca.pem')
    if field == 'audit':
        other['examAudit'] = copy.deepcopy(courses.child('demo')['examAudit'])
    elif field == 'proxy':
        other['examAudit'] = {**courses.child('demo').child('examAudit'),
                              'database': str(tmp_path / 'other-audit.sqlite'),
                              'proxyTokenFile': courses.child('demo').child('examAudit')['database']}
    else:
        path = other.child('bridge')['tokenFile'] if field == 'bridge' else other[field]
        courses.child('demo').child('examAudit')['database'] = path
    with pytest.raises(ValueError):
        CoursesConfiguration.model_validate(value)


def test_audit_path_aliases_to_auth_or_secret_are_rejected(tmp_path: Path) -> None:
    value = exam_configuration(tmp_path)
    path = tmp_path / 'alias'
    path.symlink_to(str(value['database']))
    value.child('examAudit')['database'] = str(path)
    with pytest.raises(ValueError):
        Configuration.model_validate(value)
    path.unlink()
    path.hardlink_to(str(value.child('examAudit')['proxyTokenFile']))
    with pytest.raises(ValueError):
        Configuration.model_validate(value)


@pytest.mark.anyio
async def test_feature_off_ignores_claimed_ip_and_does_not_create_audit(tmp_path: Path) -> None:
    platform = Platform()
    app = create_app(configuration(tmp_path), clients=platform, clock=lambda: 1000)
    async with browser_for(app) as browser:
        _, body = await begin(browser, platform)
        response = await browser.post(PREFIX + '/lti/launch', data=body, headers=trusted_headers())
        assert response.status_code == 303 and 'set-cookie' not in response.headers
    assert app.state.exam_monitor is None and not (tmp_path / 'exam.sqlite').exists()


@pytest.mark.anyio
async def test_verified_uid_baseline_and_both_exam_signals(tmp_path: Path) -> None:
    value = exam_configuration(tmp_path)
    platform = Platform()
    app = create_app(value, clients=platform, clock=lambda: 1000)
    async with browser_for(app) as browser:
        for sub, name, address in [('student-a', 'Синтетический А', CLIENT_IP),
                                   ('student-a', 'Изменённое имя', CLIENT_IP),
                                   ('student-b', 'Синтетический А', CLIENT_IP),
                                   ('student-a', 'Синтетический А', '10.20.30.41')]:
            _, body = await begin(browser, platform, sub=sub, name=name, email='synthetic@example.invalid')
            answer = await browser.post(PREFIX + '/lti/launch', data=body, headers=trusted_headers(address))
            assert answer.status_code == 303
    recorded = rows(value)
    assert [r['uid'] for r in recorded[:2]] == [UID, UID]
    assert recorded[2]['uid'] != UID
    assert [(r['ip_multiple_students'], r['student_multiple_ips']) for r in recorded] == [(0, 0), (0, 0), (1, 0), (0, 1)]
    assert all(r['event'] == 'verified_launch' and r['evidence_status'] == 'inside_classroom'
               and r['exam_id'] == 'synthetic-2026-11-17' and r['course'] == PREFIX and r['activity'] == 'catalog' for r in recorded)
    assert set(recorded[0].keys()) == {'id', 'occurred_at', 'event', 'exam_id', 'course', 'activity', 'uid',
                                      'display_name', 'source_ip', 'evidence_status', 'gate_decision', 'ip_multiple_students', 'student_multiple_ips'}
    assert recorded[1]['display_name'] == 'Изменённое имя'
    assert all(r['gate_decision'] == 'monitor' for r in recorded)
    assert 'synthetic@example.invalid'.encode() not in Path(str(value.child('examAudit')['database'])).read_bytes()
    with closing(sqlite3.connect(str(value['database']))) as db:
        assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {'auth_schema', 'auth_attempts'}
    assert platform.calls == []  # Это событие launch, а не успешный enrollment.


@pytest.mark.anyio
@pytest.mark.parametrize('case', ['missing', 'wrong-token', 'empty', 'port', 'list', 'zone', 'combined',
                                 'duplicate-token', 'duplicate-ip'])
async def test_untrusted_evidence_is_unknown_without_denial(tmp_path: Path, case: str) -> None:
    value = exam_configuration(tmp_path)
    platform = Platform()
    app = create_app(value, clients=platform, clock=lambda: 1000)
    headers = trusted_headers()
    if case == 'missing':
        headers = []
    elif case == 'wrong-token':
        headers[0] = (headers[0][0], 'x' * 32)
    elif case in ('empty', 'port', 'list', 'zone'):
        headers[1] = (headers[1][0], {'empty': '', 'port': '10.20.30.40:1234',
                                    'list': '10.20.30.40, 10.20.30.41', 'zone': 'fe80::1%eth0'}[case])
    elif case == 'combined':
        headers[0] = (headers[0][0], TOKEN + ', ' + TOKEN)
    else:
        headers.append(headers[['duplicate-token', 'duplicate-ip'].index(case)])
    async with browser_for(app) as browser:
        _, body = await begin(browser, platform)
        assert (await browser.post(PREFIX + '/lti/launch', data=body, headers=headers)).status_code == 303
    record = rows(value)[0]
    assert record['source_ip'] is None and record['evidence_status'] != 'inside_classroom'
    assert record['ip_multiple_students'] == record['student_multiple_ips'] == 0


@pytest.mark.anyio
@pytest.mark.parametrize('case', ['signature', 'state', 'expired', 'resource', 'role', 'extra-field', 'cookie', 'replay'])
async def test_invalid_lti_does_not_produce_exam_event(tmp_path: Path, case: str) -> None:
    value = exam_configuration(tmp_path)
    platform = Platform()
    app = create_app(value, clients=platform, clock=lambda: 1000)
    async with browser_for(app) as browser:
        _, body = await begin(browser, platform)
        headers = trusted_headers()
        if case in ('expired', 'resource', 'role'):
            claims = Payload.validated(jwt.decode(body['id_token'], platform.key, algorithms=['RS256']).claims)
            if case == 'expired':
                claims['exp'] = 999
            elif case == 'resource':
                claims[LTI + 'resource_link'] = {'id': 'foreign-resource'}
            else:
                claims[LTI + 'roles'] = ['Administrator']
            body['id_token'] = jwt.encode({'alg': 'RS256', 'kid': platform.key.kid}, claims, platform.key, algorithms=['RS256'])
        elif case == 'signature':
            body['id_token'] = 'invalid.jwt.signature'
        elif case == 'state':
            body['state'] = 'foreign-state'
        elif case == 'extra-field':
            body['examId'] = 'browser-choice'
        elif case == 'cookie':
            nonce = browser.cookies['GatewayLogin']
            headers.append(('Cookie', 'GatewayLogin=' + nonce + '; GatewayLogin=' + nonce))
        else:
            assert (await browser.post(PREFIX + '/lti/launch', data=body, headers=headers)).status_code == 303
        assert (await browser.post(PREFIX + '/lti/launch', data=body, headers=headers)).status_code == 403
    assert len(rows(value)) == (1 if case == 'replay' else 0)


@pytest.mark.anyio
@pytest.mark.parametrize('excluded', ['teacher', 'activity'])
async def test_only_selected_student_activity_is_observed(tmp_path: Path, excluded: str) -> None:
    value = exam_configuration(tmp_path)
    value.child('examAudit')['restrictLaunchToClassroom'] = True
    if excluded == 'activity':
        value.child('registration').child('activities')['exam'] = {'resourceLinkId': 'exam-resource'}
        value.child('examAudit')['activities'] = ['exam']
    platform = Platform()
    app = create_app(value, clients=platform, clock=lambda: 1000)
    async with browser_for(app) as browser:
        _, body = await begin(browser, platform, **({'role': INSTRUCTOR} if excluded == 'teacher' else {}))
        assert (await browser.post(PREFIX + '/lti/launch', data=body)).status_code == 303
    assert rows(value) == []


@pytest.mark.anyio
@pytest.mark.parametrize('failure', ['foreign-db', 'unopenable-db', 'missing-token', 'public-token', 'invalid-token', 'locked-db', 'lost-db'])
async def test_audit_failure_preserves_login_and_has_fixed_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: str) -> None:
    value = exam_configuration(tmp_path)
    audit = Path(str(value.child('examAudit')['database']))
    before: bytes | None = None
    if failure == 'foreign-db':
        with sqlite3.connect(audit) as db:
            db.execute('CREATE TABLE users(uid TEXT)')
        audit.chmod(0o640)
        before = audit.read_bytes()
    elif failure == 'unopenable-db':
        audit.mkdir()
    token = Path(str(value.child('examAudit')['proxyTokenFile']))
    if failure == 'missing-token':
        token.unlink()
    elif failure == 'public-token':
        token.chmod(0o644)
    elif failure == 'invalid-token':
        token.write_text('short')
    platform = Platform()
    with caplog.at_level(logging.WARNING, logger='gateway.exam'):
        app = create_app(value, clients=platform, clock=lambda: 1000)
        lock: sqlite3.Connection | None = None
        if failure == 'locked-db':
            lock = sqlite3.connect(audit)
            lock.execute('BEGIN IMMEDIATE')
        elif failure == 'lost-db':
            audit.unlink()
            audit.mkdir()
        try:
            async with browser_for(app) as browser:
                _, body = await begin(browser, platform)
                started = time.monotonic()
                assert (await browser.post(PREFIX + '/lti/launch', data=body, headers=trusted_headers())).status_code == 303
                assert time.monotonic() - started < 2
        finally:
            if lock is not None:
                lock.close()
    monitor = app.state.exam_monitor
    assert isinstance(monitor, ExamMonitor) and monitor.degraded
    assert caplog.records and all(record.getMessage() == WARNING for record in caplog.records)
    if before is not None:
        assert audit.read_bytes() == before and audit.stat().st_mode & 0o777 == 0o640
        assert not audit.with_name(audit.name + '-journal').exists()
    if failure.endswith('token'):
        assert rows(value)[0]['source_ip'] is None


def test_foreign_auth_db_is_never_modified_by_audit_store(tmp_path: Path) -> None:
    path = tmp_path / 'auth.sqlite'
    Store(path)
    path.chmod(0o640)
    before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        ExamStore(str(path))
    assert before == {p.name: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.iterdir()}


def test_signals_do_not_cross_exam_course_or_unknown_ip(tmp_path: Path) -> None:
    store = ExamStore(str(tmp_path / 'exam.sqlite'))
    for exam, course, uid, address in [('one', 'demo', 'uid-a', CLIENT_IP), ('two', 'demo', 'uid-b', CLIENT_IP),
                                      ('one', 'other', 'uid-b', CLIENT_IP), ('one', 'demo', 'uid-b', None)]:
        store.record(now=1000, exam_id=exam, course=course, activity='catalog', uid=uid, name='Участник',
                     source_ip=address, status='synthetic', decision='monitor')
    with closing(store.connect()) as db:
        assert list(db.execute('SELECT ip_multiple_students,student_multiple_ips FROM exam_launches')) == [(0, 0)] * 4


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal', '.startup-lock'])
@pytest.mark.parametrize('field', ['database', 'proxyTokenFile'])
def test_audit_rejects_auth_sqlite_sidecar_paths(tmp_path: Path, suffix: str, field: str) -> None:
    value = exam_configuration(tmp_path)
    value.child('examAudit')[field] = str(value['database']) + suffix
    with pytest.raises(ValueError):
        Configuration.model_validate(value)


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
@pytest.mark.parametrize('target', ['auth', 'token', 'audit'])
def test_cross_course_audit_sidecars_do_not_overlap_private_files(tmp_path: Path, suffix: str, target: str) -> None:
    value = named(tmp_path)
    courses = value.child('courses')
    courses.child('demo')['examAudit'] = exam_configuration(tmp_path)['examAudit']
    sidecar = str(courses.child('demo').child('examAudit')['database']) + suffix
    other = courses.child('other')
    if target == 'auth':
        other['database'] = sidecar
    elif target == 'token':
        other.child('bridge')['tokenFile'] = sidecar
    else:
        other['examAudit'] = {**courses.child('demo').child('examAudit'), 'database': sidecar}
    with pytest.raises(ValueError):
        CoursesConfiguration.model_validate(value)


@pytest.mark.anyio
@pytest.mark.parametrize('restricted', [False, True])
@pytest.mark.parametrize('address,expected', [
    ('10.0.0.0', 'inside_classroom'), ('10.255.255.255', 'inside_classroom'),
    ('9.255.255.255', 'outside_classroom'), ('11.0.0.0', 'outside_classroom'),
    ('::ffff:10.20.30.40', 'outside_classroom'), ('2001:0db8:0000::1', 'outside_classroom'),
    ('10.20.30.40, 192.0.2.1', 'invalid_ip'), ('fe80::1%12', 'invalid_ip'),
    ('[2001:db8::1]:443', 'invalid_ip'), (' 10.20.30.40', 'invalid_ip')])
async def test_classroom_cidr_policy_is_opt_in_and_uses_only_trusted_ip(tmp_path: Path, restricted: bool, address: str, expected: str) -> None:
    value = exam_configuration(tmp_path)
    value.child('examAudit')['restrictLaunchToClassroom'] = restricted
    platform = Platform()
    app = create_app(value, clients=platform, clock=lambda: 1000)
    async with browser_for(app) as browser:
        _, body = await begin(browser, platform)
        response = await browser.post(PREFIX + '/lti/launch', data=body, headers=trusted_headers(address))
    allowed = not restricted or expected == 'inside_classroom'
    assert response.status_code == (303 if allowed else 403)
    assert 'set-cookie' not in response.headers
    if not allowed:
        assert 'location' not in response.headers and '/pl/' not in response.text
    record = rows(value)[0]
    assert record['evidence_status'] == expected
    assert record['gate_decision'] == ('monitor' if not restricted else 'allowed' if allowed else 'denied')
    assert record['source_ip'] == (None if expected == 'invalid_ip' else ipaddress.ip_address(address).compressed)
    assert platform.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize('headers', [[], [('X-Exam-Client-IP', CLIENT_IP)],
                                    [('X-Exam-Proxy-Token', 'forged'), ('X-Exam-Client-IP', CLIENT_IP)],
                                    trusted_headers() + [('X-Exam-Client-IP', CLIENT_IP)]])
async def test_restricted_launch_denies_missing_untrusted_and_duplicate_evidence(tmp_path: Path, headers: list[tuple[str, str]]) -> None:
    value = exam_configuration(tmp_path)
    value.child('examAudit')['restrictLaunchToClassroom'] = True
    platform = Platform()
    app = create_app(value, clients=platform, clock=lambda: 1000)
    async with browser_for(app) as browser:
        _, body = await begin(browser, platform)
        answer = await browser.post(PREFIX + '/lti/launch', data=body, headers=headers)
        assert answer.status_code == 403 and 'location' not in answer.headers
    assert rows(value)[0]['gate_decision'] == 'denied' and platform.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize('address,expected', [(CLIENT_IP, 303), ('192.0.2.1', 403)])
async def test_audit_failure_does_not_change_restricted_policy(tmp_path: Path, address: str, expected: int) -> None:
    value = exam_configuration(tmp_path)
    value.child('examAudit')['restrictLaunchToClassroom'] = True
    Path(str(value.child('examAudit')['database'])).mkdir()
    platform = Platform()
    app = create_app(value, clients=platform, clock=lambda: 1000)
    async with browser_for(app) as browser:
        _, body = await begin(browser, platform)
        assert (await browser.post(PREFIX + '/lti/launch', data=body, headers=trusted_headers(address))).status_code == expected
    assert isinstance(app.state.exam_monitor, ExamMonitor) and app.state.exam_monitor.degraded


@pytest.mark.anyio
async def test_ipv6_is_canonicalized_and_can_use_an_explicit_network(tmp_path: Path) -> None:
    value = exam_configuration(tmp_path)
    value.child('examAudit').update(classroomCidrs=['2001:db8::/32'], restrictLaunchToClassroom=True)
    platform = Platform()
    app = create_app(value, clients=platform, clock=lambda: 1000)
    async with browser_for(app) as browser:
        _, body = await begin(browser, platform)
        assert (await browser.post(PREFIX + '/lti/launch', data=body, headers=trusted_headers('2001:0DB8:0000::1'))).status_code == 303
    assert rows(value)[0]['source_ip'] == '2001:db8::1'


@pytest.mark.parametrize('field', ['database', 'proxyTokenFile'])
def test_runtime_cli_rejects_private_audit_paths_in_repository(tmp_path: Path, field: str) -> None:
    value = exam_configuration(tmp_path)
    repo = Path(__file__).resolve().parents[1]
    forbidden = repo / 'exam-private-runtime-probe'
    value.child('examAudit')[field] = str(forbidden)
    runtime = tmp_path / 'runtime.json'
    runtime.write_text(json.dumps({'schemaVersion': 2, 'courses': {'demo': value}}))
    result = subprocess.run([sys.executable, 'tools/run-gateway.py', '--config', str(runtime)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and 'вне репозитория' in result.stderr
    assert not forbidden.exists() and not Path(str(value['database'])).exists()


@pytest.mark.anyio
@pytest.mark.parametrize('restricted', [False, True])
async def test_real_nginx_tls_to_gateway_trust_boundary(tmp_path: Path, restricted: bool) -> None:
    """Настоящие Nginx TLS/RealIP и Uvicorn; producer Squid здесь заменён TCP peer."""
    import asyncio
    import shutil
    import socket
    import ssl
    import subprocess
    from threading import Thread
    import uvicorn
    if not shutil.which('nginx') or not shutil.which('openssl'):
        pytest.skip('Нужны системные nginx и openssl для реальной TLS проверки')
    value = exam_configuration(tmp_path)
    value.child('examAudit')['restrictLaunchToClassroom'] = restricted
    cert, key = tmp_path / 'server.crt', tmp_path / 'server.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                    '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        tls_port = reserved.getsockname()[1]
    origin = f'https://127.0.0.1:{tls_port}'
    value['publicOrigin'] = origin
    platform = Platform()
    app = create_app(value, clients=platform, clock=lambda: 1000)
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    gateway_port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, proxy_headers=False))
    thread = Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    token_include = tmp_path / 'proxy-token.conf'
    token_include.write_text('proxy_set_header X-Exam-Proxy-Token "' + TOKEN + '";\n')
    token_include.chmod(0o600)
    location = Path('config/exam-proxy.example.conf').read_text()
    location = location.replace('/run/private/exam-proxy-token.conf', str(token_include))
    location = location.replace('http://gateway:18080', f'http://127.0.0.1:{gateway_port}')
    nginx_config = tmp_path / 'nginx.conf'
    nginx_config.write_text(f'''
worker_processes 1;
pid {tmp_path}/nginx.pid;
error_log {tmp_path}/nginx-error.log error;
events {{ worker_connections 32; }}
http {{
    access_log off;
    client_body_temp_path {tmp_path}/body;
    proxy_temp_path {tmp_path}/proxy;
    fastcgi_temp_path {tmp_path}/fastcgi;
    uwsgi_temp_path {tmp_path}/uwsgi;
    scgi_temp_path {tmp_path}/scgi;
    set_real_ip_from 127.0.0.2;
    real_ip_header X-Forwarded-For;
    real_ip_recursive on;
    map "$realip_remote_addr|$remote_addr" $exam_verified_client_ip {{
        default $remote_addr;
        "127.0.0.2|127.0.0.2" "";
    }}
    server {{
        listen 127.0.0.1:{tls_port} ssl;
        server_name 127.0.0.1;
        ssl_certificate {cert};
        ssl_certificate_key {key};
        {location}
    }}
}}
''')
    thread.start()
    nginx: subprocess.Popen[bytes] | None = None
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        nginx = subprocess.Popen(['nginx', '-p', str(tmp_path), '-c', str(nginx_config), '-g', 'daemon off;'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        context = ssl.create_default_context(cafile=str(cert))
        async with httpx.AsyncClient(verify=context, trust_env=False) as probe:
            for _ in range(200):
                if nginx.poll() is not None:
                    _, failure = nginx.communicate()
                    pytest.fail(failure.decode())
                try:
                    if (await probe.get(origin + '/gateway/not-found')).status_code == 404:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.01)
            else:
                pytest.fail('Nginx не открыл TLS listener')
        scenarios: list[tuple[str, dict[str, str], str]] = [('127.0.0.2', {'X-Forwarded-For': CLIENT_IP}, 'inside_classroom'),
                     ('127.0.0.2', {}, 'missing_headers'),
                     ('127.0.0.3', {'X-Forwarded-For': CLIENT_IP,
                                    'X-Exam-Proxy-Token': TOKEN, 'X-Exam-Client-IP': CLIENT_IP}, 'outside_classroom'),
                     ('127.0.0.2', {'X-Forwarded-For': '192.0.2.1'}, 'outside_classroom')]
        for peer, headers, expected in scenarios:
            transport = httpx.AsyncHTTPTransport(verify=context, local_address=peer, trust_env=False)
            async with httpx.AsyncClient(transport=transport, base_url=origin, trust_env=False) as browser:
                target = origin + PREFIX + '/lti/launch'
                opened = await browser.get(PREFIX + '/lti/login', params={
                    'iss': 'https://moodle.example', 'target_link_uri': target,
                    'login_hint': 'hint', 'lti_message_hint': 'message'})
                assert opened.status_code == 303
                oidc = httpx.URL(opened.headers['location']).params
                claims: dict[str, object] = {
                    'iss': 'https://moodle.example', 'aud': 'client', 'sub': 'student-a',
                    'iat': 990, 'exp': 1100, 'nonce': oidc['nonce'], 'name': 'Вымышленный участник',
                    LTI + 'deployment_id': 'deploy', LTI + 'context': {'id': 'context'},
                    LTI + 'resource_link': {'id': 'resource'},
                    LTI + 'roles': ['http://purl.imsglobal.org/vocab/lis/v2/membership#Learner'],
                    LTI + 'message_type': 'LtiResourceLinkRequest', LTI + 'version': '1.3.0',
                    LTI + 'target_link_uri': target}
                body = {'state': oidc['state'], 'id_token': jwt.encode(
                    {'alg': 'RS256', 'kid': platform.key.kid}, claims, platform.key, algorithms=['RS256'])}
                before = len(rows(value))
                invalid = {**body, 'id_token': 'invalid.signature.token'}
                assert (await browser.post(PREFIX + '/lti/launch', data=invalid, headers=headers)).status_code == 403
                assert len(rows(value)) == before
                response = await browser.post(PREFIX + '/lti/launch', data=body, headers=headers)
                assert response.status_code == (403 if restricted and expected != 'inside_classroom' else 303)
                assert rows(value)[-1]['evidence_status'] == expected
        recorded = rows(value)
        assert len(recorded) == 4
        assert [r['source_ip'] for r in recorded] == [CLIENT_IP, None, '127.0.0.3', '192.0.2.1']
        assert platform.calls == []
    finally:
        if nginx is not None:
            nginx.terminate()
            nginx.communicate(timeout=10)
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        listener.close()
        assert not thread.is_alive()
