#!/usr/bin/env bash
# Launch a Quark Cloud Drive instance under Wine.
#
# Reads configuration from the environment (populated by run-quark.sh):
#   EXE             absolute path to QuarkCloudDrive.exe
#   START_LNK       Windows-style .lnk path to start from (spark runtime) or empty
#   WINEPREFIX      prefix directory
#   QUARK_EXTRA_ARGS extra Chromium flags
#   WINE_BIN, WINESERVER_BIN
#   DISPLAY, LIBVA_DRIVER_NAME, MESA_*
#
# Intended to be exec'd as the wineuser so the resulting process group is owned
# by wineuser. When launched by manager.py we do `su wineuser` first; this
# script does not assume a particular uid.
set -u

EXE="${EXE:?EXE is required}"
WINE_BIN="${WINE_BIN:-/opt/deepin-wine8-stable/bin/wine}"
WINESERVER_BIN="${WINESERVER_BIN:-/opt/deepin-wine8-stable/bin/wineserver}"
START_LNK="${START_LNK:-}"
DISPLAY="${DISPLAY:-:0}"
WINEDEBUG="${WINEDEBUG:--all}"
QUARK_EXTRA_ARGS="${QUARK_EXTRA_ARGS:-}"

# Make sure the global flags below are set on the wine process.
export DISPLAY
export LIBVA_DRIVER_NAME="${LIBVA_DRIVER_NAME:-}"
export WINEDEBUG
export XDG_RUNTIME_DIR="/tmp/runtime-wineuser"
export ALSA_CONFIG_PATH="/tmp/runtime-wineuser/asoundrc"

# Mesa env-var lines: exporting an empty string is rejected by Mesa validators,
# so unset the variable when it's not set.
if [ -n "${MESA_GL_VERSION_OVERRIDE:-}" ]; then
    export MESA_GL_VERSION_OVERRIDE
else
    unset MESA_GL_VERSION_OVERRIDE 2>/dev/null || true
fi
if [ -n "${MESA_LOADER_DRIVER_OVERRIDE:-}" ]; then
    export MESA_LOADER_DRIVER_OVERRIDE
else
    unset MESA_LOADER_DRIVER_OVERRIDE 2>/dev/null || true
fi

"$WINESERVER_BIN" -k >/dev/null 2>&1 || true
cd "$(dirname "$EXE")"

# Split QUARK_EXTRA_ARGS into an array so word splitting produces individual flags.
# shellcheck disable=SC2206
_extra=( $QUARK_EXTRA_ARGS )

if [ -n "$START_LNK" ]; then
    # Spark runtime — start via the .lnk so the launcher gets the right cwd.
    exec "$WINE_BIN" 'C:\windows\command\start.exe' /Unix "$START_LNK" \
        --no-sandbox \
        --remote-debugging-port=9222 \
        --remote-debugging-address=0.0.0.0 \
        --remote-allow-origins='*' \
        "${_extra[@]}"
else
    # Standard wineprefix runtime — clear stale snapshots then exec the exe.
    DATA_DIR="$WINEPREFIX/drive_c/users/wineuser/AppData/Local/QuarkCloudDrive/User Data"
    rm -rf "$DATA_DIR/Snapshots" "$DATA_DIR"/Snapshots.CHROME_DELETE* 2>/dev/null || true
    exec "$WINE_BIN" "$(basename "$EXE")" \
        --launch-from=startmenu \
        --brand-clouddrive \
        --remote-debugging-port=9222 \
        --remote-debugging-address=0.0.0.0 \
        --remote-allow-origins='*' \
        "${_extra[@]}"
fi
