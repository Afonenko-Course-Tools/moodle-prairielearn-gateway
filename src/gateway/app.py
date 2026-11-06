"""Одноразовый адаптер Moodle LTI → private native PL login."""
import base64
from contextlib import asynccontextmanager
import re
from pathlib import Path
import secrets
import ssl
import time
from urllib.parse import urlencode, urlsplit
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from .config import Configuration
from .store import Store
from .launch import LaunchService, LaunchError
from .errors import GatewayError
from .pl import PLClient

def verified_name_header(name):
    """ASCII wire-format подтверждённого имени без изменения signed identity."""
    if not isinstance(name, str) or len(name) > 256 or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in name):
        raise LaunchError("Некорректное подтверждённое имя Gateway")
    data = name.encode("utf-8", errors="strict")
    if len(data) > 1024:
        raise LaunchError("Некорректное подтверждённое имя Gateway")
    return "v1." + base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

def secret(path):
    value=Path(path).read_text().strip()
    if len(value)<32 or any(ord(c)<33 or ord(c)>126 for c in value): raise ValueError("Private service secret должен содержать не менее 32 печатных ASCII символов")
    return value

class Clients:
    def __init__(self, config):
        self.registration = config.registration.model_dump()
        bridge_token = secret(config.bridge.tokenFile)
        tls = ssl.create_default_context(cafile=config.caFile)
        self.http = httpx.AsyncClient(verify=tls, follow_redirects=False, timeout=15, limits=httpx.Limits(max_connections=8))
        self.bridge = PLClient(self.http, config.bridge.url, bridge_token)

    async def jwks(self):
        try:
            mapping = self.registration.get('privateJwksUrl')
            headers = {'Host': urlsplit(self.registration['issuer']).netloc, 'X-Forwarded-Proto': 'https'} if mapping else {}
            response = await self.http.get(mapping or self.registration['jwksUrl'], headers=headers, follow_redirects=False, timeout=15)
            if response.status_code != 200 or len(response.content) > 65536:
                raise LaunchError('Moodle JWKS недоступен')
            value = response.json()
            if (not isinstance(value, dict) or set(value) != {'keys'} or not isinstance(value['keys'], list)
                    or not 1 <= len(value['keys']) <= 32):
                raise LaunchError('Некорректный Moodle JWKS')
            return value
        except (httpx.HTTPError, ValueError) as error:
            raise LaunchError('Moodle JWKS недоступен') from error


def browser_nonce(request):
    # Разбор parser winner недопустим при дубликатах из разных cookie scope.
    values = []
    for header in request.headers.getlist('cookie'):
        for part in header.split(';'):
            name, separator, value = part.strip().partition('=')
            if name == 'GatewayLogin':
                if not separator:
                    raise LaunchError('Некорректный browser credential')
                values.append(value)
    if len(values) > 1 or values and not re.fullmatch(r'[A-Za-z0-9_-]{43}', values[0]):
        raise LaunchError('Неоднозначный browser credential; повторите вход из Moodle')
    return values[0] if values else None


def create_app(config, *, store=None, clients=None, clock=time.time):
    if not isinstance(config, Configuration):
        config = Configuration.model_validate(config)
    store = store or Store(config.database)
    own_clients = clients is None
    handoff_secret = secret(config.handoffTokenFile) if own_clients else None
    clients = clients or Clients(config)
    registration = config.registration.model_dump()
    registration['targetLinkUri'] = config.publicOrigin.rstrip('/') + config.routePrefix + '/lti/launch'
    launch = LaunchService(store, registration, clock, course_key=config.courseKey, instance=config.instance, prefix=config.routePrefix)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if own_clients:
                await clients.http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.handoff_secret = handoff_secret

    @app.exception_handler(GatewayError)
    async def rejected(request, error):
        return JSONResponse({'detail': str(error)}, status_code=error.status, headers={'Cache-Control': 'no-store'})

    @app.middleware('http')
    async def security(request, call_next):
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer'})
        return response

    @app.api_route(config.routePrefix + '/lti/login', methods=['GET', 'POST'])
    async def login(request: Request):
        binding = browser_nonce(request) or secrets.token_urlsafe(32)
        params = dict(await request.form()) if request.method == 'POST' else dict(request.query_params)
        allowed = {'iss', 'client_id', 'target_link_uri', 'login_hint', 'lti_message_hint', 'lti_deployment_id'}
        if set(params) - allowed or params.get('lti_deployment_id', registration['deploymentId']) != registration['deploymentId']:
            raise LaunchError('Отклонены поля входа')
        pending = launch.begin(params.get('iss'), params.get('client_id'), params.get('target_link_uri'), params.get('login_hint'), params.get('lti_message_hint'), binding)
        query = {'scope': 'openid', 'response_type': 'id_token', 'response_mode': 'form_post', 'prompt': 'none',
                 'client_id': registration['clientId'], 'redirect_uri': registration['targetLinkUri'],
                 'login_hint': params['login_hint'], 'lti_message_hint': params['lti_message_hint'], **pending}
        response = RedirectResponse(registration['authUrl'] + '?' + urlencode(query), status_code=303)
        response.set_cookie('GatewayLogin', binding, secure=True, httponly=True, samesite='none', path='/gateway', max_age=300)
        return response

    app.state.login_handler = login

    @app.post(config.routePrefix + '/lti/launch')
    async def verified_launch(request: Request):
        binding = browser_nonce(request)
        if not binding or len(await request.body()) > 65536:
            raise LaunchError('Отсутствует browser credential или превышен размер launch')
        params = dict(await request.form())
        if set(params) != {'state', 'id_token'}:
            raise LaunchError('Отклонены поля launch')
        flow, _ = launch.verify(params['state'], binding, params['id_token'], await clients.jwks())
        return RedirectResponse(config.routePrefix + '/pl/' + flow, status_code=303)

    @app.get('/internal/handoff' + config.routePrefix.removeprefix('/gateway') + '/{flow}')
    async def consume(flow: str, request: Request):
        binding = browser_nonce(request)
        if not binding:
            raise LaunchError('Требуется browser credential')
        expected = 'Bearer ' + (app.state.handoff_secret or secrets.token_urlsafe(32))
        if not secrets.compare_digest(request.headers.get('authorization', ''), expected):
            raise LaunchError('Требуется private service authentication')
        value = store.consume_handoff(flow, binding, int(clock()))
        name = verified_name_header(value['displayName'])
        return JSONResponse(value, headers={'X-Verified-Uid': value['uid'], 'X-Verified-Name-UTF8-B64': name, 'X-Verified-Target': value['target']})

    @app.get(config.routePrefix + '/bootstrap/{flow}')
    async def bootstrap(flow: str, request: Request):
        binding = browser_nonce(request)
        if not binding:
            raise LaunchError('Требуется новый переход из Moodle')
        who = store.consume_bootstrap(flow, binding, int(clock()), registration, config.courseKey, config.instance, config.routePrefix + '/bootstrap/' + flow)
        answer = await clients.bridge.enroll(who.uid, who.instance, who.courseKey, role=who.role, email=who.email)
        return RedirectResponse(answer['path'], status_code=303)

    return app
