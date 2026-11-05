# Вход из Moodle в штатный список курса

Режим `mode: login-only` использует существующие Moodle LTI регистрацию и ссылку.
В конфигурации сохраняются фактические `issuer`, `clientId`, `deploymentId`,
`contextId` и `resourceLinkId`, подписывающий ключ и HTTPS origin. Выбор native
курса задают `courseKey` и `instance`; неизвестные поля конфигурации отклоняются.
Образец двух профилей: [courses.example.json](../config/courses.example.json).
Все значения образца вымышленные; они не являются настройками учреждения.

Для входа не нужны `deliveryPath`, `flow`, AGS `tokenUrl` и `lineitem`.
В `activities` разрешены только `catalog` без `workId`. Настройки delivery,
назначений и `administratorSubject` в этом режиме отклоняются. Если AGS URL
оставлены в регистрации, их origin по-прежнему обязан совпасть с issuer;
сам AGS claim не служит условием входа.

Приложение не загружает delivery и не создаёт `Assignments`, `Grades`, AGS
клиент или poll worker. Маршруты собственного портала работ, назначения,
запуска отдельной работы и administrator grant не регистрируются. Корневая
страница проверенной сессии повторно начинает штатную native авторизацию.

## Подписанный вход и переход

OIDC login принимает только зарегистрированные issuer, target и client ID;
`client_id` можно опустить, но явно чужое или пустое значение отклоняется.
Проверки подписи RS256, уникального JWKS kid, audience/azp, subject,
deployment/context/resource, target, ролей, nonce, state, browser binding,
времени жизни и повторного использования сохраняются. Допустимы только роли
Learner и Instructor. Прежний UID вычисляется без изменений:

```text
gw-<SHA256(compact JSON [issuer,clientId,deploymentId,subject])>@example.invalid
```

Контекст, роль, отображаемое имя и native course instance не входят в UID.
Одна и та же identity получает прежний UID при переходе между ролями,
профилями и режимами. Session действует 3600 секунд; state — 300 секунд.

Сначала Gateway выдаёт непрозрачный handoff со сроком 60 секунд.
Доверенный proxy получает identity через приватный endpoint с отдельным bearer,
удаляет клиентские identity headers и запускает native callback. Bootstrap
принимается лишь после потребления handoff, для той же identity и роли,
в том же зарегистрированном профиле. Второе потребление и прямой bootstrap
без одноразового токена отклоняются. Секреты и токены нельзя журналировать.

После callback Gateway вызывает существующий приватный endpoint:

```http
PUT /internal/gateway/enrollment
Authorization: Bearer <private service token>
Content-Type: application/json

{"userUid":"gw-synthetic@example.invalid","instance":"pilot","courseKey":"java","role":"teacher","email":"teacher@example.invalid"}
```

Login-only запрос содержит ровно пять полей. `role` равен только `student`
или `teacher`, а `email` — нормализованному проверенному адресу либо `null`.
Bootstrap берёт оба значения из исходной сохранённой сессии handoff, а не
из параметров запроса, заголовков или более позднего входа того же пользователя.
Сохранённый режим `grading` отправляет прежние три поля без role/email.

Успешный ответ содержит ровно шесть полей, без role/email echo:

```json
{
  "schemaVersion": 1,
  "userUid": "gw-synthetic@example.invalid",
  "instance": "pilot",
  "courseKey": "java",
  "enrolled": true,
  "path": "/pl/course_instance/42/assessments"
}
```

Bridge строит путь из проверенного native binding. Gateway проверяет точные
типы и значения версии, identity, instance, courseKey и `enrolled`, отсутствие
лишних полей и форму пути `/pl/course_instance/[1-9][0-9]*/assessments`.
Абсолютный URL, query, fragment, завершающий slash, assessment конкретной
работы и нулевой ID отклоняются. HTTP redirect и неуспешный ответ Bridge
не становятся успешным входом. Bridge origin берётся из закрытой серверной
конфигурации с фиксированным портом 3001, credentials/path/query запрещены;
сетевой доступ к приватным listeners ограничивает оператор.

Gateway передаёт в приватный enrollment проверенную роль и email, но сам
не выбирает native административные права и не дублирует `ownerEmail`.
Настройка владельца и сопоставление native прав находятся только в Bridge:
точное совпадение доверенного email с `ownerEmail` даёт глобальные права
администратора независимо от роли Learner/Instructor. Затем для остальных
пользователей Instructor даёт преподавательские права выбранного курса,
Learner — Student. Отсутствие email у Instructor не мешает его преподавательским
правам, но не даёт глобальных прав владельца. Вход не отзывает уже существующие
native права; отдельное снятие прав выполняется штатно в PL.
Публичные `email`/`role`, произвольный пользователь и незарегистрированный
LTI issuer не являются источниками прав.

Вход по-прежнему не вызывает `/internal/gateway/administrator` и не включает
administrator cookie в Gateway. Старый login-only admin endpoint отсутствует.
Native grant выполняется только через приватный enrollment Bridge; доступность
лабораторных остаётся штатной политикой курса PL.

## Доверенный email

После проверки подписи и всех LTI регистрационных claims Gateway читает
стандартный `email`. Moodle может не передавать `email_verified`; отсутствие
этого claim допускается для подписанного зарегистрированного Moodle issuer.
Если `email_verified` присутствует, только boolean `true` разрешает использование
email. `false`, строки, числа и `null` не подтверждают адрес.

Нормализация убирает только внешние ASCII пробелы и приводит ASCII буквы к
нижнему регистру. Допустим обычный mailbox длиной не более 254 символов,
local part до 64 и domain labels до 63. Управляющие символы, Unicode, quoted
local part и неправильная форма дают `null`, сохраняя сам вход и проверенную
роль. Plus alias остаётся другим адресом; не выполняются удаление точек,
Unicode преобразования или другие правила объединения адресов.

Email хранится только в конкретной session и не меняет users identity tuple
или UID. Новый вход того же пользователя не заменяет email старого handoff.
Поле `email` приватного native-auth handoff по-прежнему равно UID, как и прежде;
проверенный адрес передаётся только в enrollment после bootstrap.

## SQLite версии 5

Новая база имеет версию 5. При открытии существующей распознанной версии 3
или 4 startup lock исключает одновременную миграцию. До writable connection
проверяются версия и форма `native_attempts`/`sessions`. Для v3 атомарная
транзакция сначала пересоздаёт только `sessions` с nullable `lineitem`, копируя
все девять прежних значений. Затем для v3/v4 в той же транзакции добавляется
nullable `sessions.email`, проверяются foreign keys и выставляется версия 5.
Все исторические sessions получают `email: null`. Ошибка откатывает и схему,
и номер версии. Остальные таблицы и все их строки сохраняются.
Прототип версии 1001 не принимается и не переносится.

Login-only activation явно не создаёт AGS activity binding. Обычная activation
сохраняет требование `lineitem` и прежнее поведение оценивания. Старые session,
nonce/state, handoff, CSRF, assignments, requests, audit, native attempts,
observations, outbox и activity bindings не удаляются. Это общая совместимая
runtime база с legacy таблицами, а не отдельное security-only хранилище.

Проверки используют вымышленные SQLite и подписанные HTTP launches. Они
покрывают UID обеих ролей, отсутствие AGS, приватный bearer, одноразовость,
сохранность заполненной версии 3, переход версий 3/4 с NULL email,
rollback при ошибке миграции и отказ для
неизвестной схемы. Они не подтверждают рабочую Moodle регистрацию,
существование десяти native лабораторных, фактические права преподавателя,
перенос production данных или проверенное восстановление резервной копии.
