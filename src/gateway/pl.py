from urllib.parse import quote
import httpx
from .errors import GatewayError

class PLClient:
    def __init__(self,http,url,token):
        if len(token)<32: raise ValueError("Private bridge service token must be at least 32 characters")
        self.http,self.url,self.token=http,url.rstrip("/"),token

    async def _call(self,method,path,**kwargs):
        try:
            response=await self.http.request(method,self.url+path,headers={"Authorization":"Bearer "+self.token},follow_redirects=False,timeout=15,**kwargs)
            if response.status_code!=200 or len(response.content)>1024*1024: raise GatewayError("Private bridge response rejected",503)
            result=response.json()
            if not isinstance(result,dict): raise GatewayError("Invalid private bridge JSON",503)
            return result
        except (httpx.HTTPError,ValueError) as error: raise GatewayError("Private bridge unavailable",503) from error

    async def apply(self,assignment_id,payload):
        return await self._call("PUT","/internal/gateway/assignments/"+quote(assignment_id,safe=""),json=payload)

    async def results(self,assignment_id,version):
        return await self._call("GET","/internal/gateway/results",params={"assignmentId":assignment_id,"version":version})

    async def work_results(self,uid,instance,delivery,work):
        return await self._call("GET","/internal/gateway/work-results",params={"userUid":uid,"instance":instance,"deliveryHash":delivery,"workId":work})

    async def enroll(self,uid,instance,delivery):
        payload={"userUid":uid,"instance":instance,"deliveryHash":delivery}
        answer=await self._call("PUT","/internal/gateway/enrollment",json=payload)
        expected={"schemaVersion":1,**payload,"enrolled":True}
        if set(answer)!=set(expected) or any(answer[k]!=v or type(answer[k]) is not type(v) for k,v in expected.items()): raise GatewayError("Native enrollment evidence mismatch",503)
        return answer

    async def work_launch(self,uid,instance,delivery,work):
        answer=await self._call("GET","/internal/gateway/work-launch",params={"userUid":uid,"instance":instance,"deliveryHash":delivery,"workId":work})
        expected={"userUid":uid,"instance":instance,"deliveryHash":delivery,"workId":work}
        if set(answer)!=set(expected)|{"path"} or any(answer[k]!=v for k,v in expected.items()) or not isinstance(answer["path"],str) or not __import__("re").fullmatch(r"/pl/course_instance/[1-9][0-9]*/assessment/[1-9][0-9]*/",answer["path"]): raise GatewayError("Native launch scope mismatch",503)
        return answer["path"]
