from collections.abc import Callable
from payloads import Payload, mutations
"""Конфигурация принимает только зависимости auth-only приложения."""
import copy
import json
from pathlib import Path
import pytest
from gateway.config import Configuration
from gateway.courses import CoursesConfiguration, create_courses_app
from gateway.app import GatewayApplication, create_app
from test_auth_flow import handoff_value, configuration, Platform, browser_for, begin, handoff, PREFIX


def test_configuration_needs_no_tool_signing_key_and_closed_old_fields(tmp_path: Path) -> None:
    cfg=Configuration.model_validate(configuration(tmp_path))
    assert cfg.courseKey=='demo'
    for name,value in [('mode','login-only'),('privateKeyFile','/private/old'),('deliveryPath','/private/old'),
                       ('flow',{'mode':'immediate'}),('pollSeconds',15),('administratorSubject','owner')]:
        with pytest.raises(ValueError):Configuration.model_validate({**configuration(tmp_path),name:value})


@pytest.mark.parametrize('mutate',mutations([
    lambda c:c.update(courseKey=None),lambda c:c.update(courseKey='../demo'),lambda c:c.pop('courseKey'),
    lambda c:c.update(database='relative.sqlite'),lambda c:c.update(caFile='relative.crt'),
    lambda c:c.update(publicOrigin='http://course.example'),lambda c:c.update(publicOrigin='https://course.example/path'),
    lambda c:c.update(routePrefix='/gateway/other'),
    lambda c:c.child('bridge').update(url='http://prairielearn:3001/path'),
    lambda c:c.child('bridge').update(url='http://user:password@prairielearn:3001'),
    lambda c:c.child('registration').update(tokenUrl='https://moodle.example/token'),
    lambda c:c.child('registration').child('activities').child('catalog').update(kind='catalog'),
    lambda c:c.child('registration').child('activities').child('catalog').update(lineitem='https://moodle.example/score'),
    lambda c:c.child('registration').update(authUrl='https://evil.example/auth'),
    lambda c:c.child('registration').update(privateJwksUrl='http://evil.example/certs'),
    lambda c:c.child('registration').update(privateJwksUrl='http://moodle:81/certs'),
    lambda c:c.child('registration').update(privateJwksUrl='http://moodle/certs?secret=x'),
    lambda c:c.child('registration').child('activities').update(duplicate={'resourceLinkId':'resource'}),
]))
def test_unsafe_or_obsolete_configuration_rejected(tmp_path: Path,mutate: Callable[[Payload], object]) -> None:
    value=configuration(tmp_path);mutate(value)
    with pytest.raises(ValueError):Configuration.model_validate(value)


def test_fixed_private_jwks_transport_keeps_public_identity(tmp_path: Path) -> None:
    value=configuration(tmp_path);value.child('registration')['privateJwksUrl']='http://moodle/certs'
    assert Configuration.model_validate(value).registration.issuer=='https://moodle.example'


def named(tmp_path: Path) -> Payload:
    first=configuration(tmp_path)
    second=copy.deepcopy(first)
    second.update(routePrefix='/gateway/courses/other',database=str(tmp_path/'other.sqlite'),courseKey='other')
    second.child('registration').child('activities').child('catalog')['resourceLinkId']='other-resource'
    return Payload({'schemaVersion':2,'courses':{'demo':first,'other':second}})


@pytest.mark.parametrize('mutate',mutations([
    lambda c:c.child('other').update(database=c.child('demo')['database']),
    lambda c:c.child('other').update(courseKey='demo'),
    lambda c:c.child('other').update(routePrefix='/gateway/courses/wrong'),
    lambda c:c.child('other').update(publicOrigin='https://other.example'),
    lambda c:c.child('other').update(handoffTokenFile='/private/different'),
    lambda c:c.child('other').child('registration').child('activities').child('catalog').update(resourceLinkId='resource'),
]))
def test_named_courses_reject_ambiguous_scope(tmp_path: Path,mutate: Callable[[Payload], object]) -> None:
    value=named(tmp_path);mutate(value.child('courses'))
    with pytest.raises(ValueError):CoursesConfiguration.model_validate(value)


@pytest.mark.anyio
async def test_shared_root_and_nested_routes_keep_same_cookie_and_independent_course_attempts(tmp_path: Path) -> None:
    value=named(tmp_path);platform=Platform()
    # Разные resource id остаются подписанными; одна identity Moodle может входить в оба курса.
    def factory(cfg: Configuration) -> GatewayApplication:
        app=create_app(cfg,clients=platform,clock=lambda:1000);app.state.handoff_secret='h'*32
        return app
    app=create_courses_app(value,factory=factory)
    async with browser_for(app) as browser:
        opened,first=await begin(browser,platform,login_path='/gateway/lti/login')
        credential=browser.cookies['GatewayLogin']
        assert 'Path=/gateway;' in opened.headers['set-cookie']
        opened,second=await begin(browser,platform,prefix='/gateway/courses/other')
        from gateway.launch import LTI
        from joserfc import jwt
        claims=Payload.validated(jwt.decode(second['id_token'],platform.key,algorithms=['RS256']).claims)
        claims[LTI+'resource_link']={'id':'other-resource'}
        second['id_token']=jwt.encode({'alg':'RS256','kid':platform.key.kid},claims,platform.key,algorithms=['RS256'])
        assert browser.cookies['GatewayLogin']==credential and len(list(browser.cookies.jar))==1
        for prefix,body in [(PREFIX,first),('/gateway/courses/other',second)]:
            launched=await browser.post(prefix+'/lti/launch',data=body)
            assert launched.status_code==303 and 'set-cookie' not in launched.headers
            flow=launched.headers['location'].rsplit('/',1)[1]
            private=await handoff(browser,flow,prefix)
            assert (await browser.get(handoff_value(private)['target'])).status_code==303
        assert [row[2] for row in platform.calls]==['demo','other']
        assert (await browser.get('/gateway/')).status_code==404
        assert (await browser.get('/gateway/lti/login',params={'target_link_uri':'https://evil.example'})).status_code==403
        bad=await browser.get('/gateway/lti/login',params={'target_link_uri':'https://course.example'+PREFIX+'/lti/launch'},headers={'Cookie':'GatewayLogin='+credential+'; GatewayLogin='+credential})
        assert bad.status_code==403 and 'set-cookie' not in bad.headers


@pytest.mark.anyio
async def test_real_composition_loads_only_service_tokens_and_no_private_signing_key(tmp_path: Path) -> None:
    value=configuration(tmp_path)
    for field in ('handoffTokenFile',):
        path=tmp_path/field;path.write_text('h'*32);value[field]=str(path)
    token=tmp_path/'bridge';token.write_text('b'*32);value.child('bridge')['tokenFile']=str(token)
    app=create_app(value)
    async with app.router.lifespan_context(app):
        async with browser_for(app) as browser:
            assert (await browser.get(PREFIX+'/.well-known/jwks.json')).status_code==404


def test_example_configuration_validates() -> None:
    config=CoursesConfiguration.model_validate(json.loads(Path('config/courses.example.json').read_text()))
    assert len(config.courses)==2
