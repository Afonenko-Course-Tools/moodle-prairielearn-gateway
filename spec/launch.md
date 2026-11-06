# Контракт auth-only входа

## Identity и LTI

UID сохраняет прежнее вычисление буквально:
`gw-` + SHA-256 от JSON массива `[issuer,clientId,deploymentId,sub]` с
`sort_keys=True,separators=(",",":"),allow_nan=False` + `@example.invalid`.
JSON сериализация не меняется. Email не является identity и не объединяет учётные
записи; displayName также не является ключом.

Принимается только зарегистрированный Moodle `LtiResourceLinkRequest` версии
`1.3.0`, подписанный RS256 известным уникальным `kid` из Moodle JWKS. Header
`jku`, `jwk`, `x5u`, `x5c` отклоняется. Проверяются issuer/aud/azp, непустой `sub`,
state/nonce/browser credential, deployment, context, resource и target_link_uri.
`iat`/`exp` — целые числа; возраст `iat` не более 300 секунд, допустимый уход часов
вперёд не более 5 секунд, `exp>now`, `exp>iat`, `exp<=iat+600`. Использованный state
не принимается повторно. Роли только Learner/Instructor; Instructor даёт `teacher`,
Learner — `student`. Email владельца в Learner launch не меняет роль Gateway.

Подтверждённый email нормализуется в ASCII lower-case mailbox; неподходящий адрес
или явный `email_verified`, отличный от bool `true`, даёт `null`. Сохраняется
исходное подтверждённое значение каждого launch.

## Private handoff

Требуется `Authorization: Bearer <handoff service token>`. Однократный GET
`/internal/handoff/courses/<key>/<flow>` возвращает ровно
`{uid,displayName,email:uid,target}` и `X-Verified-Uid`,
`X-Verified-Name-UTF8-B64`, `X-Verified-Target`. Target — сохранённый одноразовый
bootstrap URL того же курса. Native PL proxy использует этот verified target
во внутреннем callback. Gateway не выставляет native PL cookies.

Bootstrap требует точный flow и browser credential, потреблённый handoff, TTL и
равенство текущей регистрации/native logical mapping сохранённым значениям.
Все callbacks оставляют cookie без изменений. До `await Bridge` вся попытка
становится terminal; повторного зачисления по ней нет даже после сетевой ошибки,
cancellation или native logout.

## Private enrollment

Единственный запрос: `PUT /internal/gateway/enrollment` с Bearer Bridge token.
Request имеет ровно пять полей:

```json
{"userUid":"gw-synthetic@example.invalid","instance":"pilot","courseKey":"java","role":"student","email":null}
```

`role` — `student|teacher`, `email` — canonical string либо `null`.
Ожидается HTTP 200 с ровно шестью полями:

```json
{"schemaVersion":1,"userUid":"gw-synthetic@example.invalid","instance":"pilot","courseKey":"java","enrolled":true,"path":"/pl/course_instance/42/assessments"}
```

`schemaVersion` — int `1`, `enrolled` — bool `true`, userUid/instance/courseKey
должны буквально совпадать с запросом. Path допускает только
`/pl/course_instance/[1-9][0-9]*/assessments` без origin/query/trailing slash.
Extra/missing/type/mapping mismatch, non-200, redirect, malformed JSON или ответ
больше 65536 байт дают отказ. Повторных запросов нет. Ошибка предлагает новый
переход из Moodle; возможные native изменения не откатываются.
