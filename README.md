# Вход из Moodle в PrairieLearn

Gateway 3 — небольшой адаптер Moodle LTI 1.3 → штатная сессия PrairieLearn →
private enrollment. Moodle подтверждает identity и роль курса, PL хранит
пользователей, длительную сессию, права, работы и оценки. Gateway хранит только
кратковременные одноразовые попытки входа.

## Последовательность входа

1. Moodle открывает `/gateway/lti/login` с точным зарегистрированным
   `target_link_uri`, либо `/gateway/courses/<key>/lti/login`.
2. Gateway выдаёт `GatewayLogin`: случайный browser nonce с `Secure`, `HttpOnly`,
   `SameSite=None`, `Path=/gateway`, сроком 300 секунд. Новый вход в тот же курс
   отменяет прежнюю попытку; попытки разных курсов независимы.
3. Moodle подписывает `LtiResourceLinkRequest`. Gateway проверяет RS256, `kid`,
   issuer/audience/azp, deployment/context/resource/target, state/nonce, сроки и роль.
   Подтверждённые identity, имя, email, роль, регистрация и native mapping сохраняются
   в одном неизменяемом snapshot. Email не определяет UID и не объединяет identity.
4. Redirect `/gateway/courses/<key>/pl/<flow>` обрабатывает private proxy PL.
   Он однократно получает `{uid,displayName,email:uid,target}` через
   `/internal/handoff/courses/<key>/<flow>` с Bearer service token и создаёт native
   PL session. Header имени — `X-Verified-Name-UTF8-B64: v1.<base64url UTF-8>`;
   также передаются `X-Verified-Uid` и `X-Verified-Target`.
5. `/gateway/courses/<key>/bootstrap/<flow>` требует точный flow, browser nonce,
   потреблённый handoff и неизменные регистрацию/native mapping. Попытка атомарно
   и необратимо потребляется **до** обращения к Bridge. В Bridge идут сохранённые
   UID, instance, courseKey, role и email. После успеха браузер переходит к
   `/pl/course_instance/<id>/assessments`.

Попытка живёт не более 300 секунд; handoff/bootstrap — не более 60 секунд после
launch и не дольше попытки. Launch/bootstrap/error callbacks не выставляют и не
удаляют cookie: поздний ответ не может стереть credential нового входа. Два
`GatewayLogin` в Cookie header отклоняются без потребления flow. Просроченные
данные удаляются ограниченными порциями при auth операциях; фонового worker нет.

После успеха остаётся штатная PL session. Home не перевыдаёт handoff. Logout PL
не позволяет восстановить сессию по прежнему flow или старым Gateway cookies.
Новый явный переход из Moodle разрешён: logout PL не является logout Moodle.
Timeout, cancellation, ошибка или неизвестный исход Bridge не возвращают flow
в оборот; нужен новый переход из Moodle. Автоматических retries и rollback native
account/session/grants нет.

## Конфигурация и запуск

Требуется Python 3.14 и существующие зависимости из `requirements.lock`.
Пример двух курсов — [config/courses.example.json](config/courses.example.json).
Скопируйте его **вне репозитория**, задайте реальные server-owned registration,
routePrefix, courseKey, instance и отдельный **новый** database path каждого курса.
У курсов общий HTTPS origin и handoff token; DB, LTI resource и native mapping
не должны пересекаться.

Нужны два service token файла (не менее 32 печатных ASCII символов), доступ к
Moodle JWKS и private Bridge `:3001`; для локального CA задайте `caFile`.
`privateJwksUrl` допускает только фиксированный HTTP transport на `moodle` с тем же
path, сохраняя публичный Moodle issuer. Он не меняет зарегистрированную identity.

```sh
python3.14 -m pip install --no-deps -r requirements.lock
python3.14 tools/run-gateway.py --config /absolute/private/runtime.json
```

Команда запрещает DB и service tokens внутри репозитория. Пример private Compose
service находится в `compose/compose.gateway.yml`; конфигурацию proxy и deployment
ведёт владелец стенда. Публичные заголовки identity не заменяют private service
authentication. Штатные native cookies выставляет PL; Gateway не выставляет
`pl2_pre_auth_url`.

Tool signing key, `privateKeyFile`, tool JWKS endpoint, AGS, delivery, portal,
assignments, capacity/approval, administrator override и grading workers отсутствуют.
Исходящий launch подписывает Moodle; потребители tool signing key в отключённых
AGS/client_assertion/deep-linking сценариях этому приложению не нужны. Прежние
private key файлы сохраняются. Удаление stale tool-JWKS URL из регистрации Moodle
выполняется отдельно штатной настройкой без смены issuer/clientId/deploymentId/sub.

Контракт Bridge описан в [spec/launch.md](spec/launch.md), переход со старой БД —
в [docs/migration.md](docs/migration.md), два курса — в
[docs/two-courses.md](docs/two-courses.md), локальный TLS — в
[docs/local-tls.md](docs/local-tls.md).

## Проверка

```sh
python3.14 -m pip install --no-deps -r requirements-dev.lock
python3.14 -m pytest -q
```

Синтетические проверки используют настоящие RSA-подписи LTI, временные SQLite и
HTTP transport без внешней сети. Проверки реального Moodle, proxy/native PL,
браузерных cookies и устройств выполняются отдельно на стенде. Локальный pytest
не подтверждает готовность такого развёртывания.

Ручной release workflow собирает wheel и стандартный OCI image из проверенного
version tag. Самописного release receipt или хеша исходников нет. Запуск workflow
и публикация образов не являются частью локальной проверки.
