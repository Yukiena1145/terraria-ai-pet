"""ClrMD 32-bit bridge client.

为什么不是纯 Python：
* Terraria 是 32-bit .NET Framework 进程；
* 当前 Python 是 64-bit，直接加载 ClrMD 会 DAC 架构不匹配；
* 因此用一个 32-bit 的 ClrMD helper 读取，Python 只通过 JSONL 通信。

helper 源码：bridge/probe.cs
helper 可执行：bridge/clrmd_probe32.exe
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_BRIDGE = BASE_DIR / "bridge" / "clrmd_probe32.exe"


class ClrMDReader:
    """持续读取 Terraria.Main.player[0] 的 position / velocity / HP。"""

    def __init__(
        self,
        *,
        exe_path: str,
        bridge_path: str | Path | None = None,
        interval_ms: int = 100,
        tiles_enabled: bool = False,
        log=None,
    ) -> None:
        self.exe_path = str(exe_path or "")
        self.bridge = Path(bridge_path) if bridge_path else DEFAULT_BRIDGE
        self.interval_ms = max(30, int(interval_ms))
        self.tiles_enabled = bool(tiles_enabled)
        self.log = log
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._latest: dict = {}
        self._latest_at = 0.0
        self._last_error = ""
        self._last_error_logged = ""
        self._last_start_attempt = 0.0
        self._started_at = 0.0
        self._watchdog = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def ensure_started(self) -> bool:
        """游戏可能比面板晚启动；限频重试附加，避免每次读取都 spawn。"""
        if self.is_ready:
            return True
        now = time.monotonic()
        if now - self._last_start_attempt < 2.0:
            return False
        self._last_start_attempt = now
        return self.start()

    def start(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        pid = self.find_pid()
        if not pid:
            self._error(f"未找到进程: {self.exe_path or 'Terraria.exe'}")
            return False
        if not self.bridge.exists():
            self._error(f"ClrMD bridge 不存在: {self.bridge}")
            return False

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        self._stop.clear()
        try:
            self._proc = subprocess.Popen(
                [
                    str(self.bridge),
                    str(pid),
                    "watch",
                    str(self.interval_ms),
                    "tiles" if self.tiles_enabled else "notiles",
                ],
                cwd=str(self.bridge.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as exc:
            self._error(f"启动 ClrMD bridge 失败: {exc}")
            return False

        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._reader_loop,
            name="clrmd-reader",
            daemon=True,
        )
        self._thread.start()
        if self._watchdog is None or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(
                target=self._watchdog_loop,
                name="clrmd-watchdog",
                daemon=True,
            )
            self._watchdog.start()
        self._emit(f"[ClrMD] 已附加进程 PID={pid}，读取 player.position/velocity", "success")
        return True

    def find_pid(self) -> int:
        target = os.path.normcase(os.path.normpath(str(self.exe_path or "")))
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() != "terraria.exe":
                    continue
                if target:
                    actual = str(proc.info.get("exe") or "")
                    if actual and os.path.normcase(os.path.normpath(actual)) != target:
                        continue
                return int(proc.info["pid"])
            except Exception:
                continue
        return 0

    def restart(self) -> bool:
        """重启 bridge 进程。

        ClrMD 常驻进程跑久了，NPC 数组的指针会读成 0（敌人列表变空），
        但玩家坐标还能读——所以定期重连一次，保证“找怪”还看得见。
        """
        with self._lock:
            self._stop.set()
            if self._proc and self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None
            self._latest = {}
            self._latest_at = 0.0
        logger.warning("ClrMD bridge 重新连接（定期刷新，避免敌人列表读失效）")
        return self.start()

    def _watchdog_loop(self) -> None:
        while not self._stop.wait(5.0):
            try:
                # 只在 bridge 进程真的退出时重连；不要定期重启，避免周期性断数据。
                if self._proc is None or self._proc.poll() is not None:
                    self.restart()
            except Exception:
                logger.exception("ClrMD watchdog 异常")

    def close(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        with self._lock:
            return bool(self._latest.get("ok")) and (time.monotonic() - self._latest_at < 1.0)

    @property
    def error(self) -> str:
        return self._last_error

    def latest(self, *, max_age_s: float = 1.0) -> dict:
        with self._lock:
            age = time.monotonic() - self._latest_at
            data = dict(self._latest)
        if not data:
            raise RuntimeError(self._last_error or "ClrMD 还没有数据")
        if age > max_age_s:
            raise RuntimeError(f"ClrMD 数据过期 {age:.2f}s: {self._last_error}")
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or self._last_error or "ClrMD 读取失败")
        return data

    def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return

        last_log_at = 0.0
        for line in proc.stdout:
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("ClrMD 非 JSON 输出: %s", line[:200])
                continue
            now = time.monotonic()
            with self._lock:
                self._latest = data
                self._latest_at = now
            if data.get("ok"):
                self._last_error = ""
                if now - last_log_at >= 1.0:
                    last_log_at = now
                    logger.info(
                        "ClrMD 坐标 X=%.2f Y=%.2f VX=%.2f VY=%.2f HP=%s",
                        float(data.get("x", 0.0)),
                        float(data.get("y", 0.0)),
                        float(data.get("vx", 0.0)),
                        float(data.get("vy", 0.0)),
                        data.get("hp", "?"),
                    )
            else:
                self._error(str(data.get("error", "ClrMD 读取失败")))

        if not self._stop.is_set():
            self._error("ClrMD bridge 进程已退出")

    def _error(self, message: str) -> None:
        self._last_error = message
        if message != self._last_error_logged:
            self._last_error_logged = message
            logger.warning(message)
            self._emit(f"[ClrMD] {message}", "warning")

    def _emit(self, message: str, level: str = "info") -> None:
        if self.log:
            try:
                self.log(message, level)
            except Exception:
                pass


def probe_once(
    *,
    exe_path: str,
    bridge_path: str | Path | None = None,
) -> dict:
    """同步读取一帧，便于终端调试。"""
    bridge = Path(bridge_path) if bridge_path else DEFAULT_BRIDGE
    target = os.path.normcase(os.path.normpath(str(exe_path or "")))
    pid = 0
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if (proc.info["name"] or "").lower() != "terraria.exe":
                continue
            actual = str(proc.info.get("exe") or "")
            if target and actual and os.path.normcase(os.path.normpath(actual)) != target:
                continue
            pid = int(proc.info["pid"])
            break
        except Exception:
            continue
    if not pid:
        raise RuntimeError("未找到 Terraria.exe 进程")
    result = subprocess.run(
        [str(bridge), str(pid)],
        cwd=str(bridge.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"bridge 退出码 {result.returncode}")
    return json.loads(result.stdout.strip())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = probe_once(exe_path=r"F:/terraria/【PC】Terraria v1.4.5.8/原版客户端/Terraria.exe")
    tiles = data.pop("tiles", None)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if isinstance(tiles, list):
        print(f"tiles: {len(tiles)}x{len(tiles[0]) if tiles else 0}")
