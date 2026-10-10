import httpx
import pytest
from gateway.pl import PLClient
from gateway.errors import GatewayError

@pytest.mark.anyio
async def test_bridge_service_auth_no_user_headers_and_scoped_query():
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(200,json={"ok":True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client=PLClient(http,"http://private-pl:3001","s"*32)
        payload={"version":1,"userUid":"gw-verified","instance":"pilot","deliveryHash":"a"*64,"oldWork":None,"newWork":"defense-a"}
        await client.apply("assignment-a",payload)
        await client.results("assignment-a",1)
        assert seen[0].headers["authorization"]=="Bearer "+"s"*32
        assert "x-user-id" not in seen[0].headers and "x-gateway-user" not in seen[0].headers
        assert seen[1].url.params==httpx.QueryParams({"assignmentId":"assignment-a","version":"1"})

@pytest.mark.anyio
async def test_bridge_redirect_and_failed_enrollment_not_success():
    for status in (302,403,409,500):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(status,headers={"location":"https://evil.example"}))) as http:
            with pytest.raises(GatewayError): await PLClient(http,"http://private-pl:3001","s"*32).results("assignment-a",1)

@pytest.mark.anyio
async def test_bootstrap_enrollment_and_manifest_scoped_native_launch():
    seen=[];binding={"userUid":"gw-synthetic@example.invalid","instance":"pilot","deliveryHash":"a"*64}
    def endpoint(request):
        seen.append(request)
        if request.method=="PUT":return httpx.Response(200,json={"schemaVersion":1,**binding,"enrolled":True})
        return httpx.Response(200,json={**binding,"workId":"lab","path":"/pl/course_instance/1/assessment/9/"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as http:
        bridge=PLClient(http,"http://private-pl:3001","s"*32)
        await bridge.enroll(binding["userUid"],binding["instance"],binding["deliveryHash"])
        assert await bridge.work_launch(binding["userUid"],"pilot","a"*64,"lab")=="/pl/course_instance/1/assessment/9/"
        assert seen[0].url.path=="/internal/gateway/enrollment"
        assert dict(seen[1].url.params)=={**binding,"workId":"lab"}

@pytest.mark.anyio
@pytest.mark.parametrize("extra",[{"path":"//evil.example/"},{"path":"/pl/course_instance/1/assessment/9/?role=Administrator"},{"userUid":"foreign"},{"role":"Administrator"}])
async def test_native_launch_response_cannot_change_user_or_add_role(extra):
    value={"userUid":"gw-synthetic@example.invalid","instance":"pilot","deliveryHash":"a"*64,"workId":"lab","path":"/pl/course_instance/1/assessment/9/",**extra}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=value))) as http:
        with pytest.raises(GatewayError):await PLClient(http,"http://private-pl:3001","s"*32).work_launch("gw-synthetic@example.invalid","pilot","a"*64,"lab")

@pytest.mark.anyio
@pytest.mark.parametrize("extra",[{"enrolled":1},{"schemaVersion":True},{"role":"Student"}])
async def test_native_enrollment_requires_closed_typed_evidence(extra):
    answer={"schemaVersion":1,"userUid":"gw-synthetic@example.invalid","instance":"pilot","deliveryHash":"a"*64,"enrolled":True,**extra}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json=answer))) as http:
        with pytest.raises(GatewayError):await PLClient(http,"http://private-pl:3001","s"*32).enroll("gw-synthetic@example.invalid","pilot","a"*64)
