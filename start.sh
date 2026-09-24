#!/usr/bin/env bash
# Ubuntu / GNOME Wayland：用 Python 3.12 建 venv（RapidOCR 1.4 不支持 3.13+），采集走系统 Python 的 PyGObject。
# 显示应用 / Dock：packaging/linux/install-desktop.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  if ! command -v uv >/dev/null 2>&1; then
    echo "需要 uv 或已建好的 .venv。安装 uv: curl -fsSL https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
  uv venv --python 3.12 "$ROOT/.venv"
  uv pip install --python "$ROOT/.venv/bin/python" -r "$ROOT/requirements.txt"
  PY="$ROOT/.venv/bin/python"
fi

PYSIDE_PLUGINS="$ROOT/.venv/lib/python3.12/site-packages/PySide6/Qt/plugins"
if [[ -d "$PYSIDE_PLUGINS" ]]; then
  export QT_PLUGIN_PATH="$PYSIDE_PLUGINS"
  export QT_QPA_PLATFORM_PLUGIN_PATH="$PYSIDE_PLUGINS/platforms"
fi
if [[ -n "${JEV_QT_PLATFORM:-}" ]]; then
  export QT_QPA_PLATFORM="$JEV_QT_PLATFORM"
fi

exec "$PY" "$ROOT/main.py" "$@"
