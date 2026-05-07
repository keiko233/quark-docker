#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-quark-docker:install-debug}"
CONTAINER="${CONTAINER:-quark-install-debug}"
VNC_PORT="${VNC_PORT:-5900}"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/.prepared}"
OUT_FILE="${OUT_FILE:-$ROOT_DIR/wineprefix.tar.zst}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-1800}"

docker_cmd=(sudo docker)

cd "$ROOT_DIR"
mkdir -p "$OUT_DIR"

echo "Building installer/VNC image: $IMAGE"
"${docker_cmd[@]}" build --target install-debug -t "$IMAGE" .

echo "Starting installer container: $CONTAINER"
"${docker_cmd[@]}" rm -f "$CONTAINER" >/dev/null 2>&1 || true
"${docker_cmd[@]}" run -d \
    --name "$CONTAINER" \
    --hostname "$CONTAINER" \
    --platform linux/amd64 \
    -p "$VNC_PORT:5900" \
    --security-opt seccomp=unconfined \
    --shm-size=2gb \
    -v "$OUT_DIR:/out" \
    "$IMAGE" >/dev/null

echo "VNC is available at <host-ip>:$VNC_PORT with no password."
echo "Initializing Wine prefix and launching the Quark installer..."

"${docker_cmd[@]}" exec -u wineuser "$CONTAINER" bash -lc '
set -e
WINEDLLOVERRIDES="mscoree,mshtml=" XDG_RUNTIME_DIR=/tmp wineboot --init &
for i in $(seq 1 60); do
    [ -f "$WINEPREFIX/system.reg" ] && echo "Wine prefix ready after ${i}s" && break
    sleep 1
done
[ -f "$WINEPREFIX/system.reg" ]
mkdir -p "$WINEPREFIX/drive_c/windows/Fonts"
find /usr/share/fonts \( -name "wqy-microhei.ttc" -o -name "wqy-zenhei.ttc" \) \
    -exec cp {} "$WINEPREFIX/drive_c/windows/Fonts/" \;
XDG_RUNTIME_DIR=/tmp wine regedit /S /tmp/cjk-fonts.reg
nohup env XDG_RUNTIME_DIR=/tmp wine /tmp/installer.exe --restart=deelevated \
    >/tmp/quark-installer.log 2>&1 &
( while true; do
    WID=$(DISPLAY=:0 xdotool search --onlyvisible --name "." 2>/dev/null | while read -r w; do
        name=$(DISPLAY=:0 xdotool getwindowname "$w" 2>/dev/null)
        case "$name" in *Firefox*|"") ;; *) echo "$w"; break ;; esac
    done | head -1)
    if [ -n "$WID" ]; then
        WNAME=$(DISPLAY=:0 xdotool getwindowname "$WID" 2>/dev/null)
        echo "Clicking: $WID ($WNAME)"
        DISPLAY=:0 xdotool windowactivate --sync "$WID" 2>/dev/null || true
        sleep 1
        eval "$(DISPLAY=:0 xdotool getwindowgeometry --shell "$WID" 2>/dev/null)"
        echo "Window geometry: ${WIDTH}x${HEIGHT}+${X}+${Y}"
        TARGET_X=$((X + 445))
        TARGET_Y=$((Y + 126))
        DISPLAY=:0 xdotool mousemove "$TARGET_X" "$TARGET_Y" click 1 key Return
        echo "Clicked installer action at ${TARGET_X},${TARGET_Y}"
        sleep 8
    fi
    sleep 2
done ) >/tmp/quark-clicker.log 2>&1 &
echo "$!" >/tmp/quark-clicker.pid
'

echo "Waiting for Quark executable to appear in the prefix."
echo "The clicker is running automatically; use VNC if the installer needs manual input."

deadline=$((SECONDS + TIMEOUT_SECONDS))
exe=""
while [ "$SECONDS" -lt "$deadline" ]; do
    exe="$("${docker_cmd[@]}" exec -u wineuser "$CONTAINER" bash -lc \
        'find "$WINEPREFIX/drive_c" \( -iname "quark_cloud_drive.exe" -o -iname "QuarkCloudDrive.exe" \) 2>/dev/null | grep -v proxy | head -1' \
        | tr -d "\r")"
    if [ -n "$exe" ]; then
        echo "Found Quark executable: $exe"
        break
    fi
    sleep 5
done

if [ -z "$exe" ]; then
    echo "ERROR: Quark executable was not found after ${TIMEOUT_SECONDS}s." >&2
    echo "Clicker log:" >&2
    "${docker_cmd[@]}" exec -u wineuser "$CONTAINER" bash -lc 'tail -120 /tmp/quark-clicker.log 2>/dev/null || true' >&2
    echo "Installer log:" >&2
    "${docker_cmd[@]}" exec -u wineuser "$CONTAINER" bash -lc 'tail -120 /tmp/quark-installer.log 2>/dev/null || true' >&2
    exit 1
fi

echo "Stopping Wine processes and archiving prefix..."
"${docker_cmd[@]}" exec -u wineuser "$CONTAINER" bash -lc \
    'kill "$(cat /tmp/quark-clicker.pid 2>/dev/null)" 2>/dev/null || true'
"${docker_cmd[@]}" exec -u wineuser "$CONTAINER" bash -lc 'wineserver -k || true; sleep 2'
"${docker_cmd[@]}" exec "$CONTAINER" bash -lc \
    'rm -f /out/wineprefix.tar.zst && tar --zstd -cf /out/wineprefix.tar.zst -C "$WINEPREFIX" .'

cp "$OUT_DIR/wineprefix.tar.zst" "$OUT_FILE"
sudo chown "$(id -u):$(id -g)" "$OUT_FILE"

echo "Created $OUT_FILE"
echo "You can now run: sudo docker build -t quark-docker:latest ."
