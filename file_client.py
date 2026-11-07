"""文件收发接口的命令行客户端（只依赖标准库，可放在另一台机器上运行）。

用法：
    python file_client.py ping --host 192.168.1.10
    python file_client.py list --host 192.168.1.10
    python file_client.py push D:\\media\\demo.mp4 --host 192.168.1.10
    python file_client.py get demo.mp4 D:\\media\\copy.mp4 --host 192.168.1.10
    python file_client.py play demo.mp4 --host 192.168.1.10
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.file_protocol import (  # noqa: E402
    CHUNK,
    DEFAULT_PORT,
    ProtocolError,
    read_header,
    recv_exact,
    send_frame,
    send_header,
)


class FileClient:
    """与播放器建立一条长连接，可连续执行多条命令。"""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 30):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)

    # ---------- 基础收发 ----------
    def _request(self, header: dict, payload: bytes = b"") -> dict:
        send_frame(self.sock, header, payload)
        return read_header(self.sock)

    def ping(self) -> dict:
        return self._request({"cmd": "ping"})

    # ---------- 上传 ----------
    def push(self, file_path: str) -> dict:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在：{path}")
        send_header(self.sock, {
            "cmd": "push", "name": path.name, "size": path.stat().st_size,
        })
        with open(path, "rb") as fp:
            while True:
                chunk = fp.read(CHUNK)
                if not chunk:
                    break
                self.sock.sendall(chunk)
        return read_header(self.sock)

    # ---------- 列表 ----------
    def list(self) -> list[dict]:
        header = self._request({"cmd": "list"})
        if not header.get("ok"):
            raise ProtocolError(header.get("msg", "list 失败"))
        size = int(header.get("size", 0))
        payload = recv_exact(self.sock, size) if size else b"[]"
        return json.loads(payload.decode("utf-8"))

    # ---------- 下载 ----------
    def get(self, name: str, save_to: str) -> dict:
        header = self._request({"cmd": "get", "name": name})
        if not header.get("ok"):
            return header
        size = int(header.get("size", 0))
        dest = Path(save_to)
        dest.parent.mkdir(parents=True, exist_ok=True)
        remaining = size
        with open(dest, "wb") as fp:
            while remaining:
                chunk = self.sock.recv(min(CHUNK, remaining))
                if not chunk:
                    raise ProtocolError("传输中断")
                fp.write(chunk)
                remaining -= len(chunk)
        header["saved"] = str(dest)
        return header

    # ---------- 远程播放 ----------
    def play(self, name: str) -> dict:
        return self._request({"cmd": "play", "name": name})

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "FileClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="多媒体播放器文件收发客户端")
    parser.add_argument("cmd", choices=["ping", "list", "push", "get", "play"])
    parser.add_argument("file", nargs="?", help="push：本地文件路径；get/play：服务端文件名")
    parser.add_argument("dest", nargs="?", help="get：保存到本地的路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    try:
        with FileClient(args.host, args.port) as client:
            if args.cmd == "ping":
                print(client.ping())
            elif args.cmd == "list":
                for item in client.list():
                    print(f"{item['name']}\t{item['size']} 字节")
            elif args.cmd == "push":
                if not args.file:
                    parser.error("push 需要提供本地文件路径")
                print(client.push(args.file))
            elif args.cmd == "get":
                if not args.file or not args.dest:
                    parser.error("get 需要提供服务端文件名和本地保存路径")
                print(client.get(args.file, args.dest))
            elif args.cmd == "play":
                if not args.file:
                    parser.error("play 需要提供服务端文件名")
                print(client.play(args.file))
    except (ProtocolError, OSError, FileNotFoundError) as exc:
        print(f"失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
