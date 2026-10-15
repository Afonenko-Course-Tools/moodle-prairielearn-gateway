# Именованные курсы и устойчивые назначения

Внешний запуск принимает только `{schemaVersion:2,courses:{name:courseConfig}}`.
Курс требует routePrefix `/gateway/courses/name`; вложенного schemaVersion нет.
Standalone конфигурация отклоняется. Курсы используют отдельные delivery и SQLite,
один HTTPS origin и согласованные LTI client key/handoff token. Delivery2
проверяется до Store. Новая БД сразу получает схему3; прежняя БД отклоняется read-only
до WAL и writable connection. Автоматических миграций и aliases нет.

Delivery hash и полный inventory проверяются при startup. Доступны только works
выбранного instance. Defense имеет канонический relatedExercise и native student-label;
обычный lab не имеет relatedExercise. Каждый essay соответствует одному defense
в instance. Сходство ID не задаёт отношение.

Server-owned flow — teacher, immediate либо approval. Capacity задаётся явно
положительным числом на essay; отсутствие quota означает unlimited. Student имеет
одно текущее назначение в instance. SQLite BEGIN IMMEDIATE сериализует выбор,
capacity и idempotency; один ключ связан с пользователем и точным request body.
Прежние запросы остаются в audit после replacement/rejection. Source QMD/native
файлы не изменяются действиями runtime.

В routePrefix доступны GET `/my/works`, POST `/assignments/request`, GET
`/teacher/roster`, POST `/teacher/assignments` и операции approve/reject/reassign.
Student видит labs и текущий active defense, teacher — весь selected состав.
POST `/launch/pl/{workId}` требует verified Student session и соответствующей
зарегистрированной Moodle activity. Browser mutations требуют Origin и CSRF.

Teacher/immediate резервируют reconciling, после подтверждения native ACL становятся
assigned. Approval резервирует pending без вызова ACL; approve переводит в
reconciling и затем approved. Rejected не имеет access; последующий выбор создаёт
новый request. Полное достижение pass.at-least может перевести active в completed.

Bridge получает stable assignmentId и закрытый version, server-derived userUid,
instance/deliveryHash/oldWork/newWork. Ответ подтверждает ту же scope и reconciled.
Version начинается с1 и возрастает на каждом native transition. Replacement
сохраняет assignmentId, снимает старую метку и подтверждает новую до activation.
Неудача сохраняет durable reconciliation intent для повторения после restart.
Идентичный retry повторно согласует ACL. Native прямые URL и сессии подчиняются
ACL так же, как список работ. Активный AGS send lease временно препятствует замене.

Gateway проверяет собственное current assignment перед private work-launch;
Platform независимо проверяет joined native Student access и journal/label.
Teacher не получает native staff/admin через этот маршрут. Отдельный explicit
administrator bootstrap определяется точной verified identity в private config.
Другая delivery и прежние данные не переключаются автоматически.

Новый instance в том же профиле использует отдельный scope назначений и
результатов. При сохранении schema3 SQLite, routePrefix и LTI bindings
неистёкшие session tokens и CSRF сохраняются; native bootstrap использует
новый instance/delivery. Назначения, audit и native attempts старого instance
не переносятся и не изменяются. Это не отменяет запрет несовместимой замены
delivery в том же instance. Свежая SQLite не распознаёт старые session tokens
даже при прежних service keys; такой вариант требует нового Moodle launch.
