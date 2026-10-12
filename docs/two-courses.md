# Два native-курса из одного курса Moodle

Один Moodle-курс Java может содержать отдельные элементы для работ Java и
System-Practice. На уровне Moodle регистрируется **один** LTI 1.3 tool.
При одинаковых issuer, client ID, deployment ID и subject Gateway вычисляет
один PL UID: переход к другому native-курсу не создаёт второго человека.

`config/courses.example.json` показывает конфигурацию `schemaVersion: 2`:
каждый именованный курс содержит закрытую Configuration v2. Его путь
строго равен `/gateway/courses/<id>`, delivery и SQLite file отдельные.
Cookie `GatewaySession` ограничен этим path; session, assignment, observation
и outbox другого курса не смешиваются. Для одного курса используется тот же
контейнер конфигурации `schemaVersion: 2` с единственной записью в `courses`
и путём `/gateway/courses/<id>`. Вложенная запись не содержит `schemaVersion`.
Путь `/gateway/lti/login` служит общим OIDC endpoint; отдельный однокурсовый
профиль через `/gateway` не поддерживается. Прежняя Configuration v1
отклоняется. Новая версия требует свежую БД schema3 без переноса старых назначений.

## Регистрация Moodle

Администратор Moodle один раз задаёт:

- OIDC initiation URL: `https://SERVER/gateway/lti/login`;
- разрешённые redirect URIs: обе ссылки
  `https://SERVER/gateway/courses/java/lti/launch` и
  `https://SERVER/gateway/courses/system-practice/lti/launch`;
- один открытый tool key либо соответствующий JWKS URL;
- отправку имени и ролей, получение оценок AGS, доверие к CA сервера.

Оба профиля Gateway должны использовать один signing key для общей регистрации.
В конфигурации нужны фактические issuer/client/deployment/context, authorization,
JWKS и token endpoints Moodle. Их нельзя угадать по номеру native-курса.
Для каждого элемента Moodle отдельно фиксируются подписанные resourceLinkId
и AGS lineitem. Два native-курса не могут владеть одним LTI resource tuple.

В самом Moodle-курсе преподаватель добавляет элементы «Внешний инструмент» с
общей регистрацией, но разными tool URL согласно выбранному native-курсу.
Java может иметь defense и lab элементы; System-Practice пилот — только lab.
Общий login выбирает только заранее зарегистрированный target_link_uri, затем
профиль проверяет подписанный JWT, nonce/state и точные activity bindings.

Настройка только элемента курса достаточна **после** общей регистрации tool,
доверия HTTPS и подготовки этих bindings оператором. Приёмка с локальным mock не подтверждает настройки Moodle учреждения; их проверяют отдельно на фактической регистрации.

## Автоматическое зачисление и преподаватель

Первый проверенный Student launch создаёт постоянную запись пользователя
в SQLite выбранного профиля и обеспечивает native Student enrollment в его
course instance. Общий UID связывает обе записи с одним native PL user.
Заранее переносить весь roster не требуется; до первого launch студент не
появляется в профиле. Данные переживают перезапуск Gateway при сохранении SQLite.

`administratorSubject` содержит точный subject единственного преподавателя
для этой регистрации. Native bridge отдельно проверяет соответствующий явный
`administratorUid`, фактического native user и закрытый служебный bearer.
Только эта identity с Instructor role получает кнопку административной панели
и штатный активный admin mode PL. Один Instructor claim или email не повышает
права. Разрешение глобального PL administrator даёт доступ ко всем курсам;
добавлять преподавателя отдельно в каждый native-курс не нужно.

## Оценки и обновления

Worker каждого профиля читает фактические native результаты своей delivery,
сохраняет observation/outbox и отправляет AGS score только в соответствующий
lineitem. Повторная отправка после сбоя не создаёт другую пользовательскую
identity. Подготовка новой immutable delivery одного курса описана
[в текущей инструкции Platform](https://github.com/Afonenko-Course-Tools/prairielearn-platform/blob/v2.0.0/docs/proxmox-pilot.md).
Изменённая delivery требует отдельного нового состояния профиля и свежей SQLite
БД schema3; прежние назначения и observations автоматически не переносятся.
Старую БД и snapshots можно сохранить как архив, но они не становятся состоянием
новой delivery после native sync. Другой профиль сохраняет свою delivery и БД.

При совместной приёмке двух курсов проверьте подписанные launches для нескольких Student identities и преподавателя: общую учётную запись и отдельные зачисления, запрет Student admin и чужих attempts, реальные текстовые native submissions и независимые AGS результаты. Выполнение обязательных позиций проверяется отдельно от взвешенной native оценки. Эта проверка не подтверждает настройки Moodle учреждения.
