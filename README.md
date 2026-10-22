# moodle-prairielearn-gateway

Private FastAPI service for verified Moodle LTI 1.3 launches, native Community
PrairieLearn assignments and Moodle AGS results. It uses joserfc (Authlib JOSE),
актуальной SQLite схемы and the authenticated Platform bridge. Teacher operations never
grant PrairieLearn administrator access.

Install the exact closure in a private Python 3.14 environment, then run the owner
tests:

```sh
python3.14 -m venv /private/gateway-venv
/private/gateway-venv/bin/python -m pip install --no-deps -r requirements-dev.lock
/private/gateway-venv/bin/python -m pip check
/private/gateway-venv/bin/python -m pytest tests -q
```

Copy `config/courses.example.json` into a private runtime directory and replace
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
lab. Its source explicitly declares three points per question; exporter6 requires
authored grading values rather than inserting internal defaults. `tools/export-fixture.py --help` exports this source through standard installed
Core/exporter packages and checks the resulting native inventory. Its generated
delivery, private grading and evidence stay outside the owner source. Runtime
keys/users/tokens, native sessions, SQLite data and Moodle receipts stay outside Git.

The owner tests use synthetic identities and HTTP transports. Actual Student and
teacher Moodle round trips, native direct-URL denial, restart, course upgrade and
actual Java numbers/UTF-8 acceptance are separate release gates. Cross-delivery
assignment migration currently fails closed pending the reviewed explicit
Platform/Gateway migration workflow; restarting an unchanged delivery preserves
the persistent assignments, audit and outbox.

Новый native course с отдельным instance можно подключить к существующему
профилю, сохранив SQLite schema3, routePrefix, регистрацию и ключи. Неистёкшие
session tokens и CSRF остаются действительными; старые назначения и attempts
сохраняются в прежнем instance. Это не миграция delivery внутри того же
instance. Свежая БД утратит распознавание старых cookies. Точная граница и
проверки описаны в [инструкции обновления](docs/two-courses.md#оценки-и-обновления).

Для локального прототипа: [отдельное доверие TLS в Moodle/PHP, Gateway и браузере](docs/local-tls.md), генерация полного публичного CA bundle и Compose override без изменения томов данных.

## Текущая версия и выпуск

Кандидат 2.0.0 принимает только delivery/results/config v2 и новую БД3 без
миграций. `config/courses.example.json` показывает именованные курсы. Каждый курс
имеет собственные delivery и SQLite; вложенные конфиги не содержат schemaVersion.
Publisher проверяет отсутствие registry tag до push, сохраняет receipt v2 с
актуальными версиями схем и подтверждает pull фактического digest. Версия готова
к использованию после публикации образа и полной интеграционной приёмки; локальный
образ и candidate commit сами по себе не подтверждают выпуск.

## Несколько курсов и серверный пилот

Общая LTI-регистрация одного Moodle курса и независимые native-курсы описаны
в [инструкции](docs/two-courses.md). Пример конфигурации —
[config/courses.example.json](config/courses.example.json).

## Локальный candidate delivery v3

Новый reader принимает только delivery v3 с явными grading/submission bindings всего каталога вопросов. Results остаются `per-slot-results-v2`: один фактический представитель на слот. Для `choice` native bridge проверяет всех кандидатов и выбирает full → score → lexical ID; Gateway закрепляет весь canonical pool этой позиции внутри попытки. Поэтому смена лучшего A→B допустима, но fixed/random по-прежнему закрепляют выбранный вопрос. Чужой представитель и изменение canonical pool отвергаются.

`grading` выбранного представителя блокирует новый FullyGraded event; прежний outbox/опубликованный результат сохраняется. Pending проигрывающей альтернативы не меняет завершённый status победителя. Завершённая partial оценка не равна accepted slot: completion требует score=1 с полными native points. Assignment и lab сохраняют свои прежние шкалы.

Адресные interoperability tests используют `PLATFORM_BRIDGE_POLICY=/absolute/candidate/bridge/policy.mjs` и проверяют фактический Node producer перед Gateway. Без явного пути этот межрепозиторный тест отмечается skipped; обычные reader/attempt/outbox tests исполняются самостоятельно. Эти synthetic tests не подтверждают actual Moodle authentication или UI двух студентов.

## Вход в каталог без передачи оценки

`kind: catalog` открывает существующие «Мои работы» и выбранные обычные работы
текущего delivery/instance. Он не создаёт назначение защиты и не отправляет AGS:
ни текущие результаты каталога, ни прежний pending outbox этого activity.
Существующие sent/pending строки сохраняются; это не перенос старой оценки.

Пример внутренней привязки (адреса вымышлены):

```json
"streams": {
  "kind": "catalog",
  "resourceLinkId": "existing-resource",
  "lineitem": "https://moodle.example/lineitems/existing",
  "workId": null
}
```

При смене маршрута прежние resourceLinkId/lineitem и внешняя регистрация остаются
неизменными. `workId` у catalog запрещён. Студент проходит обычный одноразовый
handoff, enroll тем же UID и выбирает текущую обычную работу. Преподаватель
использует существующий преподавательский вход без доступа от имени студента.
Защиты, выбор/выдача назначений и roster из catalog недоступны.

Это описание source transport, а не разрешение менять production configuration
или состояние SQLite. Cutover требует отдельной проверки неизвестных/in-flight
scores, backup/restore и сохранения истории. Реальные проверки Moodle двумя
студентами и преподавателем остаются отдельным этапом.

Source candidate `2.1.0` добавляет закрытый маршрут catalog. Это подготовленный
кандидат для отдельного review/CI; публикация версии или Docker image здесь
не выполнялась.
