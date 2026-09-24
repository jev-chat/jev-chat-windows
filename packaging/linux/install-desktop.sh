#!/usr/bin/env bash
# 把 Jev 装进当前用户的「显示应用」和 GNOME Dock，不碰系统目录、不用 sudo。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
NAME=jev-chat-ubuntu
BIN="$HOME/.local/bin/$NAME"
APPDIR="$HOME/.local/share/applications"
ICON_ROOT="$HOME/.local/share/icons/hicolor"
DESKTOP_IN="$ROOT/packaging/linux/${NAME}.desktop"
DESKTOP_OUT="$APPDIR/${NAME}.desktop"

mkdir -p "$HOME/.local/bin" "$APPDIR" "$ICON_ROOT/scalable/apps"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  "$ROOT/.venv/bin/python" "$ROOT/tools/make_icon.py"
elif python3 -c "from PIL import Image" >/dev/null 2>&1; then
  python3 "$ROOT/tools/make_icon.py"
else
  echo "没有 Pillow，跳过 PNG；仍会安装 SVG 图标。" >&2
fi

cat > "$BIN" <<EOF
#!/usr/bin/env bash
exec "$ROOT/start.sh" "\$@"
EOF
chmod +x "$BIN"

install -m 644 "$ROOT/packaging/linux/${NAME}.svg" "$ICON_ROOT/scalable/apps/${NAME}.svg"
HICOLOR_SRC="$ROOT/packaging/linux/icons/hicolor"
if [[ -d "$HICOLOR_SRC" ]]; then
  while IFS= read -r src; do
    rel="${src#$HICOLOR_SRC/}"
    dest="$ICON_ROOT/$rel"
    mkdir -p "$(dirname "$dest")"
    install -m 644 "$src" "$dest"
  done < <(find "$HICOLOR_SRC" -type f -name "${NAME}.png")
fi
if [[ ! -f "$ICON_ROOT/index.theme" ]]; then
  cat > "$ICON_ROOT/index.theme" <<'EOF'
[Icon Theme]
Name=Hicolor
Comment=Fallback icon theme
Directories=16x16/apps,24x24/apps,32x32/apps,48x48/apps,64x64/apps,128x128/apps,256x256/apps,512x512/apps,scalable/apps
EOF
fi

sed "s|@BIN@|$BIN|" "$DESKTOP_IN" > "$DESKTOP_OUT"
chmod 644 "$DESKTOP_OUT"

if command -v desktop-file-validate >/dev/null 2>&1; then
  desktop-file-validate "$DESKTOP_OUT"
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPDIR"
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1 && [[ -d "$ICON_ROOT" ]]; then
  gtk-update-icon-cache -f "$ICON_ROOT" >/dev/null 2>&1 || true
fi

python3 - <<'PY'
import ast
import subprocess
import sys

desktop = "jev-chat-ubuntu.desktop"
try:
    raw = subprocess.check_output(
        ["gsettings", "get", "org.gnome.shell", "favorite-apps"], text=True
    ).strip()
except Exception as exc:
    print(f"未改 Dock：读不到 favorite-apps（{exc}）", file=sys.stderr)
    sys.exit(0)
if raw.startswith("@as "):
    raw = raw.split(" ", 1)[1]
apps = ast.literal_eval(raw)
if desktop in apps:
    print("Dock 里已经有 Jev")
    sys.exit(0)
apps.append(desktop)
subprocess.check_call(
    ["gsettings", "set", "org.gnome.shell", "favorite-apps", str(apps)]
)
print("已钉到 GNOME Dock")
PY

echo "桌面入口: $DESKTOP_OUT"
echo "启动命令: $BIN"
echo "图标: $ICON_ROOT/scalable/apps/${NAME}.svg"
echo "显示应用里搜「Jev」或「微信回复」即可打开。"
