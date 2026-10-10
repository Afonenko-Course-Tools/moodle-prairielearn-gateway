# moodle-prairielearn-gateway

Private FastAPI service for verified Moodle LTI 1.3 launches, native Community
PrairieLearn assignments and Moodle AGS results. It uses joserfc (Authlib JOSE),
SQLite migrations and the authenticated Platform bridge. Teacher operations never
grant PrairieLearn administrator access.

Install the exact closure in a private Python 3.14 environment, then run the owner
tests:

```sh
python3.14 -m venv /private/gateway-venv
/private/gateway-venv/bin/python -m pip install --no-deps -r requirements-dev.lock
/private/gateway-venv/bin/python -m pip check
/private/gateway-venv/bin/python -m pytest tests -q
```

Copy `config/runtime.example.json` into a private runtime directory and replace
the fixed registration, activity and native-delivery bindings. Unknown fields are
rejected. The instance comes from the installed exporter delivery; each lab has
its own registered Moodle resource/lineitem/work. Exactly one defense activity
receives the current assigned variant. Capacity defaults to unlimited.

Run `tools/run-gateway.py --config /private/runtime.json`. The service listens on
private port 18080; only the trusted HTTPS proxy exposes `/gateway/`. Use
`compose/compose.gateway.yml` for the private container with UID/GID 1000,
read-only source/config/delivery mounts and a writable SQLite volume. Set
`GATEWAY_PRIVATE`, `GATEWAY_DELIVERY` and `GATEWAY_DATA` to absolute private paths.
Secret files and the database directory must be readable/writable by UID 1000 as
appropriate (files 0600, data directory 0700); no public service port is published.
Mount a trusted CA at `/run/tls/ca.crt`; TLS verification remains enabled.

Protocol and operational contracts: [launch](spec/launch.md),
[assignment](spec/assignment.md), [results](spec/results.md). The Platform bridge
owns all native enrollment/label changes and result reads. Gateway stores its
workflow only. `tools/check-integration.py --database /private/runtime.sqlite`
reads durable counts without modifying the database; it does not certify live
Moodle acceptance.

`tests/fixtures/course-gateway/` contains three synthetic Java bank questions,
60 distinct manual essay declarations and 60 related defenses, plus an ordinary
lab. `tools/export-fixture.py --help` exports this source through standard installed
Core/exporter packages and checks the resulting native inventory. Its generated
delivery, private grading and evidence stay outside the owner source. Runtime
keys/users/tokens, native sessions, SQLite data and Moodle receipts stay outside Git.

The owner tests use synthetic identities and HTTP transports. Actual Student and
teacher Moodle round trips, native direct-URL denial, restart, course upgrade and
actual Java numbers/UTF-8 acceptance are separate release gates. Cross-delivery
assignment migration currently fails closed pending the reviewed explicit
Platform/Gateway migration workflow; restarting an unchanged delivery preserves
the persistent assignments, audit and outbox.

Для локального прототипа: [отдельное доверие TLS в Moodle/PHP, Gateway и браузере](docs/local-tls.md), генерация полного публичного CA bundle и Compose override без изменения томов данных.

## Versioned release

Version 1.0.0 packages runtime schema 1/2 and preserves unchanged-delivery state.
Changed deliveries fail closed; explicit delivery migration is deferred. Install
the wheel attached to the versioned GitHub release in a Python 3.14 environment,
or install the complete owner source from the same tag and locked dependencies.

The `Publish Gateway release image` workflow accepts an existing reviewed tag,
checks its packaged version and integration into `main`, runs owner tests and
publishes the Linux amd64 image. It attaches `release-images.json` with the
actual registry digest, source commit and wheel hash to that release. A release
is ready for deployment after those assets exist and the exact digest passes
local integration. A local Docker image ID is not a registry digest.

Use `ghcr.io/afonenko-course-tools/moodle-prairielearn-gateway@sha256:…` from
that receipt when deploying; replace the example with the full recorded digest.
Students need only browser access. Developers on another CachyOS laptop use their
own local CA and the portable paths described in [local TLS](docs/local-tls.md).
