import copy
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from gateway.launch import LaunchService, LaunchError, LTI, LEARNER, INSTRUCTOR
from gateway.store import Store

@pytest.fixture
def env(tmp_path):
    key = RSAKey.generate_key(parameters={"kid": "moodle-test"})
    registration = {"issuer": "https://moodle.example", "clientId": "client", "deploymentId": "deploy", "contextId": "context", "targetLinkUri": "https://course.example/gateway/lti/launch", "activities": {"defense": {"resourceLinkId": "resource", "lineitem": "https://moodle.example/lineitems/defense"}}}
    store = Store(tmp_path / "runtime.sqlite")
    return store, LaunchService(store, registration, clock=lambda: 1000), key

def attempt(env, mutate=None, state=None, binding="browser"):
    store, service, key = env
    pending = state or service.begin("https://moodle.example", "client", "https://course.example/gateway/lti/launch", "hint", "message", binding)
    claims = {"iss": "https://moodle.example", "aud": "client", "sub": "student-a", "iat": 990, "exp": 1100, "nonce": pending["nonce"], LTI+"deployment_id": "deploy", LTI+"context": {"id": "context"}, LTI+"resource_link": {"id": "resource"}, LTI+"roles": [LEARNER], LTI+"message_type": "LtiResourceLinkRequest", LTI+"version": "1.3.0", LTI+"target_link_uri": "https://course.example/gateway/lti/launch", "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint": {"lineitem": "https://moodle.example/lineitems/defense", "scope": ["https://purl.imsglobal.org/spec/lti-ags/scope/score"]}}
    if mutate: mutate(claims)
    token = jwt.encode({"alg": "RS256", "kid": "moodle-test"}, claims, key, algorithms=["RS256"])
    return service.verify(pending["state"], binding, token, {"keys": [key.as_dict(private=False)]}), pending, token

def test_launch_replay_rejected(env):
    session, pending, token = attempt(env)
    assert env[0].session(session["token"], now=1000)["subject"] == "student-a"
    with pytest.raises(LaunchError): env[1].verify(pending["state"], "browser", token, {"keys": [env[2].as_dict(private=False)]})

@pytest.mark.parametrize('offset',[1,5])
def test_small_issuer_clock_skew_does_not_expand_expiry(env,offset):
    session,_,_=attempt(env,lambda c:c.update(iat=1000+offset))
    assert session['role']=='student'

def test_exact_expiry_and_excessive_issuer_skew_rejected(env):
    for changes in [{'exp':1000},{'iat':1006}]:
        with pytest.raises(LaunchError): attempt(env,lambda c:c.update(**changes))

@pytest.mark.parametrize("mutate", [
    lambda c: c.update(iss="https://evil.example"), lambda c: c.update(aud="other"),
    lambda c: c.update(aud=["client", "other"], azp="other"),
    lambda c: c.update(exp=900), lambda c: c.update(iat=1010), lambda c: c.update(iat=500),
    lambda c: c.update(nonce="wrong"), lambda c: c.update(sub=""),
    lambda c: c.update({LTI+"deployment_id": "other"}), lambda c: c.update({LTI+"context": {"id": "other"}}),
    lambda c: c.update({LTI+"resource_link": {"id": "other"}}), lambda c: c.update({LTI+"roles": ["Administrator"]}),
    lambda c: c.update({LTI+"roles": []}), lambda c: c.update({LTI+"target_link_uri": "https://evil.example"}),
    lambda c: c.update({LTI+"message_type": "LtiDeepLinkingRequest"}),
    lambda c: c.update({"https://purl.imsglobal.org/spec/lti-ags/claim/endpoint": {"lineitem": "https://evil.example/score", "scope": []}}),
])
def test_wrong_context_rejected(env, mutate):
    with pytest.raises(LaunchError): attempt(env, mutate)

def test_browser_binding_and_unknown_signature_rejected(env):
    service = env[1]
    pending = service.begin("https://moodle.example", "client", "https://course.example/gateway/lti/launch", "hint", "message", "a")
    with pytest.raises(LaunchError): attempt(env, state=pending, binding="b")
    session, pending, token = attempt(env)
    other = RSAKey.generate_key(parameters={"kid": "moodle-test"})
    fresh = service.begin("https://moodle.example", "client", "https://course.example/gateway/lti/launch", "hint", "message", "browser")
    with pytest.raises(LaunchError): service.verify(fresh["state"], "browser", token, {"keys": [other.as_dict(private=False)]})

def test_teacher_role_does_not_grant_pl_admin(env):
    session, _, _ = attempt(env, lambda c: c.update({LTI+"roles": [INSTRUCTOR]}))
    assert session["role"] == "teacher" and "admin" not in session
    assert session["uid"].startswith("gw-") and session["uid"] != "student-a"

def test_nonce_session_handoff_persist_restart_and_one_use(env):
    session, _, _ = attempt(env)
    reopened = Store(env[0].path)
    assert reopened.session(session["token"], now=1000)["uid"] == session["uid"]
    token = reopened.handoff(session["token"], "/gateway/bootstrap", now=1000)
    assert Store(reopened.path).consume_handoff(token, now=1001)["uid"] == session["uid"]
    with pytest.raises(LaunchError): reopened.consume_handoff(token, now=1002)
    expired = reopened.handoff(session["token"], "/gateway/bootstrap", now=1000)
    with pytest.raises(LaunchError): reopened.consume_handoff(expired, now=1061)

def test_identity_tuple_and_missing_enrollment(env):
    session, _, _ = attempt(env)
    other, _, _ = attempt(env, lambda c: c.update(sub="student-b"))
    assert session["uid"] != other["uid"]
    with pytest.raises(LaunchError): attempt(env, lambda c: c.pop(LTI+"roles"))

@pytest.mark.parametrize("target",["/pl/","/pl/administrator/courses","//evil.example/","/gateway/bootstrap?next=evil","/pl/course_instance/1/assessment/2/\r\nX-Test: injected"])
def test_handoff_rejects_unscoped_or_control_targets(env,target):
    session,_,_=attempt(env)
    with pytest.raises(LaunchError):env[0].handoff(session["token"],target,1000)
