# Переход на новую auth DB

Gateway 3 заменяет прежнее приложение и не имеет режимов grading/login-only.
Старые mode, deliveryPath, flow, privateKeyFile, tokenUrl, activity kind/lineitem,
pollSeconds и administratorSubject в конфигурации отклоняются.

Для каждого курса укажите новый отдельный database path, например
`/var/lib/gateway/java-auth.sqlite`. Новая схема содержит только `auth_schema`
и `auth_attempts`, без users, sessions и учебных таблиц. Существующая старая или
неподдерживаемая SQLite отклоняется через read-only проверку до writable
connection, WAL, chmod или миграции. Автоматического переноса и очистки старой БД
нет. Никогда не направляйте новый процесс на прежнюю рабочую SQLite.

Сохраните прежний checkout/config/database и все private files. Новый Gateway
не читает private signing key и не удаляет его. UID не меняется при сохранении
issuer/clientId/deploymentId/sub; email не используется для merge. Пользователи,
работы, оценки и native grants в PL остаются его данными.

Прежние `GatewayBinding` и `GatewaySession` не принимаются. Нужен новый явный
переход из Moodle; только login initiation выдаёт `GatewayLogin` с единым
`Path=/gateway`. Старые cookie можно убрать настройками браузера, но callbacks
Gateway не удаляют их и не управляют native logout.

Согласуйте private proxy handoff и пяти-полевой enrollment контракт с Platform.
Удалите mounts delivery/private signing key из новой конфигурации Gateway,
сохранив сами исходные файлы. Владелец Moodle отдельно убирает stale tool-JWKS URL
штатной настройкой регистрации, сохраняя identity tuple. TLS и Moodle public JWKS
остаются необходимыми зависимостями.

Для отката остановите новый процесс и верните сохранённые прежние image/config/DB
своим штатным порядком deployment. Не удаляйте native PL account/session/grants
и не заменяйте старую SQLite новым auth файлом. Новая auth DB не является
совместимой БД прежнего приложения. Реальное переключение и browser acceptance
выполняются владельцем стенда отдельно от изменений этого репозитория.
