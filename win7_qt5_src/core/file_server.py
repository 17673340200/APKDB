"""集成在播放器里的 TCP 文件收发服务（后台线程运行）。

收到 push 上传的文件后会发出 fileReceived 信号，主线程收到即调用 open_path 打开播放。
"""

from __future__ import annotations

import json
import socketserver
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal

from core.file_protocol import (
    CHUNK,
    DEFAULT_PORT,
    MAX_FILE_SIZE,
    ProtocolError,
    read_header,
    safe_name,
    send_frame,
    send_header,
)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_STORAGE = Path(__file__).resolve().parent.parent / "received"
RECV_TIMEOUT = 300  # 长连接空闲超时（秒）


class _FileHandler(socketserver.BaseRequestHandler):
    """单条连接的处理循环：按帧读取命令，长连接可持续收发。"""

    def setup(self) -> None:
        self.request.settimeout(RECV_TIMEOUT)

    def handle(self) -> None:
        while True:
            try:
                header = read_header(self.request)
            except (ProtocolError, ValueError, OSError):
                return
            try:
                self._dispatch(header)
            except (ProtocolError, ValueError, OSError) as exc:
                try:
                    send_frame(self.request, {"ok": False, "msg": str(exc)})
                except OSError:
                    return
                if str(header.get("cmd", "")).lower() == "push":
                    return  # 上传中断后无法保证帧对齐，直接断开

    # ---------- 命令分发 ----------
    def _dispatch(self, header: dict) -> None:
        cmd = str(header.get("cmd", "")).lower()
        if cmd == "ping":
            send_frame(self.request, {"ok": True, "cmd": "ping", "msg": "pong"})
        elif cmd == "push":
            path = self._receive_file(header)
            self.server.notify_file(str(path))
            send_frame(self.request, {
                "ok": True, "cmd": "push", "name": path.name, "path": str(path),
            })
        elif cmd == "play":
            path = self.server.storage / safe_name(header.get("name"))
            if not path.is_file():
                raise ProtocolError("文件不存在")
            self.server.notify_file(str(path))
            send_frame(self.request, {"ok": True, "cmd": "play", "name": path.name})
        elif cmd == "list":
            items = self.server.list_files()
            payload = json.dumps(items, ensure_ascii=False).encode("utf-8")
            send_frame(self.request, {
                "ok": True, "cmd": "list", "count": len(items), "size": len(payload),
            }, payload)
        elif cmd == "get":
            self._send_file(header)
        else:
            raise ProtocolError(f"未知命令：{cmd}")

    # ---------- 收文件 ----------
    def _receive_file(self, header: dict) -> Path:
        name = safe_name(header.get("name"))
        size = int(header.get("size", 0))
        if size <= 0 or size > MAX_FILE_SIZE:
            raise ProtocolError("文件大小非法")
        path = self.server.storage / name
        received = 0
        with open(path, "wb") as fp:
            while received < size:
                chunk = self.request.recv(min(CHUNK, size - received))
                if not chunk:
                    raise ProtocolError("传输中断")
                fp.write(chunk)
                received += len(chunk)
        return path

    # ---------- 发文件 ----------
    def _send_file(self, header: dict) -> None:
        path = (self.server.storage / safe_name(header.get("name"))).resolve()
        root = self.server.storage.resolve()
        if not path.is_file() or root not in path.parents:
            raise ProtocolError("文件不存在")
        send_header(self.request, {
            "ok": True, "cmd": "get", "name": path.name, "size": path.stat().st_size,
        })
        with open(path, "rb") as fp:
            while True:
                chunk = fp.read(CHUNK)
                if not chunk:
                    break
                self.request.sendall(chunk)


def list_storage_files(storage: Path) -> list[dict]:
    """列出接收目录里的文件，按修改时间倒序（最近收到的排在最前）。"""
    items = []
    for path in storage.iterdir():
        if path.is_file():
            stat = path.stat()
            items.append({
                "name": path.name,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


class _ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    storage: Path
    owner: "FileTransferServer"

    def notify_file(self, path: str) -> None:
        self.owner.fileReceived.emit(path)

    def list_files(self) -> list[dict]:
        return list_storage_files(self.storage)


class FileTransferServer(QThread):
    """在后台线程里跑 TCP 服务，通过信号把收到的文件交给主线程播放。"""

    fileReceived = pyqtSignal(str)   # 已保存的文件路径
    statusChanged = pyqtSignal(str)  # 启动 / 停止 / 出错等信息

    def __init__(self, host: str = DEFAULT_HOST, port: int | None = None,
                 storage: str | Path | None = None, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port or DEFAULT_PORT
        self.storage = Path(storage) if storage else DEFAULT_STORAGE
        self.storage.mkdir(parents=True, exist_ok=True)
        self._server: _ThreadedTCPServer | None = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def list_files(self) -> list[dict]:
        return list_storage_files(self.storage)

    def run(self) -> None:  # noqa: D102
        try:
            server = _ThreadedTCPServer((self.host, self.port), _FileHandler)
        except OSError as exc:
            self.statusChanged.emit(f"文件服务启动失败：{exc}")
            return
        server.storage = self.storage
        server.owner = self
        self._server = server
        host, port = server.server_address[0], server.server_address[1]
        self.statusChanged.emit(f"文件服务已启动：{host}:{port}（接收目录 {self.storage}）")
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
            self.statusChanged.emit("文件服务已停止")

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        self.wait(3000)
