#!/usr/bin/env bash
# Установка DeckHaul на Steam Deck (режим рабочего стола, Konsole).
# Ничего не ставит в систему: всё лежит в домашней папке, пароль sudo не нужен.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/deckhaul-app"
BIN="$HOME/.local/bin"
DESKTOP="$HOME/.local/share/applications/deckhaul.desktop"

if ! command -v python3 >/dev/null; then
  echo "Не найден python3. На SteamOS он есть по умолчанию — проверьте, что система обновлена." >&2
  exit 1
fi
python3 - <<'PY' || { echo "Нужен Python 3.8 или новее" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY

mkdir -p "$APP" "$BIN" "$(dirname "$DESKTOP")"
rm -rf "$APP/deckhaul"
cp -r "$SRC/deckhaul" "$APP/"
cp "$SRC/assets/deckhaul.svg" "$APP/deckhaul.svg"

cat > "$BIN/deckhaul" <<'LAUNCH'
#!/usr/bin/env bash
# Запускает сервер DeckHaul (если он ещё не запущен) и открывает окно.
APP="$HOME/.local/share/deckhaul-app"
STATE="${XDG_DATA_HOME:-$HOME/.local/share}/deckhaul"
mkdir -p "$STATE"
if [ "$#" -gt 0 ]; then
  exec env PYTHONPATH="$APP" python3 -m deckhaul "$@"
fi
URL=""
if [ -f "$STATE/url" ] && curl -fs "$(cat "$STATE/url")api/ping" >/dev/null 2>&1; then
  URL="$(cat "$STATE/url")"
else
  rm -f "$STATE/url"
  PYTHONPATH="$APP" nohup python3 -m deckhaul serve --no-browser --idle-exit 900 >"$STATE/server.log" 2>&1 &
  for _ in $(seq 1 50); do
    [ -f "$STATE/url" ] && break
    sleep 0.2
  done
  URL="$(cat "$STATE/url" 2>/dev/null || true)"
fi
if [ -z "$URL" ]; then
  echo "DeckHaul не запустился, подробности в $STATE/server.log" >&2
  exit 1
fi
# Отдельное окно без адресной строки, если есть браузер на Chromium.
for id in com.google.Chrome org.chromium.Chromium com.microsoft.Edge com.brave.Browser; do
  if flatpak info "$id" >/dev/null 2>&1; then
    exec flatpak run "$id" --app="$URL" --window-size=1280,800
  fi
done
if flatpak info org.mozilla.firefox >/dev/null 2>&1; then
  exec flatpak run org.mozilla.firefox --new-window "$URL"
fi
exec xdg-open "$URL"
LAUNCH
chmod +x "$BIN/deckhaul"

cat > "$DESKTOP" <<DESK
[Desktop Entry]
Type=Application
Name=DeckHaul
Comment=Проверка и сортировка модов ETS2 и ATS
Exec=$BIN/deckhaul
Icon=$APP/deckhaul.svg
Terminal=false
Categories=Game;Utility;
DESK
chmod +x "$DESKTOP"

echo "Готово. DeckHaul есть в меню приложений, раздел «Игры»."
echo "Из Konsole: $BIN/deckhaul check — отчёт о проблемах без окна."

if [ "${1:-}" = "--add-to-steam" ] && command -v steamos-add-to-steam >/dev/null; then
  steamos-add-to-steam "$DESKTOP" && echo "Добавлено в библиотеку Steam: откройте игровой режим и найдите DeckHaul."
else
  echo "Чтобы запускать из игрового режима: ./install.sh --add-to-steam"
fi
