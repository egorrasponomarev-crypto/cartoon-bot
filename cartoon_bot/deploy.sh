#!/usr/bin/env bash
# Обновляет бота на сервере до версии с GitHub. Файл .env и база учеников (bot.db) не меняются.
#
# Запуск — одна команда в PowerShell на компьютере (подробнее — README, раздел 8.6):
#   ssh root@213.155.13.213 "bash /root/kyrs/deploy.sh"
#
# Что делает: скачивает бота с GitHub, проверяет код, сохраняет резервную копию текущей версии,
# доустанавливает библиотеки, заменяет файлы бота, перезапускает его и показывает журнал.
set -euo pipefail

# Всё тело — в функции: bash прочитает файл целиком до запуска. Скрипт обновляет и сам себя,
# и без этого bash продолжил бы читать уже новый файл с середины.
main() {
REPO="${REPO:-egorrasponomarev-crypto/cartoon-bot}"
BRANCH="${1:-${BRANCH:-claude/keen-mccarthy-t8o4bf}}"  # ветка на GitHub, из которой ставим бота
BOT_DIR="${BOT_DIR:-/root/kyrs}"
SERVICE="${SERVICE:-kyrs-bot}"
PY="$BOT_DIR/.venv/bin/python"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "== Скачиваю бота с GitHub: $REPO, ветка $BRANCH"
if ! curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" | tar -xz -C "$tmp" --strip-components=1; then
    echo "Не получилось скачать бота с GitHub (нет связи или нет ветки $BRANCH) — ничего не меняю."
    exit 1
fi
src="$tmp/cartoon_bot"
if [ ! -f "$src/bot.py" ] || [ ! -d "$src/handlers" ]; then
    echo "В скачанной версии нет cartoon_bot/bot.py или папки handlers — ничего не меняю."
    exit 1
fi
rm -f "$src/.env" "$src"/bot.db*  # в репозитории их нет, но настройки и базу на сервере не трогаем никогда

echo "== Проверяю код"
PYTHONPYCACHEPREFIX="$tmp/pycache" "$PY" -m py_compile "$src"/*.py "$src"/handlers/*.py

echo "== Доустанавливаю библиотеки"
"$PY" -m pip install -q --disable-pip-version-check --root-user-action=ignore -r "$src/requirements.txt"

backup="$(dirname "$BOT_DIR")/$(basename "$BOT_DIR")-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
echo "== Резервная копия текущей версии: $backup"
tar -czf "$backup" -C "$(dirname "$BOT_DIR")" \
    --exclude="$(basename "$BOT_DIR")/.venv" --exclude="$(basename "$BOT_DIR")/bot.db*" \
    --exclude="__pycache__" "$(basename "$BOT_DIR")"

echo "== Обновляю файлы в $BOT_DIR"
cp -r "$src"/. "$BOT_DIR"/

echo "== Перезапускаю бота"
since="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl restart "$SERVICE"
sleep 15
log="$(journalctl -u "$SERVICE" --since "$since" --no-pager -o cat | grep -v 'aiohttp.access' || true)"
echo "$log" | tail -n 25

echo
# «Бот @… запущен» бот пишет, только когда Telegram принял токен и всё загрузилось
if systemctl is-active --quiet "$SERVICE" && grep -q 'Бот @.* запущен' <<<"$log" && ! grep -q 'Traceback' <<<"$log"; then
    echo "✅ Готово: бот обновлён и работает."
else
    echo "⚠️ После обновления бот не запустился или в журнале есть ошибка (выше)."
    echo "Вернуть прежнюю версию — вставь в PowerShell на компьютере:"
    echo "  ssh root@213.155.13.213 \"tar -xzf $backup -C $(dirname "$BOT_DIR") && systemctl restart $SERVICE\""
    exit 1
fi
}

main "$@"; exit
