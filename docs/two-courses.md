# Несколько курсов на одном origin

`config/courses.example.json` задаёт `java` и `system-practice`. У каждого курса
свои routePrefix, database path, LTI resourceLinkId и native `(instance,courseKey)`.
Общий origin и общий handoff service token нужны private proxy. Identity UID
вычисляется из Moodle registration tuple и subject, поэтому один участник
сохраняет UID в обоих курсах при одинаковой регистрации.

Moodle может начать login через `/gateway/lti/login`: dispatcher выбирает ровно
один курс по точному зарегистрированному `target_link_uri`. Доступен и отдельный
`/gateway/courses/<key>/lti/login`. Cookie `GatewayLogin` всегда имеет
`Path=/gateway`; она доступна root dispatcher и nested callbacks обоих курсов.
Два одноимённых cookie отклоняются. При параллельных первых login без cookie
callback должен совпадать с фактически принятой браузером cookie.

Новый login отменяет прежнюю попытку только в своей course DB. После genuine
launch snapshot содержит регистрацию, activity, target и native mapping;
изменение mapping после restart требует нового Moodle launch. Подстановка
текущего mapping вместо сохранённого не допускается.

Native PL sessions, grants и logout принадлежат PL. Gateway не хранит часовой
SSO, registry пользователей, работы или оценки и не перевыдаёт handoff через home.
