# Проверенный LTI-вход и приватный handoff

Gateway принимает закрытую фиксированную Moodle регистрацию. HTTPS issuer,
client, deployment, context и resource-link задаются вне Git. Authorization,
JWKS и заданные token/lineitem URL обязаны иметь origin зарегистрированного
issuer. Приватный JWKS transport ограничен `http://moodle` с тем же путём;
подписанные issuer/audience остаются внешними HTTPS значениями.

Основной режим `mode: login-only` описан в [договоре входа](../docs/login-only.md).
Он направляет обе роли в штатный список курса без обязательного AGS claim,
delivery и запуска worker. Ниже описаны общие проверки и сохранённый `grading`.

`GET` или `POST /gateway/lti/login` принимает стандартные issuer, `client_id`,
`target_link_uri`, `login_hint`, `lti_message_hint` и необязательный
`lti_deployment_id`. Неизвестные поля, чужая регистрация и отсутствующие hints
отклоняются. OIDC запрос использует `form_post`, RS256 и случайные state/nonce.
SQLite хранит hash state и browser binding, nonce и срок 300 секунд. Cookie
привязки имеет Secure, HttpOnly и SameSite=None для POST из Moodle.

`POST /gateway/courses/<course>/lti/launch` принимает только `state` и `id_token`
размером не более 64 KiB. joserfc проверяет RS256 с единственным `kid` фиксированного
JWKS и обязательные claims. Заголовки с посторонними remote/embedded ключами
отклоняются. Проверяются issuer, audience, azp, subject, nonce, целочисленные
iat/exp, свежесть 300 секунд, срок токена не более 600 секунд, deployment,
LtiResourceLinkRequest/1.3.0, точные target/context/resource и роли Learner или
Instructor. В режиме `grading` подписанный AGS endpoint дополнительно обязан
совпасть с activity и содержать score scope.

UID вычисляется сервером из прежнего tuple issuer/client/deployment/subject.
Пользовательский UID из браузера, имя, cookie или identity header не выбирают
пользователя. SQLite атомарно потребляет state и создаёт часовую сессию.
Повторное использование, неверный browser binding и истечение TTL запрещены.
Проверенный email извлекается лишь после подписи и регистрационных guards,
нормализуется и сохраняется в исходной session. Этот адрес не входит в UID.
Login-only enrollment передаёт Bridge ровно userUid, instance, courseKey,
role и email; приватная Bridge политика сопоставляет native права.
Подробности доверия к отсутствующему `email_verified`, отклонения неправильного
email и миграции SQLite версии 5 — [в договоре входа](../docs/login-only.md).
Операции изменения в сохранённом режиме `grading` требуют точный публичный
Origin и session CSRF token.

Handoff хранит hash токена, session и target, действует 60 секунд и потребляется
однократно. Bootstrap с токеном проходит два состояния потребления: private
native auth и возврат проверенной identity/роли в профиль. Открытые redirects,
управляющие символы и произвольные native targets запрещены.

Proxy владеет `/gateway/courses/<course>/pl/{token}` и вызывает приватный
`GET /internal/handoff/courses/<course>/{token}` с отдельным service bearer.
Успешный JSON содержит только uid/displayName/email/target. Заголовки
`X-Verified-Uid`, `X-Verified-Name-UTF8-B64` и `X-Verified-Target`
поддерживают `auth_request`. Имя передаётся как `v1.` и canonical base64url
без padding от строгого UTF-8; JSON сохраняет исходную Unicode-строку.
Граница — 256 Unicode scalar values, 1024 UTF-8 bytes, 1369 ASCII wire chars.
Старый raw Name header не выдаётся. Недопустимый signed claim использует
прежнюю fallback policy; пустая допустимая строка кодируется `v1.`.
Имя не входит в UID/digest и не определяет роль.

Proxy удаляет клиентские identity headers/cookies на этом пути, передаёт только
проверенную identity и серверный `pl2_pre_auth_url`, затем вызывает Community
`/pl/shibcallback`. PL создаёт собственную обычную сессию. Private PL, Bridge
и Gateway listeners должны быть недоступны публично. Gateway отключает доверие
forwarded headers и access logs; окружающий proxy не должен писать секреты.

## Сохранённый режим grading

Этот режим остаётся режимом по умолчанию прежней конфигурации и требует delivery,
flow и AGS настройки. Лабораторная ведёт на assessment соответствующей работы,
каталог — на прежний интерфейс Gateway. Worker продолжает обработку своих
реальных результатов. Вход Instructor сам по себе не назначает native права.

Только явно заданный `administratorSubject` с ролью teacher и точной проверенной
issuer/client/deployment identity может вызвать
`POST /gateway/courses/<course>/launch/administrator` с Origin/CSRF.
Bridge отдельно сверяет `administratorUid`, существующего native пользователя
и service bearer. После подтверждённого grant устанавливается штатный cookie
`pl2_access_as_administrator=active` (Secure/HttpOnly/SameSite=Lax); native
middleware требует настоящую administrator запись. Удаление config binding
не снимает уже существующие native права. В `login-only` эта настройка запрещена.
