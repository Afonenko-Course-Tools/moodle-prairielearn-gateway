# Auth-only Gateway — план реализации

> **Для исполнителя:** использовать superpowers:subagent-driven-development; один implementer для связанного auth приложения, затем независимая рецензия всей дельты. Без собственных hash/proof/model-admission пакетов.

**Цель:** заменить старую учебную программу небольшим адаптером Moodle LTI → native PL session → private enrollment.

**Архитектура:** FastAPI проверяет LTI прежними криптографическими средствами, хранит только одну кратковременную попытку на browser/course и передаёт подтверждённый UID приватному callback. Bootstrap необратимо потребляет точный flow до Bridge. PL владеет пользователями, длительной сессией, правами, работами и оценками.

**Стек:** существующие Python3.14/FastAPI/joserfc/Pydantic/httpx/SQLite. Новых зависимостей и SDK нет.

**Спецификация:** root-approved /home/tolya/Java/.superpowers/morning-20261106/MISSION.md, docs/superpowers/plans/2026-11-06-gateway-cleanup.md в integration-java, его evening-report.md и /home/tolya/Java/.superpowers/evening-20261105/gateway-cleanup-proposal-analysis.txt. Пользователь уже разрешил исполнение.

## Общие ограничения

- Только gpt-6.1-sol; существенный код проходит независимую рецензию. Runtime, Docker, Moodle и proxy меняет S1; исполнитель работает только в этом isolated checkout.
- Не менять историческую VM/production. Старая SQLite и прежние checkout сохраняются; не мигрировать/очищать старые данные.
- Буквально сохранить прежний UID: digest([issuer,clientId,deploymentId,sub]), существующая digest JSON serialization, префикс gw- и суффикс @example.invalid. Email не identity и не merge.
- Удалить исполняемые portal/assignment/approval/capacity/AGS/grading/worker/outbox/attempttracking и их конфигурацию, SQL, tests/fixtures, инструменты и docs consumers. Нет grading default, mode branches или совместимости со старой учебной схемой.
- Нет постоянных users и часовой SSO session. Новая маленькая auth DB; открытие старой БД отказывает до mutable operations. Никаких migration compatibility layers.
- Один согласованный PUT /internal/gateway/enrollment: request ровно userUid,instance,courseKey,role(student|teacher),email(string|null). Response ровно schemaVersion(int1),userUid,instance,courseKey,enrolled(booltrue),path. Path только /pl/course_instance/[1-9][0-9]*/assessments без query/origin/trailing slash. Extra/missing/type/mapping mismatch и non200/malformed/oversized ответ — отказ.
- Signing key и tool JWKS endpoint больше не нужны: S1 прочитал реальную регистрацию Moodle и source. Outgoing launch подписывает Moodle; AGS/client_assertion/deeplink отключены. Private key files не удалять; новые config/app не загружают их. S1 отдельно удаляет stale tool-JWKS URL штатной настройкой регистрации, сохраняя identity tuple.
- Русские новые README/docs/PR и авторские тексты. Credentials вне репозитория и не в logs.

## Утверждённые решения lifecycle

1. Новый GatewayLogin — случайный кратковременный browser nonce, Secure/HttpOnly/SameSite=None, единый Path=/gateway для root dispatcher и всех nested course routes. Старые GatewayBinding/GatewaySession не используются. TTL попытки не более300s; private handoff не более60s и не дольше попытки.
2. Только login initiation может выставить GatewayLogin. Ни один launch/bootstrap/error callback не выставляет и не удаляет его: даже точная проверка cookie в старом request не защищает от стирания нового cookie поздним response. Terminal state, а не удаление browser cookie, прекращает возможность входа. Root принял это уточнение.
3. Активная попытка хранится по browser/course; разные course DB уже существуют как изоляция конфигурации, нового registry нет. Новый login отменяет старый в своём course. Параллельные первые запросы без cookie определяются фактически принятым browser nonce; callbacks требуют его точное совпадение.
4. Два GatewayLogin в Cookie header не должны давать неявный выбор parser winner. Отказать без cookies/Bridge effects. Проверить root /gateway/ и nested /gateway/courses/<key>/ вместе, единый scope и независимые course attempts.
5. После genuine JWT validation сохраняется одно именованное immutable значение с исходными identity/UID/name/role/email и registration tuple/context/resource/activity/target_link_uri/courseKey/instance/одноразовым bootstrap target. Snapshot не заменяется текущей конфигурацией или поздним входом того же пользователя.
6. Private handoff остаётся прежним wire {uid,displayName,email:uid,target} и X-Verified-Uid/Name-UTF8-B64/Target для S1 proxy. Gateway не выставляет native pl2_pre_auth_url: proxy уже использует verified target во внутреннем callback. Native cookies выставляет PL.
7. Bootstrap требует точный opaque flow и browser credential, consumed handoff phase, TTL и равенство сохранённой/текущей регистрации и course mapping. В Bridge передаются SAVED значения после сравнения. Смена mapping требует нового Moodle launch.
8. Атомарный необратимый consume всей попытки происходит ДО await Bridge. Timeout/ошибка/неизвестный исход не открывает её заново. Нет retries/reconciler/rollback native account/session/grants. Ошибка предлагает новый переход из Moodle.
9. После успеха существует только native PL session. Home/root/старые cookies/старый flow не могут reissue handoff или восстановить PL после logout. PL logout не означает Moodle logout; новый явный Moodle launch разрешён.
10. Просроченные временные данные убираются ограниченно на auth операциях, без worker. Криптографические token hashes и стабильный UID digest сохраняются; собственных freshness/proof/version протоколов нет.

## Фокус рецензии

- Старый callback при двух параллельных входах, в том числе при await Bridge, не меняет новый cookie и не потребляет новый flow.
- Saved/current registration и native logical mapping проверяются после restart/reconfiguration до Bridge; старый same-user/same-role credential не заменяет точный flow.
- Сетевой timeout, cancellation и malformed Bridge answer оставляют flow terminal, без повторного зачисления по старой cookie.
- Полный LTI reject path сохраняет RS256/kid/issuer/aud/azp/deployment/context/resource/target/state/nonce/iat/expiry/replay; public headers не дают private handoff.
- Новая auth DB не изменяет старую SQLite; удалённая учебная программа не остаётся в CLI/tests/config/compose или optional imports.

## Задача1 — связанное новое приложение

**Файлы:** src/gateway/{app,config,store,launch,pl,courses,errors}.py, schema/runtime.sql; tests auth/config/transport; tools/run-gateway.py, Dockerfile/compose/config, CI, README/spec/docs. Удалить src/gateway/{assignments,grades,ags,delivery_v4}.py и реальные obsolete consumers/fixtures, не переносить их в product archive.

- [x] Написать содержательные RED tests против baseline: reuse after success/Bridge failure, saved mapping change, exact credential, old parallel callbacks, no users/legacy writes и oldDB preservation.
- [x] Наблюдать ожидаемые RED от отсутствующего поведения, а не import failures/typos; записать краткие результаты в обычный implementation report.
- [x] Реализовать минимальный новый auth-only flow; проверенную LTI криптографию/UID сохранить. Типизировать реальные границы существующим Pydantic/обычными dataclasses, без повторных DTO layers/own AST/parser/framework.
- [x] Проверить positive genuine signed Student/Instructor/Owner-email Learner, negative LTI matrix, expiry/replay, duplicate cookies/root+nested profiles, exact current flow, success+native logout/rebootstrap, registration+course mapping replacement, Bridge failure/timeout/cancel/malformed ответ и отсутствие legacy runtime routes/writes.
- [x] Удалить obsolete tooling/docs/fixtures и составить простой переход: новая auth DB, прежние cookies недействительны, oldDB сохранена, точные новые config dependencies и откат без удаления native data.
- [ ] Полный pytest один раз после focused tests: по уточнению координатора передан ему после локального commit; исполнитель не запускал полный suite. Moodle/Docker/экспорт Quarto не запускались, packages не устанавливались.
- [x] Self-review, локальные commits, краткий report /tmp/s2-auth-only-implementation.txt; не push.

## Задача2 — независимая приёмка и передача

- [ ] Fresh Sol reviewer читает spec, diff и meaningful test results; проверяет поведение/качество, не повторяет всю матрицу без конкретной причины.
- [ ] Исправить найденное и выполнить только оправданные проверки; потом whole-branch acceptance.
- [ ] Опубликовать новый архитектурный PR от accepted9e отдельной веткой; прежний PR/данные не менять.
- [ ] Передать S1 принятый commit и минимальную private конфигурацию; развертывание только S1. Browser acceptance после15приCA не объявлять заранее.
