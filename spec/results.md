# Результаты позиций и Moodle AGS

Единственный транспорт: `schemaVersion:2`, `source:per-slot-results-v2`. Defense
добавляет assignmentId/version; lab сохраняет scoreGiven/scoreMaximum. Оба ответа
содержат userUid/instance/deliveryHash/workId, state/currentAttempt/slots/completion.
Старые results и delivery отклоняются. Gateway получает результаты только от
аутентифицированного private Bridge; публичных callbacks с оценкой нет.

`not-started` требует null currentAttempt и пустые slots, без выдуманного варианта;
completedRequired0, requiredCount/atLeast берутся из delivery. `started` содержит
currentAttempt `{id,number}` и ровно одну строку `{slotId,qualifiedId,score,points,
maxPoints,status}` каждой позиции. Gateway независимо проверяет позицию, кандидата,
числа, штатный статус и completion. Не выбранные альтернативы отсутствуют в
знаменателе. Полное выполнение требует complete, score1 и points=maxPoints>0.
Optional и частичный результат не заменяют обязательную позицию.

Defense связывается с активным назначением, его version, пользователем и точной
поставкой. AGS scoreGiven=completedRequired, scoreMaximum=requiredCount. Lab
связывается с зарегистрированной Moodle activity/context/lineitem, work и delivery;
его AGS сохраняет фактические native weighted points (например6/8) независимо
от completion. Отдельного defense назначения для lab нет.

Попытка — штатный assessment instance. Старый номер не создаёт событие; ID при том
же номере не меняется. Набор выбранных вопросов внутри экземпляра сохраняется
в native_attempts уже при первом валидном started, до законченной оценки,
при повторных отправках, переоценке и после перезапуска. Привязка содержит
instance/delivery/work и фиксированную assignment/activity scope. not-started
не создаёт привязку. Отсутствие законченной
оценки не меняет прежнюю Moodle grade.

Observation и outbox фиксируются одной SQLite транзакцией. Событие связывает
достоверные данные, фиксированную activity/assignment scope и предыдущую ревизию.
Повтор одинакового опроса возвращает прежнее событие и payload; A→B→A переоценка
создаёт новую ревизию. Timestamp монотонен для user/lineitem, а транспортный retry
сохраняет его. Перед отправкой повторно проверяются active assignment/version,
delivery, latest attempt/revision и Moodle binding. Старые события становятся
obsolete и не перезаписывают новую оценку. Expiring send lease защищает от
конкурентной отправки и замены назначения, восстановление возможно после restart.

Worker опрашивает Bridge по умолчанию каждые15 секунд, согласует durable ACL intent
и затем отправляет outbox. OAuth использует фиксированный issuer/token endpoint,
короткую RS256 client assertion и score scope. POST направляется только на
зарегистрированный lineitem `/scores` с сохранением query, стандартным content type
и стабильным Idempotency-Key. Redirects запрещены, TLS verification включён;
401 сбрасывает token cache, транспортные ошибки повторяются с bounded backoff.

При regrade ниже pass.at-least completed возвращается в assigned/approved.
Замена назначения сохраняет прежнюю Moodle оценку до достоверного нового результата.
Другая поставка и прежняя БД требуют нового состояния, автоматических миграций нет.
`tools/check-integration.py` читает существующее состояние read-only и не подтверждает
настоящую Moodle приёмку: она требует отдельного live readback и двух пользователей.
