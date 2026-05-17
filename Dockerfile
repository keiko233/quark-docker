# syntax=docker/dockerfile:1
FROM scottyhardy/docker-wine:latest AS base

# Common runtime for both the manual installer environment and the final image.
# Quark's current Chromium build exits with status 5 under Wine 11 in this
# image. The old AUR package uses deepin-wine8-stable, so keep that runtime
# available and run Quark through it.
ENV RDP_SERVER=no \
    TZ=Asia/Shanghai \
    USER_UID=1000 \
    USER_GID=1000 \
    LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh \
    LC_ALL=zh_CN.UTF-8 \
    WINEPREFIX=/opt/wineprefix \
    WINEARCH=win64 \
    WINEDEBUG=-all \
    DISPLAY=:0 \
    DEEPIN_WINE8_URL=https://mirrors.sdu.edu.cn/spark-store/amd64-store/depends/deepin-wine8/deepin-wine8-stable_8.16deepin41_spark1_amd64.deb \
    SPARK_QUARK_URL=https://mirrors.sdu.edu.cn/spark-store/store/network/cn.quarkclouddrive.spark/cn.quarkclouddrive.spark_3.2.6spark4_all.deb \
    WINE_BIN=/opt/deepin-wine8-stable/bin/wine \
    WINESERVER_BIN=/opt/deepin-wine8-stable/bin/wineserver

# WINEDLLOVERRIDES is not set globally because the installer may need mscoree.
# Suppress Mono/Gecko only during wineboot in the prepare script.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        fonts-wqy-microhei \
        fonts-wqy-zenhei \
        locales \
        x11vnc \
        xdotool \
        zstd \
        p7zip-full \
        curl \
        ca-certificates \
        libdrm2 \
        libva2 \
        libva-drm2 \
        libva-x11-2 \
        libgl1-mesa-dri \
        intel-media-va-driver \
        i965-va-driver \
        libasound2-plugins \
        libcapi20-3 \
        libgstreamer1.0-0 \
        libgstreamer-plugins-base1.0-0 \
        libpulse0 \
        libsdl2-2.0-0 \
        libwayland-client0 \
        libxcomposite1 \
        libxcursor1 \
        libxfixes3 \
        libxi6 \
        libxinerama1 \
        libxrandr2 \
        libxrender1 \
        libxxf86vm1 \
    && locale-gen zh_CN.UTF-8 \
    && curl -fL "$DEEPIN_WINE8_URL" -o /tmp/deepin-wine8.deb \
    && dpkg-deb -x /tmp/deepin-wine8.deb / \
    && rm -f /tmp/deepin-wine8.deb \
    && rm -rf /var/lib/apt/lists/* \
    && (getent group 1000 >/dev/null || groupadd -g 1000 wineuser) \
    && (id -u wineuser >/dev/null 2>&1 || useradd -u 1000 -g 1000 -m -s /bin/bash wineuser) \
    && mkdir -p /opt/wineprefix /tmp/.X11-unix \
    && chown wineuser:wineuser /opt/wineprefix \
    && chmod 1777 /tmp/.X11-unix \
    && mkdir -p /etc/X11 \
    && cat > /etc/X11/xorg-headless.conf <<'XORGEOF'
Section "ServerFlags"
    Option "AutoAddDevices"  "false"
    Option "AutoEnableDevices" "false"
    Option "AllowEmptyInput" "true"
    Option "DontVTSwitch"    "true"
EndSection

Section "Device"
    Identifier "GPU"
    Driver     "modesetting"
    Option     "DRI" "3"
EndSection

Section "Monitor"
    Identifier "Monitor0"
    HorizSync   28-80
    VertRefresh 48-75
EndSection

Section "Screen"
    Identifier "Screen0"
    Device     "GPU"
    Monitor    "Monitor0"
    DefaultDepth 24
    SubSection "Display"
        Depth  24
        Modes  "1280x720"
    EndSubSection
EndSection
XORGEOF

FROM base AS install-debug

COPY --chown=wineuser:wineuser ./installer.exe /tmp/installer.exe
COPY --chown=wineuser:wineuser ./cjk-fonts.reg /tmp/cjk-fonts.reg

USER root

CMD rm -f /tmp/.X0-lock /tmp/.X11-unix/X0 && \
    Xvfb :0 -screen 0 1024x768x16 -ac & \
    sleep 2 && \
    x11vnc -display :0 -forever -nopw -rfbport 5900 -cursor most -quiet & \
    tail -f /dev/null

FROM base

RUN --mount=type=bind,source=wineprefix.tar.zst,target=/tmp/wineprefix.tar.zst \
    rm -rf /opt/wineprefix && \
    mkdir -p /opt/wineprefix && \
    tar --zstd -xf /tmp/wineprefix.tar.zst -C /opt/wineprefix && \
    curl -fL "$SPARK_QUARK_URL" -o /tmp/quark-spark.deb && \
    mkdir -p /tmp/quark-spark /opt/spark-bottle && \
    dpkg-deb -x /tmp/quark-spark.deb /tmp/quark-spark && \
    7z x -y -o/opt/spark-bottle /tmp/quark-spark/opt/apps/cn.quarkclouddrive.spark/files/files.7z; \
    rc=$?; ([ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]) && \
    rm -rf /opt/spark-bottle/drive_c/users/wineuser && \
    cp -a /opt/spark-bottle/drive_c/users/@current_user@ /opt/spark-bottle/drive_c/users/wineuser && \
    mkdir -p /opt/spark-bottle/dosdevices && \
    ln -sfn ../drive_c /opt/spark-bottle/dosdevices/c: && \
    ln -sfn / /opt/spark-bottle/dosdevices/z: && \
    chown -R wineuser:wineuser /opt/wineprefix && \
    chown -R wineuser:wineuser /opt/spark-bottle && \
    rm -rf /tmp/quark-spark.deb /tmp/quark-spark

COPY ./scripts/run-quark.sh /usr/local/bin/run-quark.sh
COPY ./scripts/cdp-proxy.py /usr/local/bin/cdp-proxy.py

RUN chmod +x /usr/local/bin/run-quark.sh

USER root

EXPOSE 9223

ENTRYPOINT ["/usr/local/bin/run-quark.sh"]
