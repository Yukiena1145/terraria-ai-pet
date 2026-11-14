"""Terraria 状态层：真实窗口截图 + 假数据开发模式。

两种实现
--------
* ``RealGameMonitor``：默认模式。用 pygetwindow 激活窗口、mss 截图、
  OpenCV 做相机位移/血量/地形颜色识别，并保留内存/像素 provider 接口。
* ``FakeGameMonitor``：仅供离线测试/演示，不会操作真实游戏。

真实模式坐标约定
----------------
* 游戏窗口客户区左上角为截图原点。
* Terraria 摄像机基本以玩家为中心，``screen_center`` 是玩家在屏幕上的锚点。
* ``virtual_x_px`` 是相对初始位置的“视觉世界坐标”，由相邻帧相机位移累计；
  它不是内存里的绝对世界坐标，但足够驱动相对闭环移动。
* 精确绝对坐标/背包可后续实现 ``PlayerStateProvider`` / ``MemoryBridge`` 替换。

假数据模式坐标
--------------
* 世界像素：1 格 = 16 px，x 向右，y 向下。
* 玩家中心初始为 ``(0, 0)``，脚下地面从 y=2 开始。
* 21x21 网格以玩家中心所在格为中心；前端中心格就是玩家。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Protocol, runtime_checkable

from executor import PlayerSnapshot
from game_window import client_rect, find_window
from tile_ids import (
    DIRT_TILE_IDS,
    PLANT_TILE_IDS,
    STONE_TILE_IDS,
    TREE_TILE_IDS,
    WOOD_TILE_IDS,
    tile_name,
)

try:  # 真实模式可选依赖；缺失时仍可运行假数据/单元测试
    import cv2
    import mss
    import numpy as np
    import pygetwindow as gw
    import win32gui
    import win32ui
except Exception:  # pragma: no cover - 仅在缺少依赖时触发
    cv2 = None
    mss = None
    np = None
    gw = None
    win32gui = None
    win32ui = None

logger = logging.getLogger(__name__)

@runtime_checkable
class PlayerStateProvider(Protocol):
    """精确状态接口：可注入内存读取实现，替换视觉近似坐标。"""

    def read_player_state(self) -> PlayerSnapshot:
        ...


class TileGridProvider(Protocol):
    """地形识别接口：可注入 OCR/内存 tile 数组实现。"""

    def get_grid(self) -> list[str]:
        ...


TILE_SIZE_PX = 16.0

# 一维假物理参数，只为让 Web 面板看到平滑移动
MAX_WALK_SPEED_PX_S = 120.0
WALK_ACCEL_PX_S2 = 1600.0
GROUND_FRICTION_PX_S2 = 2200.0
GRAVITY_PX_S2 = 1800.0
JUMP_VELOCITY_PX_S = -360.0

# 地形字符：前端颜色映射与后端工具规则共用
TILE_AIR = "."
TILE_DIRT = "D"
TILE_TREE = "T"
TILE_STONE = "S"
TILE_METEORITE = "M"
TILE_WOOD_BLOCK = "W"
TILE_WALL = "B"
TILE_PLANT = "P"
TILE_ENEMY = "E"

# 树木世界坐标（树干底部贴着 y=1 的地面）
TREE_TRUNKS: dict[int, int] = {
    -8: 1,
    -3: 1,
    4: 1,
    8: 1,
}

SPECIAL_TILES: dict[tuple[int, int], str] = {
    (6, 1): TILE_WOOD_BLOCK,
    (-5, 0): TILE_WALL,
    (-8, 3): TILE_METEORITE,
    (2, 1): TILE_PLANT,
    (5, 0): TILE_ENEMY,
}

# 目标物块需要的镐力。V1 用“陨石需要 100 镐力”演示拒绝逻辑。
TILE_REQUIRED_PICKAXE_POWER: dict[str, int] = {
    TILE_DIRT: 0,
    TILE_STONE: 0,
    TILE_WOOD_BLOCK: 0,
    TILE_METEORITE: 100,
}

TARGET_KIND_BY_TILE: dict[str, str] = {
    TILE_TREE: "tree",
    TILE_DIRT: "block",
    TILE_STONE: "block",
    TILE_METEORITE: "block",
    TILE_WOOD_BLOCK: "block",
    TILE_WALL: "wall",
    TILE_PLANT: "plant",
    TILE_ENEMY: "enemy",
}

# 假背包。slot 1..10 对应键盘 1..0。
DEFAULT_INVENTORY: list[dict] = [
    {"slot": 1, "name": "Copper Axe", "kind": "axe", "power": 5, "damage": 3},
    {"slot": 2, "name": "Copper Pickaxe", "kind": "pickaxe", "power": 35, "damage": 4},
    {"slot": 3, "name": "Iron Pickaxe", "kind": "pickaxe", "power": 40, "damage": 5},
    {"slot": 4, "name": "Wooden Sword", "kind": "weapon", "damage": 7},
    {"slot": 5, "name": "Iron Broadsword", "kind": "weapon", "damage": 12},
    {"slot": 6, "name": "Hammer", "kind": "hammer", "power": 30, "damage": 2},
    {"slot": 7, "name": "Torch", "kind": "misc"},
    {"slot": 8, "name": "Dirt Block", "kind": "block"},
    {"slot": 9, "name": "Healing Potion", "kind": "misc"},
]


class FakeGameMonitor:
    """V1 假数据监控器。

    同时承担三个角色：

    * 状态源：实现 ``read_player_state()``，供 ActionExecutor 闭环读取；
    * 地形源：生成 21x21 字符矩阵，供 Canvas 渲染；
    * 点击靶场：接收模拟鼠标点击，砍掉树/敌人并更新网格。
    """

    def __init__(
        self,
        *,
        grid_radius: int = 10,
        move_speed: float = 1.0,
        log: Callable[[str, str], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._grid_radius = max(3, int(grid_radius))
        self._move_speed = max(0.2, float(move_speed))
        self._log_cb = log

        self._x_px = 0.0
        self._y_px = 0.0
        self._vx_px_s = 0.0
        self._vy_px_s = 0.0
        self._on_ground = True
        self._held: set[str] = set()

        self._hp = 100
        self._max_hp = 100
        self._action = "Idle"
        self._last_tick = time.monotonic()

        self._inventory = [dict(item) for item in DEFAULT_INVENTORY]
        self._current_slot = 1

        self._removed: set[tuple[int, int]] = set()
        self._tree_hp: dict[tuple[int, int], int] = {}
        self._enemy_hp: dict[tuple[int, int], int] = {}
        self._outgoing_chat: list[str] = []
        self._incoming_chat: list[str] = []

    # ------------------------------------------------------------------
    # 生命周期 / 配置
    # ------------------------------------------------------------------

    def set_logger(self, log: Callable[[str, str], None] | None) -> None:
        self._log_cb = log

    def reconfigure(
        self,
        *,
        grid_radius: int | None = None,
        move_speed: float | None = None,
    ) -> None:
        with self._lock:
            if grid_radius is not None:
                self._grid_radius = max(3, int(grid_radius))
            if move_speed is not None:
                self._move_speed = max(0.2, float(move_speed))
            self._emit(
                f"监控参数已热更新: 网格半径={self._grid_radius}, "
                f"速度倍率={self._move_speed:.2f}",
                "info",
            )

    @property
    def grid_radius(self) -> int:
        return self._grid_radius

    @property
    def player_tile(self) -> tuple[int, int]:
        with self._lock:
            return self._player_tile_locked()

    @property
    def current_tool(self) -> dict:
        with self._lock:
            return self._find_slot_locked(self._current_slot)

    @property
    def inventory(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._inventory]

    @property
    def current_action(self) -> str:
        return self._action

    def set_action(self, action: str) -> None:
        with self._lock:
            self._action = str(action)

    def record_outgoing_chat(self, text: str) -> None:
        """模拟器收到“游戏内聊天”发送动作。真实模式由游戏处理。"""
        with self._lock:
            self._outgoing_chat.append(str(text))
            if len(self._outgoing_chat) > 50:
                self._outgoing_chat = self._outgoing_chat[-50:]

    def inject_incoming_chat(self, text: str) -> None:
        """测试用：模拟玩家在游戏聊天栏对 Agent 说话。"""
        with self._lock:
            self._incoming_chat.append(str(text))

    def poll_incoming_chat(self) -> list[str]:
        with self._lock:
            messages = self._incoming_chat[:]
            self._incoming_chat.clear()
            return messages

    @property
    def outgoing_chat(self) -> list[str]:
        with self._lock:
            return list(self._outgoing_chat)

    def _find_slot_locked(self, slot: int) -> dict:
        for item in self._inventory:
            if item["slot"] == slot:
                return dict(item)
        return dict(self._inventory[0])

    # ------------------------------------------------------------------
    # 输入模拟
    # ------------------------------------------------------------------

    def key_down(self, key: str) -> None:
        key = key.lower()
        with self._lock:
            if key in {"a", "d"}:
                self._held.add(key)
            elif key == "space":
                self.jump()

    def key_up(self, key: str) -> None:
        with self._lock:
            self._held.discard(key.lower())

    def release_all(self, keys=()) -> None:
        with self._lock:
            self._held.clear()

    def jump(self) -> None:
        with self._lock:
            if self._on_ground:
                self._on_ground = False
                self._vy_px_s = JUMP_VELOCITY_PX_S
                self._emit("模拟玩家起跳", "info")

    def select_slot(self, slot: int) -> dict:
        with self._lock:
            if slot == 0:
                slot = 10
            item = self._find_slot_locked(slot)
            self._current_slot = slot
            self._emit(f"切换物品: [{slot}] {item['name']}", "info")
            return item

    # ------------------------------------------------------------------
    # 状态读取：ActionExecutor 的 PlayerStateReader 接口
    # ------------------------------------------------------------------

    def read_player_state(self) -> PlayerSnapshot:
        with self._lock:
            self._advance_locked()
            self._last_tick = time.monotonic()
            return PlayerSnapshot(
                center_x_px=self._x_px,
                center_y_px=self._y_px,
                velocity_x_px_s=self._vx_px_s,
                velocity_y_px_s=self._vy_px_s,
                on_ground=self._on_ground,
                timestamp=time.monotonic(),
            )

    def _advance_locked(self) -> None:
        now = time.monotonic()
        dt = min(max(now - self._last_tick, 0.0), 0.10)
        self._last_tick = now
        if dt <= 0:
            return

        # 水平方向：目标速度 + 加速度逼近
        target_vx = 0.0
        if "d" in self._held and "a" not in self._held:
            target_vx = MAX_WALK_SPEED_PX_S * self._move_speed
        elif "a" in self._held and "d" not in self._held:
            target_vx = -MAX_WALK_SPEED_PX_S * self._move_speed

        if target_vx != 0:
            step = WALK_ACCEL_PX_S2 * dt
            if self._vx_px_s < target_vx:
                self._vx_px_s = min(target_vx, self._vx_px_s + step)
            else:
                self._vx_px_s = max(target_vx, self._vx_px_s - step)
        else:
            step = GROUND_FRICTION_PX_S2 * dt
            if self._vx_px_s > 0:
                self._vx_px_s = max(0.0, self._vx_px_s - step)
            else:
                self._vx_px_s = min(0.0, self._vx_px_s + step)

        self._x_px += self._vx_px_s * dt

        # 垂直方向：简单重力 + 落地
        if not self._on_ground:
            self._vy_px_s += GRAVITY_PX_S2 * dt
            self._y_px += self._vy_px_s * dt
            if self._y_px >= 0:
                self._y_px = 0.0
                self._vy_px_s = 0.0
                self._on_ground = True

    def _player_tile_locked(self) -> tuple[int, int]:
        # 与 PlayerSnapshot.body_tiles 保持一致：x 边界归左列（ceil-1），
        # 这样 Canvas 的中心格就是 2x3 逻辑体的左列/中行。
        return (
            int(math.ceil(self._x_px / TILE_SIZE_PX)) - 1,
            int(math.floor(self._y_px / TILE_SIZE_PX)),
        )

    # ------------------------------------------------------------------
    # 地形
    # ------------------------------------------------------------------

    def get_grid(self) -> list[str]:
        """返回 (2r+1) x (2r+1) 字符矩阵，每行一个字符串。"""
        with self._lock:
            cx, cy = self._player_tile_locked()
            r = self._grid_radius
            rows: list[str] = []
            for dy in range(-r, r + 1):
                rows.append(
                    "".join(
                        self.tile_at(cx + dx, cy + dy, _locked=True)
                        for dx in range(-r, r + 1)
                    )
                )
            return rows

    def tile_at(self, world_x: int, world_y: int, *, _locked: bool = False) -> str:
        if _locked:
            return self._tile_at_locked(world_x, world_y)
        with self._lock:
            return self._tile_at_locked(world_x, world_y)

    def _tile_at_locked(self, world_x: int, world_y: int) -> str:
        if (world_x, world_y) in self._removed:
            return TILE_AIR

        # 树：树干 + 树冠
        for tx, base_y in TREE_TRUNKS.items():
            if world_x == tx and base_y - 4 <= world_y <= base_y:
                return TILE_TREE
            if world_y in {base_y - 4, base_y - 3} and abs(world_x - tx) <= 1:
                return TILE_TREE

        special = SPECIAL_TILES.get((world_x, world_y))
        if special:
            return special

        if world_y >= 2:
            return TILE_STONE if world_y >= 5 else TILE_DIRT

        return TILE_AIR

    # ------------------------------------------------------------------
    # 点击 / 采集模拟
    # ------------------------------------------------------------------

    def world_to_screen(self, world_x: float, world_y: float) -> tuple[int, int]:
        """V1 假映射；真实版本由相机偏移 + 缩放计算。"""
        return int(round(world_x)), int(round(world_y))

    def screen_to_world(self, screen_x: int, screen_y: int) -> tuple[float, float]:
        return float(screen_x), float(screen_y)

    def handle_click_at_screen(
        self,
        screen_x: int,
        screen_y: int,
        *,
        button: str = "left",
    ) -> None:
        if button != "left":
            return
        world_x, world_y = self.screen_to_world(screen_x, screen_y)
        tile_x = int(math.floor(world_x / TILE_SIZE_PX))
        tile_y = int(math.floor(world_y / TILE_SIZE_PX))
        with self._lock:
            tile = self._tile_at_locked(tile_x, tile_y)
            if tile == TILE_TREE:
                hp = self._tree_hp.get((tile_x, tile_y), 3)
                hp -= 1
                self._tree_hp[(tile_x, tile_y)] = hp
                if hp <= 0:
                    self._remove_tree_cluster_locked(tile_x, tile_y)
                    self._emit(f"砍倒树木 @ ({tile_x}, {tile_y})", "success")
                else:
                    self._emit(f"砍击树木 @ ({tile_x}, {tile_y})，剩余 {hp}", "info")
            elif tile == TILE_ENEMY:
                hp = self._enemy_hp.get((tile_x, tile_y), 30)
                hp -= 10
                self._enemy_hp[(tile_x, tile_y)] = hp
                if hp <= 0:
                    self._removed.add((tile_x, tile_y))
                    self._emit(f"击败敌人 @ ({tile_x}, {tile_y})", "success")
                else:
                    self._emit(f"攻击敌人 @ ({tile_x}, {tile_y})，剩余 {hp}", "info")

    def _remove_tree_cluster_locked(self, start_x: int, start_y: int) -> None:
        """把一棵树的树干/树冠整片删除，避免只掉一个方块。"""
        stack = [(start_x, start_y)]
        seen: set[tuple[int, int]] = set()
        while stack:
            x, y = stack.pop()
            if (x, y) in seen or (x, y) in self._removed:
                continue
            if self._tile_at_locked(x, y) != TILE_TREE:
                continue
            seen.add((x, y))
            self._removed.add((x, y))
            stack.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])

    # ------------------------------------------------------------------
    # 状态汇总 / 事件
    # ------------------------------------------------------------------

    def _apply_real_inventory(self, data: dict) -> None:
        items = data.get("inventory")
        if not isinstance(items, list):
            return
        converted: list[dict] = []
        for item in items[:50]:
            if not isinstance(item, dict):
                continue
            slot = int(item.get("slot", 0) or 0)
            if not 1 <= slot <= 50:
                continue
            if int(item.get("type", 0) or 0) == 0 and int(item.get("stack", 0) or 0) == 0:
                continue
            axe = int(item.get("axe", 0) or 0)
            pick = int(item.get("pickaxe", 0) or 0)
            hammer = int(item.get("hammer", 0) or 0)
            if axe > 0:
                kind, power = "axe", axe
            elif pick > 0:
                kind, power = "pickaxe", pick
            elif hammer > 0:
                kind, power = "hammer", hammer
            else:
                kind, power = "weapon", 0
            converted.append({
                "slot": slot,
                "name": str(item.get("name") or f"Item#{int(item.get('type', 0) or 0)}"),
                "kind": kind,
                "power": power,
                "damage": int(item.get("damage", 0) or 0),
                "stack": int(item.get("stack", 1) or 1),
                "type": int(item.get("type", 0) or 0),
                "create_tile": int(item.get("create_tile", 0) or 0),
                "create_wall": int(item.get("create_wall", 0) or 0),
                "hotbar": slot <= 10,
            })
        if converted:
            self._inventory = converted
        selected = int(data.get("selected_slot", 0) or 0)
        if 1 <= selected <= 10:
            self._current_slot = selected
        if "inventory_open" in data:
            self._inventory_open = bool(data.get("inventory_open"))

    def get_state(self) -> dict:
        with self._lock:
            self._advance_locked()
            self._last_tick = time.monotonic()
            cx, cy = self._player_tile_locked()
            item = self._find_slot_locked(self._current_slot)
            return {
                "player": {
                    "x_tiles": round(self._x_px / TILE_SIZE_PX, 3),
                    "y_tiles": round(self._y_px / TILE_SIZE_PX, 3),
                    "tile_x": cx,
                    "tile_y": cy,
                    "vx": round(self._vx_px_s, 2),
                    "vy": round(self._vy_px_s, 2),
                    "on_ground": self._on_ground,
                    "hp": self._hp,
                    "max_hp": self._max_hp,
                },
                "grid": self.get_grid(),
                "grid_center": [self._grid_radius, self._grid_radius],
                "grid_radius": self._grid_radius,
                "tool": item,
                "inventory": [dict(i) for i in self._inventory],
                "action": self._action,
                "last_chat": self._outgoing_chat[-1] if self._outgoing_chat else "",
            }

    def _emit(self, message: str, level: str = "info") -> None:
        if self._log_cb:
            try:
                self._log_cb(message, level)
            except Exception:
                pass


class RealGameMonitor:
    """真实 Terraria 窗口监控器（mss 截图 + 视觉近似状态）。

    重要边界
    --------
    这不是“读取游戏内存”的实现。它用窗口截图和颜色/位移识别提供：

    * 窗口客户区截图；
    * 相机水平/垂直位移累计出的“视觉世界坐标”；
    * 21x21 颜色分类网格；
    * 红心血量近似；
    * 可配置快捷栏工具。

    若需要绝对世界坐标、精确背包、生命/魔力/ buff，请实现
    ``PlayerStateProvider`` 并在 ``__init__`` 中注入；技能层不需要改。
    """

    def __init__(
        self,
        *,
        grid_radius: int = 10,
        move_speed: float = 1.0,
        window_titles: list[str] | None = None,
        exe_path: str = "",
        capture_fps: float = 12.0,
        tile_px: float = 16.0,
        screen_scale: float = 1.0,
        player_offset: tuple[float, float] = (0.5, 0.5),
        hotbar: list[dict] | None = None,
        max_hp: int = 100,
        log: Callable[[str, str], None] | None = None,
        state_provider=None,
    ) -> None:
        if any(mod is None for mod in (np, cv2, mss, gw, win32gui, win32ui)):
            raise RuntimeError(
                "真实模式需要 numpy / opencv-python / mss / pygetwindow / pywin32；"
                "请先执行 pip install -r requirements.txt"
            )

        self._lock = threading.RLock()
        self._grid_radius = max(3, int(grid_radius))
        self._move_speed = max(0.2, float(move_speed))
        self._window_titles = list(
            window_titles or ["Terraria", "泰拉瑞亚", "Terraria: "]
        )
        self._exe_path = str(exe_path or "")
        self._capture_fps = max(1.0, float(capture_fps))
        self._tile_px = max(8.0, float(tile_px))
        self._scale = max(0.1, float(screen_scale))
        self._player_offset = (
            min(0.9, max(0.1, float(player_offset[0]))),
            min(0.9, max(0.1, float(player_offset[1]))),
        )
        self._max_hp = max(1, int(max_hp))
        self._hp = self._max_hp
        self._log_cb = log
        self._state_provider = state_provider

        self._inventory = [dict(item) for item in (hotbar or DEFAULT_INVENTORY)]
        self._current_slot = int(self._inventory[0].get("slot", 1)) if self._inventory else 1
        self._action = "Idle"

        self._window = None
        self._hwnd = 0
        self._window_info: dict = {}
        self._ready = False
        self._error = "尚未找到 Terraria 窗口"
        self._last_error_logged = ""

        self._sct = mss.MSS()
        self._last_frame = None
        self._last_capture_at = 0.0
        self._last_gray = None
        self._last_motion_at = time.monotonic()
        self._frame_w = 0
        self._frame_h = 0

        # 视觉世界坐标：以接管时刻为 0，靠相邻帧相机位移累计。
        self._virtual_x_px = 0.0
        self._virtual_y_px = 0.0
        self._velocity_x_px_s = 0.0
        self._velocity_y_px_s = 0.0

        self._outgoing_chat: list[str] = []
        self._incoming_chat: list[str] = []

    # ------------------------------------------------------------------
    # 窗口
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def error(self) -> str:
        return self._error

    @property
    def window_info(self) -> dict:
        return dict(self._window_info)

    @property
    def current_action(self) -> str:
        return self._action

    @property
    def current_tool(self) -> dict:
        with self._lock:
            return self._find_slot_locked(self._current_slot)

    @property
    def inventory(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._inventory]

    @property
    def player_tile(self) -> tuple[int, int]:
        with self._lock:
            return (
                int(math.floor(self._virtual_x_px / TILE_SIZE_PX)),
                int(math.floor(self._virtual_y_px / TILE_SIZE_PX)),
            )

    def set_action(self, action: str) -> None:
        with self._lock:
            self._action = str(action)

    def reconfigure(
        self,
        *,
        grid_radius: int | None = None,
        move_speed: float | None = None,
    ) -> None:
        with self._lock:
            if grid_radius is not None:
                self._grid_radius = max(3, int(grid_radius))
            if move_speed is not None:
                self._move_speed = max(0.2, float(move_speed))

    def set_window_titles(self, titles: list[str]) -> None:
        with self._lock:
            self._window_titles = [str(t).strip() for t in titles if str(t).strip()]
            self._window = None
            self._hwnd = 0
            self._ready = False

    def reset_motion(self) -> None:
        """接管新任务时把视觉坐标归零，避免菜单动画污染位移。"""
        with self._lock:
            self._virtual_x_px = 0.0
            self._virtual_y_px = 0.0
            self._velocity_x_px_s = 0.0
            self._velocity_y_px_s = 0.0
            self._last_gray = None

    def ensure_window(self) -> bool:
        with self._lock:
            if self._window is not None and self._hwnd:
                try:
                    if win32gui.IsWindow(self._hwnd):
                        info = self._read_window_info_locked()
                        if (
                            getattr(self._window, "isMinimized", False)
                            or int(info.get("left", 0)) <= -30000
                            or int(info.get("width", 0)) < 100
                        ):
                            try:
                                self._window.restore()
                            except Exception:
                                pass
                            try:
                                win32gui.ShowWindow(self._hwnd, 9)
                            except Exception:
                                pass
                            time.sleep(0.25)
                            info = self._read_window_info_locked()
                        self._window_info = info
                        return True
                except Exception:
                    pass

            self._window = None
            self._hwnd = 0
            window = find_window(self._window_titles, exe_path=self._exe_path)
            if window is not None:
                hwnd = int(getattr(window, "_hWnd", 0) or 0)
                if hwnd:
                    self._window = window
                    self._hwnd = hwnd
                    try:
                        if getattr(window, "isMinimized", False):
                            window.restore()
                    except Exception:
                        try:
                            win32gui.ShowWindow(hwnd, 9)
                        except Exception:
                            pass
                    info = self._read_window_info_locked()
                    if int(info.get("width", 0)) <= 1 or int(info.get("height", 0)) <= 1:
                        try:
                            win32gui.ShowWindow(hwnd, 9)
                            time.sleep(0.15)
                        except Exception:
                            pass
                        info = self._read_window_info_locked()
                    self._window_info = info
                    self._ready = True
                    self._error = ""
                    self._emit(
                        f"[真实] 已连接游戏窗口: {window.title} "
                        f"({self._window_info.get('width')}x{self._window_info.get('height')})",
                        "success",
                    )
                    return True

            self._ready = False
            self._error = (
                "未找到 Terraria 窗口。请先启动游戏并进入世界；"
                f"当前查找标题: {self._window_titles}"
            )
            self._emit_once(self._error)
            return False

    def activate(self) -> bool:
        """强制激活游戏窗口（pygetwindow 优先，AttachThreadInput 兜底）。"""
        if not self.ensure_window():
            return False
        with self._lock:
            window = self._window
            hwnd = self._hwnd

        if window is not None:
            try:
                if getattr(window, "isMinimized", False):
                    window.restore()
                try:
                    window.activate()
                except Exception:
                    # pygetwindow 在 SetForegroundWindow 返回 0 时会误报异常；
                    # 以 isActive 的实际结果为准。
                    if getattr(window, "isActive", False):
                        return True
                if getattr(window, "isActive", False):
                    return True
            except Exception:
                logger.debug("pygetwindow 激活失败，尝试 win32 兜底", exc_info=True)

        return force_foreground(hwnd)

    def _read_window_info_locked(self) -> dict:
        left, top, right, bottom = win32gui.GetClientRect(self._hwnd)
        screen_x, screen_y = win32gui.ClientToScreen(self._hwnd, (0, 0))
        width = max(1, right - left)
        height = max(1, bottom - top)
        return {
            "hwnd": self._hwnd,
            "title": str(getattr(self._window, "title", "")),
            "left": screen_x,
            "top": screen_y,
            "width": width,
            "height": height,
            "capture_fps": self._capture_fps,
        }

    # ------------------------------------------------------------------
    # 截图 / 视觉状态
    # ------------------------------------------------------------------

    def capture_frame(self, *, force: bool = False):
        """返回 BGR numpy 帧；没有窗口/依赖时返回 None。"""
        with self._lock:
            if not self.ensure_window():
                return None

            now = time.monotonic()
            if (
                not force
                and self._last_frame is not None
                and now - self._last_capture_at < 1.0 / self._capture_fps
            ):
                return self._last_frame

            try:
                info = self._read_window_info_locked()
                self._window_info = info
                if int(win32gui.GetForegroundWindow() or 0) == int(self._hwnd):
                    region = {
                        "left": int(info["left"]),
                        "top": int(info["top"]),
                        "width": int(info["width"]),
                        "height": int(info["height"]),
                    }
                    raw = np.asarray(self._sct.grab(region))
                    frame = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
                else:
                    # 游戏在后台时 mss 只能截到遮挡它的窗口；用 PrintWindow
                    # 直接抓取窗口内容，避免轮询状态时反复抢焦点。
                    frame = self._capture_printwindow_locked(info)
                self._process_motion_locked(frame, now)
                self._last_frame = frame
                self._last_capture_at = now
                self._frame_h, self._frame_w = frame.shape[:2]
                self._ready = True
                self._error = ""
                return frame
            except Exception as exc:
                self._ready = False
                self._error = f"截图失败: {exc}"
                self._emit_once(self._error, "error")
                return None

    def _capture_printwindow_locked(self, info: dict):
        """后台窗口抓取：PrintWindow(PW_CLIENTONLY|PW_RENDERFULLCONTENT)。"""
        hwnd = int(info["hwnd"])
        width = int(info["width"])
        height = int(info["height"])
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        try:
            import ctypes

            ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 3)
            if not ok:
                raise RuntimeError("PrintWindow 返回 0")
            bits = bitmap.GetBitmapBits(True)
            image = np.frombuffer(bits, dtype=np.uint8).reshape(height, width, 4)
            return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        finally:
            try:
                win32gui.DeleteObject(bitmap.GetHandle())
            except Exception:
                pass
            try:
                save_dc.DeleteDC()
                mfc_dc.DeleteDC()
            finally:
                win32gui.ReleaseDC(hwnd, hwnd_dc)

    def _process_motion_locked(self, frame, now: float) -> None:
        """用相位相关估计背景位移，转成视觉世界坐标。"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        previous = self._last_gray
        self._last_gray = gray
        if previous is None or previous.shape != gray.shape:
            self._last_motion_at = now
            return

        h, w = gray.shape[:2]
        # 裁掉 HUD/边缘，减少血条、快捷栏、鼠标提示对位移估计的干扰
        y0, y1 = int(h * 0.08), int(h * 0.92)
        x0, x1 = int(w * 0.05), int(w * 0.95)
        if y1 - y0 < 32 or x1 - x0 < 32:
            return

        a = previous[y0:y1, x0:x1].astype(np.float32)
        b = gray[y0:y1, x0:x1].astype(np.float32)
        try:
            (dx, dy), response = cv2.phaseCorrelate(a, b)
        except Exception:
            return
        if response < 0.15 or abs(dx) > w * 0.2 or abs(dy) > h * 0.2:
            self._last_motion_at = now
            return

        dt = max(1e-3, now - self._last_motion_at)
        self._last_motion_at = now
        world_dx = -dx / max(1e-6, self._scale)
        world_dy = -dy / max(1e-6, self._scale)
        self._virtual_x_px += world_dx
        self._virtual_y_px += world_dy
        self._velocity_x_px_s = world_dx / dt
        self._velocity_y_px_s = world_dy / dt

    def read_player_state(self) -> PlayerSnapshot:
        if self._state_provider is not None:
            return self._state_provider.read_player_state()
        frame = self.capture_frame()
        if frame is None:
            raise RuntimeError(self._error)
        self._hp = self._estimate_hp(frame)
        with self._lock:
            return PlayerSnapshot(
                center_x_px=self._virtual_x_px,
                center_y_px=self._virtual_y_px,
                velocity_x_px_s=self._velocity_x_px_s,
                velocity_y_px_s=self._velocity_y_px_s,
                on_ground=True,  # 纯截图模式无法可靠判断落地，跳跃技能需谨慎
                timestamp=time.monotonic(),
            )

    def get_grid(self) -> list[str]:
        frame = self.capture_frame()
        if frame is None:
            return ["." * (self._grid_radius * 2 + 1) for _ in range(self._grid_radius * 2 + 1)]
        return self._build_grid(frame)

    def _build_grid(self, frame) -> list[str]:
        h, w = frame.shape[:2]
        cx = int(w * self._player_offset[0])
        cy = int(h * self._player_offset[1])
        step = self._tile_px
        r = self._grid_radius
        rows: list[str] = []
        for gy in range(-r, r + 1):
            chars: list[str] = []
            for gx in range(-r, r + 1):
                px = int(cx + gx * step)
                py = int(cy + gy * step)
                if not (0 <= px < w and 0 <= py < h):
                    chars.append(".")
                    continue
                half = max(2, int(step * 0.28))
                patch = frame[
                    max(0, py - half) : min(h, py + half + 1),
                    max(0, px - half) : min(w, px + half + 1),
                ]
                chars.append(self._classify_patch(patch))
            rows.append("".join(chars))
        return rows

    def _classify_patch(self, patch) -> str:
        if patch is None or getattr(patch, "size", 0) == 0:
            return "."
        b, g, r = (float(v) for v in np.mean(patch, axis=(0, 1)))
        hsv = cv2.cvtColor(
            np.uint8([[[int(b), int(g), int(r)]]]), cv2.COLOR_BGR2HSV
        )[0, 0]
        hue, sat, val = int(hsv[0]), int(hsv[1]), int(hsv[2])

        if val < 25:
            return "."
        # 天空/背景：蓝色或低饱和高亮
        if 90 <= hue <= 140 and sat >= 40:
            return "."
        if sat >= 45 and 35 <= hue <= 95:
            return "T"  # 树叶/草
        if sat >= 35 and 5 <= hue <= 30 and val >= 30:
            return "D"  # 泥土/木材
        if sat < 32 and 35 <= val <= 215:
            return "S"  # 石头/灰色墙
        return "."

    def _estimate_hp(self, frame) -> int:
        """红心数量近似；识别不到时保留上一次值。"""
        try:
            h, w = frame.shape[:2]
            x0, x1 = int(w * 0.70), int(w * 0.99)
            y0, y1 = int(h * 0.02), int(h * 0.14)
            roi = frame[y0:y1, x0:x1]
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            m1 = cv2.inRange(hsv, (0, 120, 120), (10, 255, 255))
            m2 = cv2.inRange(hsv, (170, 120, 120), (180, 255, 255))
            mask = cv2.bitwise_or(m1, m2)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            hearts = sum(1 for c in contours if cv2.contourArea(c) >= 12)
            if hearts > 0:
                return min(self._max_hp, hearts * 20)
        except Exception:
            pass
        return self._hp

    def tile_at(self, world_x: int, world_y: int) -> str:
        frame = self.capture_frame()
        if frame is None:
            return "."
        screen_x, screen_y = self._world_to_frame(
            (world_x + 0.5) * TILE_SIZE_PX,
            (world_y + 0.5) * TILE_SIZE_PX,
        )
        h, w = frame.shape[:2]
        if not (0 <= screen_x < w and 0 <= screen_y < h):
            return "."
        half = max(2, int(self._tile_px * 0.28))
        patch = frame[
            max(0, screen_y - half) : min(h, screen_y + half + 1),
            max(0, screen_x - half) : min(w, screen_x + half + 1),
        ]
        return self._classify_patch(patch)

    # ------------------------------------------------------------------
    # 坐标映射
    # ------------------------------------------------------------------

    def _world_to_frame(self, world_x: float, world_y: float) -> tuple[int, int]:
        """世界像素 -> 游戏客户区内的像素坐标（不含窗口偏移）。"""
        with self._lock:
            w = self._frame_w or self._window_info.get("width", 800)
            h = self._frame_h or self._window_info.get("height", 600)
            cx = w * self._player_offset[0]
            cy = h * self._player_offset[1]
            sx = cx + (float(world_x) - self._virtual_x_px) * self._scale
            sy = cy + (float(world_y) - self._virtual_y_px) * self._scale
            return int(round(sx)), int(round(sy))

    def world_to_screen(self, world_x: float, world_y: float) -> tuple[int, int]:
        """世界像素 -> 屏幕绝对像素（pydirectinput 需要屏幕坐标）。"""
        fx, fy = self._world_to_frame(world_x, world_y)
        with self._lock:
            return (
                int(self._window_info.get("left", 0)) + fx,
                int(self._window_info.get("top", 0)) + fy,
            )

    def screen_to_world(self, screen_x: int, screen_y: int) -> tuple[float, float]:
        with self._lock:
            w = self._frame_w or self._window_info.get("width", 800)
            h = self._frame_h or self._window_info.get("height", 600)
            cx = w * self._player_offset[0]
            cy = h * self._player_offset[1]
            left = int(self._window_info.get("left", 0))
            top = int(self._window_info.get("top", 0))
            world_x = self._virtual_x_px + (float(screen_x) - left - cx) / self._scale
            world_y = self._virtual_y_px + (float(screen_y) - top - cy) / self._scale
            return world_x, world_y

    # ------------------------------------------------------------------
    # 快捷栏 / 状态
    # ------------------------------------------------------------------

    def _find_slot_locked(self, slot: int) -> dict:
        for item in self._inventory:
            if int(item.get("slot", -1)) == int(slot):
                return dict(item)
        return dict(self._inventory[0]) if self._inventory else {
            "slot": 1,
            "name": "Unknown",
            "kind": "misc",
        }

    def select_slot(self, slot: int) -> dict:
        with self._lock:
            if slot == 0:
                slot = 10
            item = self._find_slot_locked(slot)
            self._current_slot = slot
            self._emit(f"[执行] 切换快捷栏 [{slot}] {item.get('name', 'Unknown')}", "info")
            return item

    def get_state(self) -> dict:
        frame = self.capture_frame()
        if frame is None:
            size = self._grid_radius * 2 + 1
            return {
                "player": {
                    "x_tiles": 0.0,
                    "y_tiles": 0.0,
                    "tile_x": 0,
                    "tile_y": 0,
                    "vx": 0.0,
                    "vy": 0.0,
                    "on_ground": True,
                    "hp": 0,
                    "max_hp": self._max_hp,
                },
                "grid": ["." * size for _ in range(size)],
                "grid_names": [["空气" for _ in range(size)] for _ in range(size)],
                "grid_center": [self._grid_radius, self._grid_radius],
                "grid_radius": self._grid_radius,
                "tool": self.current_tool,
                "inventory": self.inventory,
                "action": self._action,
                "last_chat": self._outgoing_chat[-1] if self._outgoing_chat else "",
                "ready": False,
                "error": self._error,
                "window": self._window_info,
                "coordinate_mode": "vision_virtual",
            }

        self._hp = self._estimate_hp(frame)
        with self._lock:
            x_tiles = self._virtual_x_px / TILE_SIZE_PX
            y_tiles = self._virtual_y_px / TILE_SIZE_PX
            return {
                "player": {
                    "x_tiles": round(x_tiles, 2),
                    "y_tiles": round(y_tiles, 2),
                    "tile_x": int(math.floor(x_tiles)),
                    "tile_y": int(math.floor(y_tiles)),
                    "vx": round(self._velocity_x_px_s, 2),
                    "vy": round(self._velocity_y_px_s, 2),
                    "on_ground": True,
                    "hp": self._hp,
                    "max_hp": self._max_hp,
                },
                "grid": self._build_grid(frame),
                "grid_center": [self._grid_radius, self._grid_radius],
                "grid_radius": self._grid_radius,
                "tool": self.current_tool,
                "inventory": self.inventory,
                "action": self._action,
                "last_chat": self._outgoing_chat[-1] if self._outgoing_chat else "",
                "ready": True,
                "error": "",
                "window": self._window_info,
                "coordinate_mode": "vision_virtual",
            }

    def capture_jpeg(self) -> bytes | None:
        frame = self.capture_frame()
        if frame is None or cv2 is None:
            return None
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return encoded.tobytes() if ok else None

    # ------------------------------------------------------------------
    # 聊天 / 日志
    # ------------------------------------------------------------------

    def record_outgoing_chat(self, text: str) -> None:
        with self._lock:
            self._outgoing_chat.append(str(text))
            self._outgoing_chat = self._outgoing_chat[-50:]

    def inject_incoming_chat(self, text: str) -> None:
        with self._lock:
            self._incoming_chat.append(str(text))

    def poll_incoming_chat(self) -> list[str]:
        with self._lock:
            messages = self._incoming_chat[:]
            self._incoming_chat.clear()
            return messages

    def _emit(self, message: str, level: str = "info") -> None:
        if self._log_cb:
            try:
                self._log_cb(message, level)
            except Exception:
                pass

    def _emit_once(self, message: str, level: str = "warning") -> None:
        if message == self._last_error_logged:
            return
        self._last_error_logged = message
        self._emit(message, level)


class OfflineGameMonitor:
    """真实依赖缺失/窗口不可用时的只读占位，不使用假地形。"""

    def __init__(self, grid_radius: int = 10, error: str = "", log=None) -> None:
        self._grid_radius = max(3, int(grid_radius))
        self._error = error or "真实游戏监控器不可用"
        self._log = log
        self._action = "Idle"
        self._inventory = [dict(item) for item in DEFAULT_INVENTORY]
        self._current_slot = 1
        self._outgoing_chat: list[str] = []
        self._incoming_chat: list[str] = []

    @property
    def is_ready(self) -> bool:
        return False

    @property
    def error(self) -> str:
        return self._error

    @property
    def window_info(self) -> dict:
        return {}

    @property
    def current_action(self) -> str:
        return self._action

    @property
    def current_tool(self) -> dict:
        return dict(self._inventory[0])

    @property
    def inventory(self) -> list[dict]:
        return [dict(item) for item in self._inventory]

    @property
    def player_tile(self) -> tuple[int, int]:
        return 0, 0

    def set_action(self, action: str) -> None:
        self._action = str(action)

    def ensure_window(self) -> bool:
        return False

    def activate(self) -> bool:
        return False

    def read_player_state(self):
        raise RuntimeError(self._error)

    def get_grid(self) -> list[str]:
        size = self._grid_radius * 2 + 1
        return ["." * size for _ in range(size)]

    def get_state(self) -> dict:
        size = self._grid_radius * 2 + 1
        return {
            "player": {
                "x_tiles": 0.0,
                "y_tiles": 0.0,
                "tile_x": 0,
                "tile_y": 0,
                "vx": 0.0,
                "vy": 0.0,
                "on_ground": True,
                "hp": 0,
                "max_hp": 100,
            },
            "grid": ["." * size for _ in range(size)],
            "grid_center": [self._grid_radius, self._grid_radius],
            "grid_radius": self._grid_radius,
            "tool": self.current_tool,
            "inventory": self.inventory,
            "enemies": self.enemies,
            "action": self._action,
            "last_chat": self._outgoing_chat[-1] if self._outgoing_chat else "",
            "ready": False,
            "error": self._error,
            "window": {},
            "coordinate_mode": "offline",
        }

    def world_to_screen(self, world_x: float, world_y: float) -> tuple[int, int]:
        return int(world_x), int(world_y)

    def screen_to_world(self, screen_x: int, screen_y: int) -> tuple[float, float]:
        return float(screen_x), float(screen_y)

    def tile_at(self, world_x: int, world_y: int) -> str:
        return "."

    def select_slot(self, slot: int) -> dict:
        self._current_slot = int(slot)
        return self.current_tool

    def inject_incoming_chat(self, text: str) -> None:
        self._incoming_chat.append(str(text))

    def poll_incoming_chat(self) -> list[str]:
        messages = self._incoming_chat[:]
        self._incoming_chat.clear()
        return messages

    def capture_jpeg(self) -> bytes | None:
        return None



class MemoryGameMonitor:
    """ClrMD 精确坐标监控器。

    * 玩家 position / velocity / HP：来自 Terraria.Main.player[0]；
    * 不使用截图坐标，也不抢焦点；
    * 地形网格暂时返回空；未来可注入 tile 内存读取器或保留 vision 兼容。

    如果传入 ``vision``，只使用它的 21x21 地形/截图，不用它的坐标。
    """

    def __init__(
        self,
        reader,
        *,
        vision=None,
        exe_path: str = "",
        window_titles: list[str] | None = None,
        grid_radius: int = 10,
        position_offset: tuple[float, float] = (10.0, 21.0),
        hotbar: list[dict] | None = None,
        max_hp: int = 100,
        log=None,
    ) -> None:
        self.reader = reader
        self.vision = vision
        self._exe_path = str(exe_path or "")
        self._window_titles = list(window_titles or ["Terraria", "泰拉瑞亚"])
        self._grid_radius = max(3, int(grid_radius))
        self._offset_x, self._offset_y = position_offset
        self._max_hp = max(1, int(max_hp))
        self._ticks_per_second = 60.0
        self._log = log
        self._action = "Idle"
        self._last_state_at = 0.0
        self._window = None
        self._vision_cache = None
        self._inventory = [dict(item) for item in (hotbar or DEFAULT_INVENTORY)]
        self._ui_scale = 1.0
        self._enemies: list[dict] = []
        self._inventory_open = False
        self._current_slot = int(self._inventory[0].get("slot", 1)) if self._inventory else 1
        self._outgoing: list[str] = []
        self._incoming: list[str] = []

    # ------------------------------------------------------------------
    # 窗口 / 状态
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        if not self.reader.is_ready:
            starter = getattr(self.reader, "ensure_started", None)
            if callable(starter):
                starter()
        return bool(self.reader.is_ready and self.ensure_window() and self._in_world())

    @property
    def error(self) -> str:
        if not self.reader.is_ready:
            return self.reader.error or "ClrMD 尚未读到玩家数据"
        if not self.ensure_window():
            return "未找到 Terraria 窗口"
        if not self._in_world():
            return "ClrMD 已连接，但玩家还未进入世界（position=0,0）"
        return ""

    def _in_world(self) -> bool:
        try:
            starter = getattr(self.reader, "ensure_started", None)
            if callable(starter) and not self.reader.is_ready:
                starter()
            data = self.reader.latest(max_age_s=1.0)
            return abs(float(data.get("x", 0.0))) > 0.01 or abs(float(data.get("y", 0.0))) > 0.01
        except Exception:
            return False

    @property
    def window_info(self) -> dict:
        if not self.ensure_window():
            return {}
        w = self._window
        try:
            return {
                "hwnd": int(getattr(w, "_hWnd", 0) or 0),
                "title": str(getattr(w, "title", "")),
                "left": int(getattr(w, "left", 0)),
                "top": int(getattr(w, "top", 0)),
                "width": int(getattr(w, "width", 800)),
                "height": int(getattr(w, "height", 600)),
            }
        except Exception:
            return {}

    @property
    def current_action(self) -> str:
        return self._action

    @property
    def current_tool(self) -> dict:
        for item in self._inventory:
            if int(item.get("slot", -1)) == self._current_slot:
                return dict(item)
        return dict(self._inventory[0]) if self._inventory else {
            "slot": 1,
            "name": "Unknown",
            "kind": "misc",
        }

    @property
    def enemies(self) -> list[dict]:
        return [dict(e) for e in self._enemies]

    @property
    def ui_scale(self) -> float:
        return float(getattr(self, "_ui_scale", 1.0) or 1.0)

    @property
    def inventory_open(self) -> bool:
        return bool(getattr(self, "_inventory_open", False))

    @property
    def inventory(self) -> list[dict]:
        return [dict(item) for item in self._inventory]

    @property
    def player_tile(self) -> tuple[int, int]:
        data = self.reader.latest(max_age_s=2.0)
        x = float(data.get("x", 0.0)) + self._offset_x
        y = float(data.get("y", 0.0)) + self._offset_y
        return int(math.floor(x / TILE_SIZE_PX)), int(math.floor(y / TILE_SIZE_PX))

    def ensure_window(self) -> bool:
        if self._window is not None:
            try:
                import win32gui

                if win32gui.IsWindow(int(getattr(self._window, "_hWnd", 0) or 0)):
                    return True
            except Exception:
                pass
        self._window = find_window(self._window_titles, exe_path=self._exe_path)
        return self._window is not None

    def activate(self) -> bool:
        return self.ensure_window()

    def set_window_titles(self, titles: list[str]) -> None:
        self._window_titles = [str(t).strip() for t in titles if str(t).strip()]
        self._window = None
        if self.vision is not None:
            setter = getattr(self.vision, "set_window_titles", None)
            if callable(setter):
                setter(titles)

    def reconfigure(self, **kwargs) -> None:
        if "grid_radius" in kwargs:
            self._grid_radius = max(3, int(kwargs["grid_radius"]))
        if self.vision is not None:
            self.vision.reconfigure(**kwargs)

    def set_action(self, action: str) -> None:
        self._action = str(action)
        if self.vision is not None:
            self.vision.set_action(action)

    def select_slot(self, slot: int) -> dict:
        self._current_slot = int(slot)
        if self.vision is not None:
            try:
                self.vision.select_slot(slot)
            except Exception:
                pass
        return self.current_tool

    # ------------------------------------------------------------------
    # 地形 / 截图（当前为空，未来接内存 tile）
    # ------------------------------------------------------------------

    def _vision_foreground(self) -> bool:
        if self.vision is None:
            return False
        try:
            import win32gui
            if not self.ensure_window():
                return False
            return int(win32gui.GetForegroundWindow() or 0) == int(getattr(self._window, "_hWnd", 0) or 0)
        except Exception:
            return False

    def get_grid(self) -> list[str]:
        size = self._grid_radius * 2 + 1
        if self.vision is not None:
            try:
                vg = self.vision.get_grid()
                if vg and any(ch in "TDSPW" for row in vg for ch in row):
                    self._vision_cache = vg
                    return vg
            except Exception:
                pass
            if self._vision_cache is not None:
                return self._vision_cache
        try:
            data = self.reader.latest(max_age_s=1.0)
            tiles = data.get("tiles")
        except Exception:
            return ["." * size for _ in range(size)]
        if not isinstance(tiles, list) or len(tiles) != size:
            return ["." * size for _ in range(size)]
        rows: list[str] = []
        for row in tiles:
            if not isinstance(row, list):
                rows.append("." * size)
                continue
            chars = []
            for cell in row:
                chars.append(self._tile_char(cell))
            rows.append("".join(chars))
        return rows

    def get_grid_names(self) -> list[list[str]]:
        size = self._grid_radius * 2 + 1
        if self.vision is not None:
            vg = self.get_grid()
            if vg and any(ch in "TDSPW" for row in vg for ch in row):
                names = {"T": "树木", "D": "泥土", "S": "石块", "B": "背景墙", "W": "木材块", "P": "植物", "?": "未知", ".": "空气"}
                return [[names.get(ch, "空气") for ch in row] for row in vg]
        try:
            data = self.reader.latest(max_age_s=1.0)
            tiles = data.get("tiles")
        except Exception:
            return [["空气" for _ in range(size)] for _ in range(size)]
        if not isinstance(tiles, list) or len(tiles) != size:
            return [["空气" for _ in range(size)] for _ in range(size)]
        result: list[list[str]] = []
        for row in tiles:
            names = []
            for cell in row if isinstance(row, list) else []:
                if not isinstance(cell, dict):
                    names.append("空气")
                elif not cell.get("has_tile"):
                    names.append("背景墙" if cell.get("has_wall") else "空气")
                else:
                    names.append(tile_name(int(cell.get("tile_type", 0) or 0)))
            while len(names) < size:
                names.append("空气")
            result.append(names[:size])
        while len(result) < size:
            result.append(["空气" for _ in range(size)])
        return result

    @staticmethod
    def _tile_char(cell: dict) -> str:
        if not isinstance(cell, dict):
            return TILE_AIR
        if not cell.get("has_tile"):
            return TILE_WALL if cell.get("has_wall") else TILE_AIR
        tile_type = int(cell.get("tile_type", 0) or 0)
        if tile_type in TREE_TILE_IDS:
            return TILE_TREE
        if tile_type in WOOD_TILE_IDS:
            return TILE_WOOD_BLOCK
        if tile_type in DIRT_TILE_IDS:
            return TILE_DIRT
        if tile_type in PLANT_TILE_IDS:
            return TILE_PLANT
        if tile_type in STONE_TILE_IDS:
            return TILE_STONE
        # 房屋检查要用的特殊方块：火把/门/桌/椅/平台/宝箱
        if tile_type == 4:
            return "L"  # 火把（光源）
        if tile_type in (10, 11):
            return "O"  # 门
        if tile_type == 14:
            return "A"  # 桌子
        if tile_type == 15:
            return "C"  # 椅子
        if tile_type == 19:
            return "="  # 木平台
        if tile_type == 21:
            return "H"  # 宝箱
        if tile_type == 18:
            return "K"  # 工作台（craft 要靠近它）
        # 不认识的东西不要一律当成石块：给一个问号，界面画成灰蓝，避免“绿色的草变成石头”。
        return "?"

    def tile_at(self, world_x: int, world_y: int) -> str:
        try:
            data = self.reader.latest(max_age_s=1.0)
            px = int(math.floor((float(data.get("x", 0.0)) + self._offset_x) / TILE_SIZE_PX))
            py = int(math.floor((float(data.get("y", 0.0)) + self._offset_y) / TILE_SIZE_PX))
        except Exception:
            return TILE_AIR
        dx = int(world_x) - px + self._grid_radius
        dy = int(world_y) - py + self._grid_radius
        grid = self.get_grid()
        if 0 <= dy < len(grid) and 0 <= dx < len(grid[dy]):
            return grid[dy][dx]
        return TILE_AIR

    def wall_at(self, world_x: int, world_y: int) -> bool:
        """该格背后有没有背景墙（不受“格子上有植物/家具”影响）。"""
        try:
            data = self.reader.latest(max_age_s=1.0)
            tiles = data.get("tiles")
            if not isinstance(tiles, list):
                return False
            px = int(math.floor((float(data.get("x", 0.0)) + self._offset_x) / TILE_SIZE_PX))
            py = int(math.floor((float(data.get("y", 0.0)) + self._offset_y) / TILE_SIZE_PX))
            dx = int(world_x) - px + self._grid_radius
            dy = int(world_y) - py + self._grid_radius
            if 0 <= dy < len(tiles) and 0 <= dx < len(tiles[dy]):
                cell = tiles[dy][dx]
                return bool(isinstance(cell, dict) and cell.get("has_wall"))
        except Exception:
            pass
        return False

    def capture_frame(self, *, force: bool = False):
        if self.vision is not None:
            try:
                return self.vision.capture_frame(force=force)
            except Exception:
                pass
        return None

    def capture_jpeg(self) -> bytes | None:
        if self.vision is not None:
            try:
                return self.vision.capture_jpeg()
            except Exception:
                pass
        return None

    def poll_incoming_chat(self) -> list[str]:
        messages = self._incoming[:]
        self._incoming.clear()
        return messages

    def inject_incoming_chat(self, text: str) -> None:
        self._incoming.append(str(text))

    def record_outgoing_chat(self, text: str) -> None:
        self._outgoing.append(str(text))
        self._outgoing = self._outgoing[-50:]

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def read_player_state(self) -> PlayerSnapshot:
        starter = getattr(self.reader, "ensure_started", None)
        if callable(starter):
            starter()
        data = self.reader.latest(max_age_s=1.0)
        self._apply_real_inventory(data)
        x = float(data.get("x", 0.0)) + self._offset_x
        try:
            self._enemies = [dict(e) for e in (data.get("enemies") or []) if isinstance(e, dict)]
        except Exception:
            self._enemies = []
        y = float(data.get("y", 0.0)) + self._offset_y
        vx = float(data.get("vx", 0.0)) * self._ticks_per_second
        vy = float(data.get("vy", 0.0)) * self._ticks_per_second
        return PlayerSnapshot(
            center_x_px=x,
            center_y_px=y,
            velocity_x_px_s=vx,
            velocity_y_px_s=vy,
            on_ground=self._compute_on_ground(data, vy),
            timestamp=time.monotonic(),
        )

    SOLID_GROUND_CHARS = {"D", "S", "W", "M", "?"}

    def _compute_on_ground(self, data: dict, vy_px_s: float) -> bool:
        """判断玩家是不是真的站在地面上。

        以前用 `abs(vy) < 1` 当“着地”，但跳到最高点时垂直速度也是 0，会被误判成
        已经落地（跳跃回执报 5.5 格、其实还在空中）。现在要求同时满足：
        * 垂直速度接近 0；
        * 脚底那格是实心方块；
        * 脚底贴着那格的顶边（误差 ≤3px）。
        """
        if abs(vy_px_s) >= 1.0:
            return False
        tiles = data.get("tiles")
        if not isinstance(tiles, list):
            return True  # 没有格子数据时，退回旧判定的结果
        try:
            raw_x = float(data.get("x", 0.0))
            raw_y = float(data.get("y", 0.0))
            feet_y = raw_y + self._offset_y + 21.0
            feet_tile = int(math.floor(feet_y / TILE_SIZE_PX))
            if (feet_y - feet_tile * TILE_SIZE_PX) > 3.0:
                return False
            center_x = int(math.floor((raw_x + self._offset_x) / TILE_SIZE_PX))
            center_y = int(math.floor((raw_y + self._offset_y) / TILE_SIZE_PX))
            dx = center_x - center_x + self._grid_radius
            dy = feet_tile - center_y + self._grid_radius
            if not (0 <= dy < len(tiles) and 0 <= dx < len(tiles[dy])):
                return True
            cell = tiles[dy][dx]
            return self._tile_char(cell) in self.SOLID_GROUND_CHARS
        except Exception:
            return True

    def _apply_real_inventory(self, data: dict) -> None:
        items = data.get("inventory")
        if not isinstance(items, list):
            return
        converted: list[dict] = []
        for item in items[:50]:
            if not isinstance(item, dict):
                continue
            slot = int(item.get("slot", 0) or 0)
            if not 1 <= slot <= 50:
                continue
            if int(item.get("type", 0) or 0) == 0 and int(item.get("stack", 0) or 0) == 0:
                continue
            axe = int(item.get("axe", 0) or 0)
            pick = int(item.get("pickaxe", 0) or 0)
            hammer = int(item.get("hammer", 0) or 0)
            if axe > 0:
                kind, power = "axe", axe
            elif pick > 0:
                kind, power = "pickaxe", pick
            elif hammer > 0:
                kind, power = "hammer", hammer
            else:
                kind, power = "weapon", 0
            converted.append({
                "slot": slot,
                "name": str(item.get("name") or f"Item#{int(item.get('type', 0) or 0)}"),
                "kind": kind,
                "power": power,
                "damage": int(item.get("damage", 0) or 0),
                "stack": int(item.get("stack", 1) or 1),
                "type": int(item.get("type", 0) or 0),
                "create_tile": int(item.get("create_tile", 0) or 0),
                "create_wall": int(item.get("create_wall", 0) or 0),
                "hotbar": slot <= 10,
            })
        if converted:
            self._inventory = converted
        selected = int(data.get("selected_slot", 0) or 0)
        if 1 <= selected <= 10:
            self._current_slot = selected
        if "inventory_open" in data:
            self._inventory_open = bool(data.get("inventory_open"))
        try:
            self._ui_scale = float(data.get("ui_scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            self._ui_scale = 1.0

    def get_state(self) -> dict:
        try:
            starter = getattr(self.reader, "ensure_started", None)
            if callable(starter):
                starter()
            data = self.reader.latest(max_age_s=1.0)
        except Exception as exc:
            size = self._grid_radius * 2 + 1
            return {
                "player": {
                    "x_tiles": 0.0,
                    "y_tiles": 0.0,
                    "tile_x": 0,
                    "tile_y": 0,
                    "vx": 0.0,
                    "vy": 0.0,
                    "on_ground": False,
                    "hp": 0,
                    "max_hp": self._max_hp,
                },
                "grid": ["." * size for _ in range(size)],
                "grid_names": [["空气" for _ in range(size)] for _ in range(size)],
                "grid_center": [self._grid_radius, self._grid_radius],
                "grid_radius": self._grid_radius,
                "tool": self.current_tool,
                "inventory": self.inventory,
            "enemies": self.enemies,
                "action": self._action,
                "inventory_open": bool(getattr(self, "_inventory_open", False)),
                "last_chat": self._outgoing[-1] if self._outgoing else "",
                "ready": False,
                "error": str(exc),
                "window": self.window_info,
                "coordinate_mode": "clrmd",
            }

        self._apply_real_inventory(data)
        # 每次出状态都刷新敌人列表：以前这里漏了，导致 see/找怪一直看到空缓存。
        try:
            self._enemies = [dict(e) for e in (data.get("enemies") or []) if isinstance(e, dict)]
        except Exception:
            self._enemies = []
        x = float(data.get("x", 0.0)) + self._offset_x
        y = float(data.get("y", 0.0)) + self._offset_y
        vx = float(data.get("vx", 0.0)) * self._ticks_per_second
        vy = float(data.get("vy", 0.0)) * self._ticks_per_second
        hp = int(data.get("hp", 0) or 0)
        max_hp = int(data.get("max_hp", 0) or self._max_hp)
        grid = self.get_grid()
        grid_names = self.get_grid_names()
        self._log_coords(x, y, vx, vy, hp)
        return {
            "player": {
                "x_tiles": round(x / TILE_SIZE_PX, 2),
                "y_tiles": round(y / TILE_SIZE_PX, 2),
                "tile_x": int(math.floor(x / TILE_SIZE_PX)),
                "tile_y": int(math.floor(y / TILE_SIZE_PX)),
                "vx": round(vx, 2),
                "vy": round(vy, 2),
                "on_ground": abs(vy) < 1.0,
                "hp": hp,
                "max_hp": max_hp,
            },
            "grid": grid,
            "grid_names": grid_names,
            "grid_center": [len(grid) // 2, len(grid) // 2],
            "grid_radius": max(0, len(grid) // 2),
            "tool": self.current_tool,
            "inventory": self.inventory,
            "enemies": self.enemies,
            "action": self._action,
            "inventory_open": bool(self._inventory_open),
            "last_chat": self._outgoing[-1] if self._outgoing else "",
            "ready": bool(self.reader.is_ready and self.ensure_window() and self._in_world()),
            "error": self.error,
            "window": self.window_info,
            "coordinate_mode": "clrmd",
            "memory": {
                "x": x,
                "y": y,
                "vx": vx,
                "vy": vy,
                "screen_x": data.get("screen_x"),
                "screen_y": data.get("screen_y"),
                "player_ptr": data.get("player_ptr"),
            },
        }

    # ------------------------------------------------------------------
    # 坐标映射：Terraria 摄像机基本以玩家为中心
    # ------------------------------------------------------------------

    def world_to_screen(self, world_x: float, world_y: float) -> tuple[int, int]:
        """真实 Main.screenPosition 映射：client = world - screenPosition。"""
        data = self.reader.latest(max_age_s=2.0)
        info = self.window_info
        hwnd = int(getattr(self, "_hwnd", 0) or info.get("hwnd", 0) or 0)
        cl, ct, cw, ch = client_rect(hwnd) if hwnd else (0, 0, 0, 0)
        left = cl if cw > 0 else int(info.get("left", 0))
        top = ct if ch > 0 else int(info.get("top", 0))
        try:
            sx = float(data.get("screen_x", 0.0))
            sy = float(data.get("screen_y", 0.0))
        except (TypeError, ValueError):
            sx = sy = 0.0
        if math.isfinite(sx) and math.isfinite(sy) and abs(sx) < 1e8 and abs(sy) < 1e8 and (abs(sx) > 1.0 or abs(sy) > 1.0):
            return int(round(left + float(world_x) - sx)), int(round(top + float(world_y) - sy))
        width = int(info.get("width", 800) or 800)
        height = int(info.get("height", 600) or 600)
        px = float(data.get("x", 0.0)) + self._offset_x
        py = float(data.get("y", 0.0)) + self._offset_y
        return (
            int(round(left + width / 2.0 + (float(world_x) - px))),
            int(round(top + height / 2.0 + (float(world_y) - py))),
        )

    def screen_to_world(self, screen_x: int, screen_y: int) -> tuple[float, float]:
        data = self.reader.latest(max_age_s=2.0)
        info = self.window_info
        hwnd = int(getattr(self, "_hwnd", 0) or info.get("hwnd", 0) or 0)
        cl, ct, cw, ch = client_rect(hwnd) if hwnd else (0, 0, 0, 0)
        left = cl if cw > 0 else int(info.get("left", 0))
        top = ct if ch > 0 else int(info.get("top", 0))
        try:
            sx = float(data.get("screen_x", 0.0))
            sy = float(data.get("screen_y", 0.0))
        except (TypeError, ValueError):
            sx = sy = 0.0
        if math.isfinite(sx) and math.isfinite(sy) and abs(sx) < 1e8 and abs(sy) < 1e8 and (abs(sx) > 1.0 or abs(sy) > 1.0):
            return float(screen_x - left) + sx, float(screen_y - top) + sy
        width = int(info.get("width", 800) or 800)
        height = int(info.get("height", 600) or 600)
        px = float(data.get("x", 0.0)) + self._offset_x
        py = float(data.get("y", 0.0)) + self._offset_y
        return px + (screen_x - left - width / 2.0), py + (screen_y - top - height / 2.0)

    def _log_coords(self, x: float, y: float, vx: float, vy: float, hp: int) -> None:
        now = time.monotonic()
        if now - self._last_state_at < 1.0:
            return
        self._last_state_at = now
        if self._log:
            self._log(
                f"[ClrMD] 真实坐标 X={x:.2f} Y={y:.2f} "
                f"VX={vx:.2f} VY={vy:.2f} HP={hp}",
                "info",
            )

    def close(self) -> None:
        try:
            self.reader.close()
        except Exception:
            pass
        if self.vision is not None:
            try:
                self.vision.close()
            except Exception:
                pass



class SimulatedInputBackend:
    """把执行层的键鼠动作转成 FakeGameMonitor 的状态变化。"""

    def __init__(self, monitor: FakeGameMonitor) -> None:
        self._monitor = monitor
        self._mouse = (0, 0)
        self._window_title = "Terraria"

    def key_down(self, key: str) -> None:
        key = key.lower()
        if key.isdigit():
            self._monitor.select_slot(int(key))
        else:
            self._monitor.key_down(key)

    def key_up(self, key: str) -> None:
        self._monitor.key_up(key)

    def mouse_move(self, x: int, y: int) -> None:
        self._mouse = (int(x), int(y))

    def mouse_click(self, button: str = "left") -> None:
        self._monitor.handle_click_at_screen(*self._mouse, button=button)

    def release_all(self, keys) -> None:
        self._monitor.release_all(keys)

    def type_text(self, text: str) -> None:
        self._monitor.record_outgoing_chat(text)

    def ensure_game_focus(self) -> None:
        return None

    def set_window_title(self, title: str) -> None:
        self._window_title = title




__all__ = [
    "DEFAULT_INVENTORY",
    "FakeGameMonitor",
    "MemoryGameMonitor",
    "OfflineGameMonitor",
    "PlayerStateProvider",
    "RealGameMonitor",
    "SimulatedInputBackend",
    "TileGridProvider",
    "TARGET_KIND_BY_TILE",
    "TILE_REQUIRED_PICKAXE_POWER",
    "TILE_AIR",
    "TILE_DIRT",
    "TILE_TREE",
    "TILE_STONE",
    "TILE_METEORITE",
    "TILE_WOOD_BLOCK",
    "TILE_WALL",
    "TILE_PLANT",
    "TILE_ENEMY",
]
