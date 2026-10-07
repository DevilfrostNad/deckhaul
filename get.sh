#!/usr/bin/env bash
# Установка или обновление DeckHaul одной командой (Steam Deck, режим рабочего стола, Konsole):
#
#   curl -fsSL https://raw.githubusercontent.com/DevilfrostNad/deckhaul/main/get.sh | bash
#
# Скачивает последнюю версию с GitHub и запускает её install.sh.
# При первой установке DeckHaul добавляется в библиотеку Steam.
# Чтобы не добавлять: ... | bash -s -- --no-steam
set -euo pipefail

REPO="DevilfrostNad/deckhaul"

for tool in curl tar python3; do
  if ! command -v "$tool" >/dev/null; then
    echo "Не найдена программа $tool, установка невозможна." >&2
    exit 1
  fi
done

TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/tags?per_page=100" 2>/dev/null | python3 -c '
import json, re, sys
best = None
for t in json.load(sys.stdin):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", t.get("name", ""))
    if m:
        v = tuple(map(int, m.groups()))
        if best is None or v > best[0]:
            best = (v, t["name"])
print(best[1] if best else "")
' 2>/dev/null || true)"

if [ -n "$TAG" ]; then
  URL="https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz"
  echo "Скачиваю DeckHaul $TAG…"
else
  URL="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
  echo "Скачиваю DeckHaul (ветка main)…"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
curl -fsSL "$URL" | tar -xz -C "$WORK"
SRC="$(find "$WORK" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [ ! -f "$SRC/install.sh" ]; then
  echo "Скачанный архив не похож на DeckHaul." >&2
  exit 1
fi

ARGS=()
if [ "${1:-}" != "--no-steam" ] && [ ! -x "$HOME/.local/bin/deckhaul" ]; then
  ARGS+=(--add-to-steam)
fi
bash "$SRC/install.sh" "${ARGS[@]+"${ARGS[@]}"}"
