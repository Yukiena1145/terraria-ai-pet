"""Flask 控制面板后端 + Agent 运行时。

包含三块：
1. ``ConfigStore``：config.json 的原子读写、校验、热重载；
2. ``AgentRuntime``：把 monitor / router / skills / executor 串成闭环；
3. ``create_app``：Flask 页面与 AJAX API。

所有路径都兼容 PyInstaller：
* 只读资源（templates/static/config.json）从 ``sys._MEIPASS`` 读取；
* 可写配置优先放在 exe 同目录，不可写时回退到 ``%APPDATA%``。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request

from clrmd_reader import ClrMDReader
from executor import ActionExecutor, ExecutorConfig, InputBackendError, PyDirectInputBackend
from item_catalog import find_items
from monitor import FakeGameMonitor, MemoryGameMonitor, OfflineGameMonitor, RealGameMonitor, SimulatedInputBackend
from router import Intent, KeywordRouter
from skills import SkillContext, SkillResult, build_default_registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 默认配置与路径
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "deepseek_api_key": "",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_model": "deepseek-chat",
    "window_title": "泰拉瑞亚",
    "move_speed": 1.0,
    "grid_radius": 15,
    "simulation_mode": False,
    # 开发阶段：动作失败时自动截一张游戏画面，方便判断是程序错还是不会玩；正式版改成 false。
    "dev_screenshot_on_failure": True,
    "input_backend": "keyboard",
    "inventory_key": "escape",
    # 打怪/找怪的搜索半径（格）：矿车战需要很大的范围
    "enemy_range_tiles": 150,
    "memory": {
        "enabled": True,
        "bridge_path": "",
        "interval_ms": 50,
        "tiles_enabled": False,
    },
    "game": {
        "exe_path": r"E:\SteamLibrary\steamapps\common\Terraria\Terraria.exe",
        "window_titles": ["Terraria", "泰拉瑞亚"],
        "capture_fps": 12,
        "tile_px": 16,
        "screen_scale": 1.0,
        "player_offset": [0.5, 0.5],
        "max_hp": 100,
        "hotbar": [
            {"slot": 1, "name": "Copper Axe", "kind": "axe", "power": 5, "damage": 3},
            {"slot": 2, "name": "Copper Pickaxe", "kind": "pickaxe", "power": 35, "damage": 4},
            {"slot": 3, "name": "Iron Pickaxe", "kind": "pickaxe", "power": 40, "damage": 5},
            {"slot": 4, "name": "Wooden Sword", "kind": "weapon", "damage": 7},
            {"slot": 5, "name": "Iron Broadsword", "kind": "weapon", "damage": 12},
            {"slot": 6, "name": "Hammer", "kind": "hammer", "power": 30, "damage": 2},
        ],
    },
    "server": {
        "host": "127.0.0.1",
        "port": 5000,
        "open_browser": True,
    },
    "agent": {
        "max_log_lines": 300,
        "chop_clicks": 3,
    },
    "commentary": {
        "enabled": True,
        "cooldown_s": 5.0,
        "low_hp_ratio": 0.30,
    },
}


class ConfigError(ValueError):
    def __init__(self, message: str, fields: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.fields = fields or {}


def resource_dir() -> Path:
    """只读资源目录。PyInstaller onefile 下是临时解压目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """可写数据目录。打包后优先 exe 同级目录，失败回退 APPDATA。"""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent

    exe_dir = Path(sys.executable).resolve().parent
    try:
        probe = exe_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return exe_dir
    except OSError:
        fallback = Path(os.getenv("APPDATA", str(Path.home()))) / "TerrariaAgentV1"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def default_config_path() -> Path:
    return data_dir() / "config.json"


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigStore:
    """线程安全的 config.json 管理器。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_config_path()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._ensure_file()
        self.reload()

    def _ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            return

        bundled = resource_dir() / "config.json"
        if bundled.exists() and bundled.resolve() != self.path.resolve():
            try:
                shutil.copy2(bundled, self.path)
                logger.info("已从内置资源复制配置: %s", self.path)
                return
            except OSError:
                logger.exception("复制默认 config.json 失败，将直接生成默认配置")

        self._write(copy.deepcopy(DEFAULT_CONFIG))

    def reload(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if not isinstance(raw, dict):
                    raise ValueError("config.json 顶层必须是对象")
            except Exception as exc:
                logger.exception("读取 config.json 失败，使用默认配置: %s", exc)
                backup = self.path.with_suffix(".broken.json")
                try:
                    if self.path.exists():
                        shutil.copy2(self.path, backup)
                except OSError:
                    pass
                raw = {}
            self._data = _deep_merge(DEFAULT_CONFIG, raw)
            return copy.deepcopy(self._data)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        """校验并合并用户提交的配置；空 API Key 表示“保持原值”。"""
        if not isinstance(payload, dict):
            raise ConfigError("请求体必须是 JSON 对象")

        with self._lock:
            data = copy.deepcopy(self._data)
            fields: dict[str, str] = {}

            if payload.get("clear_api_key") is True:
                data["deepseek_api_key"] = ""
            elif "deepseek_api_key" in payload:
                key = payload.get("deepseek_api_key")
                if not isinstance(key, str):
                    fields["deepseek_api_key"] = "必须是字符串"
                elif key.strip():
                    data["deepseek_api_key"] = key.strip()

            if "deepseek_base_url" in payload:
                value = str(payload["deepseek_base_url"]).strip()
                if not value.startswith(("http://", "https://")):
                    fields["deepseek_base_url"] = "必须以 http:// 或 https:// 开头"
                else:
                    data["deepseek_base_url"] = value.rstrip("/")

            if "deepseek_model" in payload:
                value = str(payload["deepseek_model"]).strip()
                if not value:
                    fields["deepseek_model"] = "不能为空"
                else:
                    data["deepseek_model"] = value

            if "window_title" in payload:
                value = str(payload["window_title"]).strip()
                if not value:
                    fields["window_title"] = "不能为空"
                elif len(value) > 120:
                    fields["window_title"] = "不能超过 120 个字符"
                else:
                    data["window_title"] = value

            if "move_speed" in payload:
                try:
                    value = float(payload["move_speed"])
                except (TypeError, ValueError):
                    fields["move_speed"] = "必须是数字"
                else:
                    if not 0.2 <= value <= 3.0:
                        fields["move_speed"] = "必须在 0.2 ~ 3.0 之间"
                    else:
                        data["move_speed"] = round(value, 2)

            if "grid_radius" in payload:
                try:
                    value = int(payload["grid_radius"])
                except (TypeError, ValueError):
                    fields["grid_radius"] = "必须是整数"
                else:
                    if not 3 <= value <= 20:
                        fields["grid_radius"] = "必须在 3 ~ 20 之间"
                    else:
                        data["grid_radius"] = value

            if "simulation_mode" in payload:
                data["simulation_mode"] = bool(payload["simulation_mode"])

            if "input_backend" in payload:
                backend = str(payload["input_backend"]).lower()
                if backend not in {"keyboard", "none"}:
                    fields["input_backend"] = "只支持 keyboard / none"
                else:
                    data["input_backend"] = backend

            memory_payload = payload.get("memory")
            if isinstance(memory_payload, dict):
                if "enabled" in memory_payload:
                    data["memory"]["enabled"] = bool(memory_payload["enabled"])
                if "bridge_path" in memory_payload:
                    data["memory"]["bridge_path"] = str(memory_payload["bridge_path"]).strip()
                if "interval_ms" in memory_payload:
                    try:
                        interval = int(memory_payload["interval_ms"])
                    except (TypeError, ValueError):
                        fields["memory.interval_ms"] = "必须是整数"
                    else:
                        if not 30 <= interval <= 1000:
                            fields["memory.interval_ms"] = "必须在 30 ~ 1000 ms 之间"
                        else:
                            data["memory"]["interval_ms"] = interval

            server_payload = payload.get("server")
            if isinstance(server_payload, dict):
                if "host" in server_payload:
                    data["server"]["host"] = str(server_payload["host"]).strip() or "127.0.0.1"
                if "port" in server_payload:
                    try:
                        port = int(server_payload["port"])
                    except (TypeError, ValueError):
                        fields["server.port"] = "必须是整数"
                    else:
                        if not 1024 <= port <= 65535:
                            fields["server.port"] = "必须在 1024 ~ 65535 之间"
                        else:
                            data["server"]["port"] = port
                if "open_browser" in server_payload:
                    data["server"]["open_browser"] = bool(server_payload["open_browser"])

            agent_payload = payload.get("agent")
            if isinstance(agent_payload, dict):
                if "chop_clicks" in agent_payload:
                    try:
                        clicks = int(agent_payload["chop_clicks"])
                    except (TypeError, ValueError):
                        fields["agent.chop_clicks"] = "必须是整数"
                    else:
                        if not 1 <= clicks <= 20:
                            fields["agent.chop_clicks"] = "必须在 1 ~ 20 之间"
                        else:
                            data["agent"]["chop_clicks"] = clicks
                if "max_log_lines" in agent_payload:
                    try:
                        lines = int(agent_payload["max_log_lines"])
                    except (TypeError, ValueError):
                        fields["agent.max_log_lines"] = "必须是整数"
                    else:
                        data["agent"]["max_log_lines"] = max(50, min(2000, lines))

            commentary_payload = payload.get("commentary")
            if isinstance(commentary_payload, dict):
                if "enabled" in commentary_payload:
                    data["commentary"]["enabled"] = bool(commentary_payload["enabled"])
                if "cooldown_s" in commentary_payload:
                    try:
                        cooldown = float(commentary_payload["cooldown_s"])
                    except (TypeError, ValueError):
                        fields["commentary.cooldown_s"] = "必须是数字"
                    else:
                        if not 1.0 <= cooldown <= 120.0:
                            fields["commentary.cooldown_s"] = "必须在 1 ~ 120 秒之间"
                        else:
                            data["commentary"]["cooldown_s"] = round(cooldown, 1)
                if "low_hp_ratio" in commentary_payload:
                    try:
                        ratio = float(commentary_payload["low_hp_ratio"])
                    except (TypeError, ValueError):
                        fields["commentary.low_hp_ratio"] = "必须是数字"
                    else:
                        if not 0.1 <= ratio <= 0.9:
                            fields["commentary.low_hp_ratio"] = "必须在 0.1 ~ 0.9 之间"
                        else:
                            data["commentary"]["low_hp_ratio"] = round(ratio, 2)

            game_payload = payload.get("game")
            if isinstance(game_payload, dict):
                if "exe_path" in game_payload:
                    data["game"]["exe_path"] = str(game_payload["exe_path"]).strip()
                if "window_titles" in game_payload:
                    titles = game_payload["window_titles"]
                    if isinstance(titles, list) and all(isinstance(t, str) for t in titles):
                        cleaned = [t.strip() for t in titles if t.strip()]
                        if cleaned:
                            data["game"]["window_titles"] = cleaned
                        else:
                            fields["game.window_titles"] = "至少保留一个窗口标题"
                    else:
                        fields["game.window_titles"] = "必须是字符串数组"
                if "capture_fps" in game_payload:
                    try:
                        fps = float(game_payload["capture_fps"])
                    except (TypeError, ValueError):
                        fields["game.capture_fps"] = "必须是数字"
                    else:
                        if not 1 <= fps <= 30:
                            fields["game.capture_fps"] = "必须在 1 ~ 30 之间"
                        else:
                            data["game"]["capture_fps"] = round(fps, 1)
                if "tile_px" in game_payload:
                    try:
                        tile_px = float(game_payload["tile_px"])
                    except (TypeError, ValueError):
                        fields["game.tile_px"] = "必须是数字"
                    else:
                        if not 8 <= tile_px <= 64:
                            fields["game.tile_px"] = "必须在 8 ~ 64 像素之间"
                        else:
                            data["game"]["tile_px"] = round(tile_px, 2)
                if "screen_scale" in game_payload:
                    try:
                        scale = float(game_payload["screen_scale"])
                    except (TypeError, ValueError):
                        fields["game.screen_scale"] = "必须是数字"
                    else:
                        if not 0.5 <= scale <= 3.0:
                            fields["game.screen_scale"] = "必须在 0.5 ~ 3.0 之间"
                        else:
                            data["game"]["screen_scale"] = round(scale, 2)
                if "player_offset" in game_payload:
                    offset = game_payload["player_offset"]
                    try:
                        values = [float(v) for v in offset] if isinstance(offset, list) else []
                    except (TypeError, ValueError):
                        values = []
                    if len(values) == 2 and all(0.1 <= v <= 0.9 for v in values):
                        data["game"]["player_offset"] = values
                    else:
                        fields["game.player_offset"] = "必须是 [x,y]，范围 0.1 ~ 0.9"
                if "max_hp" in game_payload:
                    try:
                        max_hp = int(game_payload["max_hp"])
                    except (TypeError, ValueError):
                        fields["game.max_hp"] = "必须是整数"
                    else:
                        if not 20 <= max_hp <= 1000:
                            fields["game.max_hp"] = "必须在 20 ~ 1000 之间"
                        else:
                            data["game"]["max_hp"] = max_hp
                if "hotbar" in game_payload:
                    hotbar = game_payload["hotbar"]
                    if isinstance(hotbar, list) and all(isinstance(i, dict) for i in hotbar):
                        data["game"]["hotbar"] = [dict(i) for i in hotbar]
                    else:
                        fields["game.hotbar"] = "必须是物品对象数组"

            if fields:
                raise ConfigError("配置校验失败", fields)

            self._write(data)
            self._data = data
            logger.info("配置已保存: %s", self.path)
            return copy.deepcopy(data)

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def public(self) -> dict[str, Any]:
        with self._lock:
            data = copy.deepcopy(self._data)
            key = str(data.pop("deepseek_api_key", "") or "")
            return {
                **data,
                "deepseek_api_key_set": bool(key),
                "deepseek_api_key_hint": self._mask_key(key),
            }

    @staticmethod
    def _mask_key(key: str) -> str:
        if not key:
            return ""
        if len(key) <= 8:
            return "••••••"
        return f"{key[:3]}••••{key[-4:]}"


# ---------------------------------------------------------------------------
# 游戏内解说 / 防刷屏
# ---------------------------------------------------------------------------


class CommentaryManager:
    """管理“游戏内聊天解说”的开关、文案和同动作冷却。

    * 同一个 key（例如 ``result:chop_tree``）默认 5 秒内只发一次；
    * 用户直接问话使用 ``force=True``，优先回复，不受动作冷却限制；
    * 所有消息都走 ``ActionExecutor.send_chat_message``，因此会先松键再打字。
    """

    def __init__(
        self,
        executor: ActionExecutor,
        log: Any,
        *,
        cooldown_s: float = 5.0,
        enabled: bool = True,
    ) -> None:
        self.executor = executor
        self.log = log
        self.cooldown_s = float(cooldown_s)
        self.enabled = bool(enabled)
        self._last_sent: dict[str, float] = {}
        self._lock = threading.RLock()

    def configure(self, *, cooldown_s: float, enabled: bool) -> None:
        with self._lock:
            self.cooldown_s = max(1.0, float(cooldown_s))
            self.enabled = bool(enabled)

    def say(self, key: str, text: str, *, force: bool = False) -> bool:
        message = " ".join(str(text or "").split()).strip()
        if not message or not self.enabled:
            return False

        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key, 0.0)
            if not force and now - last < self.cooldown_s:
                logger.debug("解说冷却中，跳过: %s", key)
                return False
            self._last_sent[key] = now

        try:
            self.executor.send_chat_message(message)
        except Exception as exc:
            logger.warning("游戏内聊天发送失败: %s", exc)
            self.log(f"游戏内聊天失败: {exc}", "warning")
            return False

        logger.debug("游戏内聊天已发送: %s", message)
        return True


class UnavailableInputBackend:
    """真实键鼠依赖缺失时的“拒绝执行”后端，绝不静默回退假输入。"""

    def __init__(self, error: str) -> None:
        self._error = error or "真实键鼠后端不可用"

    def _raise(self, *args, **kwargs):
        raise InputBackendError(self._error)

    key_down = _raise
    key_up = _raise
    mouse_move = _raise
    mouse_click = _raise
    type_text = _raise

    def ensure_game_focus(self) -> None:
        raise InputBackendError(self._error)

    def release_all(self, keys) -> None:
        return None

    def set_window_title(self, title: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Agent 运行时
# ---------------------------------------------------------------------------


class AgentRuntime:
    """单进程内的 Agent 编排器。"""

    def __init__(self, config_store: ConfigStore) -> None:
        self.config_store = config_store
        self.config = config_store.get()
        self._lock = threading.RLock()
        self._busy = False
        self._last_intent: dict[str, Any] | None = None
        self._last_result: dict[str, Any] | None = None
        self._logs: deque[dict[str, Any]] = deque(
            maxlen=int(self.config["agent"].get("max_log_lines", 300))
        )

        self._armed = False
        self.monitor, self.simulation_mode = self._build_monitor()
        backend = self._build_backend()
        self.executor = ActionExecutor(
            self.monitor,
            backend=backend,
            config=ExecutorConfig(),
        )
        self.executor.set_inventory_key(str(self.config.get("inventory_key", "escape")))
        self.registry = build_default_registry()
        self.router = KeywordRouter(
            api_key=self.config.get("deepseek_api_key", ""),
            base_url=self.config.get("deepseek_base_url", "https://api.deepseek.com"),
            model=self.config.get("deepseek_model", "deepseek-chat"),
        )

        commentary_cfg = self.config.get("commentary", {})
        self.commentary = CommentaryManager(
            self.executor,
            self.log,
            cooldown_s=float(commentary_cfg.get("cooldown_s", 5.0)),
            enabled=bool(commentary_cfg.get("enabled", True)),
        )
        self._low_hp_ratio = float(commentary_cfg.get("low_hp_ratio", 0.30))
        self._low_hp_announced = False
        self._enemy_announced = False
        self._last_reply = ""
        self._interrupt_counter = 0
        self._reply_lock = threading.Lock()
        self._shutdown = threading.Event()

        self.log("Agent V1 已启动，当前处于待命状态；输入首条指令后开始接管", "success")
        if self.simulation_mode:
            self.log("当前为假数据模拟模式：不会操作真实游戏", "warning")

        # 游戏内聊天监听 + 状态解说循环
        threading.Thread(
            target=self._game_chat_loop,
            name="game-chat-listener",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._commentary_loop,
            name="commentary-loop",
            daemon=True,
        ).start()

    def _build_monitor(self):
        """构建监控器：默认 ClrMD 真实坐标，只有 simulation_mode 才用假数据。"""
        if bool(self.config.get("simulation_mode", False)):
            self.log("已启用假数据模式（仅用于离线开发/测试）", "warning")
            return (
                FakeGameMonitor(
                    grid_radius=int(self.config["grid_radius"]),
                    move_speed=float(self.config["move_speed"]),
                    log=self.log,
                ),
                True,
            )

        game_cfg = self.config.get("game", {})
        window_titles = list(
            game_cfg.get("window_titles")
            or [self.config.get("window_title", "泰拉瑞亚"), "Terraria"]
        )
        if bool(self.config.get("memory", {}).get("enabled", True)):
            mem_cfg = self.config.get("memory", {})
            reader = ClrMDReader(
                exe_path=str(game_cfg.get("exe_path", "")),
                bridge_path=mem_cfg.get("bridge_path") or None,
                interval_ms=int(mem_cfg.get("interval_ms", 50)),
                tiles_enabled=bool(mem_cfg.get("tiles_enabled", False)),
                log=self.log,
            )
            started = reader.start()
            if started:
                self.log(
                    "ClrMD 已附加（Passive）：真实 X/Y/Velocity；"
                    "tile 读取当前关闭，先保证游戏不卡死",
                    "success",
                )
            else:
                self.log(
                    f"ClrMD 暂未附加（{reader.error}）；启动游戏后会自动重试",
                    "warning",
                )
            vision = None
            if str(self.config.get("terrain_source", "vision")).lower() == "vision":
                try:
                    vision = RealGameMonitor(
                        grid_radius=int(self.config["grid_radius"]),
                        move_speed=float(self.config["move_speed"]),
                        window_titles=window_titles,
                        exe_path=str(game_cfg.get("exe_path", "")),
                        capture_fps=float(game_cfg.get("capture_fps", 12)),
                        tile_px=float(game_cfg.get("tile_px", 16)),
                        screen_scale=float(game_cfg.get("screen_scale", 1.0)),
                        player_offset=tuple(game_cfg.get("player_offset", [0.5, 0.5])),
                        hotbar=list(game_cfg.get("hotbar") or []),
                        max_hp=int(game_cfg.get("max_hp", 100)),
                        log=self.log,
                    )
                    self.log("地形优先使用视觉网格（本地识别，不发截图给大模型）", "success")
                except Exception as exc:
                    self.log(f"视觉地形不可用，回退内存网格: {exc}", "warning")
            return MemoryGameMonitor(
                reader,
                vision=vision,
                exe_path=str(game_cfg.get("exe_path", "")),
                window_titles=window_titles,
                grid_radius=int(self.config["grid_radius"]),
                hotbar=list(game_cfg.get("hotbar") or []),
                max_hp=int(game_cfg.get("max_hp", 100)),
                log=self.log,
            ), False

        # 显式关闭 memory.enabled 时才回退视觉模式（仅调试用）
        try:
            monitor = RealGameMonitor(
                grid_radius=int(self.config["grid_radius"]),
                move_speed=float(self.config["move_speed"]),
                window_titles=window_titles,
                exe_path=str(game_cfg.get("exe_path", "")),
                capture_fps=float(game_cfg.get("capture_fps", 12)),
                tile_px=float(game_cfg.get("tile_px", 16)),
                screen_scale=float(game_cfg.get("screen_scale", 1.0)),
                player_offset=tuple(game_cfg.get("player_offset", [0.5, 0.5])),
                hotbar=list(game_cfg.get("hotbar") or []),
                max_hp=int(game_cfg.get("max_hp", 100)),
                log=self.log,
            )
            self.log("已回退视觉监控（memory.enabled=false）", "warning")
            return monitor, False
        except Exception as exc:
            logger.exception("视觉监控器初始化失败")
            return OfflineGameMonitor(
                grid_radius=int(self.config["grid_radius"]),
                error=str(exc),
                log=self.log,
            ), False

    def launch_game(self) -> tuple[bool, str]:
        """启动配置中的 Terraria.exe；已在运行则只尝试激活窗口。"""
        game_cfg = self.config.get("game", {})
        exe_path = str(game_cfg.get("exe_path", "")).strip()
        if not exe_path:
            return False, "未配置 game.exe_path"
        exe = Path(exe_path)
        if not exe.exists():
            return False, f"游戏路径不存在: {exe}"

        ensure = getattr(self.monitor, "ensure_window", None)
        activate = getattr(self.monitor, "activate", None)
        if callable(ensure) and ensure():
            activated = True
            if callable(activate):
                activated = bool(activate())
            if activated:
                return True, "游戏已在运行，已切到前台"
            return False, "游戏窗口已找到，但无法激活；请手动点击游戏窗口后重试"

        try:
            subprocess.Popen(
                [str(exe)],
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception as exc:
            logger.exception("启动游戏失败")
            return False, f"启动游戏失败: {exc}"

        self.log("[启动] 已调用 Terraria.exe，等待窗口出现", "info")
        for _ in range(30):
            time.sleep(0.5)
            if callable(ensure) and ensure():
                if callable(activate):
                    activate()
                self.log("[启动] 已检测到游戏窗口，请进入世界后输入指令", "success")
                return True, "游戏已启动，请进入世界后输入指令"
        return False, "游戏进程已启动，但暂未检测到窗口，请确认是否全屏/启动中"

    def _build_backend(self):
        """默认前台键鼠；缺失时返回不可用后端，不偷偷抢焦点。"""
        if self.simulation_mode:
            return SimulatedInputBackend(self.monitor)

        game_cfg = self.config.get("game", {})
        backend_kind = str(self.config.get("input_backend", "keyboard")).lower()
        try:
            if backend_kind == "none":
                raise RuntimeError("input_backend=none：已禁用真实输入")
            if backend_kind == "keyboard":
                return PyDirectInputBackend(
                    window_title=str(self.config.get("window_title", "泰拉瑞亚")),
                    window_titles=list(
                        game_cfg.get("window_titles")
                        or [self.config.get("window_title", "泰拉瑞亚"), "Terraria"]
                    ),
                    exe_path=str(game_cfg.get("exe_path", "")),
                )
            raise RuntimeError(f"未知 input_backend: {backend_kind}")
        except Exception as exc:
            self.log(f"真实输入后端不可用: {exc}", "error")
            return UnavailableInputBackend(str(exc))

    # ------------------------------------------------------------------
    # 游戏内聊天 / 解说
    # ------------------------------------------------------------------

    def _game_chat_loop(self) -> None:
        """轮询游戏聊天栏输入。

        V1 的 FakeGameMonitor 提供 ``poll_incoming_chat`` 接口，可通过
        ``POST /api/game-chat`` 注入测试消息。真实内存读取版本在这里对接
        Terraria 的聊天行即可，不需要改 Agent 逻辑。
        """
        while not self._shutdown.wait(0.5):
            poller = getattr(self.monitor, "poll_incoming_chat", None)
            if not callable(poller):
                # 真实 MemoryGameMonitor 尚未实现聊天读取时，静默跳过。
                continue
            try:
                messages = poller()
            except Exception:
                logger.exception("读取游戏内聊天失败")
                continue
            for message in messages:
                try:
                    self.handle_user_message(message, source="game")
                except Exception:
                    logger.exception("处理游戏内聊天失败: %s", message)

    def _commentary_loop(self) -> None:
        """每秒检查关键状态，主动在游戏里自言自语。"""
        while not self._shutdown.wait(1.0):
            if not self._armed or not self.commentary.enabled:
                continue
            try:
                state = self.monitor.get_state()
            except Exception:
                logger.exception("解说循环读取状态失败")
                continue

            player = state.get("player", {})
            hp = float(player.get("hp", 0))
            max_hp = float(player.get("max_hp", 100)) or 100.0
            ratio = hp / max_hp

            if ratio <= self._low_hp_ratio and not self._low_hp_announced:
                self._low_hp_announced = True
                self._say(
                    "low_hp",
                    f"血量只剩 {hp:.0f} 了，我得小心一点！",
                )
            elif ratio > min(0.9, self._low_hp_ratio + 0.15):
                self._low_hp_announced = False

            has_enemy = any("E" in row for row in state.get("grid", []))
            if has_enemy and not self._enemy_announced:
                self._enemy_announced = True
                self._say("enemy_spotted", "发现敌人！先别乱跑，我看看能不能打。")
            elif not has_enemy:
                self._enemy_announced = False

    def _say(self, key: str, text: str, *, force: bool = False) -> bool:
        """统一出口：Web 日志 + 游戏内聊天。"""
        if not text:
            return False
        self._last_reply = text
        self.log(f"她：{text}", "reply")
        # 游戏内聊天已关闭：发送聊天会打开聊天框，导致后续合成/背包操作失效。
        return False

    def _status_reply(self, command: str = "", *, action_override: str = "") -> str:
        """根据当前状态生成一句自然语言回复。"""
        state = self.monitor.get_state()
        player = state.get("player", {})
        action = action_override or state.get("action", "Idle")
        if action == "Idle":
            action = "待命"
        # “走向树木 x=-1.5”这种动作名在口语里只保留前半段
        action_short = action.split(" x=")[0]
        tool = state.get("tool", {})
        hp = player.get("hp", 100)
        max_hp = player.get("max_hp", 100)
        x = player.get("x_tiles", 0.0)
        y = player.get("y_tiles", 0.0)

        if "血" in command:
            return f"我现在 {hp:.0f}/{max_hp:.0f} 血，{'有点危险' if hp < max_hp * 0.3 else '还撑得住'}。"
        if "工具" in command or "拿" in command:
            return f"我手上是 {tool.get('name', '空手')}，正在{action_short}。"
        if "坐标" in command or "在哪" in command:
            return f"我在 x={x:.1f}, y={y:.1f} 附近。"
        if "干嘛" in command or "做什么" in command:
            return f"我在{action_short}呀，当前坐标 x={x:.1f}, y={y:.1f}，血量 {hp:.0f}。"
        return f"我在呢。当前{action_short}，血量 {hp:.0f}/{max_hp:.0f}，坐标 x={x:.1f}, y={y:.1f}。"

    def handle_user_message(self, message: str, *, source: str = "web") -> str:
        """用户说话/提问：优先停止非战斗动作并回复。"""
        command = " ".join(str(message or "").split()).strip()
        if not command:
            return ""

        # Web 与游戏聊天可能同时来消息，串行化避免两条回复互相覆盖
        with self._reply_lock:
            if not self.simulation_mode:
                ensure = getattr(self.monitor, "ensure_window", None)
                if callable(ensure):
                    ensure()
                if not bool(getattr(self.monitor, "is_ready", False)):
                    reply = "游戏还没启动哦，请先按新手引导启动 Terraria 并进入世界。"
                    self._last_reply = reply
                    self.log(f"她：{reply}", "reply")
                    return reply

            # 先记下她正在做什么，再打断；这样回复里能说“我刚才在砍树”
            previous_action = self.monitor.current_action
            with self._lock:
                self._interrupt_counter += 1
            try:
                self.executor.stop()
            except Exception:
                logger.exception("打断当前动作失败")
            self.log(f"收到{source}消息: {command}", "info")

            reply = self._status_reply(command, action_override=previous_action)
            self.monitor.set_action("回应主人")
            if self._armed:
                self._say("user_reply", reply, force=True)
            else:
                # 接管前只在 Web 回复，避免在游戏菜单里误开聊天栏
                self._last_reply = reply
                self.log(f"她：{reply}", "reply")
            self.monitor.set_action("Idle")
            return reply

    @staticmethod
    def _completion_text(action: str, result: SkillResult) -> str:
        if not result.ok:
            return f"这次没成功：{result.message[:80]}"
        if action == "chop_tree":
            return "砍倒啦！这棵树归我们了。"
        if action == "move":
            return "到啦，位置刚刚好。"
        if action == "jump":
            return "跳过来了，稳稳落地。"
        if action == "select_tool":
            return "工具换好了。"
        if action == "stop":
            return "好，我停下了。"
        return "搞定。"

    # ------------------------------------------------------------------
    # 日志 / 状态
    # ------------------------------------------------------------------

    def log(self, message: str, level: str = "info") -> None:
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": str(message),
        }
        with self._lock:
            self._logs.append(entry)
        py_level = {
            "success": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
        }.get(level, logging.INFO)
        logger.log(py_level, message)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def api_key_set(self) -> bool:
        return bool(self.config.get("deepseek_api_key", ""))

    def public_config(self) -> dict[str, Any]:
        return self.config_store.public()

    def state(self) -> dict[str, Any]:
        monitor_state = self.monitor.get_state()
        with self._lock:
            logs = list(self._logs)
            busy = self._busy
            last_intent = copy.deepcopy(self._last_intent)
            last_result = copy.deepcopy(self._last_result)

        return {
            "ok": True,
            "server_time": datetime.now().isoformat(timespec="seconds"),
            **monitor_state,
            "logs": logs[-120:],
            "busy": busy,
            "last_intent": last_intent,
            "last_result": last_result,
            "last_reply": self._last_reply,
            "commentary": {
                "enabled": self.commentary.enabled,
                "cooldown_s": self.commentary.cooldown_s,
            },
            "api_key_set": self.api_key_set,
            "config": self.public_config(),
            "simulation_mode": self.simulation_mode,
            "armed": self._armed,
            "game_ready": bool(monitor_state.get("ready", False)),
            "game_error": str(monitor_state.get("error", "")),
            "window": monitor_state.get("window", {}),
            "coordinate_mode": monitor_state.get("coordinate_mode", ""),
        }

    # ------------------------------------------------------------------
    # 指令执行
    # ------------------------------------------------------------------

    def submit(self, command: str) -> tuple[bool, str, Intent | None]:
        command = (command or "").strip()
        if not command:
            return False, "指令不能为空", None

        local = self.router.match_local(command)
        if not self.api_key_set and local is None:
            return False, "请先在设置中填写 DeepSeek API Key", local

        if not self.simulation_mode:
            ensure = getattr(self.monitor, "ensure_window", None)
            if callable(ensure):
                ensure()
            if not bool(getattr(self.monitor, "is_ready", False)):
                return False, "游戏窗口未就绪：请先启动 Terraria 并进入世界", local

        with self._lock:
            if self._busy:
                return False, "上一条指令还在执行，请稍候", local
            self._busy = True
            self._last_intent = local.to_dict() if local else None
            self._last_result = None
            if not self._armed:
                self._armed = True
                reset_motion = getattr(self.monitor, "reset_motion", None)
                if callable(reset_motion):
                    reset_motion()
                self.log("[状态] 已进入接管状态，Agent 开始接收游戏状态", "success")
            interrupt_at_submit = self._interrupt_counter

        thread = threading.Thread(
            target=self._execute,
            args=(command, local, interrupt_at_submit),
            name="agent-worker",
            daemon=True,
        )
        thread.start()
        return True, "指令已接收", local

    def _capture_failure_screenshot(self, action: str) -> str | None:
        """开发阶段：动作失败时截一张游戏窗口图，存到 logs/failures/。"""
        if not bool(self.config.get("dev_screenshot_on_failure", True)):
            return None
        try:
            import mss
            import mss.tools
            from game_window import client_rect, find_window

            title = str(self.config.get("window_title", "泰拉瑞亚"))
            exe = str(self.config.get("exe_path", ""))
            window = find_window([title, "Terraria"], exe_path=exe)
            hwnd = int(getattr(window, "_hWnd", 0))
            if not hwnd:
                return None
            left, top, width, height = client_rect(hwnd)
            folder = Path("logs") / "failures"
            folder.mkdir(parents=True, exist_ok=True)
            safe = "".join(ch for ch in action if ch.isalnum() or ch in "-_")[:40] or "action"
            path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.png"
            with mss.mss() as sct:
                shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
                mss.tools.to_png(shot.rgb, shot.size, output=str(path))
            return str(path)
        except Exception:
            logger.exception("失败截图失败")
            return None

    def run_action(self, action: str, params: dict[str, Any] | None = None) -> SkillResult:
        """外部插件专用：跳过关键词路由和大模型，直接同步执行一个注册技能。

        桌宠（Coopanion）把每个技能包装成自己的工具，调用 ``POST /api/action``。
        这里不再让本地 LLM 规划，避免“桌宠一个大模型 + 本地一个大模型”互相打架。
        """
        action = str(action or "").strip()
        if not action:
            return SkillResult(False, "no_action", "没有指定动作名")
        if action == "ai_plan":
            return SkillResult(
                False,
                "not_allowed",
                "ai_plan 会再调用一次大模型，插件接口不开放；请让桌宠自己选具体动作",
            )
        skill = self.registry.get(action)
        if skill is None:
            return SkillResult(False, "unknown_action", f"没有注册动作: {action}")

        if not self.simulation_mode:
            ensure = getattr(self.monitor, "ensure_window", None)
            if callable(ensure):
                ensure()
            if not bool(getattr(self.monitor, "is_ready", False)):
                return SkillResult(
                    False,
                    "game_not_ready",
                    "游戏未就绪：请先启动 Terraria 并进入世界",
                )

        with self._lock:
            if self._busy:
                return SkillResult(False, "busy", "上一条指令还在执行，请稍候")
            self._busy = True
            self._last_result = None
            self._last_intent = {
                "action": action,
                "params": dict(params or {}),
                "source": "plugin",
                "raw": "",
                "message": "插件直连",
                "reply": "",
            }
            if not self._armed:
                self._armed = True
                reset_motion = getattr(self.monitor, "reset_motion", None)
                if callable(reset_motion):
                    reset_motion()
                self.log("[状态] 已进入接管状态，Agent 开始接收游戏状态", "success")

        try:
            self.executor.reset_cancel()
            values = dict(params or {})
            repeat = max(1, min(999, int(values.pop("repeat", 1) or 1)))
            self.log(f"[插件] 执行动作: {action} 参数={values}", "info")
            context = SkillContext(
                monitor=self.monitor,
                executor=self.executor,
                config=self.config,
                log=self.log,
            )
            result: SkillResult | None = None
            for index in range(repeat):
                if repeat > 1:
                    self.log(f"[插件] 第 {index + 1}/{repeat} 次", "info")
                result = skill.run(context, **values)
                if not result.ok:
                    break
            if result is None:
                result = SkillResult(False, "error", "没有执行任何动作")
            if not result.ok:
                shot = self._capture_failure_screenshot(action)
                if shot:
                    result.data = dict(result.data or {})
                    result.data["screenshot"] = shot
                    self.log(f"[截图] 失败画面已保存: {shot}", "info")
            with self._lock:
                self._last_result = result.to_dict()
            self.log(
                f"[插件] 动作结束: {result.status} - {result.message}",
                "success" if result.ok else "error",
            )
            return result
        except Exception as exc:
            logger.exception("插件动作执行异常")
            self.log(f"插件动作异常: {exc}", "error")
            return SkillResult(False, "exception", str(exc))
        finally:
            # 无论成败都松开所有键鼠：长按制作/批量刷墙中途出错也不会卡住鼠标。
            try:
                self.executor.release_all()
            except Exception:
                logger.exception("动作结束时释放输入失败")
            self.monitor.set_action("Idle")
            with self._lock:
                self._busy = False

    def _execute(
        self,
        command: str,
        local: Intent | None,
        interrupt_at_submit: int,
    ) -> None:
        result: SkillResult | None = None
        try:
            self.log(f"[思考] 收到指令: {command}", "info")
            intent = local or self.router.route(command)
            with self._lock:
                self._last_intent = intent.to_dict()

            # 新指令开始，清除上一次“用户插话”留下的执行层取消标记。
            self.executor.reset_cancel()
            self.log(
                f"[解析] 动作={intent.action} 来源={intent.source} "
                f"参数={intent.params} 台词={intent.reply!r}",
                "info",
            )

            # 双轨输出：先把她准备做什么说出来，再执行动作。
            # 与完成播报共用 action key，确保同一动作 5 秒内最多一条。
            if intent.reply:
                self._say(f"action:{intent.action}", intent.reply)

            # 如果在说台词/解析期间用户插话，放弃这条旧动作。
            with self._lock:
                interrupted = self._interrupt_counter != interrupt_at_submit
            if interrupted:
                self.log("动作被用户插话打断，已停止执行", "warning")
                return

            if intent.action in {"config_required", "error"}:
                self.log(intent.message or "意图解析失败", "error")
                return

            skill = self.registry.get(intent.action)
            if skill is None:
                self.log(f"没有注册动作: {intent.action}", "error")
                return

            context = SkillContext(
                monitor=self.monitor,
                executor=self.executor,
                config=self.config,
                log=self.log,
            )
            params = dict(intent.params)
            repeat = int(params.pop("repeat", 1) or 1)
            repeat = max(1, min(999, repeat))
            self.log(f"[执行] 开始技能: {intent.action}", "info")
            if repeat > 1:
                self.log(f"[执行] 重复执行 {repeat} 次", "info")
            result = None
            for index in range(repeat):
                if repeat > 1:
                    self.log(f"[执行] 第 {index + 1}/{repeat} 次", "info")
                result = skill.run(context, **params)
                if not result.ok:
                    break
            if result is None:
                result = SkillResult(False, "error", "没有执行任何动作")
            with self._lock:
                self._last_result = result.to_dict()
            self.log(
                f"[执行] 技能结束: {result.status} - {result.message}",
                "success" if result.ok else "error",
            )

            # 每个 Skill 结束后主动汇报；被用户插话打断的旧动作不播报。
            # 同一个动作 5 秒内只播一次。
            if result.status != "cancelled" and not self.executor.is_cancelled:
                completion = self._completion_text(intent.action, result)
                # 同一动作 key：若刚说过出发台词，5 秒内完成播报会被冷却抑制。
                self._say(f"action:{intent.action}", completion)
        except Exception as exc:
            logger.exception("指令执行异常")
            self.log(f"执行异常: {exc}", "error")
            self._say("error", "呜，这次操作出错了，主人稍等一下。")
            with self._lock:
                self._last_result = {
                    "ok": False,
                    "status": "exception",
                    "message": str(exc),
                }
        finally:
            self.monitor.set_action("Idle")
            with self._lock:
                self._busy = False

    def stop(self) -> None:
        with self._lock:
            self._interrupt_counter += 1
        try:
            self.executor.stop()
        except Exception:
            logger.exception("停止执行器失败")
        self.monitor.set_action("Idle")
        self.log("已发送停止指令", "warning")
        if self._armed:
            self._say("manual_stop", "好，我停下了。")
        else:
            self._last_reply = "已停止。"
            self.log("她：已停止。", "reply")

    # ------------------------------------------------------------------
    # 热重载
    # ------------------------------------------------------------------

    def reload_config(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self.config = config or self.config_store.get()
            max_logs = int(self.config["agent"].get("max_log_lines", 300))
            if self._logs.maxlen != max_logs:
                self._logs = deque(self._logs, maxlen=max_logs)

        self.monitor.reconfigure(
            grid_radius=int(self.config["grid_radius"]),
            move_speed=float(self.config["move_speed"]),
        )
        self.router.set_api_key(self.config.get("deepseek_api_key", ""))
        self.router.base_url = str(self.config.get("deepseek_base_url", "")).rstrip("/")
        self.router.model = str(self.config.get("deepseek_model", "deepseek-chat"))
        self.executor.set_window_title(str(self.config.get("window_title", "泰拉瑞亚")))
        self.executor.set_inventory_key(str(self.config.get("inventory_key", "escape")))
        set_monitor_titles = getattr(self.monitor, "set_window_titles", None)
        if callable(set_monitor_titles):
            game_cfg = self.config.get("game", {})
            set_monitor_titles(
                list(
                    game_cfg.get("window_titles")
                    or [self.config.get("window_title", "泰拉瑞亚"), "Terraria"]
                )
            )

        commentary_cfg = self.config.get("commentary", {})
        self.commentary.configure(
            cooldown_s=float(commentary_cfg.get("cooldown_s", 5.0)),
            enabled=bool(commentary_cfg.get("enabled", True)),
        )
        self._low_hp_ratio = float(commentary_cfg.get("low_hp_ratio", 0.30))

        self.log("配置已热重载，无需重启程序", "success")
        return copy.deepcopy(self.config)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._shutdown.set()
        try:
            self.executor.close()
        except Exception:
            logger.exception("关闭执行器失败")
        try:
            closer = getattr(self.monitor, "close", None)
            if callable(closer):
                closer()
        except Exception:
            logger.exception("关闭监控器失败")


# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------


def create_app(config_path: str | Path | None = None) -> Flask:
    config_store = ConfigStore(config_path)
    runtime = AgentRuntime(config_store)

    app = Flask(
        __name__,
        template_folder=str(resource_dir() / "templates"),
        static_folder=str(resource_dir() / "static"),
    )
    app.config["JSON_AS_ASCII"] = False
    try:
        app.json.ensure_ascii = False  # Flask 3.x
    except Exception:
        pass
    app.extensions["runtime"] = runtime
    app.extensions["config_store"] = config_store

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "busy": runtime.busy,
                "simulation_mode": runtime.simulation_mode,
                "armed": runtime._armed,
                "game_ready": bool(getattr(runtime.monitor, "is_ready", False)),
                "version": "1.0.0",
            }
        )

    @app.get("/api/state")
    def api_state():
        return jsonify(runtime.state())

    @app.get("/api/grid")
    def api_grid():
        """独立地形接口：需要单独刷新地图时使用。"""
        state = runtime.state()
        return jsonify(
            {
                "ok": True,
                "grid": state["grid"],
                "grid_center": state["grid_center"],
                "grid_radius": state["grid_radius"],
                "player": state["player"],
                "tool": state["tool"],
                "action": state["action"],
            }
        )

    @app.get("/api/config")
    def api_get_config():
        return jsonify({"ok": True, "config": config_store.public()})

    @app.post("/api/config")
    def api_save_config():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        try:
            new_config = config_store.update(payload)
        except ConfigError as exc:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": str(exc),
                        "fields": exc.fields,
                    }
                ),
                400,
            )
        except OSError as exc:
            logger.exception("写入 config.json 失败")
            return jsonify({"ok": False, "error": f"配置写入失败: {exc}"}), 500

        runtime.reload_config(new_config)
        return jsonify(
            {
                "ok": True,
                "message": "配置已保存并热重载",
                "config": config_store.public(),
            }
        )

    @app.post("/api/command")
    def api_command():
        payload = request.get_json(silent=True) or {}
        command = str(payload.get("command", "")).strip()
        if not command:
            return jsonify({"ok": False, "error": "指令不能为空"}), 400

        # 问话优先：即使正在移动/砍树，也先停下来回复主人。
        if runtime.router.is_question(command):
            reply = runtime.handle_user_message(command, source="web")
            return jsonify(
                {
                    "ok": True,
                    "message": "已回复",
                    "reply": reply,
                    "intent": {
                        "action": "chat",
                        "source": "web",
                        "raw": command,
                        "reply": reply,
                    },
                }
            ), 200

        local = runtime.router.match_local(command)
        if local and local.action == "stop":
            # 停止指令永远优先，即使当前正在执行别的技能
            runtime.stop()
            return (
                jsonify(
                    {
                        "ok": True,
                        "message": "已停止",
                        "intent": local.to_dict(),
                    }
                ),
                200,
            )

        if not runtime.api_key_set and local is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "code": "api_key_required",
                        "error": "请先在设置中填写 DeepSeek API Key",
                    }
                ),
                428,
            )

        ok, message, intent = runtime.submit(command)
        if not ok:
            if "还在执行" in message:
                status = 409
            elif "未就绪" in message or "进入世界" in message:
                status = 503
            else:
                status = 400
            return jsonify({"ok": False, "error": message}), status
        return (
            jsonify(
                {
                    "ok": True,
                    "message": message,
                    "intent": intent.to_dict() if intent else None,
                }
            ),
            202,
        )

    @app.post("/api/action")
    def api_action():
        """桌宠插件直连入口：{"action": "move", "params": {"direction": "right"}}。

        跳过关键词路由和大模型，直接执行注册技能；返回技能结果和最新状态摘要。
        """
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action", "")).strip()
        raw_params = payload.get("params")
        if raw_params is None:
            params: dict[str, Any] = {}
        elif isinstance(raw_params, dict):
            params = dict(raw_params)
        else:
            return jsonify({"ok": False, "error": "params 必须是 JSON 对象"}), 400
        if not action:
            return jsonify({"ok": False, "error": "action 不能为空"}), 400

        result = runtime.run_action(action, params)
        if result.status == "busy":
            status_code = 409
        elif result.status == "game_not_ready":
            status_code = 503
        elif result.status in {"unknown_action", "no_action", "not_allowed"}:
            status_code = 400
        else:
            status_code = 200
        return (
            jsonify(
                {
                    "ok": result.ok,
                    "action": action,
                    "result": result.to_dict(),
                    "busy": runtime.busy,
                }
            ),
            status_code,
        )

    @app.get("/api/items")
    def api_items():
        """本地物品查询（6194 个），供桌宠插件查询物品名与 ID。"""
        query = str(request.args.get("q", "")).strip()
        limit = max(1, min(50, int(request.args.get("limit", 12) or 12)))
        hits = find_items(query, limit) if query else []
        return jsonify({"ok": True, "query": query, "items": hits})

    @app.post("/api/game/launch")
    def api_game_launch():
        ok, message = runtime.launch_game()
        return jsonify({"ok": ok, "message": message}), (200 if ok else 500)

    @app.get("/api/frame")
    def api_frame():
        capture = getattr(runtime.monitor, "capture_jpeg", None)
        if not callable(capture):
            return jsonify({"ok": False, "error": "当前监控器不支持截图"}), 503
        jpeg = capture()
        if not jpeg:
            return jsonify({"ok": False, "error": runtime.monitor.error}), 503
        return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/game-chat")
    def api_game_chat():
        """测试/桥接：模拟玩家在游戏聊天栏里对 Agent 说话。

        真实版本由 GameMonitor 读取 Terraria 聊天行后调用同一入口；
        AgentRuntime 的监听线程会在 0.5 秒内停下当前动作并回复。
        """
        payload = request.get_json(silent=True) or {}
        message = str(payload.get("message", "")).strip()
        if not message:
            return jsonify({"ok": False, "error": "message 不能为空"}), 400
        runtime.monitor.inject_incoming_chat(message)
        return jsonify({"ok": True, "message": "已注入游戏内聊天"}), 202

    @app.post("/api/stop")
    def api_stop():
        runtime.stop()
        return jsonify({"ok": True, "message": "已发送停止指令"})

    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "接口不存在"}), 404
        return render_template("index.html"), 404

    return app


__all__ = [
    "AgentRuntime",
    "ConfigError",
    "ConfigStore",
    "DEFAULT_CONFIG",
    "create_app",
    "data_dir",
    "default_config_path",
    "resource_dir",
]
