#!/usr/bin/env bash
set -u

# Use the host GPU (DRI) for Wine's own GL calls instead of the llvmpipe
# software renderer.  llvmpipe is pure-CPU and causes both high CPU usage and
# inflated memory.  The host's /dev/dri is passed through via docker-compose
# so Mesa/DRI hardware rendering is available when Xorg is used.
export MESA_GL_VERSION_OVERRIDE=4.5
# WINEFSYNC uses futex-based synchronisation (kernel ≥ 5.16, esync compatible).
# This replaces the busy-polling done by wineserver and dramatically lowers
# idle CPU.  Kernel 6.12 fully supports it.
export WINEFSYNC=1
export WINESYNC=1

export DISPLAY="${DISPLAY:-:0}"
export WINEPREFIX="${WINEPREFIX:-/opt/wineprefix}"
export WINE_BIN="${WINE_BIN:-/opt/deepin-wine8-stable/bin/wine}"
export WINESERVER_BIN="${WINESERVER_BIN:-/opt/deepin-wine8-stable/bin/wineserver}"
export QUARK_RUNTIME="${QUARK_RUNTIME:-spark}"

echo "=== quark-docker startup ==="
echo "  QUARK_RUNTIME : $QUARK_RUNTIME"
echo "  WINEPREFIX    : $WINEPREFIX"
echo "  WINE_BIN      : $WINE_BIN"
echo "  DISPLAY       : $DISPLAY"
echo "  Running as UID: $(id -u)"

rm -f /tmp/.X0-lock /tmp/.X11-unix/X0
mkdir -p /tmp/.X11-unix /tmp/runtime-wineuser
chmod 1777 /tmp/.X11-unix 2>/dev/null || true
if [ "$(id -u)" = "0" ]; then
    chown wineuser:wineuser /tmp/runtime-wineuser
fi
chmod 700 /tmp/runtime-wineuser
cat > /tmp/runtime-wineuser/asoundrc <<'EOF'
pcm.!default {
    type null
}
EOF
if [ "$(id -u)" = "0" ]; then
    chown wineuser:wineuser /tmp/runtime-wineuser/asoundrc
fi

# Auto-detect VAAPI driver for Intel GPU hardware video acceleration.
echo "--- DRI devices ---"
ls /dev/dri/ 2>/dev/null || echo "  (none)"
if [ -z "${LIBVA_DRIVER_NAME:-}" ]; then
    if [ -f /usr/lib/x86_64-linux-gnu/dri/iHD_drv_video.so ]; then
        export LIBVA_DRIVER_NAME=iHD    # Gen8+ (Broadwell and newer)
    elif [ -f /usr/lib/x86_64-linux-gnu/dri/i965_drv_video.so ]; then
        export LIBVA_DRIVER_NAME=i965   # Gen4–7 (Sandy/Ivy Bridge)
    fi
fi
if [ -n "${LIBVA_DRIVER_NAME:-}" ]; then
    echo "VAAPI driver  : $LIBVA_DRIVER_NAME"
else
    echo "VAAPI driver  : (not detected)"
fi

# Try hardware-accelerated Xorg (modesetting + DRI3) so Wine's wined3d can
# use the Intel GPU via Mesa/iris.  Fall back to Xvfb if DRI card is absent
# or Xorg fails to start.
#
# Key fixes vs. the previous (white-screen) attempt:
#   - MESA_GL_VERSION_OVERRIDE is unset for real hardware (it was a llvmpipe
#     hack; overriding on real hardware causes extension-set mismatches).
#   - --in-process-gpu is NOT passed so the GPU process is separate; if GPU
#     init fails it crashes independently and the renderer falls back to
#     software instead of hanging the whole browser (white screen).
_use_hw_xorg=0
if [ -e /dev/dri/card0 ] || [ -e /dev/dri/card1 ]; then
    echo "--- Starting Xorg (modesetting/DRI3) ---"
    Xorg :0 -noreset -nolisten tcp -config /etc/X11/xorg-headless.conf \
         -logfile /tmp/Xorg.0.log &
    _xorg_pid=$!
    _waited=0
    while [ $_waited -lt 8 ] && ! [ -S /tmp/.X11-unix/X0 ]; do
        sleep 1
        _waited=$((_waited + 1))
    done
    if [ -S /tmp/.X11-unix/X0 ]; then
        echo "Xorg started with hardware DRI3 (pid $_xorg_pid)."
        _use_hw_xorg=1
        # Real hardware reports its own GL version — override is not needed.
        unset MESA_GL_VERSION_OVERRIDE
        export MESA_LOADER_DRIVER_OVERRIDE="${MESA_LOADER_DRIVER_OVERRIDE:-iris}"
        echo "  MESA_LOADER_DRIVER_OVERRIDE: $MESA_LOADER_DRIVER_OVERRIDE"
    else
        echo "Xorg failed to start (see /tmp/Xorg.0.log); falling back to Xvfb."
        kill "$_xorg_pid" 2>/dev/null || true
    fi
fi

if [ "$_use_hw_xorg" = "0" ]; then
    echo "--- Starting Xvfb (software fallback) ---"
    Xvfb :0 -screen 0 1024x768x16 -ac -noreset +extension GLX -dpi 96 &
fi
sleep 2
echo "--- Starting x11vnc on :5900 ---"
x11vnc -display :0 -forever -nopw -rfbport 5900 -cursor most -quiet &

echo "--- Resolving Quark runtime ---"
if [ "$QUARK_RUNTIME" = "spark" ] && [ -f "/opt/spark-bottle/drive_c/Program Files (x86)/quark-cloud-drive/QuarkCloudDrive.exe" ]; then
    export WINEPREFIX="/opt/spark-bottle"
    EXE="/opt/spark-bottle/drive_c/Program Files (x86)/quark-cloud-drive/QuarkCloudDrive.exe"
    START_LNK="C:\\users\\Public\\Desktop\\夸克网盘.lnk"
    DATA_DIR="$WINEPREFIX/drive_c/users/wineuser/Application Data/quark-cloud-drive"
    DOWNLOADS_DIR="$WINEPREFIX/drive_c/users/wineuser/Downloads"
    echo "  Runtime       : spark bottle"
else
    EXE="$WINEPREFIX/drive_c/users/wineuser/AppData/Local/Programs/QuarkCloudDrive/quark_cloud_drive.exe"
    if [ ! -f "$EXE" ]; then
        EXE="$(find "$WINEPREFIX/drive_c" \( -iname 'quark_cloud_drive.exe' -o -iname 'QuarkCloudDrive.exe' \) 2>/dev/null | grep -v proxy | head -1)"
    fi
    START_LNK=""
    DATA_DIR="$WINEPREFIX/drive_c/users/wineuser/AppData/Local/QuarkCloudDrive"
    DOWNLOADS_DIR="$WINEPREFIX/drive_c/users/wineuser/Downloads"
    echo "  Runtime       : standard wineprefix"
fi
if [ -z "${EXE:-}" ] || [ ! -f "$EXE" ]; then
    echo "ERROR: Quark executable not found"
    tail -f /dev/null
fi
echo "  EXE           : $EXE"
echo "  DATA_DIR      : $DATA_DIR"
echo "  DOWNLOADS_DIR : $DOWNLOADS_DIR"

mkdir -p "$DATA_DIR"
mkdir -p "$DOWNLOADS_DIR"
if [ "$(id -u)" = "0" ]; then
    chown -R wineuser:wineuser "$DATA_DIR"
    chmod -R u+rwX "$DATA_DIR"
    if ! su -s /bin/bash wineuser -c "test -w '$DATA_DIR'"; then
        echo "ERROR: Quark data directory is not writable by wineuser: $DATA_DIR"
        ls -ld "$DATA_DIR"
        tail -f /dev/null
    fi

    chown -R wineuser:wineuser "$DOWNLOADS_DIR"
    chmod -R u+rwX "$DOWNLOADS_DIR"
    if ! su -s /bin/bash wineuser -c "test -w '$DOWNLOADS_DIR'"; then
        echo "ERROR: Quark downloads directory is not writable by wineuser: $DOWNLOADS_DIR"
        ls -ld "$DOWNLOADS_DIR"
        tail -f /dev/null
    fi
fi

echo "--- Launching Wine ---"
echo "  MESA_GL_VERSION_OVERRIDE : ${MESA_GL_VERSION_OVERRIDE:-}"
echo "  WINEFSYNC / WINESYNC     : $WINEFSYNC / $WINESYNC"
echo "Starting Quark: $EXE"

# Auto-detect GPU: if a DRI render node is accessible (passed through via
# docker-compose devices), let Chromium use hardware acceleration through
# Wine's wined3d → Mesa/DRI stack and omit --disable-gpu.
# Without a GPU, fall back to software rendering.
if [ -e /dev/dri/renderD128 ] || [ -e /dev/dri/card0 ]; then
    echo "GPU detected via DRI; enabling hardware acceleration for Chromium."
    _gpu_flags=""
else
    echo "No DRI device found; disabling Chromium GPU acceleration."
    _gpu_flags="--disable-gpu
    --disable-gpu-compositing"
fi

# --in-process-gpu is intentionally omitted: it merges the GPU process into
# the browser process, so a GPU init hang blocks rendering (white screen).
# With a separate GPU process, failures trigger a graceful software fallback.
# The three background-throttling flags (--disable-background-timer-throttling,
# --disable-backgrounding-occluded-windows, --disable-renderer-backgrounding)
# are intentionally NOT set: when the manager minimizes the Quark window we
# want Chromium to actually throttle background tabs to free up CPU.
QUARK_EXTRA_ARGS="
    $_gpu_flags
    --disable-dev-shm-usage
    --renderer-process-limit=1
    --disable-background-networking
    --js-flags=--max-old-space-size=256
"

echo "--- Launching manager (API on :${QUARK_API_PORT:-8080}) ---"
# The manager owns: FastAPI, the in-process CDP proxy (9223 → 9222), and the
# idle monitor. It will exec launch-quark.sh to start the actual Quark
# process group; that one lives in its own session so we can kill it cleanly.
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 is required for the manager"
    tail -f /dev/null
fi
export EXE
export START_LNK
export WINEPREFIX
export WINEDEBUG
export WINE_BIN
export WINESERVER_BIN
export QUARK_EXTRA_ARGS
export WINEFSYNC
export WINESYNC
export LIBVA_DRIVER_NAME
# Mesa env-vars: pass through; launch-quark.sh unsets them if empty.
exec python3 /usr/local/bin/manager.py
