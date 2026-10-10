# TLS локального Moodle и Gateway

Доверие браузера Windows, TLS-клиента Moodle/PHP и TLS-клиента Gateway настраивается
отдельно. Добавление CA в Windows не меняет доверие внутри контейнера Moodle:
при пустых `curl.cainfo`/`openssl.cafile` PHP cURL может вернуть error 60.
Проверка TLS остаётся включённой; отключать `CURLOPT_SSL_VERIFYPEER`, проверку имени
или `verify` в Gateway не требуется.

Для локального стенда преподаватель передаёт публичный корневой CA и независимо
подтверждённый SHA-256 **сертификата DER**, не хеш PEM-файла. Приватные ключи и PFX
не участвуют. Исходные публичные корни, локальный CA и готовый bundle храните
вне Git. Это настройка локального прототипа, не публикация CA или новых релизов.

## Новый ноутбук

На новом ноутбуке создайте собственный локальный CA и сертификат сервера
штатными средствами Platform (см. ссылку ниже). Не копируйте приватный ключ
CA этого стенда на другой ноутбук и не используйте его отпечаток как общий
постоянный pin. Подтвердите SHA-256 нового публичного CA, выберите образ Moodle,
извлеките его публичные корни и выполните шаги ниже. При общей учебной сети CA
выдаёт преподаватель; это отдельное развёртывание.

Зависимости: Docker/Compose для стека, Python 3 и установленный `openssl` для
помощника. Все входы передаются через аргументы/env; пути `/absolute/...` ниже
— заглушки для вашего checkout и каталога вне Git, а не пути автора инструкции.

## Bundle для Moodle/PHP

Скопируйте исходный bundle публичных корней **из того образа, который используется
для Moodle**, например `/etc/ssl/certs/ca-certificates.crt` для Debian-based Docker
PHP. Проверьте фактический путь в образе: универсального пути для всех образов нет.
Можно скопировать файл из уже работающего контейнера, не изменяя его:

```sh
docker cp MOODLE_CONTAINER:/etc/ssl/certs/ca-certificates.crt /absolute/private/public-roots.crt
```

Укажите реальные абсолютные пути и подтверждённый отпечаток вместо заглушек:

```sh
python3 tools/make-ca-bundle.py \
  --public-roots /absolute/private/public-roots.crt \
  --local-ca /absolute/private/ca.crt \
  --expected-ca-sha256 CERTIFICATE_DER_SHA256_64_HEX_DIGITS \
  --output /absolute/private/ca-bundle.crt
```

Нужны Python 3 и OpenSSL. Помощник сохраняет все различные сертификаты публичного
bundle, добавляет ровно один локальный CA и удаляет дубликаты по DER. Он отклоняет
приватные ключи, несовпадение SHA-256, не-CA, отсутствие `keyCertSign`, неверный срок
действия или самоподпись и symlink в путях. Выход атомарный, режим `0644`, чтобы
его мог читать `www-data`; родительские каталоги также должны разрешать проход
пользователю контейнера. Повторный запуск с теми же входами сохраняет байты и
mtime. Новый CA/обновление публичных корней требуют повторной генерации и
подтверждения нового pin. Не заменяйте публичный bundle одним локальным корнем:
Moodle нужны также обычные публичные HTTPS-сервисы.

`config/moodle-local-tls.ini` задаёт оба клиентских пути:

```ini
curl.cainfo=/run/tls/ca-bundle.crt
openssl.cafile=/run/tls/ca-bundle.crt
```

Override рассчитан на **Docker PHP** с `/usr/local/etc/php/conf.d/`; для другого
образа подтвердите каталог конфигурации PHP отдельно. Оба source-пути задаются
явно: относительные пути Compose относятся к первому Compose-файлу.

```sh
export MOODLE_TLS_BUNDLE=/absolute/private/ca-bundle.crt
export MOODLE_TLS_INI=/absolute/gateway-owner/config/moodle-local-tls.ini
docker compose -f /absolute/moodle/compose.yml \
  -f /absolute/gateway-owner/compose/compose.moodle-tls.yml config --quiet
docker compose -f /absolute/moodle/compose.yml \
  -f /absolute/gateway-owner/compose/compose.moodle-tls.yml \
  up -d --no-deps --force-recreate moodle
```

Сохраните остальные действующие override-файлы и прежнее имя Compose-проекта в
этих командах. Bundle/ini монтируются read-only, создание отсутствующего bind-path
запрещено. Пересоздаётся только Moodle; **не запускайте `down -v`**, не удаляйте
тома данных. Проверьте пути через PHP того же образа/SAPI, а затем реальный HTTPS
запрос Moodle с проверкой сертификата и имени. Простая проверка из браузера этого
не доказывает. Логи диагностики не должны содержать пароли, OAuth токены или ключи.

## Gateway, адреса и ключи

Для Python Gateway задайте `config.caFile` и смонтируйте соответствующий публичный
CA/bundle в этот путь, например `/run/tls/ca.crt`; контейнер должен читать файл.
`caFile` не является настройкой PHP и не добавляет доверие в Windows.

Доверие CA не решает маршрутизацию. В локальном прототипе зарегистрированный
`https://localhost:18443` из контейнера должен достигать нужного reverse proxy,
а не случайного loopback контейнера. В изолированной PHP-диагностике
`CURLOPT_RESOLVE` может направить **точное имя и порт** на IP proxy, сохраняя
исходный URL/SNI и включённую TLS-проверку. Это только тест маршрута, не глобальная
правка `/etc/hosts` и не способ отключить проверку имени. Постоянный маршрут
оформляет конфигурация развёртывания.

Для будущей LAN без DNS используйте согласованный статический IP VM. Серверный
сертификат должен иметь этот IP в SAN типа IP Address; регистрации Moodle/Gateway
и HTTPS origin должны соответствовать выбранному адресу. Не переносите
`localhost` на компьютеры студентов и не предполагаете, что доверенный CA
исправляет несовпадение SAN.

JWK/JWKS ключ инструмента LTI проверяет подпись сообщений; CA TLS проверяет сервер
HTTPS. Это разные ключи и разные контракты; приватный JWK не импортируется в
хранилище доверенных CA.

См. [Platform: локальное развёртывание и TLS](https://github.com/Afonenko-Course-Tools/prairielearn-platform/blob/main/docs/development.md)
и [студентам Windows: доверие CA устройств](https://github.com/BSU-RFCT-Afonenko-Courses/Java/blob/codex/windows-device-ca/docs/student-windows-certificates.md).
Последняя ссылка указывает на ветку инструкции до её включения в основную ветку;
наличие документа не означает, что CA уже выдан студентам.
