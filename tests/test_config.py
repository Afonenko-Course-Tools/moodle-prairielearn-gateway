import pytest
from gateway.config import Configuration

def example():
    return {"routePrefix":"/gateway/courses/demo","publicOrigin":"https://course.example","database":"/private/runtime.sqlite","privateKeyFile":"/private/tool-key.json","handoffTokenFile":"/private/handoff-token","bridge":{"url":"http://private-pl:3001","tokenFile":"/private/bridge-token"},"deliveryPath":"/private/delivery","instance":"pilot","registration":{"issuer":"https://moodle.example","clientId":"client","deploymentId":"deploy","contextId":"context","authUrl":"https://moodle.example/auth","jwksUrl":"https://moodle.example/certs","tokenUrl":"https://moodle.example/token","activities":{"defense":{"kind":"defense","resourceLinkId":"resource","lineitem":"https://moodle.example/lineitems/defense"}}},"flow":{"mode":"immediate","capacity":{}}}

def test_closed_config_default_capacity_unlimited():
    config=Configuration.model_validate(example())
    assert config.pollSeconds==15 and config.flow.capacity=={}

@pytest.mark.parametrize("mutate",[
    lambda c:c.update(admin=True),lambda c:c["bridge"].update(userUid="supplied"),
    lambda c:c["flow"].update(capacity={"essay":-1}),lambda c:c.update(publicOrigin="http://public.example"),
    lambda c:c["registration"].update(jwksUrl="https://evil.example"),
])
def test_unknown_unsafe_config_rejected(mutate):
    value=example();mutate(value)
    with pytest.raises(ValueError):Configuration.model_validate(value)

@pytest.mark.parametrize("mapped",["http://evil.example/certs","http://moodle:3001/certs","http://moodle/certs?secret=1","http://moodle/private","http://user@moodle/certs"])
def test_jwks_transport_mapping_cannot_change_identity_or_origin(mapped):
    value=example();value["registration"]["privateJwksUrl"]=mapped
    with pytest.raises(ValueError):Configuration.model_validate(value)

def test_fixed_private_jwks_transport_keeps_https_signed_issuer():
    value=example();value["registration"]["privateJwksUrl"]="http://moodle/certs"
    config=Configuration.model_validate(value)
    assert config.registration.issuer=="https://moodle.example"
