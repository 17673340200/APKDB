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
# 无条件安装（apt 对已装包秒过）：p4a 编译 native 组件需要完整 autotools 工具链，
# 缺 libtool 宏时 libffi 的 autoreconf 会报 LT_SYS_SYMBOL_USCORE undefined
log "安装 JDK 21 与构建工具链（需要 sudo；Qt 6.12 的安卓构建要求 21+）"
sudo apt-get update -y >/dev/null 2>&1 || true
sudo apt-get install -y openjdk-21-jdk-headless unzip wget curl zip unzip ccache \
    build-essential autoconf automake libtool libtool-bin libltdl-dev gettext pkg-config zlib1g-dev
JAVA_MAJOR="$(java -version 2>&1 | head -n1 | sed 's/[^0-9]*\([0-9]*\).*/\1/')"
[ "${JAVA_MAJOR:-0}" -ge 21 ] || die "需要 JDK 21+（Qt 6.12 要求；当前 java 主版本 $JAVA_MAJOR）"

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

# buildozer 只认旧版 SDK 布局（tools/bin/sdkmanager），给它补符号链接（缓存命中时也要补）
if [ -x "$SDKMANAGER" ] && [ ! -e "$SDK_ROOT/tools/bin/sdkmanager" ]; then
    log "为 buildozer 补旧版 SDK 目录布局（tools/bin → cmdline-tools/latest）"
    mkdir -p "$SDK_ROOT/tools"
    ln -sfn "$SDK_ROOT/cmdline-tools/latest/bin" "$SDK_ROOT/tools/bin"
    ln -sfn "$SDK_ROOT/cmdline-tools/latest/lib" "$SDK_ROOT/tools/lib"
fi

# ---------- 3. 宿主 Python 环境（必须建在项目目录之外） ----------
if [ ! -x "$WORK/venv/bin/python" ]; then
    log "创建构建虚拟环境 $WORK/venv"
    python3 -m venv "$WORK/venv" || { sudo apt-get install -y python3-venv && python3 -m venv "$WORK/venv"; }
fi
PIP="$WORK/venv/bin/pip"
# buildozer 只看 VIRTUAL_ENV 环境变量决定是否给 pip 加 --user（venv 里 --user 会报错），
# 必须显式导出；同时把 venv/bin 放进 PATH，让 buildozer 找到 cython 等可执行文件
export VIRTUAL_ENV="$WORK/venv"
export PATH="$WORK/venv/bin:$PATH"
"$PIP" install -q --upgrade pip wheel

# ---------- 4. 从 Qt 官方公开归档下载安卓版轮子（开源版，无需登录/qtpip） ----------
# 归档结构：QtForPython/pyside6/pyside6-<ver>-<ver>-cp310-abi3-android_<arch>.whl（直接在顶层）
QT_ARCHIVE="https://download.qt.io/official_releases/QtForPython"
mkdir -p "$WORK/wheels"

if ! ls "$WORK/wheels/"*shiboken* >/dev/null 2>&1; then
    log "从 Qt 公开归档下载安卓版轮子（架构 $ARCH）"
    for pkg in pyside6 shiboken6; do
        name="$(curl -fsSL "$QT_ARCHIVE/$pkg/" | grep -oE 'href="[^"]+"' \
            | sed 's/^href="//;s/"$//' \
            | grep -Ei "android_${ARCH}\.whl$" | sort -V | tail -n1)"
        [ -n "$name" ] || die "$pkg 在归档里没有 android_${ARCH} 的轮子（检查 $QT_ARCHIVE/$pkg/）"
        log "下载 $name"
        curl -fL --retry 3 -o "$WORK/wheels/$name" "$QT_ARCHIVE/$pkg/$name"
    done
fi
log "wheels 目录内容：$(ls "$WORK/wheels/" 2>/dev/null | tr '\n' ' ')"
PYSIDE_WHEEL="$(ls -t "$WORK/wheels/"*[Ss]ide6-*android*"$ARCH"*.whl 2>/dev/null | head -n1 || true)"
[ -n "$PYSIDE_WHEEL" ] || die "没有找到安卓版 PySide6 轮子（见上面 wheels 目录内容）"
SHIBOKEN_WHEEL="$(ls -t "$WORK/wheels/"*shiboken*"$ARCH"*.whl 2>/dev/null | head -n1 || true)"
[ -n "$SHIBOKEN_WHEEL" ] || die "没有找到安卓版 Shiboken6 轮子"

PY_VER="$(basename "$PYSIDE_WHEEL" | sed 's/^[^-]*-\([^-]*\)-.*/\1/')"
log "安卓轮子版本：$PY_VER（宿主 PySide6 安装同版本以保持一致）"
"$PIP" install -q --upgrade "pyside6==$PY_VER"

# 部署工具自检要求安装的安卓构建依赖（buildozer / python-for-android 等）
REQ="$("$WORK/venv/bin/python" -c "import pathlib, PySide6; print(pathlib.Path(PySide6.__file__).parent / 'scripts' / 'requirements-android.txt')")"
if [ -f "$REQ" ]; then
    log "安装部署工具要求的安卓依赖：$(basename "$REQ")"
    "$PIP" install -q -r "$REQ"
fi

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

# ---------- 5.5 给部署工具打补丁：修 android.add_jars ----------
# 部署工具的 __find_jars 依赖 Qt 轮子内 XML 的 <jar file="jar/..."> 条目，
# Qt 6.12 轮子里解析不出（QtActivityBase not found 的根因），这里改为
# 把 jars_dir 下全部 jar 都写进 android.add_jars（buildozer 原生支持，会透传给 p4a --add-jar）
BOOSTR_PY="$("$WORK/venv/bin/python" -c "import pathlib, PySide6; print(pathlib.Path(PySide6.__file__).parent / 'scripts' / 'deploy_lib' / 'android' / 'buildozer.py')")"
"$WORK/venv/bin/python" - "$BOOSTR_PY" <<'PYEOF'
import sys
from pathlib import Path

p = Path(sys.argv[1])
t = p.read_text(encoding="utf-8")
old = '        self.set_value("app", "android.add_jars", ",".join(jars))'
new = ('        # PATCH v2: robust jar injection — extract jars directly from the\n'
       '        # wheel; do NOT rely on jars_dir being set/valid at this point\n'
       '        # (observed flaky: jars_dir sometimes unset despite spec value).\n'
       '        import zipfile as _zf\n'
       '        _jars_out = (Path(pysidedeploy_config.generated_files_path)\n'
       '                     / "jar" / "PySide6" / "jar")\n'
       '        try:\n'
       '            with _zf.ZipFile(pysidedeploy_config.wheel_pyside) as _z:\n'
       '                for _n in _z.namelist():\n'
       '                    if _n.startswith("PySide6/jar/") and _n.endswith(".jar"):\n'
       '                        _out = _jars_out / Path(_n).name\n'
       '                        if not _out.exists() or _out.stat().st_size == 0:\n'
       '                            _out.parent.mkdir(parents=True, exist_ok=True)\n'
       '                            _out.write_bytes(_z.read(_n))\n'
       '                        jars.add(str(_out))\n'
       '            print(f"jar 补丁：{len(jars)} 个 jar 进入编译类路径", flush=True)\n'
       '        except Exception as _e:\n'
       '            print(f"jar 补丁执行失败（关键！）：{_e}", flush=True)\n'
       '        self.set_value("app", "android.add_jars", ",".join(jars))')
assert old in t, "补丁定位失败：android.add_jars 写入点不存在"
p.write_text(t.replace(old, new, 1), encoding="utf-8")
print("deploy_lib/android/buildozer.py 补丁1（add_jars）完成")

# 补丁2：--load-local-libs 为空时不要传该参数（p4a 会把空字符串当库名，
# 生成 lib_arm64-v8a.so 占位条目，QtLoader 按清单加载必然失败退出）；
# "main" 始终保留在加载清单里
old2 = '''        extra_args = (f"--qt-libs={modules} --load-local-libs={local_libs}"
                      f" --init-classes={init_classes}")'''
new2 = '''        # PATCH: p4a requires --load-local-libs, but an empty value makes p4a
        # generate a bogus "lib_arm64-v8a.so" entry that QtLoader cannot load.
        # "main" -> libmain_<arch>.so (p4a app main lib) must always be loaded.
        if not local_libs:
            local_libs = "main"
        elif "main" not in local_libs.split(","):
            local_libs = local_libs + ",main"
        extra_args = (f"--qt-libs={modules} --load-local-libs={local_libs}"
                      f" --init-classes={init_classes}")'''
assert old2 in t, "补丁定位失败：extra_args 写入点不存在"
p.write_text(t.replace(old2, new2, 1), encoding="utf-8")
print("deploy_lib/android/buildozer.py 补丁2（load-local-libs）完成")
PYEOF

# ---------- 5.7 补丁3：插件扫描回退 ----------
# 部署工具的 _find_local_libs 依赖 Qt 轮子内 XML（6.12 里解析不出），导致
# qt_plugins 为空 → 平台插件 libplugins_platforms_qtforandroid 从未进 APK →
# QtActivityDelegateBase 的 native 方法报 UnsatisfiedLinkError 闪退。
# 这里改为：XML 无结果时直接扫描轮子里的插件 so（ platforms/qtforandroid
# 是启动必需，multimedia/ffmpeg 是播放必需），并按 recipe 会复制的确切
# 路径存在性过滤，避免 designer 等怪名文件混入加载清单。
CFG_PY="$("$WORK/venv/bin/python" -c "import pathlib, PySide6; print(pathlib.Path(PySide6.__file__).parent / 'scripts' / 'deploy_lib' / 'android' / 'android_config.py')")"
"$WORK/venv/bin/python" - "$CFG_PY" <<'PYEOF'
import sys
from pathlib import Path

p = Path(sys.argv[1])
t = p.read_text(encoding="utf-8")
old = "        return list(local_libs), list(plugins)"
new = (
    "        if not local_libs and not plugins:\n"
    "            # PATCH: Qt 6.12 wheels ship no parsable dependency XMLs; scan\n"
    "            # the wheel directly for plugin libs (qtforandroid platform\n"
    "            # plugin is required before any Qt JNI call, multimedia/ffmpeg\n"
    "            # for playback). Only accept plugins the recipe will actually\n"
    "            # copy (path exists in wheel), so odd names get filtered out.\n"
    "            with zipfile.ZipFile(self.wheel_pyside) as zf:\n"
    "                wheel_files = set(zf.namelist())\n"
    "            suffix = f\"_{self.arch}.so\"\n"
    "            for name in wheel_files:\n"
    "                if \"/plugins/\" not in name or not name.endswith(suffix):\n"
    "                    continue\n"
    "                match = lib_pattern.search(Path(name).stem)\n"
    "                if not match:\n"
    "                    continue\n"
    "                lib_name = match.group(\"lib_name\")\n"
    "                plugin_key = lib_name.split(\"plugins_\", 1)[1]\n"
    "                category, _, rest = plugin_key.partition(\"_\")\n"
    "                expected = (f\"PySide6/Qt/plugins/{category}/\"\n"
    "                            f\"libplugins_{category}_{rest}_{self.arch}.so\")\n"
    "                if rest and expected in wheel_files:\n"
    "                    local_libs.add(lib_name)\n"
    "                    plugins.add(plugin_key)\n"
    "        return list(local_libs), list(plugins)"
)
assert old in t, "补丁定位失败：_find_local_libs 返回点不存在"
p.write_text(t.replace(old, new, 1), encoding="utf-8")
print("deploy_lib/android/android_config.py 补丁3（插件扫描回退）完成")
PYEOF

# ---------- 6. 预下载 Gradle 分发包 ----------
# gradlew 下载 Gradle 发行包（托管在 GitHub Releases）曾因 504 抖动失败，
# 这里用 wget 自动重试 5 次预下载进 wrapper 缓存，gradlew 会直接复用
GRADLE_URL="https://services.gradle.org/distributions/gradle-8.14.3-all.zip"
GRADLE_HASH="$("$WORK/venv/bin/python" -c "
import hashlib
n = int(hashlib.sha1('$GRADLE_URL'.encode()).hexdigest(), 16)
d = '0123456789abcdefghijklmnopqrstuvwxyz'
s = ''
while n:
    s = d[n % 36] + s
    n //= 36
print(s)
")"
GRADLE_CACHE="$HOME/.gradle/wrapper/dists/gradle-8.14.3-all/$GRADLE_HASH"
mkdir -p "$GRADLE_CACHE"
if [ ! -e "$GRADLE_CACHE/gradle-8.14.3-all" ]; then
    log "预下载 Gradle 分发包（wget 重试 5 次）"
    wget -t 5 -c -q -O "$GRADLE_CACHE/gradle-8.14.3-all.zip.part" "$GRADLE_URL" \
        || die "Gradle 分发包下载失败"
    mv "$GRADLE_CACHE/gradle-8.14.3-all.zip.part" "$GRADLE_CACHE/gradle-8.14.3-all.zip"
    log "Gradle 分发包已就绪：$GRADLE_CACHE"
fi

# ---------- 7. 构建包 ----------
cd "$STAGING"
log "运行 pyside6-android-deploy（首次要下 Gradle 依赖，耐心等）"
set +e
"$DEPLOY" --name "$APP_NAME" \
    --wheel-pyside="$PYSIDE_WHEEL" \
    --wheel-shiboken="$SHIBOKEN_WHEEL" \
    --ndk-path="$NDK_PATH" \
    --sdk-path="$SDK_ROOT" \
    --extra-modules Network,Multimedia,Pdf \
    --force
RC=$?
set -e

# 注意：该工具出错时也可能返回 0，所以以"有没有产物"为准
APK="$(ls -t "$STAGING"/*.apk 2>/dev/null | head -n1 || true)"
AAB="$(ls -t "$STAGING"/*.aab 2>/dev/null | head -n1 || true)"
if [ -z "$APK" ] && [ -z "$AAB" ]; then
    log "没有产物（deploy exit=$RC），倾倒诊断信息以定位问题"
    echo "===== pysidedeploy.spec（部署工具生成的配置） ====="
    cat "$STAGING/pysidedeploy.spec" 2>/dev/null || echo "(不存在)"
    echo "===== buildozer.spec（部署工具生成，看 jar/gradle 相关键） ====="
    cat "$STAGING/buildozer.spec" 2>/dev/null | head -80 || echo "(不存在)"
    echo "===== 轮子中的 jar 文件 ====="
    unzip -l "$PYSIDE_WHEEL" 2>/dev/null | grep -i "\.jar" || echo "(轮子里没有 jar)"
    echo "===== dist 目录中的 jar / aar / build.gradle ====="
    DIST_DIR="$(find "$STAGING/.buildozer" -maxdepth 9 -type d -name "$APP_NAME" 2>/dev/null | head -n1)"
    echo "dist: $DIST_DIR"
    if [ -n "$DIST_DIR" ]; then
        find "$DIST_DIR" \( -name "*.jar" -o -name "*.aar" \) 2>/dev/null | head -10
        echo "----- build.gradle 全文 -----"
        cat "$DIST_DIR/build.gradle" 2>/dev/null || echo "(无 build.gradle)"
        ls "$DIST_DIR"
    fi
    echo "===== deploy 工具生成的 PySide6 recipe ====="
    find "$STAGING/deployment/recipes" -type f 2>/dev/null | head -10
    cat "$STAGING/deployment/recipes/PySide6/__init__.py" 2>/dev/null
    exit "${RC:-1}"
fi
