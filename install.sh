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

# A running copy keeps the old code in memory: stop it, the launcher starts the new one.
# The in-app updater restarts itself and sets DECKHAUL_SELF_UPDATE.
if [ -z "${DECKHAUL_SELF_UPDATE:-}" ] && pkill -f "(python3 -m deckhaul|deckhaul\.__main__ import main.*) serve" 2>/dev/null; then
  echo "Открытый DeckHaul закрыт, запустите его снова после установки."
fi

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
# "python3 -m" would prefer a deckhaul folder in the current directory (a git
# clone, for example). Put the installed copy first on the path explicitly.
BOOT='import sys; sys.path.insert(0, sys.argv.pop(1)); from deckhaul.__main__ import main; sys.exit(main())'
if [ "$#" -gt 0 ]; then
  exec python3 -c "$BOOT" "$APP" "$@"
fi

# Started from the menu or from Steam there is no terminal: keep a log.
LOG="$STATE/launcher.log"
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 200000 ]; then mv -f "$LOG" "$LOG.old"; fi
exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T') запуск DeckHaul"

game_mode() {
  [ -n "${GAMESCOPE_WAYLAND_DISPLAY:-}" ] || [ "${XDG_CURRENT_DESKTOP:-}" = "gamescope" ] ||
    [ "${SteamGamepadUI:-}" = "1" ]
}

show_error() {
  echo "ОШИБКА: $1"
  if command -v kdialog >/dev/null; then kdialog --title DeckHaul --error "$1" && return; fi
  if command -v zenity >/dev/null; then zenity --error --title=DeckHaul --text="$1" && return; fi
  if command -v notify-send >/dev/null; then notify-send DeckHaul "$1"; fi
}

alive() { curl -fs --max-time 2 "${1}api/ping" >/dev/null 2>&1; }

URL=""
if [ -f "$STATE/url" ] && alive "$(cat "$STATE/url")"; then
  URL="$(cat "$STATE/url")"
  echo "сервер уже запущен: $URL"
else
  rm -f "$STATE/url"
  nohup python3 -c "$BOOT" "$APP" serve --no-browser --idle-exit 900 >"$STATE/server.log" 2>&1 &
  PID=$!
  for _ in $(seq 1 150); do
    [ -f "$STATE/url" ] && break
    kill -0 "$PID" 2>/dev/null || break
    sleep 0.2
  done
  URL="$(cat "$STATE/url" 2>/dev/null || true)"
fi
if [ -z "$URL" ]; then
  show_error "DeckHaul не запустился.

$(tail -n 6 "$STATE/server.log" 2>/dev/null)

Полный журнал: $STATE/server.log"
  exit 1
fi
echo "адрес: $URL, игровой режим: $(game_mode && echo да || echo нет)"

# 1. A browser from Discover (Flatpak). Chromium-based ones open as an app window.
for id in com.google.Chrome org.chromium.Chromium com.microsoft.Edge com.brave.Browser; do
  if flatpak info "$id" >/dev/null 2>&1; then
    echo "открываю в $id"
    if game_mode; then
      exec flatpak run "$id" --kiosk --start-fullscreen "$URL"
    fi
    exec flatpak run "$id" --app="$URL" --window-size=1280,800
  fi
done
if flatpak info org.mozilla.firefox >/dev/null 2>&1; then
  echo "открываю в Firefox"
  if game_mode; then exec flatpak run org.mozilla.firefox --kiosk "$URL"; fi
  exec flatpak run org.mozilla.firefox --new-window "$URL"
fi

# 2. A browser installed into the system.
for b in google-chrome-stable chromium firefox; do
  if command -v "$b" >/dev/null; then
    echo "открываю в $b"
    exec "$b" "$URL"
  fi
done

# 3. No browser at all (a fresh Steam Deck): use the one built into Steam.
#    Stay alive while DeckHaul runs, otherwise game mode closes the overlay.
if command -v steam >/dev/null; then
  echo "браузера нет, открываю во встроенном браузере Steam"
  steam "steam://openurl/$URL" >/dev/null 2>&1 &
  while alive "$URL"; do sleep 5; done
  exit 0
fi

echo "открываю через xdg-open"
xdg-open "$URL" || show_error "Не нашёл, чем открыть DeckHaul. Установите Firefox или Google Chrome в Discover и запустите DeckHaul снова. Адрес программы: $URL"
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

VERSION="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import deckhaul; print(deckhaul.__version__)' "$APP")"
echo "Готово: DeckHaul $VERSION. Он есть в меню приложений, раздел «Игры»."
echo "Из Konsole: $BIN/deckhaul check — отчёт о проблемах без окна."

if [ "${1:-}" = "--add-to-steam" ] && command -v steamos-add-to-steam >/dev/null; then
  steamos-add-to-steam "$DESKTOP" && echo "Добавлено в библиотеку Steam: откройте игровой режим и найдите DeckHaul."
elif command -v steamos-add-to-steam >/dev/null; then
  echo "Чтобы запускать из игрового режима, добавьте DeckHaul в Steam:"
  echo "  steamos-add-to-steam $DESKTOP"
fi
