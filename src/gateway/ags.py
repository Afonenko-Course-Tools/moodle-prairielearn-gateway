import json
import secrets
import time
from urllib.parse import urlsplit
import httpx
from joserfc import jwt
from joserfc.jwk import RSAKey
from .errors import GatewayError
from .launch import SCORE_SCOPE

class AGSClient:
    def __init__(self,http,registration,key,clock=time.time):
        self.http,self.registration,self.key,self.clock=http,registration,key,clock
        self.cached=None

    async def _token(self):
        now=int(self.clock())
        if self.cached and self.cached[1]>now+30: return self.cached[0]
        r=self.registration
        assertion=jwt.encode({"alg":"RS256","kid":self.key.kid},{"iss":r["clientId"],"sub":r["clientId"],"aud":r["tokenUrl"],"iat":now,"exp":now+60,"jti":secrets.token_urlsafe(32)},self.key,algorithms=["RS256"])
        response=await self.http.post(r["tokenUrl"],data={"grant_type":"client_credentials","client_assertion_type":"urn:ietf:params:oauth:client-assertion-type:jwt-bearer","client_assertion":assertion,"scope":SCORE_SCOPE},follow_redirects=False,timeout=15)
        if response.status_code!=200 or len(response.content)>65536: raise GatewayError("Moodle OAuth rejected",503)
        data=response.json()
        if not isinstance(data,dict) or not isinstance(data.get("access_token"),str) or not data["access_token"] or not isinstance(data.get("token_type"),str) or data["token_type"].lower()!="bearer" or type(data.get("expires_in")) is not int or data["expires_in"]<=0: raise GatewayError("Invalid Moodle OAuth response",503)
        self.cached=data["access_token"],now+min(data["expires_in"],3600)
        return data["access_token"]

    async def score(self,lineitem,payload,key):
        if lineitem not in {a["lineitem"] for a in self.registration["activities"].values()}: raise GatewayError("Unregistered AGS lineitem",503)
        parsed=urlsplit(lineitem)
        path=parsed.path.rstrip("/")+"/scores"
        endpoint=parsed._replace(path=path).geturl()  # Preserve Moodle's signed type_id query.
        try:
            token=await self._token()
            response=await self.http.post(endpoint,json=payload,headers={"Authorization":"Bearer "+token,"Content-Type":"application/vnd.ims.lis.v1.score+json","Idempotency-Key":key},follow_redirects=False,timeout=15)
            if response.status_code not in (200,201,204):
                if response.status_code==401: self.cached=None
                raise GatewayError("Moodle score transport rejected",503)
        except (httpx.HTTPError,ValueError,KeyError) as error: raise GatewayError("Moodle AGS unavailable",503) from error
