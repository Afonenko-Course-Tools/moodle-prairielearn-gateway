"""Подписанный LTI, закрытый handoff и пассивная доставка ExamLab."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from gateway.app import create_app
from gateway.config import Configuration
from gateway.errors import GatewayError
from gateway.launch import INSTRUCTOR, LEARNER
from payloads import Payload
from test_auth_flow import PREFIX, UID, Platform, activate, browser_for, configuration, handoff_value


TOKEN = '9' * 64
CERT_ID = 'a' * 64 + ':0A'
EXAM_ID = 'synthetic-exam'
EXAM_ORIGIN = 'https://course.example:24443'
RECEIVER = 'http://controller:8088/exam/internal/lti-binding'
NAME = 'Тестовый Участник'


def configured(tmp_path: Path) -> Payload:
    value = configuration(tmp_path)
    binding_token = tmp_path / 'binding-token'
    binding_token.write_text(TOKEN + '\n')
    binding_token.chmod(0o600)
    proxy_token = tmp_path / 'proxy-token'
    proxy_token.write_text('p' * 32)
    proxy_token.chmod(0o600)
    value.update(examAudit={'examId': EXAM_ID, 'activities': ['catalog'],
                            'database': str(tmp_path / 'exam.sqlite'), 'proxyTokenFile': str(proxy_token),
                            'classroomCidrs': ['10.0.0.0/8']},
                 examBinding={'controllerOrigin': 'http://controller:8088', 'tokenFile': str(binding_token),
                              'handoffOrigin': EXAM_ORIGIN})
    return value


async def private_handoff(browser: httpx.AsyncClient, flow: str,
                          proofs: tuple[str, ...] = (CERT_ID,), auth: str = 'h' * 32) -> httpx.Response:
    headers = [('Authorization', 'Bearer ' + auth), ('Cookie', 'GatewayLogin=' + browser.cookies['GatewayLogin'])]
    headers.extend(('X-Exam-Client-Cert-Id', value) for value in proofs)
    return await browser.get('/internal/handoff/courses/demo/' + flow, headers=headers)


@pytest.mark.anyio
async def test_reports_only_after_enrollment_and_one_shot_handoff(tmp_path: Path) -> None:
    platform = Platform()
    sent: list[httpx.Request] = []

    async def receiver(req: httpx.Request) -> httpx.Response:
        assert len(platform.calls) == 1
        sent.append(req)
        return httpx.Response(204)

    app = create_app(configured(tmp_path), clients=platform, clock=lambda: 1000,
                     binding_transport=httpx.MockTransport(receiver))
    app.state.handoff_secret = 'h' * 32
    async with app.router.lifespan_context(app), browser_for(app) as browser:
        _, launch_response, flow = await activate(browser, platform, name=NAME)
        assert launch_response.headers['location'] == EXAM_ORIGIN + PREFIX + '/pl/' + flow
        assert (await private_handoff(browser, flow, auth='forged')).status_code == 403
        handoff = await private_handoff(browser, flow)
        assert handoff.status_code == 200 and not sent
        assert CERT_ID not in handoff.text and 'X-Exam-Client-Cert-Id' not in handoff.headers
        target = handoff_value(handoff)['target']
        bootstrap = await browser.get(target, headers={'X-Exam-Client-Cert-Id': 'c' * 64 + ':66'})
        assert bootstrap.status_code == 303 and bootstrap.headers['location'] == '/pl/course_instance/42/assessments'
        assert (await private_handoff(browser, flow)).status_code == 403
        assert (await browser.get(target)).status_code == 403
    assert len(sent) == 1
    req = sent[0]
    assert req.method == 'POST' and str(req.url) == RECEIVER
    assert req.headers['authorization'] == 'Bearer ' + TOKEN
    assert req.headers['content-type'] == 'application/json'
    assert json.loads(req.content) == {'examId': EXAM_ID, 'certId': CERT_ID, 'uid': UID, 'student': NAME}


@pytest.mark.anyio
@pytest.mark.parametrize('case', ['unscoped', 'teacher', 'blank_name', 'long_name', 'format_name',
                                 'missing', 'duplicate', 'malformed', 'bridge_failure'])
async def test_no_report_without_proof_scope_and_successful_bridge(tmp_path: Path, case: str) -> None:
    platform = Platform()
    value = configured(tmp_path)
    if case == 'unscoped':
        value.child('registration').child('activities')['other'] = {'resourceLinkId': 'other'}
        value.child('examAudit')['activities'] = ['other']
    if case == 'bridge_failure':
        platform.failure = GatewayError('Отказ тестового Bridge', 503)

    def reject_call(request: httpx.Request) -> httpx.Response:
        pytest.fail('Доставка должна быть пропущена')

    app = create_app(value, clients=platform, clock=lambda: 1000,
                     binding_transport=httpx.MockTransport(reject_call))
    app.state.handoff_secret = 'h' * 32
    async with app.router.lifespan_context(app), browser_for(app) as browser:
        role = INSTRUCTOR if case == 'teacher' else LEARNER
        names = {'blank_name': ' ', 'long_name': 'А' * 101, 'format_name': 'А\u200dБ'}
        _, launch_response, flow = await activate(browser, platform, role=role, name=names.get(case, NAME))
        prefix = '' if case in {'unscoped', 'teacher'} or case in names else EXAM_ORIGIN
        assert launch_response.headers['location'] == prefix + PREFIX + '/pl/' + flow
        proofs = {'missing': (), 'duplicate': (CERT_ID, CERT_ID), 'malformed': ('b' * 64 + ':ABC',)}.get(case, (CERT_ID,))
        handoff = await private_handoff(browser, flow, proofs)
        assert handoff.status_code == 200
        bootstrap = await browser.get(handoff_value(handoff)['target'], headers={'X-Exam-Client-Cert-Id': CERT_ID})
        assert bootstrap.status_code == (503 if case == 'bridge_failure' else 303)


@pytest.mark.anyio
@pytest.mark.parametrize('result', ['redirect', 'timeout', 'unavailable'])
async def test_report_failure_preserves_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: str) -> None:
    platform = Platform()
    sent: list[httpx.Request] = []
    monkeypatch.setattr('gateway.binding.TIMEOUT', 0.03)

    async def receiver(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        if result == 'timeout':
            await asyncio.sleep(10)
        if result == 'unavailable':
            raise httpx.ConnectError('Синтетический отказ', request=req)
        return httpx.Response(307, headers={'Location': 'https://other.invalid'})

    app = create_app(configured(tmp_path), clients=platform, clock=lambda: 1000,
                     binding_transport=httpx.MockTransport(receiver))
    app.state.handoff_secret = 'h' * 32
    async with app.router.lifespan_context(app), browser_for(app) as browser:
        _, _, flow = await activate(browser, platform)
        handoff = await private_handoff(browser, flow)
        bootstrap = await browser.get(handoff_value(handoff)['target'])
        assert bootstrap.status_code == 303 and bootstrap.headers['location'] == '/pl/course_instance/42/assessments'
    assert len(sent) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['exam', 'activity', 'disabled'])
async def test_bootstrap_after_scope_change_preserves_enrollment_only(tmp_path: Path, change: str) -> None:
    platform = Platform()
    value = configured(tmp_path)
    value.child('registration').child('activities')['other'] = {'resourceLinkId': 'other'}

    def reject_call(request: httpx.Request) -> httpx.Response:
        pytest.fail('Доставка устаревшей привязки должна быть пропущена')

    transport = httpx.MockTransport(reject_call)
    before = create_app(value, clients=platform, clock=lambda: 1000, binding_transport=transport)
    before.state.handoff_secret = 'h' * 32
    async with before.router.lifespan_context(before), browser_for(before) as browser:
        _, _, flow = await activate(browser, platform)
        handoff = await private_handoff(browser, flow)
        assert handoff.status_code == 200
        target = handoff_value(handoff)['target']
        cookies = browser.cookies
    if change == 'exam':
        value.child('examAudit')['examId'] = 'another-exam'
    elif change == 'activity':
        value.child('examAudit')['activities'] = ['other']
    else:
        value.pop('examBinding')
    after = create_app(value, clients=platform, clock=lambda: 1001, binding_transport=transport)
    async with after.router.lifespan_context(after), browser_for(after) as browser:
        browser.cookies.update(cookies)
        response = await browser.get(target)
        assert response.status_code == 303 and response.headers['location'] == '/pl/course_instance/42/assessments'
    assert len(platform.calls) == 1


@pytest.mark.anyio
async def test_unavailable_token_keeps_regular_lti(tmp_path: Path) -> None:
    platform = Platform()
    value = configured(tmp_path)
    (tmp_path / 'binding-token').chmod(0o644)
    app = create_app(value, clients=platform, clock=lambda: 1000)
    app.state.handoff_secret = 'h' * 32
    async with app.router.lifespan_context(app), browser_for(app) as browser:
        _, launch_response, flow = await activate(browser, platform)
        assert launch_response.headers['location'] == PREFIX + '/pl/' + flow
        handoff = await private_handoff(browser, flow)
        assert handoff.status_code == 200
        assert (await browser.get(handoff_value(handoff)['target'])).status_code == 303


@pytest.mark.parametrize('origin', ['http://course.example:24443', 'https://other.invalid:24443',
                                  'https://course.example:443', 'https://course.example:24443/path'])
def test_handoff_origin_requires_same_host_different_https_port(tmp_path: Path, origin: str) -> None:
    value = configured(tmp_path)
    value.child('examBinding')['handoffOrigin'] = origin
    with pytest.raises(ValueError):
        Configuration.model_validate(value)
