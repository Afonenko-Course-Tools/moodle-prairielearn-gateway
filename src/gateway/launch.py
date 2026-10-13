import time
from joserfc import jwt
from joserfc.jwk import KeySet
from .errors import LaunchError
from .store import digest

LTI = "https://purl.imsglobal.org/spec/lti/claim/"
LEARNER = "http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"
INSTRUCTOR = "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
AGS = "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"
SCORE_SCOPE = "https://purl.imsglobal.org/spec/lti-ags/scope/score"

class LaunchClaimsRegistry(jwt.JWTClaimsRegistry):
    def validate_iat(self,value):
        if type(value) is not int or value>self.now+5: raise LaunchError('Issuer clock skew rejected')
        self.check_value('iat',value)

def platform_key_set(jwks):
    keys = []
    for original in jwks["keys"]:
        key = dict(original)
        if key.get("kty") == "RSA":
            for name in ("n", "e"):
                value = key[name]
                if isinstance(value, str) and value.endswith("="):
                    encoded = value.rstrip("=")
                    padding = -len(encoded) % 4
                    if padding not in (1, 2) or value != encoded + "=" * padding:
                        raise LaunchError("Invalid Moodle RSA encoding")
                    key[name] = encoded
        keys.append(key)
    return KeySet.import_key_set({"keys": keys})


class LaunchService:
    def __init__(self, store, registration, clock=time.time):
        self.store, self.registration, self.clock = store, registration, clock

    def begin(self, issuer, client, target, hint, message, binding):
        r = self.registration
        if issuer != r["issuer"] or (client is not None and client != r["clientId"]) or target != r["targetLinkUri"] or not hint or not message or not binding:
            raise LaunchError("Login registration/binding rejected")
        return self.store.state(binding, int(self.clock()))

    def verify(self, state, binding, encoded, jwks):
        now, r = int(self.clock()), self.registration
        pending = self.store.pending(state, binding, now)
        try:
            token = jwt.decode(encoded, platform_key_set(jwks), algorithms=["RS256"])
            header, c = token.header, token.claims
            if header.get("alg") != "RS256" or not header.get("kid") or sum(k.get("kid")==header["kid"] for k in jwks["keys"]) != 1 or any(k in header for k in ("jku","jwk","x5u","x5c")):
                raise LaunchError()
            LaunchClaimsRegistry(now=now, leeway=0, iss={"essential":True,"value":r["issuer"]}, aud={"essential":True,"value":r["clientId"]}, sub={"essential":True}, exp={"essential":True}, iat={"essential":True}, nonce={"essential":True,"value":pending["nonce"]}).validate(c)
            if any(type(c[k]) is not int for k in ("iat","exp")) or c["iat"] > now+5 or c["iat"] < now-300 or c["exp"] > c["iat"]+600 or c["exp"] <= now or c["exp"] <= c["iat"]:
                raise LaunchError()
            aud = c["aud"]
            if not isinstance(aud, (str,list)) or isinstance(aud,list) and (not aud or any(not isinstance(v,str) for v in aud) or len(aud)>1 and c.get("azp")!=r["clientId"]): raise LaunchError()
            if c.get("azp") is not None and c["azp"] != r["clientId"]: raise LaunchError()
            if c[LTI+"deployment_id"] != r["deploymentId"] or c[LTI+"context"]["id"] != r["contextId"] or c[LTI+"message_type"] != "LtiResourceLinkRequest" or c[LTI+"version"] != "1.3.0" or c[LTI+"target_link_uri"] != r["targetLinkUri"]:
                raise LaunchError()
            resource = c[LTI+"resource_link"]["id"]
            matches = [(key,value) for key,value in r["activities"].items() if value["resourceLinkId"]==resource]
            if len(matches) != 1: raise LaunchError()
            activity, settings = matches[0]
            roles = c[LTI+"roles"]
            if not isinstance(roles,list) or not roles or any(role not in (LEARNER,INSTRUCTOR) for role in roles): raise LaunchError("Course enrollment role required")
            endpoint = c[AGS]
            if endpoint["lineitem"] != settings["lineitem"] or not isinstance(endpoint["scope"],list) or SCORE_SCOPE not in endpoint["scope"]: raise LaunchError("AGS activity binding rejected")
            sub = c["sub"]
            if not isinstance(sub,str) or not sub or len(sub)>512: raise LaunchError()
            identity_key = digest([r["issuer"],r["clientId"],r["deploymentId"],sub])
            name = c.get("name", "Moodle participant")
            if not isinstance(name,str) or len(name)>256 or any(ord(v)<32 for v in name): name="Moodle participant"
            identity = {"userId":identity_key,"uid":"gw-"+identity_key+"@example.invalid","issuer":r["issuer"],"client":r["clientId"],"deployment":r["deploymentId"],"subject":sub,"name":name,"role":"teacher" if INSTRUCTOR in roles else "student","context":r["contextId"],"resource":resource,"activity":activity,"lineitem":settings["lineitem"]}
        except LaunchError: raise
        except Exception as error: raise LaunchError("Signature/claims rejected") from error
        return self.store.activate(state, binding, now, identity)
