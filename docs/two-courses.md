# Два native-курса из одного курса Moodle

Один Moodle-курс Java может содержать отдельные элементы для работ Java и
System-Practice. На уровне Moodle регистрируется **один** LTI 1.3 tool.
При одинаковых issuer, client ID, deployment ID и subject Gateway вычисляет
один PL UID: переход к другому native-курсу не создаёт второго человека.

`config/courses.example.json` показывает конфигурацию `schemaVersion: 2`:
каждый именованный курс содержит обычную закрытую Configuration v1. Его путь
строго равен `/gateway/courses/<id>`, delivery и SQLite file отдельные.
Cookie `GatewaySession` ограничен этим path; session, assignment, observation
и outbox другого курса не смешиваются. Старый однокурсовый формат продолжает
работать через `/gateway`.

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
доверия HTTPS и подготовки этих bindings оператором. Для настоящего Moodle эти
действия пока не выполнены: приёмка использует локальный mock.

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
identity. Замена одного native курса описана в платформенной инструкции
`docs/proxmox-pilot.md`: выбирается новая immutable delivery только у одного
профиля, затем выполняется его native sync. SQLite и старые snapshots сохраняются.

Серверная проверка mock Moodle → VM использует две Student identities и явного
преподавателя: общая учётная запись и два зачисления, запрет Student admin и
чужих attempts, текстовые native submissions, два независимых AGS результата.
Она не подтверждает настройки настоящего Moodle учреждения.
