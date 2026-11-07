"""播放器侧的控制客户端：连到控制服务端，接受远程指令、接收文件、回传截屏。"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal

from core.control_protocol import (
    DEFAULT_FRAME_WIDTH,
    DEFAULT_LISTEN_PORT,
    RECONNECT_INTERVAL,
    SOCKET_TIMEOUT,
)
from core.file_protocol import (
    CHUNK,
    MAX_FILE_SIZE,
    ProtocolError,
    read_header,
    safe_name,
    send_frame,
    send_header,
)
from core.file_server import list_storage_files

NO_OP_COMMANDS = {"welcome", "pong", "shot_data", "event"}  # 收到即忽略的帧


class _DeleteTicket:
    """一次删除请求的回执单。

    主线程删除完成后填写 ok / msg 并 done.set()，会话线程据此回执。
    用对象而不是字典：跨线程信号若对容器做了浅拷贝，字典里的改动就丢了。
    """

    def __init__(self, name: str):
        self.name = name
        self.done = threading.Event()
        self.ok = False
        self.msg = ""


class ControlClient(QThread):
    """后台线程维持与控制服务端的长连接。

    所有对界面有影响的东西都通过信号交给主线程处理；
    截屏由主线程抓取后调用 send_screenshot() 回传（内部加锁，可跨线程调用）。
    """

    statusChanged = pyqtSignal(str)
    fileReceived = pyqtSignal(str)  # 收到推送的文件（已落盘）
    controlReceived = pyqtSignal(str, dict)  # action / args
    screenshotRequested = pyqtSignal(object)  # 参数为发起请求的本会话，用于回传截屏
    autoshotChanged = pyqtSignal(int)  # 自动截屏间隔（秒，0 关闭）
    recordChanged = pyqtSignal(object, int, int)  # 会话、帧率（0 停止）、画面宽度
    audioChanged = pyqtSignal(object, int)  # 会话、是否采集系统声音（1 开 / 0 关）
    fileDeleteRequested = pyqtSignal(str, object)  # 文件名、回执容器（主线程填结果）

    def __init__(self, host: str, port: int, storage: str | Path,
                 name: str = "播放器", parent=None,
                 sock: socket.socket | None = None, reconnect: bool = True):
        """sock 非空时直接在该连接上跑会话（服务端主动连入的场景），断线不重连。"""
        super().__init__(parent)
        self.host = host
        self.port = port
        self.storage = Path(storage)
        self.storage.mkdir(parents=True, exist_ok=True)
        self.name = name
        self._sock = sock
        self._reconnect = reconnect
        self._lock = threading.Lock()
        self._running = True

    # ---------- 对外发送（可在主线程调用） ----------
    def send_screenshot(self, png: bytes, meta: str = "") -> None:
        with self._lock:
            if self._sock is None:
                return
            try:
                send_frame(self._sock,
                           {"cmd": "shot_data", "name": meta, "size": len(png)}, png)
            except OSError:
                pass

    def send_video_frame(self, jpeg: bytes, width: int, height: int) -> None:
        """实时画面：把一帧 JPEG 发给服务端。"""
        with self._lock:
            if self._sock is None:
                return
            try:
                send_frame(self._sock,
                           {"cmd": "frame", "size": len(jpeg),
                            "w": int(width), "h": int(height)}, jpeg)
            except OSError:
                pass

    def send_audio_frame(self, pcm: bytes, rate: int, channels: int) -> None:
        """回传一段系统声音的 PCM（16bit 小端）。"""
        with self._lock:
            if self._sock is None or not pcm:
                return
            try:
                send_frame(self._sock, {"cmd": "audio", "size": len(pcm),
                                        "rate": int(rate), "ch": int(channels)}, pcm)
            except OSError:
                pass

    def send_event(self, message: str) -> None:
        with self._lock:
            if self._sock is None:
                return
            try:
                send_header(self._sock, {"cmd": "event", "msg": message})
            except OSError:
                pass

    def stop(self) -> None:
        self._running = False
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.wait(2000)

    # ---------- 线程主体 ----------
    def run(self) -> None:  # noqa: D102
        if self._sock is not None:  # 服务端主动连入的会话
            self._session(self._sock)
            return
        while self._running:
            try:
                sock = socket.create_connection((self.host, self.port),
                                                timeout=SOCKET_TIMEOUT)
            except OSError as exc:
                self.statusChanged.emit(
                    f"连接控制服务端失败：{exc}（{RECONNECT_INTERVAL:.0f} 秒后重试）")
                if not self._wait_retry():
                    return
                continue

            self.statusChanged.emit(f"已连接控制服务端 {self.host}:{self.port}")
            self._session(sock)
            if not self._reconnect or not self._wait_retry():
                return

    def _session(self, sock: socket.socket) -> None:
        """在一条连接上完成握手并收发帧。"""
        with self._lock:
            self._sock = sock
        try:
            send_header(sock, {"cmd": "hello", "name": self.name})
            self._loop(sock)
        except (ProtocolError, OSError) as exc:
            self.statusChanged.emit(f"控制连接断开：{exc}")
        finally:
            with self._lock:
                self._sock = None
            try:
                sock.close()
            except OSError:
                pass

    def _wait_retry(self) -> bool:
        """等待重连间隔，期间若要求停止则返回 False。"""
        deadline = RECONNECT_INTERVAL
        step = 0.2
        while deadline > 0:
            if not self._running:
                return False
            self.msleep(int(step * 1000))
            deadline -= step
        return self._running

    def _loop(self, sock: socket.socket) -> None:
        while self._running:
            try:
                header = read_header(sock)
            except socket.timeout:
                continue
            cmd = header.get("cmd")
            if cmd in NO_OP_COMMANDS:
                continue  # 服务端回执 / 心跳应答，无需处理
            if cmd == "ping":
                send_header(sock, {"ok": True, "cmd": "pong"})
            elif cmd == "control":
                args = header.get("args") or {}
                self.controlReceived.emit(str(header.get("action") or ""), dict(args))
            elif cmd == "push":
                path = self._receive_file(sock, header)
                self.fileReceived.emit(str(path))
                send_header(sock, {"ok": True, "cmd": "push", "msg": f"已保存 {path.name}"})
            elif cmd == "shot":
                self.screenshotRequested.emit(self)
            elif cmd == "autoshot":
                self.autoshotChanged.emit(max(0, int(header.get("interval", 0))))
            elif cmd == "list":  # 服务端要查看接收目录里有哪些文件
                send_header(sock, {"ok": True, "cmd": "files",
                                   "files": self._storage_files()})
            elif cmd == "delete":  # 服务端要删除接收目录里的某个文件
                self._handle_delete(sock, header)
            elif cmd == "record":
                fps = max(0, int(header.get("fps", 0)))
                self.recordChanged.emit(
                    self, fps,
                    max(160, int(header.get("width", DEFAULT_FRAME_WIDTH))))
                # 停止实时画面时一起关掉声音采集；只有正在录屏时才采
                self.audioChanged.emit(self, 1 if fps > 0 and header.get("audio") else 0)
            else:
                send_header(sock, {"ok": False, "cmd": cmd or "", "msg": f"未知命令 {cmd}"})

    def _storage_files(self) -> list[dict]:
        """接收目录里的文件（最近收到的排最前）。"""
        try:
            return list_storage_files(self.storage)
        except OSError:
            return []

    def _handle_delete(self, sock: socket.socket, header: dict) -> None:
        """删除请求交给主线程执行（那边才能先释放正在播放的文件），等结果再回执。"""
        try:
            name = safe_name(header.get("name"))
        except ProtocolError as exc:
            send_header(sock, {"ok": False, "cmd": "deleted", "msg": str(exc)})
            return
        ticket = _DeleteTicket(name)
        self.fileDeleteRequested.emit(name, ticket)
        if not ticket.done.wait(10.0):  # 主线程没响应（例如正忙），不无限等
            send_header(sock, {"ok": False, "cmd": "deleted", "name": name,
                               "msg": "删除请求超时"})
            return
        send_header(sock, {"ok": ticket.ok, "cmd": "deleted", "name": name,
                           "msg": ticket.msg or (f"已删除 {name}" if ticket.ok
                                                 else "删除失败")})

    def _receive_file(self, sock: socket.socket, header: dict) -> Path:
        name = safe_name(header.get("name"))
        size = int(header.get("size", 0))
        if size <= 0 or size > MAX_FILE_SIZE:
            raise ProtocolError("文件大小非法")
        target = self.storage / name
        remaining = size
        with open(target, "wb") as fp:
            while remaining:
                chunk = sock.recv(min(CHUNK, remaining))
                if not chunk:
                    raise ProtocolError("连接已断开")
                fp.write(chunk)
                remaining -= len(chunk)
        return target


class ControlListener(QThread):
    """播放器端监听端口，等待控制服务端主动连入。

    这样服务端只要"添加客户端"（填 IP 和端口）就能控制播放器，
    播放器本机无需配置 MEDIAPLAYER_CONTROL_HOST。
    """

    connectionAccepted = pyqtSignal(object)  # 参数为已建立的 socket
    statusChanged = pyqtSignal(str)

    def __init__(self, port: int = DEFAULT_LISTEN_PORT, host: str = "0.0.0.0", parent=None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self._running = True

    def stop(self) -> None:
        self._running = False
        self.wait(2000)

    def run(self) -> None:  # noqa: D102
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.host, self.port))
        except OSError as exc:
            self.statusChanged.emit(f"受控监听启动失败：{exc}")
            return
        listener.listen(8)
        listener.settimeout(0.5)
        self.statusChanged.emit(f"受控监听已开启：{self.host}:{self.port}")
        try:
            while self._running:
                try:
                    conn, addr = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                conn.settimeout(SOCKET_TIMEOUT)
                self.statusChanged.emit(f"控制服务端已连入：{addr[0]}")
                self.connectionAccepted.emit(conn)
        finally:
            listener.close()
