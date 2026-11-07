"""把 PPT / PPTX / ODP 等演示文稿转换成 PDF。

转换后端（按优先级自动选择）：
    1. LibreOffice（跨平台，优先）
    2. 本机已安装的 PowerPoint / WPS 演示（Windows，通过 COM 导出 PDF，无需额外 Python 依赖）
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PRESENTATION_EXTENSIONS = {".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".odg"}

# LibreOffice 可执行文件的常见位置
_WINDOWS_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    r"C:\Program Files\LibreOffice 25\program\soffice.exe",
]
_POSIX_CANDIDATES = [
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/snap/bin/libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]

CONVERT_TIMEOUT = 300  # 秒

# 本机演示文稿软件的 COM ProgID：Microsoft PowerPoint / WPS 演示
COM_PROG_IDS = ("PowerPoint.Application", "KWPP.Application")


class ConversionError(Exception):
    """转换失败（通常是没装 LibreOffice 或文件损坏）。"""


def find_libreoffice() -> str | None:
    """定位 LibreOffice 可执行文件，找不到返回 None。"""
    candidates = list(_POSIX_CANDIDATES)
    if os.name == "nt":
        candidates = _WINDOWS_CANDIDATES + candidates
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("soffice", "libreoffice", "soffice.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = Path(base) / "MediaPlayer" / "ppt-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_key(src: str) -> str:
    """根据路径、大小、修改时间生成缓存键，源文件变化后自动重新转换。"""
    stat = os.stat(src)
    raw = f"{os.path.abspath(src)}|{stat.st_size}|{int(stat.st_mtime)}"
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()


def _convert_with_libreoffice(work_src: Path, work_dir: Path) -> Path | None:
    """用 LibreOffice 转换，返回生成的 PDF；失败返回 None。"""
    soffice = find_libreoffice()
    if not soffice:
        return None
    cmd = [
        soffice,
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--convert-to",
        "pdf",
        "--outdir",
        str(work_dir),
        str(work_src),
    ]
    try:
        subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=CONVERT_TIMEOUT,
            text=True,
            errors="ignore",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    produced = work_dir / (work_src.stem + ".pdf")
    return produced if produced.is_file() else None


def _convert_with_office_com(work_src: Path, out: Path) -> bool:
    """用本机 PowerPoint / WPS 演示（COM）另存为 PDF，成功返回 True。

    走 PowerShell 调用 COM，因此不需要额外安装 Python 包。
    """
    if os.name != "nt":
        return False

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    prog_ids = ", ".join(quote(pid) for pid in COM_PROG_IDS)
    # 依次尝试 PowerPoint、WPS 演示；32 = ppSaveAsPDF，2 = ppFixedFormatTypePDF
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"$src = {quote(str(work_src))}\n"
        f"$out = {quote(str(out))}\n"
        "$done = $false\n"
        f"foreach ($id in @({prog_ids})) {{\n"
        "  try {\n"
        "    $app = New-Object -ComObject $id\n"
        "    try {\n"
        "      $pres = $app.Presentations.Open($src, $true, $false, $false)\n"
        "      try {\n"
        "        $ok = $false\n"
        "        try { $pres.SaveAs($out, 32); $ok = $true } catch { }\n"
        "        if (-not $ok) { try { $pres.ExportAsFixedFormat($out, 2); $ok = $true } catch { } }\n"
        "        if ($ok) { $done = $true }\n"
        "      } finally { $pres.Close() }\n"
        "    } finally { $app.Quit() }\n"
        "  } catch { }\n"
        "  if ($done) { break }\n"
        "}\n"
        "if (-not $done) { exit 3 }\n"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=CONVERT_TIMEOUT,
            text=True,
            errors="ignore",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.is_file()


def convert_to_pdf(src: str) -> str:
    """把演示文稿转成 PDF，返回缓存的 PDF 路径（阻塞调用）。"""
    key = cache_key(src)
    target = cache_dir() / f"{key}.pdf"
    if target.is_file():
        return str(target)

    work_dir = Path(tempfile.mkdtemp(prefix="ppt2pdf-"))
    try:
        # 复制到工作目录，避免中文名 / 特殊字符导致的输出异常
        work_src = work_dir / ("input" + Path(src).suffix.lower())
        shutil.copy2(src, work_src)

        produced = _convert_with_libreoffice(work_src, work_dir)
        if produced is None:  # 没有 LibreOffice 时改用本机 PowerPoint / WPS
            com_out = work_dir / "com.pdf"
            if _convert_with_office_com(work_src, com_out):
                produced = com_out
        if produced is None:
            raise ConversionError(
                "未找到可用的演示文稿转换工具，无法播放 PPT。\n"
                "请安装以下任意一个后重试：\n"
                "  1) LibreOffice（推荐）：https://www.libreoffice.org/download/download/\n"
                "  2) Microsoft PowerPoint 或 WPS 演示（Windows，自动调用其导出 PDF）"
            )
        shutil.move(str(produced), str(target))
        return str(target)
    except subprocess.TimeoutExpired as exc:
        raise ConversionError("转换超时，文件可能过大或格式异常。") from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
