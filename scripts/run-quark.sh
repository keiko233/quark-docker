#!/usr/bin/env bash
set -u

# Use the host GPU (DRI) for Wine's own GL calls instead of the llvmpipe
# software renderer.  llvmpipe is pure-CPU and causes both high CPU usage and
# inflated memory (CrGpuMain 1 GB).  The host's /dev/dri is passed through via
# docker-compose so Mesa/DRI hardware rendering is available.
#
# IMPORTANT: Chromium's GPU process still gets --in-process-gpu below so it
# runs inside the browser process rather than as a separate CrGpuMain, saving
# another 1 GB of RAM even without hardware GL compositing.
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

Xvfb :0 -screen 0 1024x768x16 -ac -noreset +extension GLX -dpi 96 &
sleep 2
x11vnc -display :0 -forever -nopw -rfbport 5900 -cursor most -quiet &

if [ "$QUARK_RUNTIME" = "spark" ] && [ -f "/opt/spark-bottle/drive_c/Program Files (x86)/quark-cloud-drive/QuarkCloudDrive.exe" ]; then
    export WINEPREFIX="/opt/spark-bottle"
    EXE="/opt/spark-bottle/drive_c/Program Files (x86)/quark-cloud-drive/QuarkCloudDrive.exe"
    START_LNK="C:\\users\\Public\\Desktop\\夸克网盘.lnk"
    DATA_DIR="$WINEPREFIX/drive_c/users/wineuser/Application Data/quark-cloud-drive"
    DOWNLOADS_DIR="$WINEPREFIX/drive_c/users/wineuser/Downloads"
else
    EXE="$WINEPREFIX/drive_c/users/wineuser/AppData/Local/Programs/QuarkCloudDrive/quark_cloud_drive.exe"
    if [ ! -f "$EXE" ]; then
        EXE="$(find "$WINEPREFIX/drive_c" \( -iname 'quark_cloud_drive.exe' -o -iname 'QuarkCloudDrive.exe' \) 2>/dev/null | grep -v proxy | head -1)"
    fi
    START_LNK=""
    DATA_DIR="$WINEPREFIX/drive_c/users/wineuser/AppData/Local/QuarkCloudDrive"
    DOWNLOADS_DIR="$WINEPREFIX/drive_c/users/wineuser/Downloads"
fi
if [ -z "${EXE:-}" ] || [ ! -f "$EXE" ]; then
    echo "ERROR: Quark executable not found"
    tail -f /dev/null
fi

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

echo "Starting Quark executable: $EXE"

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

QUARK_EXTRA_ARGS="
    $_gpu_flags
    --in-process-gpu
    --disable-dev-shm-usage
    --renderer-process-limit=1
    --disable-background-networking
    --disable-background-timer-throttling=0
    --disable-backgrounding-occluded-windows
    --disable-renderer-backgrounding
    --js-flags=--max-old-space-size=256
"

if command -v python3 >/dev/null 2>&1; then
    python3 /usr/local/bin/cdp-proxy.py 9223 9222 &
else
    echo "ERROR: python3 is required for CDP proxy"
    tail -f /dev/null
fi

wine_cmd="
    export DISPLAY='$DISPLAY'
    export WINEPREFIX='$WINEPREFIX'
    export WINEDEBUG='${WINEDEBUG:--all}'
    export XDG_RUNTIME_DIR='/tmp/runtime-wineuser'
    export ALSA_CONFIG_PATH='/tmp/runtime-wineuser/asoundrc'
    export WINE_BIN='$WINE_BIN'
    export WINESERVER_BIN='$WINESERVER_BIN'
    export QUARK_EXTRA_ARGS='$QUARK_EXTRA_ARGS'
    export WINEFSYNC='$WINEFSYNC'
    export WINESYNC='$WINESYNC'
    export MESA_GL_VERSION_OVERRIDE='$MESA_GL_VERSION_OVERRIDE'
    \"\$WINESERVER_BIN\" -k >/dev/null 2>&1 || true
    cd '$(dirname "$EXE")'
    if [ -n '$START_LNK' ]; then
        \"\$WINE_BIN\" 'C:\\windows\\command\\start.exe' /Unix '$START_LNK' \
            --no-sandbox \
            --remote-debugging-port='9222' \
            --remote-debugging-address=0.0.0.0 \
            --remote-allow-origins='*' \
            \$QUARK_EXTRA_ARGS
        tail -f /dev/null
    else
        DATA_DIR=\"\$WINEPREFIX/drive_c/users/wineuser/AppData/Local/QuarkCloudDrive/User Data\"
        rm -rf \"\$DATA_DIR/Snapshots\" \"\$DATA_DIR\"/Snapshots.CHROME_DELETE*
        exec \"\$WINE_BIN\" '$(basename "$EXE")' \
            --launch-from=startmenu \
            --brand-clouddrive \
            --remote-debugging-port='9222' \
            --remote-debugging-address=0.0.0.0 \
            --remote-allow-origins='*' \
            \$QUARK_EXTRA_ARGS
    fi
"

if [ "$(id -u)" = "0" ]; then
    su -s /bin/bash wineuser -c "$wine_cmd"
else
    bash -lc "$wine_cmd"
fi
status=$?
echo "Quark exited with status $status; keeping VNC alive for inspection."
tail -f /dev/null
