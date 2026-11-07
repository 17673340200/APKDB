"""控制服务端：管理播放器客户端，远程控制、推送文件、收取客户端截屏。

启动：

    python control_server.py                      # 图形界面（默认）
    python control_server.py --cli                # 命令行界面
    python control_server.py --port 6000 --shots D:\\shots

图形界面下左侧选择客户端、右侧分区远程控制，截屏回传后直接预览；
命令行下可用 clients / use / push / play / shot 等指令操作客户端。
播放器端需要设置环境变量 MEDIAPLAYER_CONTROL_HOST=<本机 IP> 才会连上来。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.control_protocol import (
    DEFAULT_CONTROL_PORT,
    DEFAULT_FPS,
    DEFAULT_FRAME_WIDTH,
    DEFAULT_LISTEN_PORT,
    DEFAULT_SHOT_DIR,
    MAX_FPS,
    RECONNECT_INTERVAL,
    SYNC_LEAD_SECONDS,
)
from core.file_protocol import (
    CHUNK,
    ProtocolError,
    read_header,
    recv_exact,
    safe_name,
    send_frame,
    send_header,
)

HELP = """\
可用指令：
  clients                      列出已连接的客户端
  add <IP> [端口] [名称]        添加客户端并立即连接（默认端口 50519）
  remove <IP> [端口]           移除已添加的客户端
  book                         显示地址簿
  use <id>                     选择当前操作的客户端
  push <本地文件> [all|编号,编号]  发文件给客户端（收到后自动播放）；目标省略时为当前客户端
  sync <本地文件|已有文件名> [延迟秒] [all|编号,编号]
                              多客户端同步播放：先推文件（本地文件时），再约定同一时刻起播
  schedule add <开始HH:MM> <结束HH:MM|-> <文件> [all|编号,编号]
                              下发定时播放计划：每天从开始时间播到结束时间（"-" 表示播到当天结束）
  schedule off [all|编号,编号]    清空客户端的定时播放计划
  open <路径或流地址>           让客户端打开本地文件 / 网络流
  files                         列出客户端接收目录里的文件
  play <文件名>                 让客户端播放接收目录里的指定文件
  delete <文件名>               删除客户端接收目录里的文件（客户端本地同步删除）
  play | pause | toggle | stop 播放控制
  seek <毫秒>                  相对当前位置快进/快退
  volume <0-100>               音量
  mute on|off                  静音
  loop on|off                  视频循环播放
  full on|off                  全屏 / 退出全屏
  next | prev                  PDF 下一页 / 上一页
  page <页码>                  PDF 跳页（1 起）
  autoplay on|off              PDF 自动放映
  interval <秒>                PDF 放映间隔
  docloop on|off               PDF 放映末页循环
  fit width|page               PDF 适应宽度 / 整页
  record <fps|off> [宽度] [audio]  客户端实时画面（录屏），默认 8 fps / 宽 960；
                               带 audio 时同时回传客户端正在播放的系统声音
  savevideo on|off              把实时画面录成 MP4（存到录制文件夹，含声音时一并合成）
  shot [保存文件名]             让客户端截屏并回传（保存在截屏目录）
  autoshot <秒|off>            客户端定时自动截屏回传
  restart | quit               重启 / 退出客户端软件
  help                         显示本帮助
  exit                         退出服务端
"""


def _bool_arg(text: str) -> bool:
    return str(text).strip().lower() in ("1", "on", "true", "yes", "y", "开")


def _bundled_ffmpeg() -> str:
    """打包成 exe 后：在程序目录（含 _internal）里找随包带上的 ffmpeg。"""
    if not getattr(sys, "frozen", False):  # 源码运行时不找
        return ""
    base = Path(sys.executable).resolve().parent
    for folder in (base, base / "_internal",
                   base / "_internal" / "imageio_ffmpeg" / "binaries"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("ffmpeg*.exe")) + sorted(folder.glob("ffmpeg")):
            if path.is_file():
                return str(path)
    return ""


def find_ffmpeg() -> tuple[str, str]:
    """找到可用的 ffmpeg，返回 (可执行文件路径, 来源说明)；找不到返回 ("", 说明)。

    依次尝试：环境变量 IMAGEIO_FFMPEG_EXE → 打包后 exe 同目录（含 _internal）
    → imageio-ffmpeg 自带的二进制 → 系统 PATH 里的 ffmpeg。
    """
    env = os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    if env and Path(env).is_file():
        return env, "环境变量 IMAGEIO_FFMPEG_EXE"
    bundled = _bundled_ffmpeg()
    if bundled:
        return bundled, "程序目录"
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            return str(exe), "imageio-ffmpeg"
    except Exception:
        pass
    which = shutil.which("ffmpeg")
    if which:
        return which, "系统 PATH"
    return "", "未找到 ffmpeg"


class VideoRecorder:
    """把客户端实时回传的 JPEG 画面帧录成 MP4 视频。

    用 ffmpeg 读取一串 MJPEG 帧（image2pipe）并重编码为 H.264，
    不需要在 Python 里解码图片。ffmpeg 由 find_ffmpeg() 提供
    （优先 imageio-ffmpeg 自带，其次系统 PATH 里的 ffmpeg）。

    写帧走后台线程 + 有界队列：编码跟不上时丢最旧的帧，
    不会把收帧的网络线程堵住（否则实时画面会卡住甚至写不进去）。
    """

    MAX_QUEUED = 60  # 最多缓冲这么多帧

    def __init__(self, path: Path, fps: int = DEFAULT_FPS, log=print):
        self.path = Path(path)
        self.fps = max(1, min(MAX_FPS, int(fps)))
        self.log = log
        self.frames = 0
        self.dropped = 0
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=self.MAX_QUEUED)
        self._writer: threading.Thread | None = None
        self._err_file = None
        self._failed = False
        self._stopping = False
        self._lock = threading.Lock()

    def start(self) -> bool:
        exe, source = find_ffmpeg()
        if not exe:
            self.log(
                f"录制视频失败：未找到 ffmpeg（当前 Python：{sys.executable}）。\n"
                "二选一即可：\n"
                f"  1) {sys.executable} -m pip install imageio-ffmpeg\n"
                "  2) 安装 ffmpeg 并把它加到系统 PATH")
            return False
        self.log(f"录制使用 ffmpeg（{source}）：{exe}")
        cmd = [
            exe, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(self.fps), "-i", "-",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(self.path),
        ]
        try:
            # stderr 写临时文件而不是管道：日志多了也不会把 ffmpeg 憋死
            self._err_file = tempfile.TemporaryFile()
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=self._err_file)
        except OSError as exc:
            self.log(f"启动 ffmpeg 失败：{exc}")
            return False
        self._writer = threading.Thread(target=self._write_loop, daemon=True,
                                        name="recorder-writer")
        self._writer.start()
        return True

    def add_frame(self, jpeg: bytes) -> None:
        """收一帧：只入队，不阻塞调用方（网络线程）。"""
        writer = self._writer
        if writer is None or not writer.is_alive():
            return
        try:
            self._queue.put_nowait(jpeg)
        except queue.Full:  # 编码跟不上：丢最旧的一帧，保证实时性
            self.dropped += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(jpeg)
            except (queue.Empty, queue.Full):
                pass

    def _write_loop(self) -> None:
        """后台线程把队列里的帧写进 ffmpeg 的 stdin。"""
        while True:
            data = self._queue.get()
            if data is None or self._stopping:  # 结束信号
                return
            with self._lock:
                proc = self._proc
            if proc is None or proc.stdin is None:
                continue
            if proc.poll() is not None:  # ffmpeg 已经退出，后面的帧都写不进去了
                self._mark_failed(proc)
                return
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
                self.frames += 1
            except (BrokenPipeError, ValueError, OSError):
                if self._stopping:  # 正常收尾时关掉的管道，不算故障
                    return
                self._mark_failed(proc)  # 只提示一次，避免每帧刷一条日志
                return

    def _mark_failed(self, proc: subprocess.Popen) -> None:
        """ffmpeg 中途退出：提示一次，并带上它自己的报错（Broken pipe 的真正原因）。"""
        with self._lock:
            if self._failed:  # 已经提示过，后面的帧静默丢弃
                self._proc = None
                return
            self._failed = True
            self._proc = None
        try:
            detail = self._stderr_text()
        except OSError:
            detail = ""
        tip = ""
        if "Permission denied" in detail or "being used" in detail or "Invalid argument" in detail:
            tip = "（若 record.mp4 正被播放器打开，先关掉再录）"
        self.log(f"录制中断：ffmpeg 已退出（返回码 {proc.returncode}）{tip}"
                 + (f"，原因：{detail}" if detail else ""))

    def _stderr_text(self) -> str:
        """取出 ffmpeg 的报错输出（尾部若干字符）。"""
        file, self._err_file = self._err_file, None
        if file is None:
            return ""
        try:
            file.seek(0)
            text = (file.read() or b"").decode("utf-8", "ignore").strip()
            return text[-300:]
        except (OSError, ValueError):
            return ""
        finally:
            try:
                file.close()
            except OSError:
                pass

    def stop(self) -> Path | None:
        """结束录制，返回视频路径（没收到帧则返回 None，并清掉空文件夹）。"""
        writer, self._writer = self._writer, None
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return None
        self._stopping = True  # 之后的写失败不算故障，避免误报 Broken pipe
        self._queue.put(None)  # 通知写线程收尾
        if writer is not None:
            writer.join(timeout=10)
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        detail = self._stderr_text()
        try:
            usable = self.path.is_file() and self.path.stat().st_size > 0
        except OSError:
            usable = False
        if self.frames == 0 or not usable:
            self.path.unlink(missing_ok=True)
            self._remove_empty_folder()
            if self._failed:
                self.log("录制失败，未生成视频" + (f"：{detail}" if detail else ""))
            else:
                self.log("没有收到画面帧，未生成视频（请先点『开始实时画面』，再勾选录制）")
            return None
        if proc.returncode not in (0, None):
            self.log(f"ffmpeg 编码异常（返回码 {proc.returncode}）：{detail}")
        if self.dropped:
            self.log(f"录制时丢了 {self.dropped} 帧（编码跟不上实时帧率，成片仍可播放）")
        return self.path

    def _remove_empty_folder(self) -> None:
        """没录到东西时把空文件夹删掉，避免打开目录看到一堆空目录。"""
        folder = self.path.parent
        for _ in range(5):  # ffmpeg 刚退出时目录可能还被占用，重试几次
            try:
                if not folder.is_dir():
                    return
                if any(folder.iterdir()):
                    return
                folder.rmdir()
                return
            except OSError:
                time.sleep(0.2)


class _FileRequest:
    """等待客户端返回文件列表的一次请求。"""

    def __init__(self):
        self.event = threading.Event()
        self.files: list[dict] = []


class _Reply:
    """等待客户端的一次操作回执（如删除文件的结果）。"""

    def __init__(self):
        self.event = threading.Event()
        self.ok = False
        self.msg = ""


class _Client:
    """一个已连接的播放器客户端。"""

    def __init__(self, cid: int, sock: socket.socket, name: str, addr,
                 host: str = "", port: int = 0):
        self.id = cid
        self.sock = sock
        self.name = name
        self.addr = addr
        self.host = host  # 地址簿里的登记地址（服务端主动连出时才有）
        self.port = port
        self.lock = threading.Lock()


class ControlServer:
    """TCP 控制服务端。"""

    def __init__(self, host: str = "0.0.0.0", port: int = DEFAULT_CONTROL_PORT,
                 shot_dir: str | Path = DEFAULT_SHOT_DIR,
                 book: str | Path = "clients.json", log=print):
        self.host = host
        self.port = port
        self.shot_dir = Path(shot_dir)
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.book_path = Path(book)
        self.log = log
        self.shots: list[Path] = []  # 收到的截屏文件路径
        self.on_shot = None  # 可选回调：on_shot(path)
        self.on_frame = None  # 可选回调：on_frame(client_id, jpeg_bytes)
        self.on_files = None  # 可选回调：on_files(client_id, files)
        self.schedule: list[dict] = []  # 最近下发的定时播放计划配置
        self._file_requests: dict[int, _FileRequest] = {}
        self._replies: dict[int, _Reply] = {}
        self.record_dir: Path | None = None  # 当前 / 最近一次的录制文件夹
        self._recorder: VideoRecorder | None = None
        self._audio_file = None  # 客户端声音的 PCM 缓冲（录制时合成进 MP4）
        self._audio_offset = 0  # 开始录制时声音缓冲的位置（对齐声画起点）
        self._audio_rate = 48000
        self._audio_channels = 2
        self._record_with_audio = False
        self._clients: dict[int, _Client] = {}
        self._lock = threading.Lock()
        self._book_lock = threading.Lock()
        self._next_id = 1
        self._server: socketserver.ThreadingTCPServer | None = None
        self._stopped = threading.Event()
        self._offline_reported: set[tuple[str, int]] = set()
        self.book: list[dict] = self._load_book()

    # ---------- 生命周期 ----------
    def serve_forever(self) -> None:
        server = socketserver.ThreadingTCPServer((self.host, self.port), self._handler_cls())
        server.daemon_threads = True
        self._server = server
        self.log(f"控制服务端已启动：{self.host}:{self.port}"
                 f"（截屏保存目录 {self.shot_dir}）")
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
            self.log("控制服务端已停止")

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        threading.Thread(target=self._reconnect_loop, daemon=True).start()
        return thread

    def stop(self) -> None:
        self._stopped.set()
        self.stop_recording()
        self._close_audio()
        server = self._server
        if server is not None:
            threading.Thread(target=server.shutdown, daemon=True).start()

    # ---------- 客户端地址簿（服务端主动添加 / 连接） ----------
    def _load_book(self) -> list[dict]:
        try:
            data = json.loads(self.book_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict) and item.get("host")]
        except (OSError, ValueError):
            pass
        return []

    def _save_book(self) -> None:
        try:
            self.book_path.write_text(
                json.dumps(self.book, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self.log(f"地址簿保存失败：{exc}")

    def add_client(self, host: str, port: int = DEFAULT_LISTEN_PORT, name: str = "") -> bool:
        """登记一个客户端并立即尝试连上；连不上会自动重试。"""
        host = (host or "").strip()
        if not host:
            raise ValueError("客户端地址不能为空")
        entry = {"name": name.strip() or host, "host": host, "port": int(port)}
        with self._book_lock:
            if not any(e["host"] == host and int(e["port"]) == entry["port"]
                       for e in self.book):
                self.book.append(entry)
                self._save_book()
        return self._connect_out(entry)

    def remove_client(self, host: str, port: int) -> None:
        """从地址簿移除客户端，并断开由本端主动建立的连接。"""
        with self._book_lock:
            self.book = [e for e in self.book
                         if not (e["host"] == host and int(e["port"]) == int(port))]
            self._save_book()
        for client in list(self._clients.values()):
            if client.host == host and int(client.port) == int(port):
                try:
                    client.sock.close()
                except OSError:
                    pass

    def _connect_out(self, entry: dict) -> bool:
        """主动连向一个客户端并开一个会话线程。"""
        try:
            sock = socket.create_connection((entry["host"], int(entry["port"])), timeout=3)
        except (OSError, ValueError) as exc:
            key = (entry["host"], int(entry["port"]))
            if key not in self._offline_reported:
                self._offline_reported.add(key)
                self.log(f"连接客户端 {entry['host']}:{entry['port']} 失败：{exc}（稍后自动重试）")
            return False
        sock.settimeout(None)
        self._offline_reported.discard((entry["host"], int(entry["port"])))
        threading.Thread(target=self._serve_client,
                         args=(sock, (entry["host"], int(entry["port"])), entry),
                         daemon=True).start()
        return True

    def _reconnect_loop(self) -> None:
        """地址簿里没连上的客户端，每隔一段时间自动重连。"""
        while not self._stopped.wait(RECONNECT_INTERVAL):
            for entry in list(self.book):
                if self._find_online(entry["host"]) is not None:
                    continue
                self._connect_out(entry)

    def _find_online(self, host: str) -> _Client | None:
        with self._lock:
            for client in self._clients.values():
                if client.addr[0] == host or client.host == host:
                    return client
        return None

    # ---------- 客户端管理 ----------
    def _handler_cls(self):
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                outer._serve_client(self.request, self.client_address)

        return Handler

    def _serve_client(self, sock: socket.socket, addr, origin: dict | None = None) -> None:
        client: _Client | None = None
        try:
            sock.settimeout(None)
            header = read_header(sock)
            if header.get("cmd") != "hello":
                send_header(sock, {"ok": False, "cmd": "hello", "msg": "请先发送 hello"})
                return
            with self._lock:
                cid = self._next_id
                self._next_id += 1
                client = _Client(
                    cid, sock,
                    str((origin or {}).get("name") or header.get("name") or f"client{cid}"),
                    addr,
                    host=str((origin or {}).get("host", "")),
                    port=int((origin or {}).get("port", 0)),
                )
                self._clients[cid] = client
            send_header(sock, {"ok": True, "cmd": "welcome", "client_id": cid})
            self.log(f"客户端已连接：#{cid} {client.name} ({addr[0]}:{addr[1]})")

            while True:
                frame = read_header(sock)
                cmd = frame.get("cmd")
                if cmd == "event":
                    self.log(f"[#{cid}] {frame.get('msg')}")
                elif cmd == "shot_data":
                    path = self._save_shot(cid, sock, frame)
                    self.shots.append(path)
                    self.log(f"[#{cid}] 收到截屏：{path}（{path.stat().st_size} 字节）")
                    if self.on_shot is not None:
                        self.on_shot(path)
                elif cmd == "frame":  # 实时画面帧
                    data = recv_exact(sock, max(0, int(frame.get("size", 0))))
                    self._handle_frame(cid, data)
                elif cmd == "audio":  # 客户端系统声音（PCM）
                    data = recv_exact(sock, max(0, int(frame.get("size", 0))))
                    self._handle_audio(cid, data, frame)
                elif cmd == "ping":
                    send_header(sock, {"ok": True, "cmd": "pong"})
                elif cmd == "files":  # 客户端接收目录的文件列表
                    self._deliver_files(cid, list(frame.get("files") or []))
                elif cmd == "deleted":  # 删除文件的执行结果
                    self._deliver_reply(cid, frame)
                elif "ok" in frame:  # 客户端回执
                    if frame.get("msg"):
                        self.log(f"[#{cid}] {frame.get('msg')}")
                else:
                    self.log(f"[#{cid}] 未知帧：{cmd}")
        except (ProtocolError, OSError, ValueError) as exc:
            self.log(f"连接结束（#{client.id if client else '-'}）："
                     f"{type(exc).__name__}: {exc}")
        finally:
            if client is not None:
                with self._lock:
                    self._clients.pop(client.id, None)
                self.log(f"客户端断开：#{client.id} {client.name}")
            try:
                sock.close()
            except OSError:
                pass

    def _deliver_files(self, cid: int, files: list[dict]) -> None:
        """收到文件列表：唤醒等待中的请求，并回调给界面。"""
        with self._lock:
            request = self._file_requests.get(cid)
        if request is not None:
            request.files = files
            request.event.set()
        if self.on_files is not None:
            self.on_files(cid, files)

    def _deliver_reply(self, cid: int, frame: dict) -> None:
        """收到操作回执：唤醒等待中的调用者，并记日志。"""
        with self._lock:
            reply = self._replies.get(cid)
        msg = str(frame.get("msg") or "")
        if msg:
            self.log(f"[#{cid}] {msg}")
        if reply is not None:
            reply.ok = bool(frame.get("ok"))
            reply.msg = msg
            reply.event.set()

    def _handle_frame(self, cid: int, data: bytes) -> None:
        """收到一帧实时画面：按需落盘，并回调给界面。"""
        if self._recorder is not None:
            self._recorder.add_frame(data)
        if self.on_frame is not None:
            self.on_frame(cid, data)

    def _handle_audio(self, cid: int, data: bytes, frame: dict) -> None:
        """收到客户端的系统声音：按顺序写进 PCM 缓冲，供录制时合成。"""
        file = self._audio_file
        if file is None or not data:
            return
        self._audio_rate = int(frame.get("rate") or self._audio_rate)
        self._audio_channels = int(frame.get("ch") or self._audio_channels)
        try:
            file.write(data)
        except OSError as exc:
            self.log(f"写入声音数据失败：{exc}")
            self._close_audio()

    def _open_audio(self) -> None:
        """开始收声音：开一个 PCM 临时文件（录完再和画面合成）。"""
        self._close_audio()
        try:
            self._audio_file = open(self.shot_dir / "_audio.pcm", "wb")
        except OSError as exc:
            self.log(f"无法写入声音缓冲：{exc}")
            self._audio_file = None

    def _close_audio(self) -> None:
        file, self._audio_file = self._audio_file, None
        if file is None:
            return
        try:
            file.close()
        except OSError:
            pass
        try:
            Path(file.name).unlink(missing_ok=True)
        except OSError:
            pass

    def _save_shot(self, cid: int, sock: socket.socket, frame: dict) -> Path:
        size = max(0, int(frame.get("size", 0)))
        raw_name = str(frame.get("name") or "").strip()
        if raw_name:
            name = f"client{cid}_{time.strftime('%H%M%S')}_{safe_name(raw_name)}"
        else:
            name = f"client{cid}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        path = self.shot_dir / name
        path.write_bytes(recv_exact(sock, size) if size else b"")
        return path

    def clients(self) -> list[dict]:
        with self._lock:
            return [{"id": c.id, "name": c.name, "addr": f"{c.addr[0]}:{c.addr[1]}",
                     "host": c.addr[0], "port": c.addr[1],
                     "book_host": c.host, "book_port": c.port}
                    for c in self._clients.values()]

    def _get(self, cid: int) -> _Client | None:
        with self._lock:
            return self._clients.get(cid)

    # ---------- 下发指令 ----------
    def send(self, cid: int, header: dict, payload: bytes = b"") -> None:
        client = self._get(cid)
        if client is None:
            raise KeyError(f"客户端 #{cid} 不在线")
        with client.lock:
            send_frame(client.sock, header, payload)

    def control(self, cid: int, action: str, **args) -> None:
        self.send(cid, {"cmd": "control", "action": action, "args": args})

    def push_file(self, cid: int, path: str | Path, remote_name: str = "") -> None:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"文件不存在：{source}")
        name = remote_name or source.name
        payload_size = source.stat().st_size
        client = self._get(cid)
        if client is None:
            raise KeyError(f"客户端 #{cid} 不在线")
        with client.lock:
            send_header(client.sock, {"cmd": "push", "name": name, "size": payload_size})
            with open(source, "rb") as fp:
                while True:
                    chunk = fp.read(CHUNK)
                    if not chunk:
                        break
                    client.sock.sendall(chunk)
        self.log(f"已向 #{cid} 推送文件 {name}（{payload_size} 字节）")

    def request_screenshot(self, cid: int) -> None:
        self.send(cid, {"cmd": "shot"})

    def start_record(self, cid: int, fps: int = DEFAULT_FPS,
                     width: int = DEFAULT_FRAME_WIDTH, audio: bool = False) -> None:
        """让客户端按 fps 持续回传画面（audio=True 时同时回传系统声音）。"""
        fps = max(1, min(MAX_FPS, int(fps)))
        self.send(cid, {"cmd": "record", "fps": fps, "width": int(width),
                        "audio": 1 if audio else 0})
        if audio:  # 声音从现在开始收，录制时按偏移量对齐
            self._open_audio()
        self.log(f"[#{cid}] 开始实时画面 {fps} fps{'（含系统声音）' if audio else ''}")

    def stop_record(self, cid: int) -> None:
        self.send(cid, {"cmd": "record", "fps": 0})
        self.log(f"[#{cid}] 停止实时画面")

    def start_recording(self, prefix: str = "", fps: int = DEFAULT_FPS,
                        audio: bool = False) -> Path | None:
        """开始把实时画面录成 MP4；返回录制文件夹，失败返回 None。"""
        self.stop_recording()
        folder = self.shot_dir / f"{prefix}{time.strftime('%Y%m%d_%H%M%S')}"
        folder.mkdir(parents=True, exist_ok=True)
        recorder = VideoRecorder(folder / "record.mp4", fps, log=self.log)
        if not recorder.start():
            return None
        self._recorder = recorder
        self.record_dir = folder
        self._record_with_audio = bool(audio) and self._audio_file is not None
        self._audio_offset = self._audio_file.tell() if (self._audio_file
                                                         and self._record_with_audio) else 0
        self.log(f"开始录制视频：{folder / 'record.mp4'}（{recorder.fps} fps"
                 f"{'，含声音' if self._record_with_audio else ''}）"
                 "——需先点『开始实时画面』才会有画面帧")
        return folder

    @property
    def is_recording(self) -> bool:
        """是否正在录制 MP4。"""
        return self._recorder is not None

    def stop_recording(self) -> Path | None:
        """停止录制并返回视频文件；未录制返回 None（文件夹仍保留，便于打开查看）。"""
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            return None
        path = recorder.stop()
        if path is None:
            return None
        if self._record_with_audio:  # 把这段时间的声音合成进去
            self._mux_audio(path)
            self._record_with_audio = False
        self.log(f"录制结束：{path}（{recorder.frames} 帧）")
        return path

    def _mux_audio(self, video: Path) -> bool:
        """把录制期间收到的声音合成进视频（输出到 record.mp4）。"""
        file = self._audio_file
        wav = video.with_name("audio.wav")
        try:
            with open(file.name, "rb") as src:
                src.seek(self._audio_offset)
                pcm = src.read()
        except OSError as exc:
            self.log(f"读取声音缓冲失败：{exc}")
            return False
        duration = len(pcm) / max(1, self._audio_rate * self._audio_channels * 2)
        if duration < 0.2:
            self.log("没收到足够的声音数据，录像仍只有画面")
            return False
        try:
            with wave.open(str(wav), "wb") as out:
                out.setnchannels(self._audio_channels)
                out.setsampwidth(2)
                out.setframerate(self._audio_rate)
                out.writeframes(pcm)
        except (OSError, ValueError) as exc:
            self.log(f"生成声音文件失败：{exc}")
            return False
        exe, _ = find_ffmpeg()
        if not exe:
            self.log("未找到 ffmpeg，无法合成声音")
            wav.unlink(missing_ok=True)
            return False
        merged = video.with_name("record_av.mp4")
        cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
               "-i", str(video), "-i", str(wav),
               "-c:v", "copy", "-c:a", "aac", "-shortest", str(merged)]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=120, text=True, errors="ignore")
        except (OSError, subprocess.SubprocessError) as exc:
            self.log(f"合成声音失败：{exc}")
            merged.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)
            return False
        wav.unlink(missing_ok=True)
        if not merged.is_file() or merged.stat().st_size == 0:
            self.log("合成声音失败：ffmpeg 没产出文件")
            merged.unlink(missing_ok=True)
            return False
        try:
            os.replace(merged, video)  # 用带声音的版本替换纯画面版本
        except OSError as exc:
            self.log(f"替换视频失败：{exc}")
            return False
        self.log(f"已合成声音：{video}（声音 {duration:.1f} 秒）")
        return True

    def set_autoshot(self, cid: int, seconds: int) -> None:
        self.send(cid, {"cmd": "autoshot", "interval": max(0, int(seconds))})

    def open_media(self, cid: int, path: str) -> None:
        self.control(cid, "open", path=path)

    def play_file(self, cid: int, name: str) -> None:
        """让客户端播放接收目录里的指定文件（只传文件名即可）。"""
        self.control(cid, "open", name=name)

    # ---------- 多客户端批量操作 ----------
    def push_file_many(self, cids: list[int], path: str | Path) -> dict[int, str]:
        """并发把同一个文件推给多个客户端，返回 {客户端编号: 错误说明（空串表示成功）}。"""
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"文件不存在：{source}")

        def one(cid: int) -> tuple[int, str]:
            try:
                self.push_file(cid, source)
            except (KeyError, OSError, ValueError) as exc:
                return cid, str(exc)
            return cid, ""

        if len(cids) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(cids))) as pool:
                results = dict(pool.map(one, cids))
        else:
            results = dict(one(cid) for cid in cids)
        ok = [cid for cid, err in results.items() if not err]
        self.log(f"批量推送 {source.name} → {len(ok)}/{len(cids)} 个客户端成功")
        return results

    def control_many(self, cids: list[int], action: str, **args) -> dict[int, str]:
        """给多个客户端下发同一条控制指令。"""
        results: dict[int, str] = {}
        for cid in cids:
            try:
                self.control(cid, action, **args)
                results[cid] = ""
            except (KeyError, OSError, ValueError) as exc:
                results[cid] = str(exc)
        return results

    def sync_play(self, cids: list[int], name: str = "", path: str | Path = "",
                  delay: float = SYNC_LEAD_SECONDS,
                  loop: bool | None = None) -> float:
        """让多个客户端在同一时刻开始播放同一内容，返回约定的起播时间戳。

        path 非空时先把文件推给所有客户端，name 可指定客户端接收目录里已有的文件名。
        """
        remote_name = name
        if path:
            source = Path(path)
            self.push_file_many(cids, source)
            remote_name = remote_name or source.name
        start_at = time.time() + max(1.0, float(delay))
        for cid in cids:
            args = {"name": remote_name, "start_at": start_at, "server_time": time.time()}
            if loop is not None:
                args["loop"] = bool(loop)
            try:
                self.control(cid, "sync", **args)
            except (KeyError, OSError, ValueError) as exc:
                self.log(f"[#{cid}] 同步播放指令失败：{exc}")
        self.log(f"同步播放：{len(cids)} 个客户端将在 {time.strftime('%H:%M:%S', time.localtime(start_at))} 起播 {remote_name}")
        return start_at

    def set_schedule(self, cids: list[int], items: list[dict]) -> None:
        """下发定时播放计划（同时记为服务端当前的计划配置）。"""
        self.schedule = [dict(item) for item in items]
        results = self.control_many(cids, "schedule", items=self.schedule)
        for cid, err in results.items():
            if err:
                self.log(f"[#{cid}] 定时计划下发失败：{err}")
        self.log(f"已向 {len(cids)} 个客户端下发定时计划：{len(self.schedule)} 项")

    def delete_file(self, cid: int, name: str, timeout: float = 5.0) -> tuple[bool, str]:
        """删除客户端接收目录里的文件（客户端本地也一起删），返回 (是否成功, 说明)。"""
        reply = _Reply()
        with self._lock:
            self._replies[cid] = reply
        try:
            self.send(cid, {"cmd": "delete", "name": name})
            if not reply.event.wait(timeout):
                return False, "删除请求超时"
            return reply.ok, reply.msg or ("已删除" if reply.ok else "删除失败")
        finally:
            with self._lock:
                self._replies.pop(cid, None)

    def request_file_list(self, cid: int, timeout: float = 5.0) -> list[dict]:
        """向客户端索取接收目录的文件列表（阻塞等待，最多 timeout 秒）。"""
        request = _FileRequest()
        with self._lock:
            self._file_requests[cid] = request
        try:
            self.send(cid, {"cmd": "list"})
            if not request.event.wait(timeout):
                self.log(f"[#{cid}] 获取文件列表超时")
                return []
            return request.files
        finally:
            with self._lock:
                self._file_requests.pop(cid, None)


def run_cli(server: ControlServer) -> None:
    """交互式命令行。"""
    current: int | None = None
    print(HELP)
    while True:
        try:
            line = input(f"control[#{current}]> " if current else "control> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = shlex.split(line)
        cmd, args = parts[0].lower(), parts[1:]

        try:
            if cmd in ("exit", "quit_server"):
                break
            if cmd == "help":
                print(HELP)
            elif cmd == "clients":
                items = server.clients()
                if not items:
                    print("（暂无客户端连接）")
                for item in items:
                    mark = "*" if item["id"] == current else " "
                    print(f" {mark} #{item['id']} {item['name']} {item['addr']}")
            elif cmd == "use":
                current = int(args[0])
                print(f"已选择客户端 #{current}")
            elif cmd == "add":
                port = int(args[1]) if len(args) > 1 else DEFAULT_LISTEN_PORT
                name = args[2] if len(args) > 2 else ""
                print("已连接" if server.add_client(args[0], port, name)
                      else "已登记，等待自动重连")
            elif cmd == "remove":
                port = int(args[1]) if len(args) > 1 else DEFAULT_LISTEN_PORT
                server.remove_client(args[0], port)
                print("已移除")
            elif cmd == "book":
                if not server.book:
                    print("（地址簿为空）")
                for item in server.book:
                    online = "在线" if server._find_online(item["host"]) else "离线"
                    print(f" {item['name']}  {item['host']}:{item['port']}  [{online}]")
            elif cmd == "shot":
                _need(current)
                server.request_screenshot(current)
                print("已请求截屏，稍后保存到截屏目录")
            elif cmd == "autoshot":
                _need(current)
                value = 0 if args[0].lower() in ("0", "off") else int(args[0])
                server.set_autoshot(current, value)
                print(f"自动截屏间隔已设为 {value} 秒")
            elif cmd == "record":
                _need(current)
                if args[0].lower() in ("0", "off"):
                    server.stop_record(current)
                    print("已停止实时画面")
                else:
                    width = int(args[1]) if len(args) > 1 else DEFAULT_FRAME_WIDTH
                    audio = any(a.lower() in ("audio", "sound", "on")
                                for a in args[2:])
                    server.start_record(current, int(args[0]), width, audio)
                    print(f"已开始实时画面：{args[0]} fps，宽 {width}"
                          f"{'（含系统声音）' if audio else ''}")
            elif cmd == "savevideo":
                if args[0].lower() == "on":
                    prefix = f"client{current}_" if current else ""
                    folder = server.start_recording(prefix)
                    print(f"录制中，视频将保存到 {folder}")
                else:
                    path = server.stop_recording()
                    print(f"录制结束：{path}" if path else "未生成视频")
            elif cmd == "push":
                # push <文件> [all | 1,2,3]     不填目标则发给当前客户端
                targets = _targets(server, current, args[1] if len(args) > 1 else "")
                results = server.push_file_many(targets, args[0])
                for cid, err in results.items():
                    print(f" #{cid} {'已推送' if not err else '失败：' + err}")
            elif cmd == "sync":
                # sync <本地文件 | 客户端已有文件名> [延迟秒] [all | 1,2,3]
                targets = _targets(server, current, args[2] if len(args) > 2 else "all")
                source = Path(args[0])
                delay = float(args[1]) if len(args) > 1 else SYNC_LEAD_SECONDS
                if source.is_file():  # 本地文件：先推给各端再同步起播
                    start_at = server.sync_play(targets, path=source, delay=delay)
                else:  # 客户端接收目录里已有的文件名
                    start_at = server.sync_play(targets, name=args[0], delay=delay)
                print(f"{len(targets)} 个客户端将在 "
                      f"{time.strftime('%H:%M:%S', time.localtime(start_at))} 同步起播")
            elif cmd == "schedule":
                # schedule add <开始HH:MM> <结束HH:MM|-> <文件或文件名> [all|编号,编号]
                # schedule off [all|编号,编号]
                if args[0].lower() == "off":
                    targets = _targets(server, current, args[1] if len(args) > 1 else "")
                    server.set_schedule(targets, [])
                    server.schedule = []
                    print("已清空定时播放计划")
                elif args[0].lower() == "add" and len(args) >= 4:
                    end = "" if args[2] in ("-", "none", "结束") else args[2]
                    item = {"start": args[1], "end": end, "file": args[3],
                            "loop": True, "enabled": True}
                    targets = _targets(server, current, args[4] if len(args) > 4 else "")
                    server.set_schedule(targets, server.schedule + [item])
                    print(f"已下发定时计划：{item['start']} → {end or '当天结束'} 播放 "
                          f"{item['file']}（共 {len(server.schedule)} 项）")
                elif args[0].lower() == "add" and len(args) == 3:  # 兼容：只给开始时间
                    item = {"start": args[1], "end": "", "file": args[2],
                            "loop": True, "enabled": True}
                    targets = _targets(server, current, args[3] if len(args) > 3 else "")
                    server.set_schedule(targets, server.schedule + [item])
                    print(f"已下发定时计划：{item['start']} 起播放 {item['file']}"
                          f"（共 {len(server.schedule)} 项）")
                else:
                    print("用法：schedule add <开始HH:MM> <结束HH:MM|-> <文件> [目标]"
                          "  |  schedule off [目标]")
            elif cmd == "files":
                _need(current)
                for item in server.request_file_list(current):
                    print(f" {item['name']}  {item['size'] / 1024:.1f} KB")
            elif cmd == "play":
                _need(current)
                server.play_file(current, args[0])
                print(f"已让 #{current} 播放 {args[0]}")
            elif cmd in ("delete", "del", "rm"):
                _need(current)
                ok, msg = server.delete_file(current, args[0])
                print(f"{'已删除' if ok else '删除失败'}：{msg}")
            elif cmd == "open":
                _need(current)
                server.open_media(current, args[0])
                print("已下发打开指令")
            elif cmd in ("play", "pause", "toggle", "stop", "next", "prev", "restart", "quit"):
                _need(current)
                server.control(current, cmd)
                print(f"已下发 {cmd}")
            elif cmd == "seek":
                _need(current)
                server.control(current, "seek", ms=int(args[0]))
            elif cmd == "volume":
                _need(current)
                server.control(current, "volume", value=int(args[0]))
            elif cmd == "mute":
                _need(current)
                server.control(current, "mute", on=_bool_arg(args[0]))
            elif cmd == "loop":
                _need(current)
                server.control(current, "loop", on=_bool_arg(args[0]))
            elif cmd == "full":
                _need(current)
                server.control(current, "fullscreen", on=_bool_arg(args[0]))
            elif cmd == "page":
                _need(current)
                server.control(current, "page", index=int(args[0]))
            elif cmd == "autoplay":
                _need(current)
                server.control(current, "autoplay", on=_bool_arg(args[0]))
            elif cmd == "interval":
                _need(current)
                server.control(current, "interval", seconds=int(args[0]))
            elif cmd == "docloop":
                _need(current)
                server.control(current, "docloop", on=_bool_arg(args[0]))
            elif cmd == "fit":
                _need(current)
                server.control(current, "fit", mode=args[0])
            else:
                print(f"未知指令：{cmd}（输入 help 查看）")
        except (KeyError, FileNotFoundError, ValueError, IndexError) as exc:
            print(f"错误：{exc}")
        except OSError as exc:
            print(f"网络错误：{exc}（客户端可能已断开）")


def _need(current: int | None) -> None:
    if current is None:
        raise ValueError("请先用 use <id> 选择客户端")


def _targets(server: ControlServer, current: int | None, spec: str) -> list[int]:
    """解析指令里的目标客户端：all / 逗号分隔的编号 / 留空表示当前客户端。"""
    text = (spec or "").strip().lower()
    if text == "all":
        return [item["id"] for item in server.clients()]
    if text:
        return [int(part) for part in text.split(",") if part.strip()]
    _need(current)
    return [int(current)]


def main() -> int:
    parser = argparse.ArgumentParser(description="多媒体播放器远程控制服务端")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_CONTROL_PORT,
                        help=f"监听端口，默认 {DEFAULT_CONTROL_PORT}")
    parser.add_argument("--shots", default=str(DEFAULT_SHOT_DIR), help="截屏保存目录")
    parser.add_argument("--book", default="clients.json", help="客户端地址簿文件")
    parser.add_argument("--cli", action="store_true", help="使用命令行界面（默认图形界面）")
    args = parser.parse_args()

    if not args.cli:  # 默认启动图形界面
        from control_server_ui import main as ui_main

        return ui_main(["--host", args.host, "--port", str(args.port),
                        "--shots", args.shots, "--book", args.book])

    server = ControlServer(host=args.host, port=args.port, shot_dir=args.shots,
                           book=args.book)
    server.start_background()
    try:
        run_cli(server)
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
