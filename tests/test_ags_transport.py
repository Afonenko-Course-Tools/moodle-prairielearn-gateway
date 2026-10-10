import json
import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey
from gateway.ags import AGSClient
from gateway.errors import GatewayError
from gateway.launch import SCORE_SCOPE

@pytest.mark.anyio
async def test_rs256_oauth_assertion_scoped_score_query_and_idempotent_payload():
    key=RSAKey.generate_key(parameters={"kid":"tool-synthetic"});seen=[]
    lineitem="https://moodle.example/mod/lti/services.php/1/lineitem?type_id=9"
    registration={"clientId":"tool-client","tokenUrl":"https://moodle.example/mod/lti/token.php","activities":{"defense":{"lineitem":lineitem}}}
    payload={"userId":"subject-a","scoreGiven":2,"scoreMaximum":3,"activityProgress":"Completed","gradingProgress":"FullyGraded","timestamp":"2026-10-10T12:00:00.000000+00:00"}
    def endpoint(request):
        seen.append(request)
        if request.url.path.endswith("token.php"):
            form=httpx.QueryParams(request.content.decode())
            assert form["scope"]==SCORE_SCOPE and form["grant_type"]=="client_credentials"
            signed=jwt.decode(form["client_assertion"],key,algorithms=["RS256"])
            assert signed.header=={"typ":"JWT","alg":"RS256","kid":"tool-synthetic"}
            assert signed.claims["aud"]==registration["tokenUrl"]
            assert signed.claims["iss"]==signed.claims["sub"]=="tool-client"
            assert signed.claims["exp"]-signed.claims["iat"]==60 and signed.claims["jti"]
            return httpx.Response(200,json={"access_token":"private-test-token","token_type":"Bearer","expires_in":300})
        assert request.url.path.endswith("/lineitem/scores") and request.url.params["type_id"]=="9"
        assert request.headers["authorization"]=="Bearer private-test-token"
        assert request.headers["content-type"]=="application/vnd.ims.lis.v1.score+json"
        assert request.headers["idempotency-key"]=="stable-event"
        assert json.loads(request.content)==payload
        return httpx.Response(204)
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        ags=AGSClient(http,registration,key,clock=lambda:1000)
        await ags.score(lineitem,payload,"stable-event");await ags.score(lineitem,payload,"stable-event")
        assert len(seen)==3
        with pytest.raises(GatewayError):await ags.score("https://evil.example/lineitem",payload,"bad")
        assert len(seen)==3

@pytest.mark.anyio
@pytest.mark.parametrize("response",[
    httpx.Response(302,headers={"location":"https://evil.example/token"}),
    httpx.Response(200,json={"access_token":"a","token_type":"Bearer","expires_in":True}),
    httpx.Response(200,json=[]),httpx.Response(200,content="invalid json"),
])
async def test_oauth_redirect_or_malformed_response_is_retryable(response):
    key=RSAKey.generate_key(parameters={"kid":"tool"})
    registration={"clientId":"client","tokenUrl":"https://moodle.example/token","activities":{"defense":{"lineitem":"https://moodle.example/item"}}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:response)) as http:
        with pytest.raises(GatewayError): await AGSClient(http,registration,key).score("https://moodle.example/item",{},"event")
