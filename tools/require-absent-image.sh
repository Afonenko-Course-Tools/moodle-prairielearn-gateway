#!/usr/bin/env bash
set -euo pipefail
image=${1:?Укажите точный registry tag}
log=$(mktemp)
trap 'rm -f "$log"' EXIT
if docker manifest inspect "$image" >"$log" 2>&1; then
  echo 'Версия образа уже существует; выберите новый номер.' >&2
  exit 1
fi
if grep -Eqi 'unauthorized|denied|forbidden|timeout|connection|no such host' "$log" || ! grep -Eqi 'manifest unknown|no such manifest|manifest.*not found' "$log"; then
  echo 'Отсутствие версии в registry не подтверждено; публикация запрещена.' >&2
  cat "$log" >&2
  exit 1
fi
