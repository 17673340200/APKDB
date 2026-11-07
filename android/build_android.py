"""安卓构建辅助（在 Windows 上运行）。

注意：官方部署工具 `pyside6-android-deploy` 只支持 Linux/macOS 宿主
（内部走 buildozer + python-for-android），Windows 上没有这个工具，
所以 **APK 不能在 Windows 上直接构建**。本脚本做两件事：

1. 校验安卓端源码能被当前 Python 编译（提前发现语法/导入问题）；
2. 组装 `android/staging/`（入口改名为 main.py，部署工具的硬要求），
   你可以把它拷到任何 Linux/WSL2 机器上跑 `build_linux.sh`。

出包方式：
- 任意 Linux / WSL2：`./android/build_linux.sh`（自动装 JDK17、SDK、NDK、安卓轮子）
- GitHub：推仓库后在 Actions 触发 "Android APK"，产物在 Artifacts
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "android" / "staging"

# staging 里的文件清单（与 android/build_linux.sh 保持一致）
FILES = {
    "main_android.py": "main.py",
    "qtcompat.py": "qtcompat.py",
    "core/__init__.py": "core/__init__.py",
    "core/control_client.py": "core/control_client.py",
    "core/control_protocol.py": "core/control_protocol.py",
    "core/file_protocol.py": "core/file_protocol.py",
    "core/file_server.py": "core/file_server.py",
    "core/schedule.py": "core/schedule.py",
    "ui/__init__.py": "ui/__init__.py",
    "ui/video_view.py": "ui/video_view.py",
    "android/__init__.py": "android/__init__.py",
    "android/pdf_view.py": "android/pdf_view.py",
    "android/image_slideshow.py": "android/image_slideshow.py",
    "android/mobile_window.py": "android/mobile_window.py",
}


def check_sources() -> list[str]:
    """逐个编译，返回失败清单（空 = 全部通过）。"""
    problems = []
    for src in FILES:
        target = ROOT / src
        if not target.is_file():
            problems.append(f"缺文件：{src}")
            continue
        try:
            py_compile.compile(str(target), doraise=True)
        except py_compile.PyCompileError as exc:
            problems.append(f"{src}: {exc}")
    return problems


def make_staging() -> Path:
    if STAGING.exists():
        shutil.rmtree(STAGING)
    for src, dst in FILES.items():
        target = STAGING / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / src, target)
    return STAGING


def main() -> int:
    parser = argparse.ArgumentParser(description="安卓构建辅助（Windows 侧）")
    parser.add_argument("--check", action="store_true", help="只校验源码")
    options = parser.parse_args()

    problems = check_sources()
    if problems:
        print("源码校验失败：")
        for item in problems:
            print(" -", item)
        return 1
    print(f"安卓端 {len(FILES)} 个源码文件编译检查通过")

    if options.check:
        return 0
    staging = make_staging()
    print("已组装构建目录：", staging)
    print("下一步（二选一）：")
    print("  1) 任意 Linux/WSL2：./android/build_linux.sh")
    print("  2) GitHub：推仓库后在 Actions 触发『Android APK』")
    return 0


if __name__ == "__main__":
    sys.exit(main())
