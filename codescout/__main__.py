"""Run with `python -m codescout`; no dependency installation required."""

import argparse
import os
import secrets
import sys
import sqlite3
import webbrowser
from pathlib import Path

from .server import create_server
from .service import Service
from .store import Store
from .lifecycle import InstanceLock


def main():
    parser = argparse.ArgumentParser(description="巡码 CodeScout 本地项目检查服务")
    parser.add_argument("--port", type=int, default=8765, help="本机端口（默认 8765；0 为随机端口）")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    default_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "CodeScout"
    parser.add_argument("--data-dir", type=Path, default=default_data, help="本机配置、模型凭据与报告保存目录")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0～65535 之间")
    try:
        with InstanceLock(args.data_dir):
            return run_service(args)
    except (OSError, ValueError):
        print("无法启动：保存目录不可写或该目录已有巡码服务运行；请检查现有窗口", file=sys.stderr)
        return 1


def run_service(args):
    token = secrets.token_urlsafe(32)
    try:
        service = Service(Store(args.data_dir / "state.db"))
    except (OSError, ValueError, sqlite3.Error):
        print("无法读取配置或打开保存目录，请检查目录权限和数据库状态", file=sys.stderr)
        return 1
    try:
        server = create_server(service, args.port, token)
    except OSError:
        print("无法启动本机服务，请检查端口是否被占用或使用 --port 指定其他端口", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_port}/#token={token}"
    print(url, flush=True)
    service.start()
    if args.open:
        try:
            webbrowser.open_new_tab(url)
        except Exception:
            print("浏览器未能自动打开，请使用上方链接；服务继续运行", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
