"""Подписанная identity сохраняется; имя идёт только как canonical UTF-8 header."""
import base64
import httpx
import pytest
from gateway.app import create_app
from gateway.config import Configuration
from gateway.store import digest
from test_launch import env, attempt
from test_config import example
from test_assignment import Bridge, delivery


def app_for(env):
    value = example()
    value['database'] = str(env[0].path)
    class Clients:
        bridge = Bridge()
        async def jwks(self): return {'keys': []}
        class AGS:
            async def score(self, *args): pass
        ags = AGS()
    app = create_app(Configuration.model_validate(value), store=env[0], delivery=delivery(), clients=Clients(), clock=lambda: 1000, start_worker=False)
    app.state.handoff_secret = 's' * 32
    return app


def wire(name):
    return 'v1.' + base64.urlsafe_b64encode(name.encode('utf-8', errors='strict')).decode('ascii').rstrip('=')


@pytest.mark.anyio
@pytest.mark.parametrize('name', [
    pytest.param('Тестовый участник', id='cyrillic'),
    pytest.param('ASCII Name', id='ascii'),
    pytest.param('e\u0301 / é', id='combining-not-normalized'),
    pytest.param("O'Example <text> %2F + &", id='literal-punctuation'),
    pytest.param('Name 🧪', id='non-bmp'),
    pytest.param('🧪' * 256, id='utf8-max'),
    pytest.param('', id='existing-empty-policy'),
])
async def test_signed_name_survives_private_ascii_wire(env, name):
    app = app_for(env)
    session, _, _ = attempt(env, lambda c: c.update(name=name))
    token = env[0].handoff(session['token'], '/gateway/courses/demo/bootstrap', 1000)
    path = '/internal/handoff/courses/demo/' + token
    forged = [('Authorization', 'Bearer ' + 's' * 32), ('X-Trust-Auth-Uid', 'forged@example.invalid'), ('X-Trust-Auth-Name-UTF8-B64', wire('forged')), ('x-trust-auth-name-utf8-b64', wire('other'))]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://course.example') as client:
        assert (await client.get(path)).status_code == 403
        result = await client.get(path, headers=forged)
        assert result.status_code == 200
        assert result.headers['x-verified-name-utf8-b64'] == wire(name)
        assert 'x-verified-name' not in result.headers
        assert result.json()['displayName'] == name
        assert result.headers['x-verified-uid'] == 'gw-' + digest(['https://moodle.example', 'client', 'deploy', 'student-a']) + '@example.invalid'
        assert all(ord(c) < 128 for c in result.headers['x-verified-name-utf8-b64'])
        assert len(result.headers['x-verified-name-utf8-b64']) <= 1369
        assert (await client.get(path, headers=forged)).status_code == 403


@pytest.mark.anyio
async def test_same_names_different_subjects_and_relogin_keep_identity(env):
    app = app_for(env)
    sessions = [attempt(env, lambda c, sub=sub: c.update(name='Одинаковое тестовое имя', sub=sub))[0] for sub in ['student-a', 'student-b']]
    assert sessions[0]['uid'] != sessions[1]['uid']
    before = [(s['userId'], s['uid']) for s in sessions]
    updated = attempt(env, lambda c: c.update(name='Новое тестовое имя', sub='student-a'))[0]
    assert (updated['userId'], updated['uid']) == before[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://course.example') as client:
        for session, expected in [(updated, 'Новое тестовое имя'), (sessions[1], 'Одинаковое тестовое имя')]:
            token = env[0].handoff(session['token'], '/gateway/courses/demo/bootstrap', 1000)
            result = await client.get('/internal/handoff/courses/demo/' + token, headers={'Authorization': 'Bearer ' + 's' * 32})
            assert result.headers['x-verified-name-utf8-b64'] == wire(expected)
            assert result.headers['x-verified-uid'] == session['uid']
    assert [(env[0].session(s['token'], 1000)['userId'], env[0].session(s['token'], 1000)['uid']) for s in sessions] == before


@pytest.mark.anyio
@pytest.mark.parametrize('invalid', [None, 123, 'x' * 257, 'line\nbreak'])
async def test_existing_invalid_claim_fallback_uses_same_encoding(env, invalid):
    app = app_for(env)
    session = attempt(env, lambda c: c.update(name=invalid))[0]
    token = env[0].handoff(session['token'], '/gateway/courses/demo/bootstrap', 1000)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://course.example') as client:
        result = await client.get('/internal/handoff/courses/demo/' + token, headers={'Authorization': 'Bearer ' + 's' * 32})
        assert result.headers['x-verified-name-utf8-b64'] == wire('Moodle participant')


@pytest.mark.parametrize('invalid', [None, 123, 'line\nbreak', '\ud800', '\udfff', '🧪' * 257])
def test_encoder_rejects_non_scalar_or_invalid_store_name(invalid):
    from gateway.app import verified_name_header
    from gateway.errors import LaunchError
    with pytest.raises(LaunchError):
        verified_name_header(invalid)
