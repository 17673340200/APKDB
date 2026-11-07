#!/usr/bin/env bash
# 一条命令构建安卓 APK（必须在 Linux / WSL2 上跑，Windows 宿主不受 pyside6-android-deploy 支持）。
#
# 用法：
#   ./android/build_linux.sh                 # 默认 aarch64（arm64-v8a，绝大多数手机/平板），出 debug APK
#   ARCH=x86_64 ./android/build_linux.sh     # 换架构（模拟器用）
#   MODE=release ./android/build_linux.sh    # 出 .aab（上架用）
#
# 前置要求：
#   - Ubuntu/Debian（或 WSL2 Ubuntu），能 sudo（装 JDK17 与基础工具）
#   - 网络可达 dl.google.com / qt 下载源
# 首次运行会下载约 5-8 GB（Android SDK/NDK、安卓版 PySide6 轮子、Gradle 依赖），
# 之后全部缓存在 ~/.cache/mediaplayer-android，重复构建很快。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCH="${ARCH:-aarch64}"            # aarch64 = arm64-v8a
MODE="${MODE:-debug}"              # debug → .apk；release → .aab
WORK="${WORK:-$HOME/.cache/mediaplayer-android}"
SDK_ROOT="$WORK/android-sdk"
NDK_VERSION="28.2.13676358"        # NDK r28c（pyside6-android-deploy 强制要求）
CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-13114758_latest.zip"
APP_NAME="MediaPlayer"

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "此脚本只能在 Linux/WSL2 上运行（宿主限制来自 pyside6-android-deploy）"

# ---------- 1. 系统依赖 ----------
if ! command -v java >/dev/null 2>&1; then
    log "安装 JDK 17（需要 sudo）"
    sudo apt-get update -y
    sudo apt-get install -y openjdk-17-jdk-headless unzip wget
fi
JAVA_MAJOR="$(java -version 2>&1 | head -n1 | sed 's/[^0-9]*\([0-9]*\).*/\1/')"
[ "${JAVA_MAJOR:-0}" -ge 17 ] || die "需要 JDK 17+（当前 java -version 主版本 $JAVA_MAJOR）"

# ---------- 2. Android SDK / NDK ----------
if [ ! -d "$SDK_ROOT/cmdline-tools/latest" ]; then
    log "安装 Android 命令行工具 → $SDK_ROOT"
    mkdir -p "$WORK" "$SDK_ROOT/cmdline-tools"
    wget -q --show-progress -O "$WORK/cmdline-tools.zip" "$CMDLINE_TOOLS_URL" \
        || die "下载 commandlinetools 失败（检查网络/代理）"
    unzip -q "$WORK/cmdline-tools.zip" -d "$WORK"
    mv "$WORK/cmdline-tools" "$SDK_ROOT/cmdline-tools/latest"
fi
SDKMANAGER="$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager"

if [ ! -d "$SDK_ROOT/ndk/$NDK_VERSION" ]; then
    log "安装 platform-tools / android-35 / build-tools / NDK r28c（约 3GB，一次性的）"
    yes | "$SDKMANAGER" --sdk_root="$SDK_ROOT" --licenses >/dev/null || true
    "$SDKMANAGER" --sdk_root="$SDK_ROOT" \
        "platform-tools" "platforms;android-35" "build-tools;35.0.0" "ndk;$NDK_VERSION" >/dev/null
fi
NDK_PATH="$SDK_ROOT/ndk/$NDK_VERSION"

# ---------- 3. 宿主 Python 环境（必须建在项目目录之外） ----------
if [ ! -x "$WORK/venv/bin/python" ]; then
    log "创建构建虚拟环境 $WORK/venv"
    python3 -m venv "$WORK/venv" || { sudo apt-get install -y python3-venv && python3 -m venv "$WORK/venv"; }
fi
PIP="$WORK/venv/bin/pip"
"$PIP" install -q --upgrade pip wheel

# ---------- 4. 下载安卓版 PySide6 / Shiboken6 轮子并让宿主环境版本一致 ----------
if ! ls "$WORK/wheels/"*shiboken* "$WORK/wheels/"*PySide6* >/dev/null 2>&1; then
    # qtpip 是依赖系统 Qt 库的编译二进制，缺库时先补上（libqt6core6 会带上依赖）
    if ! ldconfig -p 2>/dev/null | grep -q libdouble-conversion; then
        log "安装 qtpip 所需系统库（libdouble-conversion / Qt6 Core）"
        sudo apt-get update -y >/dev/null 2>&1 || true
        sudo apt-get install -y libqt6core6 >/dev/null 2>&1 \
            || sudo apt-get install -y libdouble-conversion3 >/dev/null 2>&1 \
            || true
    fi
    log "下载安卓版 PySide6 / Shiboken6 轮子（架构 $ARCH）"
    "$PIP" install -q qtpip
    QTPIP="$WORK/venv/bin/qtpip"
    mkdir -p "$WORK/wheels"
    # qtpip 没有 --outdir 参数，默认下载到当前目录，所以切进去执行
    ( cd "$WORK/wheels" && "$QTPIP" download PySide6   --android --arch "$ARCH" )
    ( cd "$WORK/wheels" && "$QTPIP" download Shiboken6 --android --arch "$ARCH" ) || true
fi
log "wheels 目录内容：$(ls "$WORK/wheels/" 2>/dev/null | tr '\n' ' ')"
PYSIDE_WHEEL="$(ls -t "$WORK/wheels/"PySide6-*"$ARCH"*.whl 2>/dev/null | head -n1 || true)"
[ -n "$PYSIDE_WHEEL" ] || PYSIDE_WHEEL="$(ls -t "$WORK/wheels/"PySide6_Essentials-*"$ARCH"*.whl 2>/dev/null | head -n1 || true)"
[ -n "$PYSIDE_WHEEL" ] || die "没有找到安卓版 PySide6 轮子（qtpip download 是否成功？见上面 wheels 目录内容）"
SHIBOKEN_WHEEL="$(ls -t "$WORK/wheels/"shiboken6-*"$ARCH"*.whl 2>/dev/null | head -n1 || true)"
[ -n "$SHIBOKEN_WHEEL" ] || die "没有找到安卓版 Shiboken6 轮子"

PY_VER="$(basename "$PYSIDE_WHEEL" | sed 's/^[^-]*-\([^-]*\)-.*/\1/')"
log "安卓轮子版本：$PY_VER（宿主 PySide6 安装同版本以保持一致）"
"$PIP" install -q --upgrade "pyside6==$PY_VER"

DEPLOY="$WORK/venv/bin/pyside6-android-deploy"
[ -x "$DEPLOY" ] || die "pyside6-android-deploy 不在 venv 里（PySide6 版本 $PY_VER 可能不含该工具，换 6.9.x 试）"

# ---------- 5. 组装 staging（部署工具要求入口必须叫 main.py） ----------
STAGING="$ROOT/android/staging"
log "组装构建目录 $STAGING"
rm -rf "$STAGING"
mkdir -p "$STAGING/core" "$STAGING/ui" "$STAGING/android"
cp "$ROOT/main_android.py"          "$STAGING/main.py"
cp "$ROOT/qtcompat.py"              "$STAGING/"
cp "$ROOT/core/__init__.py"         "$STAGING/core/"
cp "$ROOT/core/control_client.py"   "$STAGING/core/"
cp "$ROOT/core/control_protocol.py" "$STAGING/core/"
cp "$ROOT/core/file_protocol.py"    "$STAGING/core/"
cp "$ROOT/core/file_server.py"      "$STAGING/core/"
cp "$ROOT/core/schedule.py"         "$STAGING/core/"
cp "$ROOT/ui/__init__.py"           "$STAGING/ui/"
cp "$ROOT/ui/video_view.py"         "$STAGING/ui/"
cp "$ROOT/android/__init__.py"      "$STAGING/android/"
cp "$ROOT/android/pdf_view.py"      "$STAGING/android/"
cp "$ROOT/android/image_slideshow.py" "$STAGING/android/"
cp "$ROOT/android/mobile_window.py" "$STAGING/android/"

# ---------- 6. 构建包 ----------
cd "$STAGING"
log "运行 pyside6-android-deploy（首次要下 Gradle 依赖，耐心等）"
"$DEPLOY" --name "$APP_NAME" \
    --wheel-pyside="$PYSIDE_WHEEL" \
    --wheel-shiboken="$SHIBOKEN_WHEEL" \
    --ndk-path="$NDK_PATH" \
    --sdk-path="$SDK_ROOT" \
    --extra-modules Network,Multimedia,Pdf \
    --force

APK="$(ls -t "$STAGING"/*.apk 2>/dev/null | head -n1 || true)"
if [ -n "$APK" ]; then
    log "完成：$APK（adb install 或直接拷到手机安装）"
else
    AAB="$(ls -t "$STAGING"/*.aab 2>/dev/null | head -n1 || true)"
    [ -n "$AAB" ] && log "完成：$AAB（bundletool 安装）" || die "没找到产物 apk/aab，请检查上面的构建日志"
fi
