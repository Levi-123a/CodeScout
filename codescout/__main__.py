"""Run with `python -m codescout`; no dependency installation required."""

import argparse
import os
import secrets
import sys
from pathlib import Path

from .server import create_server
from .service import Service
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="巡码 CodeScout 本地项目检查服务")
    parser.add_argument("--port", type=int, default=8765, help="本机端口（默认 8765；0 为随机端口）")
    default_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "CodeScout"
    parser.add_argument("--data-dir", type=Path, default=default_data, help="本机配置、模型凭据与报告保存目录")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0～65535 之间")
    token = secrets.token_urlsafe(32)
    service = Service(Store(args.data_dir / "state.db"))
    try:
        server = create_server(service, args.port, token)
    except OSError:
        print("无法启动本机服务，请检查端口是否被占用或使用 --port 指定其他端口", file=sys.stderr)
        return 1
    print(f"http://127.0.0.1:{server.server_port}/#token={token}", flush=True)
    service.start()
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
