# Verified launch and private handoff

The server accepts one closed, fixed Moodle registration. HTTPS issuer, client,
deployment, course context, resource-link and AGS lineitems are configured outside
Git. The authorization, JWKS, token and lineitem URLs share the registered issuer
origin. A private JWKS transport mapping is restricted to `http://moodle` with
the same JWKS path; signed issuer/audience remain the external HTTPS values.

`GET` or `POST /gateway/lti/login` accepts the standard issuer, client_id,
target_link_uri, login_hint, lti_message_hint and optional lti_deployment_id.
Unknown fields, mismatched registration and missing hints fail. The OIDC request
uses form_post, RS256 and a random state/nonce. SQLite stores hashed state and
browser binding, nonce and a 300-second expiry. The binding cookie is Secure,
HttpOnly and SameSite=None for the top-level Moodle form POST.

`POST /gateway/lti/launch` accepts only state and id_token (at most 64 KiB).
joserfc verifies RS256 using a unique fixed-JWKS kid, followed by essential claims
validation. Embedded/remote attacker key headers are rejected. The service checks
issuer, audience and azp for multiple audiences, subject, nonce, integer iat/exp,
300-second launch freshness, maximum 600-second token lifetime, deployment,
LtiResourceLinkRequest/1.3.0, exact target, context and resource binding. Supported
roles are Learner and Instructor; other roles and missing roles fail. The signed
AGS endpoint must match the configured activity and include the score scope.

The native UID is server-derived:
`gw-<SHA256(compact JSON [issuer,clientId,deploymentId,subject])>@example.invalid`.
The reserved suffix is fixed. Context/resource remain separate validated bindings.
Browser user IDs, native IDs, cookies or identity headers do not select a user.
SQLite atomically consumes state and creates the persistent one-hour session.
Replay, browser mismatch and expiry fail. Browser mutations require both the exact
public Origin and the session CSRF token.

Students receive a random 43-character opaque handoff; teachers enter the Gateway
UI without native privilege promotion. Handoff stores a hashed token, session and
target, expires after 60 seconds and is consumed exactly once. Initial target is
`/gateway/bootstrap`; later targets are native assessment paths returned by the
manifest-scoped Platform launch API. Open redirects, control characters and
unscoped `/pl/` targets fail.

The public proxy owns `/gateway/pl/{token}`. It invokes private
`GET /internal/handoff/{token}` with a separate service bearer. Successful JSON
contains only uid/displayName/email/target. Headers X-Verified-Uid,
X-Verified-Name and X-Verified-Target support auth_request; non-ASCII names use a
fixed ASCII header fallback while JSON retains the verified name. No role is sent.
The proxy strips all client identity headers/cookies on this path, forwards only
the verified identity and server-derived pl2_pre_auth_url cookie, and invokes
Community `/pl/shibcallback`. Native PrairieLearn creates its own ordinary session.
Private PL/bridge/Gateway internal listeners must be inaccessible publicly.

After callback, `/gateway/bootstrap` ensures ordinary native enrollment through
the private bridge, then retries durable ACL reconciliation. Unknown native users
and blocked memberships fail. No labels are granted by bootstrap. The service
disables forwarded-header trust and request access logs; secrets and tokens must
not be logged by the surrounding proxy.

Library reference: [joserfc JWT validation](https://jose.authlib.org/en/guide/jwt/).
