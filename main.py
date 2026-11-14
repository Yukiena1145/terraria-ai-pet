"""Terraria-Agent-V1 程序入口。

开发环境：python main.py
打包环境：PyInstaller 单文件 exe，双击后自动打开浏览器。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

from app import create_app, data_dir


def configure_logging() -> Path:
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "agent.log"

    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            log_file,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    ]
    # -w 打包后 stdout/stderr 可能为 None，此时只写文件。
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=handlers,
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return log_file


def open_browser_later(url: str, delay: float = 1.5) -> None:
    def _worker() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            logging.getLogger(__name__).exception("自动打开浏览器失败: %s", url)

    threading.Thread(target=_worker, name="open-browser", daemon=True).start()


_SINGLE_INSTANCE_HANDLE = None


def acquire_single_instance() -> bool:
    """Windows 命名互斥：防止双击两次 start.bat 导致两个 Agent/两个 ClrMD bridge。"""
    global _SINGLE_INSTANCE_HANDLE
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, 0, "Global\TerrariaAgentV1")
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return False
    _SINGLE_INSTANCE_HANDLE = handle
    return True


def main() -> None:
    if not acquire_single_instance():
        print("Terraria-Agent-V1 已经在运行；请勿重复启动。")
        sys.exit(0)
    log_file = configure_logging()
    logger = logging.getLogger(__name__)
    logger.info("日志文件: %s", log_file)

    application = create_app()
    runtime = application.extensions["runtime"]
    config = runtime.config
    host = str(config.get("server", {}).get("host", "127.0.0.1"))
    port = int(config.get("server", {}).get("port", 5000))
    url = f"http://{host}:{port}"

    logger.info("Terraria-Agent-V1 启动: %s（待命，等待首条指令）", url)
    logger.info("首次使用请先阅读网页顶部「新手必看指南」")
    if bool(config.get("server", {}).get("open_browser", True)):
        open_browser_later(url)
    try:
        import socket
        local_ip = socket.gethostbyname(socket.gethostname())
        if local_ip and not local_ip.startswith("127."):
            logger.info("手机/局域网访问: http://%s:%s", local_ip, port)
    except Exception:
        pass

    try:
        from waitress import serve

        logger.info("使用 waitress 启动生产级 WSGI 服务")
        serve(application, host=host, port=port, threads=8)
    except ImportError:
        logger.warning("未安装 waitress，回退 Flask 开发服务器")
        application.run(
            host=host,
            port=port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    main()
