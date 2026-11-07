"""用 PyInstaller 分别打包客户端（播放器）和服务端（控制端）。

用法（用哪个解释器打包，就用哪个解释器的包）：
    python build.py           # 客户端 + 服务端都打包
    python build.py client    # 只打包客户端
    python build.py server    # 只打包服务端

产物（输出到上一级目录的 dist_win7_32/，不覆盖主工程的 dist/）：
    dist_win7_32/MediaPlayer客户端/MediaPlayer客户端.exe              图形界面播放器
    dist_win7_32/MediaPlayer服务端/MediaPlayer服务端.exe              服务端图形界面
    dist_win7_32/MediaPlayer服务端命令行/MediaPlayer服务端命令行.exe  服务端命令行（--help 看指令）

这是 Windows 7 / 32 位版（PyQt5 + Qt5），必须用 32 位 Python 3.9 打包：
    <32位python> build.py
录屏用的 ffmpeg 会复制一份到服务端程序目录，运行时优先用它。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Win7/32 位版单独输出，避免覆盖主工程（Qt6）的 dist/
DIST = ROOT.parent / "dist_win7_32"
BUILD = ROOT / "build_win7"

CLIENT_NAME = "MediaPlayer客户端"
SERVER_NAME = "MediaPlayer服务端"
CLI_NAME = "MediaPlayer服务端命令行"

COMMON = ["--noconfirm", "--clean", "--onedir", "--log-level", "WARN",
          "--exclude-module", "tkinter"]


def run_pyinstaller(entry: str, name: str, extra: list[str]) -> None:
    cmd = [sys.executable, "-m", "PyInstaller", *COMMON,
           "--name", name, "--distpath", str(DIST),
           "--workpath", str(BUILD / name), "--specpath", str(BUILD),
           *extra, entry]
    print(">", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def build_client() -> None:
    """播放器客户端：图形界面，带系统声音采集（soundcard 是线程内动态导入）。"""
    run_pyinstaller("main.py", CLIENT_NAME, [
        "--windowed",  # 纯界面，不弹控制台
        "--hidden-import", "soundcard", "--hidden-import", "numpy",
        "--exclude-module", "imageio_ffmpeg",
        # pymupdf 的布局分析是可选功能，打包后读不到它的资源文件会直接崩，
        # 排掉后 pymupdf 会自己跳过（我们只用它的渲染）
        "--exclude-module", "pymupdf.layout",
    ])
    out = DIST / CLIENT_NAME
    for folder in ("received", "saves"):  # 运行时的接收目录 / 截屏目录
        (out / folder).mkdir(parents=True, exist_ok=True)
    print("客户端完成：", out / f"{CLIENT_NAME}.exe", flush=True)


def build_server() -> None:
    """控制服务端：图形界面 + 命令行两个 exe，并自带 ffmpeg。"""
    extra = ["--exclude-module", "soundcard", "--exclude-module", "numpy"]
    run_pyinstaller("control_server_ui.py", SERVER_NAME, ["--windowed", *extra])
    run_pyinstaller("control_server.py", CLI_NAME, ["--console", *extra])
    try:
        import imageio_ffmpeg

        ffmpeg = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # 没装也可以打包，只是不带 ffmpeg
        print("未复制 ffmpeg（找不到）：", exc, flush=True)
        ffmpeg = None
    for name in (SERVER_NAME, CLI_NAME):
        out = DIST / name
        (out / "saves").mkdir(parents=True, exist_ok=True)
        if ffmpeg is not None and ffmpeg.is_file() and out.is_dir():
            shutil.copy2(ffmpeg, out / ffmpeg.name)
            print("已自带 ffmpeg：", out / ffmpeg.name, flush=True)
    print("服务端完成：", DIST / SERVER_NAME / f"{SERVER_NAME}.exe", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    want = args[0] if args else "all"
    if want in ("all", "client"):
        build_client()
    if want in ("all", "server"):
        build_server()
    print("全部完成，产物在", DIST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
