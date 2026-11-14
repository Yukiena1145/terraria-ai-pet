"""Terraria-Agent-V1 物理执行层（ActionExecutor）。

职责边界
--------
本模块只负责三件事：

1. 从 ``PlayerStateReader`` 读取玩家实时状态；
2. 根据目标坐标做闭环决策；
3. 通过 ``InputBackend`` 发送键盘/鼠标输入。

它不包含 Flask、LLM、技能选择、视觉网格和内存扫描逻辑。这样未来替换
“直连 API 版”或“桌宠 API 版”时，执行层可以完全复用。

坐标约定
--------
* 世界坐标：像素（px），x 向右，y 向下。
* 网格坐标：格（tile），1 格 = 16 px。
* 玩家逻辑碰撞体：宽 2 格、高 3 格。
* 以玩家中心点所在格作为 2x3 逻辑体的“左列 / 中行”。x 方向在格边界上
  归入左列（``ceil - 1``），保证“恰好站在两格中间”时中心点仍位于左列。
  这样任何时刻 ``BodyTiles`` 都严格是 2x3，绝不会退化成 1x1 质点。
* 这是 V1 的“玩法层近似”；像素级碰撞仍由 Terraria 引擎处理。

重要物理参数（依据官方 Wiki 与需求补充）
----------------------------------------
* Hitbox：20 x 42 px（1.25 x 2.625 格），逻辑上按 2 x 3 格处理。
* 裸装跳跃高度：约 100.33 px = 6.27 格；V1 安全阈值按 6 格计算。
* 放置/挖掘最远距离：水平 5 格、垂直 5 格。
* 移动绝对不使用“按住 N 秒”的固定时间方案；每一帧都以坐标反馈为准。

线程模型
--------
Flask 的多个请求线程可能同时访问执行器。所有会输出键鼠动作的方法都由
``self._action_lock`` 串行化；``get_current_position()`` 不加动作锁，因此
Web 面板可以在移动过程中持续读取实时坐标。
"""

from __future__ import annotations

import ctypes
import logging
import math
import threading
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Final, Iterable, Protocol, runtime_checkable

from game_window import find_window

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. 物理常量：集中在这里，禁止在业务代码里出现魔法数字
# ---------------------------------------------------------------------------

TILE_SIZE_PX: Final[float] = 16.0
PLAYER_HITBOX_WIDTH_PX: Final[float] = 20.0
PLAYER_HITBOX_HEIGHT_PX: Final[float] = 42.0

# 逻辑碰撞体：宽 2 格、高 3 格（需求明确要求，不按 1.25 x 2.625 的像素值取整）
PLAYER_TILE_WIDTH: Final[int] = 2
PLAYER_TILE_HEIGHT: Final[int] = 3

# 裸装跳跃：100.33 px ≈ 6.27 格；V1 默认只承诺安全跳过 6 格
BASE_JUMP_HEIGHT_TILES: Final[float] = 6.27
SAFE_JUMP_HEIGHT_TILES: Final[float] = 6.0

# 基础放置/挖掘距离（“加长握爪”等饰品未来通过配置覆盖）
MAX_PLACE_RANGE_TILES: Final[float] = 5.0
MAX_MINE_RANGE_TILES: Final[float] = 5.0

LEFT_KEY: Final[str] = "a"
RIGHT_KEY: Final[str] = "d"
JUMP_KEY: Final[str] = "space"


# ---------------------------------------------------------------------------
# 2. 值对象与异常
# ---------------------------------------------------------------------------


class Direction(IntEnum):
    """水平方向。取值直接对应 dx 的符号，便于做速度/误差计算。"""

    LEFT = -1
    RIGHT = 1


@dataclass(frozen=True, slots=True)
class BodyTiles:
    """玩家在网格中的 2x3 逻辑碰撞体（闭区间边界）。

    ``left/top`` 是左上角格子的索引，``right/bottom`` 是右下角格子的索引。
    """

    left: int
    top: int

    @property
    def right(self) -> int:
        return self.left + PLAYER_TILE_WIDTH - 1

    @property
    def bottom(self) -> int:
        return self.top + PLAYER_TILE_HEIGHT - 1

    @property
    def center(self) -> tuple[int, int]:
        """逻辑体中心格：对 2x3 来说就是左列、中行。"""
        return self.left, self.top + 1

    def contains(self, tile_x: int, tile_y: int) -> bool:
        return self.left <= tile_x <= self.right and self.top <= tile_y <= self.bottom

    def iter_tiles(self) -> Iterable[tuple[int, int]]:
        for ty in range(self.top, self.bottom + 1):
            for tx in range(self.left, self.right + 1):
                yield tx, ty


@dataclass(frozen=True, slots=True)
class PlayerSnapshot:
    """一帧玩家状态。

    所有字段都使用世界像素/秒，避免“像素 + 格”混用导致精度问题。

    ``center_x_px/center_y_px`` 约定为 **Hitbox 几何中心**。如果内存读取到
    的是 Terraria 的 ``player.position``（Hitbox 左上角），GameMonitor 必须
    先转换：``center = position + (20 / 2, 42 / 2)``，否则会整体偏移半个
    碰撞箱。

    ``timestamp`` 建议使用 ``time.monotonic()``，由 GameMonitor 填充。
    """

    center_x_px: float
    center_y_px: float
    velocity_x_px_s: float = 0.0
    velocity_y_px_s: float = 0.0
    on_ground: bool = True
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        for name in ("center_x_px", "center_y_px", "velocity_x_px_s", "velocity_y_px_s"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"PlayerSnapshot.{name} 必须是有限数值，收到 {value!r}")

    @property
    def center_x_tiles(self) -> float:
        return self.center_x_px / TILE_SIZE_PX

    @property
    def center_y_tiles(self) -> float:
        return self.center_y_px / TILE_SIZE_PX

    @property
    def foot_y_px(self) -> float:
        """脚底 y 坐标（世界坐标向下为正）。"""
        return self.center_y_px + PLAYER_HITBOX_HEIGHT_PX / 2.0

    @property
    def body_tiles(self) -> BodyTiles:
        """返回严格 2x3 的逻辑碰撞体。

        关键约定：中心点所在格 = 左列 / 中行。
        x 方向使用 ``ceil - 1``，让“恰好站在两格边界”时中心点归入左列；
        y 方向使用 ``floor``，中心点所在行就是 2x3 的中间行。这样任何时刻
        碰撞体都严格是 2x3，不会退化成 1x1 质点。
        """
        anchor_col = int(math.ceil(self.center_x_px / TILE_SIZE_PX)) - 1
        anchor_row = int(math.floor(self.center_y_px / TILE_SIZE_PX))
        return BodyTiles(left=anchor_col, top=anchor_row - 1)


class ExecutorError(RuntimeError):
    """执行层所有可预期异常的基类。"""


class StateReadError(ExecutorError):
    """读取玩家状态失败，或状态过期。"""


class InputBackendError(ExecutorError):
    """键鼠后端不可用或发送失败。"""


class ActionCancelled(ExecutorError):
    """当前动作被用户/主循环主动打断。"""


class OutOfRangeError(ExecutorError):
    """目标方块超出放置/挖掘距离。"""


class InvalidTargetError(ExecutorError):
    """目标不合法，例如把方块放在玩家自己的身体里。"""


class JumpTooHighError(ExecutorError):
    """障碍物高于裸装跳跃上限，需要搭方块或绕路。"""


@dataclass(frozen=True, slots=True)
class MoveResult:
    """一次水平闭环移动的结果。"""

    status: str
    target_x_tiles: float
    final_x_tiles: float
    error_tiles: float
    elapsed_s: float

    @property
    def success(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True, slots=True)
class JumpResult:
    """一次跳跃/跳跃越障的结果。"""

    success: bool
    height_tiles: float
    duration_s: float
    reason: str = ""


# ---------------------------------------------------------------------------
# 3. 可注入接口：便于单元测试和未来替换实现
# ---------------------------------------------------------------------------


@runtime_checkable
class PlayerStateReader(Protocol):
    """玩家状态读取接口。

    V1 由 GameMonitor 用内存读取实现；测试时可注入假实现；未来也可以
    替换成截图识别或别的数据源。
    """

    def read_player_state(self) -> PlayerSnapshot:
        ...


@runtime_checkable
class InputBackend(Protocol):
    """原始键鼠输出接口。执行层只依赖这个协议，不直接依赖 pydirectinput。"""

    def key_down(self, key: str) -> None:
        ...

    def key_up(self, key: str) -> None:
        ...

    def mouse_move(self, x: int, y: int) -> None:
        ...

    def mouse_click(self, button: str = "left") -> None:
        ...

    def release_all(self, keys: Iterable[str]) -> None:
        ...

    def type_text(self, text: str) -> None:
        """把一段文字输入到当前焦点窗口。

        必须支持中文。推荐实现：剪贴板 + Ctrl+V，而不是逐字符按键。
        """
        ...

    def ensure_game_focus(self) -> None:
        """只做前台校验；具体实现见 PyDirectInputBackend。"""
        ...


class PyDirectInputBackend:
    """前台键盘/鼠标后端（用于先跑通，不是后台方案）。"""

    def __init__(
        self,
        *,
        pause: float = 0.0,
        failsafe: bool = True,
        window_title: str = "Terraria",
        window_titles: list[str] | None = None,
        exe_path: str = "",
    ) -> None:
        try:
            import pydirectinput as pdi  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover
            raise InputBackendError(
                "无法导入 pydirectinput。请先执行 `pip install pydirectinput`。"
            ) from exc

        self._pdi = pdi
        self._window_title = window_title
        self._window_titles = self._dedupe_titles(
            window_titles or [window_title, "Terraria", "泰拉瑞亚"]
        )
        self._exe_path = str(exe_path or "")
        pdi.PAUSE = pause
        pdi.FAILSAFE = failsafe
        self._closed = False

    @staticmethod
    def _dedupe_titles(titles: Iterable[str]) -> list[str]:
        result: list[str] = []
        for title in titles:
            text = str(title or "").strip()
            if text and text not in result:
                result.append(text)
        return result

    def key_down(self, key: str) -> None:
        self._pdi.keyDown(key.lower())

    def key_up(self, key: str) -> None:
        self._pdi.keyUp(key.lower())

    def mouse_move(self, x: int, y: int) -> None:
        self._pdi.moveTo(int(x), int(y), duration=0.0)

    def mouse_click(self, button: str = "left") -> None:
        self._pdi.click(button=button)

    def mouse_down(self, button: str = "left") -> None:
        self._pdi.mouseDown(button=button)

    def mouse_up(self, button: str = "left") -> None:
        self._pdi.mouseUp(button=button)

    def scroll(self, clicks: int) -> None:
        # pydirectinput 没有滚轮 API，用 Win32 mouse_event。
        delta = int(clicks) * 120
        ctypes.windll.user32.mouse_event(
            0x0800, 0, 0, ctypes.c_ulong(delta & 0xFFFFFFFF), 0
        )

    def release_all(self, keys: Iterable[str] = ()) -> None:
        for key in tuple(keys):
            try:
                self.key_up(key)
            except Exception:
                logger.exception("释放按键失败: %s", key)
        # 兜底松开鼠标：长按制作/放置中途出错时，避免鼠标一直按着。
        for button in ("left", "right", "middle"):
            try:
                self.mouse_up(button)
            except Exception:
                logger.exception("释放鼠标失败: %s", button)

    def type_text(self, text: str) -> None:
        """中文通过剪贴板 + Ctrl+V 输入。"""
        if not text:
            return
        try:
            import pyperclip  # type: ignore[import-not-found]
        except Exception as exc:
            if text.isascii():
                self._pdi.write(text, interval=0.01)
                return
            raise InputBackendError(
                "输入中文需要 pyperclip，请执行 `pip install pyperclip`"
            ) from exc
        try:
            previous = pyperclip.paste()
        except Exception:
            previous = ""
        try:
            pyperclip.copy(text)
            self._pdi.keyDown("ctrl")
            try:
                self._pdi.press("v")
            finally:
                self._pdi.keyUp("ctrl")
            time.sleep(0.10)
        finally:
            try:
                pyperclip.copy(previous)
            except Exception:
                pass

    def ensure_game_focus(self) -> None:
        """只检查前台，不调用会卡死的 AttachThreadInput/BringWindowToTop。"""
        window = find_window(self._window_titles, exe_path=self._exe_path)
        if window is None:
            raise InputBackendError(
                f"未找到 exe 匹配 {self._exe_path!r} 的 Terraria 窗口"
            )
        hwnd = int(getattr(window, "_hWnd", 0) or 0)
        if not hwnd:
            raise InputBackendError("游戏窗口句柄无效")

        import win32gui
        import win32con
        import time as _time

        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            left, top, right, bottom = 0, 0, 0, 0
        if win32gui.IsIconic(hwnd) or left <= -30000 or right - left < 100:
            win32gui.ShowWindow(hwnd, 9)
            try:
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOP,
                    20, 20, 0, 0,
                    win32con.SWP_SHOWWINDOW | win32con.SWP_NOSIZE,
                )
            except Exception:
                pass
            _time.sleep(0.25)
        if int(win32gui.GetForegroundWindow() or 0) != hwnd:
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            _time.sleep(0.10)
        if int(win32gui.GetForegroundWindow() or 0) != hwnd:
            # 温和兜底：置顶 + 点一下标题栏，避免 AttachThreadInput 卡死。
            try:
                import win32api
                import win32con
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
                left, top, _, _ = win32gui.GetWindowRect(hwnd)
                win32api.SetCursorPos((left + 100, top + 10))
                _time.sleep(0.05)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                _time.sleep(0.03)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                _time.sleep(0.15)
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
                )
            except Exception:
                pass
        if int(win32gui.GetForegroundWindow() or 0) != hwnd:
            raise InputBackendError(
                "请先手动点击一次 Terraria 窗口，让游戏保持前台；"
                "当前前台不是游戏，已拒绝发送按键"
            )

    def set_window_title(self, title: str) -> None:
        self._window_title = str(title)
        self._window_titles = self._dedupe_titles([title, "Terraria", "泰拉瑞亚"])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.release_all()


# ---------------------------------------------------------------------------
# 4. 执行器配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    """闭环控制参数。所有时间单位为秒，距离单位为像素/格。"""

    tick_hz: int = 60
    position_tolerance_tiles: float = 0.15       # 2.4 px，避免在目标点来回抖动
    settle_speed_px_s: float = 15.0              # 低于该速度才判定“停稳”
    brake_lookahead_s: float = 0.10              # 反应延迟补偿
    brake_deceleration_px_s2: float = 2400.0     # 保守制动减速度，可按鞋/冰面调低
    stall_speed_px_s: float = 5.0                # 贴墙时速度接近 0
    stall_window_s: float = 1.5                  # 连续无进展多久判定卡住
    stall_progress_px: float = 0.75              # 误差改善超过该值才算有进展
    move_timeout_s: float = 12.0
    max_state_age_s: float = 0.50                # timestamp=0 时表示“不检查”
    jump_timeout_s: float = 3.0
    jump_key_hold_s: float = 0.03                # 小跳/默认按空格时长
    jump_max_hold_s: float = 0.80                # 泰拉瑞亚最大跳跃蓄力
    jump_hold_per_tile_s: float = 0.11           # 每多跳 1 格增加的蓄力
    takeoff_timeout_s: float = 0.40              # 按下空格后多久内必须离地
    max_jump_height_tiles: float = SAFE_JUMP_HEIGHT_TILES
    max_place_range_tiles: float = MAX_PLACE_RANGE_TILES
    max_mine_range_tiles: float = MAX_MINE_RANGE_TILES
    chat_max_chars: int = 180
    chat_type_delay_s: float = 0.10

    def __post_init__(self) -> None:
        if self.tick_hz <= 0:
            raise ValueError("tick_hz 必须大于 0")
        if self.position_tolerance_tiles <= 0:
            raise ValueError("position_tolerance_tiles 必须大于 0")
        if self.settle_speed_px_s <= 0 or self.stall_speed_px_s <= 0:
            raise ValueError("速度阈值必须大于 0")
        if self.brake_deceleration_px_s2 <= 0:
            raise ValueError("brake_deceleration_px_s2 必须大于 0")
        if self.move_timeout_s <= 0 or self.jump_timeout_s <= 0:
            raise ValueError("超时时间必须大于 0")
        if self.max_place_range_tiles <= 0 or self.max_mine_range_tiles <= 0:
            raise ValueError("放置/挖掘距离必须大于 0")
        if not 0 < self.max_jump_height_tiles <= BASE_JUMP_HEIGHT_TILES:
            raise ValueError("max_jump_height_tiles 必须位于 (0, 6.27] 区间")
        if self.chat_max_chars <= 0:
            raise ValueError("chat_max_chars 必须大于 0")

    @property
    def tick_s(self) -> float:
        return 1.0 / self.tick_hz

    @property
    def position_tolerance_px(self) -> float:
        return self.position_tolerance_tiles * TILE_SIZE_PX


# ---------------------------------------------------------------------------
# 5. 物理执行器
# ---------------------------------------------------------------------------


class ActionExecutor:
    """把“目标坐标”翻译成“实时键鼠动作”的闭环执行层。

    典型用法::

        executor = ActionExecutor(monitor)          # monitor 实现 PlayerStateReader
        result = executor.move_to_x(tree_x - 1.5)   # 目标：玩家中心到达该 x
        executor.jump_over_obstacle(2.0, direction=Direction.RIGHT)
        executor.mine_tile(tree_x, tree_y, screen_mapper=monitor.world_to_screen)

    设计要点：

    * 移动循环 60Hz 读取坐标，不使用固定 sleep 时长；
    * 预测制动距离，提前松键/反向刹车，抑制惯性过冲；
    * 每次动作结束都在 ``finally`` 中释放水平键，防止“按键卡住”；
    * 所有输出动作串行化，避免 Flask 多线程同时操作键鼠。

    边界说明：

    * ``move_to_x`` 只做一维 x 闭环，不负责悬崖、墙体绕路和垂直寻路；
      这些属于 ``Skill`` / 未来 ``PathPlanner`` 的职责。
    * 每个公开动作开始前都会调用 ``InputBackend.ensure_game_focus()``，
      找不到游戏窗口时拒绝发送按键，防止把 A/D 发进浏览器。
    """

    def __init__(
        self,
        state_reader: PlayerStateReader,
        backend: InputBackend | None = None,
        config: ExecutorConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._reader = state_reader
        self._backend = backend if backend is not None else PyDirectInputBackend()
        self._config = config or ExecutorConfig()
        self._clock = clock
        self._sleep = sleep

        self._action_lock = threading.RLock()
        self._held_keys: set[str] = set()
        self._horizontal: Direction | None = None
        self._closed = False
        self._cancel = threading.Event()
        self._smart_cursor_on = False
        # 背包键：默认 Esc；主人改过键就通过 set_inventory_key 换成游戏里的那个键。
        self._inventory_key = "escape"
        self._last_inventory_press = 0.0
        self._hold_attack = False

    def set_inventory_key(self, key: str) -> None:
        """设置游戏里“背包/物品栏”的按键（pydirectinput 的键名，例如 escape、i）。"""
        value = str(key or "").strip().lower()
        self._inventory_key = value or "escape"

    # ------------------------------------------------------------------
    # 5.1 生命周期与状态读取
    # ------------------------------------------------------------------

    def __enter__(self) -> "ActionExecutor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """释放全部按键。应在程序退出/热重载前调用。"""
        with self._action_lock:
            if self._closed:
                return
            try:
                self._release_all_keys()
            finally:
                self._closed = True
        logger.info("ActionExecutor 已关闭，所有按键已释放")

    def get_current_position(self) -> PlayerSnapshot:
        """读取当前完整玩家状态（坐标、速度、是否着地）。

        该方法故意不获取动作锁：Web 状态面板可以在长时间移动过程中持续
        刷新，而不会被 ``move_to_x`` 阻塞。
        """
        self._ensure_open()
        return self._read_snapshot()

    def set_window_title(self, title: str) -> None:
        """配置热更新：修改需要聚焦的游戏窗口标题。"""
        setter = getattr(self._backend, "set_window_title", None)
        if callable(setter):
            setter(title)

    def use_item(self) -> None:
        """鼠标左键：挥斧/使用手持物品。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self._backend.mouse_click("left")

    def interact(self) -> None:
        """鼠标右键：放置方块/放置工作台/交互。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self._backend.mouse_click("right")

    def press_inventory(self, key: str | None = None) -> bool:
        """按一次“背包键”（默认 Esc），打开或关闭背包。

        关键：
        * 必须先把游戏切到前台再按键；切不到前台直接抛错，不假装按过；
        * 1 秒内重复调用会被忽略并返回 False——Esc 是开关同一个键，连按会来回
          开关，这正是“莫名其妙开一下又关掉”的来源；
        * 按下后由调用方读状态验证，这里不猜结果。
        """
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            now = time.monotonic()
            if now - self._last_inventory_press < 1.0:
                logger.warning(
                    "背包键在 %.2fs 内被重复调用，已忽略，避免来回开关",
                    now - self._last_inventory_press,
                )
                return False
            self._last_inventory_press = now
            self._tap_key(key or self._inventory_key)
            logger.info("背包键已按下: %s", key or self._inventory_key)
            return True

    def ui_click(self, screen_x: int, screen_y: int, button: str = "left") -> None:
        """点击 UI 绝对坐标；不参与世界目标范围检查。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self._backend.mouse_move(int(screen_x), int(screen_y))
            self._sleep(0.20)
            self._backend.mouse_down(button)
            self._sleep(0.12)
            self._backend.mouse_up(button)
            self._sleep(0.18)

    def ui_hold(
        self,
        screen_x: int,
        screen_y: int,
        hold_s: float = 1.0,
        button: str = "left",
    ) -> None:
        """在 UI 坐标上按住不放：泰拉瑞亚的制作按钮按住会连续制作。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self._backend.mouse_move(int(screen_x), int(screen_y))
            self._sleep(0.15)
            self._backend.mouse_down(button)
            try:
                self._sleep(max(0.1, float(hold_s)))
            finally:
                self._backend.mouse_up(button)
            self._sleep(0.20)

    def scroll_ui(self, clicks: int) -> None:
        """在 UI 上滚动滚轮；正数向上，负数向下。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            scroll = getattr(self._backend, "scroll", None)
            if callable(scroll):
                scroll(int(clicks))
            self._sleep(0.20)

    def ui_drag(
        self,
        from_x: int,
        from_y: int,
        to_x: int,
        to_y: int,
        *,
        duration_s: float = 0.35,
    ) -> None:
        """在 UI 上按住鼠标拖动，用于背包 -> 快捷栏。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self._backend.mouse_move(int(from_x), int(from_y))
            self._sleep(0.12)
            self._backend.mouse_down("left")
            try:
                steps = 8
                for i in range(1, steps + 1):
                    x = int(round(from_x + (to_x - from_x) * i / steps))
                    y = int(round(from_y + (to_y - from_y) * i / steps))
                    self._backend.mouse_move(x, y)
                    self._sleep(duration_s / steps)
                self._sleep(0.12)
            finally:
                self._backend.mouse_up("left")
            self._sleep(0.18)

    def smart_cursor_down(self) -> None:
        """按住 Ctrl：当前游戏设置为“按住启用智能光标”模式。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self._press_key("ctrl")

    def smart_cursor_up(self) -> None:
        with self._action_lock:
            self._release_key("ctrl")

    def auto_select_down(self) -> None:
        """按住 Shift：自动选择合适工具（斧/镐/锤），不需要手动切快捷栏。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self._press_key("shift")

    def auto_select_up(self) -> None:
        with self._action_lock:
            self._release_key("shift")

    def select_hotbar_slot(self, slot: int) -> None:
        """按数字键切换快捷栏槽位（1-10，10 对应键盘 0）。

        V1 约定：物品栏前 10 格就是快捷栏；是否真的持有该物品由技能层和
        GameMonitor 判断。这里只负责发送原始按键。
        """
        slot = int(slot)
        if not 1 <= slot <= 10:
            raise ValueError("slot 必须在 1..10 之间")
        key = "0" if slot == 10 else str(slot)

        with self._action_lock:
            self._ensure_open()
            if self._cancel.is_set():
                raise ActionCancelled("动作已取消")
            self._ensure_game_focus()
            self._tap_key(key)
        setter = getattr(self._reader, "select_slot", None)
        if callable(setter):
            try:
                setter(slot)
            except Exception:
                logger.debug("同步监控器快捷栏失败", exc_info=True)
        logger.info("切换快捷栏: slot=%s key=%s", slot, key)

    def stop(self) -> None:
        """立即请求取消当前动作并松开所有键。

        先设置取消事件，再等待动作锁；这样即使 ``move_to_x`` 正持有锁，
        它也能在下一 tick 看到取消并退出，不会被 stop 阻塞住。
        """
        self._cancel.set()
        with self._action_lock:
            self._ensure_open()
            self._release_all_keys()
        logger.info("ActionExecutor 已停止并释放按键")

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def reset_cancel(self) -> None:
        """由 Agent 主循环在新指令开始时调用，清除上一次的取消标记。"""
        self._cancel.clear()

    def _release_all_keys(self) -> None:
        self._release_horizontal()
        self._hold_attack = False
        self._backend.release_all(tuple(self._held_keys))
        self._held_keys.clear()

    def release_all(self) -> None:
        """松开所有按键和鼠标按钮；动作结束/异常时兜底调用。"""
        with self._action_lock:
            self._release_all_keys()

    def send_chat_message(self, text: str) -> None:
        """在游戏聊天栏安全地发送一条消息（支持中文）。

        流程：
        1. 释放所有移动/攻击按键，避免“边跑边打字”；
        2. 按 Enter 打开聊天栏；
        3. 剪贴板 + Ctrl+V 输入文字；
        4. 再按 Enter 发送。

        该方法不加“同动作 5 秒冷却”；冷却由上层 ``CommentaryManager``
        负责，因为只有上层知道“同一个动作”的语义。
        """
        message = " ".join(str(text or "").split()).strip()
        if not message:
            return
        message = message[: self._config.chat_max_chars]

        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            # 关键：打字前必须先停手，否则角色会一边移动一边打开聊天栏
            self._release_all_keys()

            self._tap_key("enter")
            try:
                self._backend.type_text(message)
                # 等待游戏处理 Ctrl+V，再发送
                self._sleep(self._config.chat_type_delay_s)
            except Exception:
                # 输入失败时用 Esc 关闭聊天栏，避免留下半截文字
                try:
                    self._tap_key("escape")
                except Exception:
                    pass
                raise
            else:
                self._tap_key("enter")
            finally:
                self._release_all_keys()

        logger.info("游戏内聊天: %s", message)

    def _read_snapshot(self) -> PlayerSnapshot:
        try:
            snapshot = self._reader.read_player_state()
        except ExecutorError:
            raise
        except Exception as exc:
            raise StateReadError("读取玩家状态失败") from exc

        if not isinstance(snapshot, PlayerSnapshot):
            raise StateReadError(
                f"PlayerStateReader 必须返回 PlayerSnapshot，收到 {type(snapshot)!r}"
            )

        # timestamp=0 表示读取方不提供时钟，跳过新鲜度检查（便于 V1 内存读取接入）。
        if snapshot.timestamp > 0 and self._config.max_state_age_s > 0:
            age = self._clock() - snapshot.timestamp
            if age > self._config.max_state_age_s:
                raise StateReadError(
                    f"玩家状态已过期 {age:.3f}s，可能是游戏暂停或内存读取卡住"
                )
        return snapshot

    def _ensure_open(self) -> None:
        if self._closed:
            raise ExecutorError("ActionExecutor 已关闭，不能继续执行动作")

    def _ensure_game_focus(self) -> None:
        """动作开始前确保游戏在前台，避免把 A/D 发进浏览器。"""
        try:
            self._backend.ensure_game_focus()
        except ExecutorError:
            raise
        except Exception as exc:
            raise InputBackendError("切换游戏窗口失败") from exc

    # ------------------------------------------------------------------
    # 5.2 水平闭环移动（核心算法）
    # ------------------------------------------------------------------

    def move_to_x(
        self,
        target_x_tiles: float,
        *,
        timeout_s: float | None = None,
        allow_backoff: bool = True,
    ) -> MoveResult:
        """把玩家中心闭环移动到指定世界 x 坐标。

        参数
        ----
        target_x_tiles:
            目标 x，单位“格”（世界坐标，不是屏幕坐标）。例如目标格为 10，
            希望玩家中心停在 8.5 格处，就传 ``8.5``。
        timeout_s:
            本次移动超时；默认使用配置值。

        返回
        ----
        ``MoveResult``。超时/卡住不会抛异常，而是返回 ``status``，便于技能层
        决定“搭方块/绕路/跳过”。

        算法
        ----
        1. 每 tick 读取中心坐标与水平速度；
        2. 计算 ``error = target - x``；
        3. 估算制动距离：``|v| * 反应时间 + v^2 / (2a)``；
        4. 若目标落在制动距离内，立即反向按键刹车；否则朝目标方向按键；
        5. 进入容差且速度足够低才判定成功，避免“冲过头再回来”的假成功；
        6. 连续无进展且速度接近 0，判定 STALLED（撞墙/被方块挡住）。
        """
        target_x_tiles = float(target_x_tiles)
        if not math.isfinite(target_x_tiles):
            raise ValueError("target_x_tiles 必须是有限数值")

        target_px = target_x_tiles * TILE_SIZE_PX
        timeout = self._config.move_timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise ValueError("timeout_s 必须大于 0")

        start_snapshot = None
        if not allow_backoff:
            start_snapshot = self._read_snapshot()
        travel_sign = 0.0
        if start_snapshot is not None:
            travel_sign = 1.0 if target_px >= start_snapshot.center_x_px else -1.0

        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()

            started_at = self._clock()
            last_progress_at = started_at
            best_error_px = float("inf")
            last_snapshot: PlayerSnapshot | None = None

            if self._cancel.is_set():
                return self._move_result(
                    "cancelled", target_x_tiles, last_snapshot, 0.0
                )

            try:
                while True:
                    now = self._clock()
                    elapsed = now - started_at

                    if self._cancel.is_set():
                        self._release_horizontal()
                        return self._move_result(
                            "cancelled", target_x_tiles, last_snapshot, elapsed
                        )

                    if elapsed > timeout:
                        self._release_horizontal()
                        return self._move_result(
                            "timeout", target_x_tiles, last_snapshot, elapsed
                        )

                    snapshot = self._read_snapshot()
                    last_snapshot = snapshot

                    error_px = target_px - snapshot.center_x_px
                    abs_error_px = abs(error_px)
                    speed_px_s = snapshot.velocity_x_px_s

                    # 自动寻路模式：只按前进键，不做反向刹车/回撤。
                    # 越过目标就结束，剩余惯性由下一段继续向前。
                    if not allow_backoff and travel_sign:
                        if abs_error_px < best_error_px - self._config.stall_progress_px:
                            best_error_px = abs_error_px
                            last_progress_at = now
                        if (
                            now - last_progress_at > self._config.stall_window_s
                            and abs(speed_px_s) < self._config.stall_speed_px_s
                        ):
                            self._release_horizontal()
                            return self._move_result(
                                "stalled", target_x_tiles, snapshot, elapsed
                            )
                        if error_px * travel_sign <= 0:
                            self._release_horizontal()
                            return self._move_result(
                                "success", target_x_tiles, snapshot, elapsed
                            )
                        direction = Direction.RIGHT if travel_sign > 0 else Direction.LEFT
                        self._set_horizontal(direction)
                        self._sleep(self._config.tick_s)
                        continue

                    # 成功条件必须同时满足：位置进入容差 + 速度足够低。
                    if (
                        abs_error_px <= self._config.position_tolerance_px
                        and abs(speed_px_s) <= self._config.settle_speed_px_s
                    ):
                        self._release_horizontal()
                        logger.info(
                            "move_to_x 完成: target=%.3f tile, final=%.3f tile, "
                            "error=%.2f px, elapsed=%.3fs",
                            target_x_tiles,
                            snapshot.center_x_tiles,
                            error_px,
                            elapsed,
                        )
                        return self._move_result(
                            "success", target_x_tiles, snapshot, elapsed
                        )

                    # 只有“误差显著改善”才算有进展，防止原地抖动重置卡住计时。
                    if abs_error_px < best_error_px - self._config.stall_progress_px:
                        best_error_px = abs_error_px
                        last_progress_at = now

                    # 贴墙时速度接近 0，且误差迟迟不下降 -> 交给技能层处理。
                    if (
                        now - last_progress_at > self._config.stall_window_s
                        and abs(speed_px_s) < self._config.stall_speed_px_s
                    ):
                        self._release_horizontal()
                        logger.warning(
                            "move_to_x 卡住: error=%.2f px, speed=%.2f px/s",
                            error_px,
                            speed_px_s,
                        )
                        return self._move_result(
                            "stalled", target_x_tiles, snapshot, elapsed
                        )

                    direction = self._choose_direction(error_px, speed_px_s)
                    self._set_horizontal(direction)
                    self._sleep(self._config.tick_s)

            except Exception:
                # 任何异常都必须先松开方向键，否则角色会一直朝一个方向跑。
                self._release_horizontal()
                raise
            finally:
                self._release_horizontal()

    def _choose_direction(
        self,
        error_px: float,
        speed_px_s: float,
    ) -> Direction | None:
        """根据位置误差和当前速度，决定这一 tick 按哪个方向。

        注意：这里输出的是“按键方向”，不是“期望速度方向”。当速度方向与
        目标相反时，按键方向通常就是目标方向（反向刹车）。
        """
        abs_error = abs(error_px)
        abs_speed = abs(speed_px_s)

        # 已经进入容差且速度足够低：不再输出方向，由主循环判定成功。
        if (
            abs_error <= self._config.position_tolerance_px
            and abs_speed <= self._config.settle_speed_px_s
        ):
            return None

        # 预测制动距离：反应延迟内还会前进的距离 + 以 a 减速到 0 的距离。
        braking_distance = (
            abs_speed * self._config.brake_lookahead_s
            + (speed_px_s * speed_px_s) / (2.0 * self._config.brake_deceleration_px_s2)
        )

        # 目标已进入制动区：用与当前速度相反的方向刹车。
        # 若速度已经很低，则退回“朝目标方向轻推”，避免永远停在容差外。
        if abs_error <= braking_distance:
            if abs_speed > self._config.settle_speed_px_s:
                return Direction.LEFT if speed_px_s > 0 else Direction.RIGHT
            return Direction.RIGHT if error_px > 0 else Direction.LEFT

        # 正常跟踪：朝目标方向按键。若当前速度反向，这个按键同时也起刹车作用。
        return Direction.RIGHT if error_px > 0 else Direction.LEFT

    def _move_result(
        self,
        status: str,
        target_x_tiles: float,
        snapshot: PlayerSnapshot | None,
        elapsed_s: float,
    ) -> MoveResult:
        final_x_tiles = snapshot.center_x_tiles if snapshot is not None else float("nan")
        error_tiles = (
            target_x_tiles - final_x_tiles
            if snapshot is not None
            else float("nan")
        )
        return MoveResult(
            status=status,
            target_x_tiles=target_x_tiles,
            final_x_tiles=final_x_tiles,
            error_tiles=error_tiles,
            elapsed_s=elapsed_s,
        )

    # ------------------------------------------------------------------
    # 5.3 跳跃：必须监控 Y 轴，不能“按完空格就当成功”
    # ------------------------------------------------------------------

    def jump(self) -> None:
        """原地短按一次空格。只负责触发，不等待落地。"""
        with self._action_lock:
            self._ensure_open()
            if self._cancel.is_set():
                raise ActionCancelled("动作已取消")
            self._ensure_game_focus()
            self._tap_key(JUMP_KEY)

    def jump_and_wait_for_landing(
        self,
        *,
        direction: Direction | None = None,
        timeout_s: float | None = None,
    ) -> JumpResult:
        """起跳并等待重新着地，同时记录实际跳跃高度。

        这是 ``jump_over_obstacle(0.0, ...)`` 的语义化别名。
        """
        return self.jump_over_obstacle(
            0.0, direction=direction, timeout_s=timeout_s
        )

    def face_direction(self, direction: Direction) -> None:
        """短暂按方向键，让角色正脸朝向目标，而不是用背对着树。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            try:
                self._set_horizontal(direction)
                self._sleep(0.15)
            finally:
                self._release_horizontal()

    def hop_toward(
        self,
        target_x_tiles: float,
        *,
        hold_s: float = 0.20,
        timeout_s: float = 2.5,
    ) -> JumpResult:
        """朝目标格跳一次；跳到目标 x 附近就松开方向键，避免冲过头。

        跳 1 格小台阶时，“一直按着方向键”会直接飞过去，还得再跳回来。
        这里在空中持续读 x：到达目标附近立刻松方向键，然后原地落地。
        """
        with self._action_lock:
            self._ensure_open()
            if self._cancel.is_set():
                return JumpResult(False, 0.0, 0.0, "动作已取消")
            self._ensure_game_focus()
            started_at = self._clock()
            if not self._wait_until_grounded(started_at + timeout_s):
                return JumpResult(False, 0.0, 0.0, "起跳前未能着地")
            start = self._read_snapshot()
            direction = (
                Direction.RIGHT if target_x_tiles > start.center_x_tiles else Direction.LEFT
            )
            self._set_horizontal(direction)
            released = False
            min_y = start.center_y_px
            airborne = False
            takeoff_deadline = self._clock() + self._config.takeoff_timeout_s
            try:
                self._hold_key(JUMP_KEY, hold_s)
                while True:
                    now = self._clock()
                    if self._cancel.is_set():
                        return JumpResult(False, 0.0, 0.0, "动作已取消")
                    if now - started_at > timeout_s:
                        return JumpResult(False, 0.0, now - started_at, "等待落地超时")
                    snap = self._read_snapshot()
                    min_y = min(min_y, snap.center_y_px)
                    if not snap.on_ground:
                        airborne = True
                        if not released:
                            reached = (
                                snap.center_x_tiles >= target_x_tiles - 0.1
                                if direction is Direction.RIGHT
                                else snap.center_x_tiles <= target_x_tiles + 0.1
                            )
                            if reached:
                                self._release_horizontal()
                                released = True
                    elif airborne:
                        height = max(0.0, (start.center_y_px - min_y) / TILE_SIZE_PX)
                        return JumpResult(True, height, now - started_at, "")
                    if not airborne and now > takeoff_deadline:
                        return JumpResult(False, 0.0, now - started_at, "空格未触发离地")
                    self._sleep(self._config.tick_s)
            finally:
                self._release_horizontal()

    def jump_over_obstacle(
        self,
        obstacle_height_tiles: float,
        *,
        direction: Direction | None = None,
        timeout_s: float | None = None,
    ) -> JumpResult:
        """跳过指定高度的障碍物。

        ``obstacle_height_tiles`` 是障碍物顶部相对玩家脚底的高度（格）。
        超过 6 格时直接抛 ``JumpTooHighError``，技能层应改为搭方块或绕路。

        整个过程中持续读取 Y 轴：必须观察到“离地 -> 到达最高点 -> 重新着地”
        才返回成功，而不是按完空格就假定已经落地。
        """
        height = float(obstacle_height_tiles)
        if not math.isfinite(height) or height < 0:
            raise ValueError("obstacle_height_tiles 必须是非负有限数值")
        if height > self._config.max_jump_height_tiles:
            raise JumpTooHighError(
                f"障碍物高度 {height:.2f} 格超过 V1 跳跃上限 "
                f"{self._config.max_jump_height_tiles:.2f} 格，请搭方块或绕路"
            )

        timeout = self._config.jump_timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise ValueError("timeout_s 必须大于 0")

        with self._action_lock:
            self._ensure_open()
            if self._cancel.is_set():
                return JumpResult(False, 0.0, 0.0, "动作已取消")
            self._ensure_game_focus()
            started_at = self._clock()

            try:
                start_snapshot = self._read_snapshot()
                if not self._wait_until_grounded(started_at + timeout):
                    reason = "动作已取消" if self._cancel.is_set() else "起跳前未能着地"
                    return JumpResult(
                        success=False,
                        height_tiles=0.0,
                        duration_s=self._clock() - started_at,
                        reason=reason,
                    )
                start_snapshot = self._read_snapshot()

                if direction is not None:
                    self._set_horizontal(direction)

                # 空格按得越久跳得越高；0.8s 左右到最高。
                hold_s = self._config.jump_key_hold_s
                if height > 0:
                    hold_s = min(
                        self._config.jump_max_hold_s,
                        0.12 + height * self._config.jump_hold_per_tile_s,
                    )
                self._hold_key(JUMP_KEY, hold_s)

                airborne = False
                min_center_y_px = start_snapshot.center_y_px
                takeoff_deadline = self._clock() + self._config.takeoff_timeout_s

                while True:
                    now = self._clock()
                    if self._cancel.is_set():
                        return JumpResult(
                            success=False,
                            height_tiles=max(
                                0.0,
                                (start_snapshot.center_y_px - min_center_y_px)
                                / TILE_SIZE_PX,
                            ),
                            duration_s=now - started_at,
                            reason="动作已取消",
                        )
                    if now - started_at > timeout:
                        return JumpResult(
                            success=False,
                            height_tiles=max(
                                0.0,
                                (start_snapshot.center_y_px - min_center_y_px)
                                / TILE_SIZE_PX,
                            ),
                            duration_s=now - started_at,
                            reason="等待落地超时",
                        )

                    snapshot = self._read_snapshot()
                    min_center_y_px = min(min_center_y_px, snapshot.center_y_px)

                    if not snapshot.on_ground:
                        airborne = True
                    elif airborne:
                        # 已经观察到离地，并再次着地：跳跃闭环完成。
                        actual_height = max(
                            0.0,
                            (start_snapshot.center_y_px - min_center_y_px)
                            / TILE_SIZE_PX,
                        )
                        logger.info(
                            "跳跃完成: 要求 %.2f 格, 实测 %.2f 格, 用时 %.3fs",
                            height,
                            actual_height,
                            now - started_at,
                        )
                        return JumpResult(
                            success=True,
                            height_tiles=actual_height,
                            duration_s=now - started_at,
                        )

                    if not airborne and now > takeoff_deadline:
                        return JumpResult(
                            success=False,
                            height_tiles=0.0,
                            duration_s=now - started_at,
                            reason="空格未触发离地（游戏未聚焦/被眩晕/输入被拦截）",
                        )

                    self._sleep(self._config.tick_s)

            except Exception:
                self._release_horizontal()
                raise
            finally:
                self._release_horizontal()

    def _wait_until_grounded(self, deadline: float) -> bool:
        while True:
            if self._cancel.is_set():
                return False
            if self._read_snapshot().on_ground:
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(self._config.tick_s)

    # ------------------------------------------------------------------
    # 5.4 放置 / 挖掘：先校验“水平 5 格、垂直 5 格”
    # ------------------------------------------------------------------

    def attack_screen(self, screen_x: int, screen_y: int, button: str = "left") -> None:
        """快速把鼠标移到屏幕目标并点击；用于近身战斗。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self._backend.mouse_move(int(screen_x), int(screen_y))
            self._sleep(0.03)
            self._backend.mouse_click(button)

    def begin_hold_attack(self) -> None:
        """按下左键并保持：矿车/站桩持续攻击用，整个过程中不松开。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self._backend.mouse_down("left")
            self._hold_attack = True
            logger.info("持续攻击：左键已按下")

    def _game_client_rect(self) -> tuple[int, int, int, int] | None:
        try:
            from game_window import client_rect, find_window

            backend = self._backend
            titles = getattr(backend, "_window_titles", ("Terraria", "泰拉瑞亚"))
            exe = getattr(backend, "_exe_path", "")
            window = find_window(titles, exe_path=exe)
            hwnd = int(getattr(window, "_hWnd", 0) or 0)
            if hwnd:
                return tuple(int(v) for v in client_rect(hwnd))  # type: ignore[return-value]
        except Exception:
            pass
        return None

    def aim_screen(self, screen_x: int, screen_y: int) -> tuple[int, int, bool]:
        """只移动鼠标（左键保持按住），并把目标夹在游戏窗口内。

        返回 (实际坐标x, 实际坐标y, 是否被夹住)。怪在屏幕外时坐标会被夹到
        窗口边缘，避免鼠标飞到任务栏/桌面上“锁着不动”。
        """
        with self._action_lock:
            self._ensure_open()
            if not self._hold_attack:
                self._ensure_game_focus()
            x, y = int(screen_x), int(screen_y)
            clamped = False
            rect = self._game_client_rect()
            if rect:
                left, top, width, height = rect
                margin = 6
                nx = max(left + margin, min(left + width - margin, x))
                ny = max(top + margin, min(top + height - margin, y))
                clamped = (nx != x) or (ny != y)
                x, y = nx, ny
            self._backend.mouse_move(x, y)
            return x, y, clamped

    def end_hold_attack(self) -> None:
        """松开持续攻击的左键。"""
        with self._action_lock:
            try:
                self._backend.mouse_up("left")
            finally:
                self._hold_attack = False
            logger.info("持续攻击：左键已松开")

    def hold_direction(self, direction: Direction | None) -> None:
        """按住某个方向键（矿车绕圈时用），None 表示松开。"""
        with self._action_lock:
            if direction is None:
                self._release_horizontal()
            else:
                self._set_horizontal(direction)

    def attack_move(
        self,
        screen_x: int,
        screen_y: int,
        *,
        direction: Direction | None = None,
        hold_s: float = 0.45,
        button: str = "left",
    ) -> None:
        """一边朝敌人走（按住方向键），一边把鼠标压在敌人身上按住左键挥砍。

        这是“追着怪打”的基本动作：一次持续约 0.45 秒，然后由技能层重新读怪物位置
        再调一次，形成边追边砍的循环。按住左键比反复点击更稳（泰拉瑞亚近战按住
        会连续挥动），也不会因为点击间隔太短被游戏吞掉。
        """
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            if direction is not None:
                self._set_horizontal(direction)
            try:
                self._backend.mouse_move(int(screen_x), int(screen_y))
                self._sleep(0.05)
                if not self._cursor_near(int(screen_x), int(screen_y)):
                    raise InputBackendError(
                        f"鼠标没有移到敌人位置 ({int(screen_x)},{int(screen_y)})："
                        "可能游戏不在前台，或者游戏以管理员权限运行、本进程权限不够"
                    )
                self._backend.mouse_down(button)
                try:
                    self._sleep(max(0.05, float(hold_s)))
                finally:
                    self._backend.mouse_up(button)
            finally:
                self._release_horizontal()

    def aim_tile(
        self,
        tile_x: int,
        tile_y: int,
        *,
        screen_mapper: Callable[[float, float], tuple[int, int]],
    ) -> None:
        """把鼠标移到目标格，不点击；用于长按使用物品。"""
        self._aim_tile(
            tile_x,
            tile_y,
            screen_mapper=screen_mapper,
            max_range_tiles=self._config.max_mine_range_tiles,
        )

    def hold_mouse(
        self,
        button: str,
        *,
        until: Callable[[], bool],
        timeout_s: float = 4.0,
    ) -> bool:
        """按住鼠标直到 until() 为真，或超时；真实后端支持长按。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            down = getattr(self._backend, "mouse_down", None)
            up = getattr(self._backend, "mouse_up", None)
            deadline = self._clock() + max(0.2, float(timeout_s))
            try:
                if callable(down) and callable(up):
                    down(button)
                    while not self._cancel.is_set():
                        if until():
                            return True
                        if self._clock() >= deadline:
                            return False
                        self._sleep(0.08)
                    return False
                # 测试/旧后端没有 down/up 时，用重复点击模拟按住。
                while not self._cancel.is_set():
                    if until():
                        return True
                    if self._clock() >= deadline:
                        return False
                    self._backend.mouse_click(button)
                    self._sleep(0.20)
                return False
            finally:
                if callable(up):
                    try:
                        up(button)
                    except Exception:
                        logger.exception("释放鼠标按键失败")

    def _aim_tile(
        self,
        tile_x: int,
        tile_y: int,
        *,
        screen_mapper: Callable[[float, float], tuple[int, int]],
        max_range_tiles: float,
    ) -> None:
        self._ensure_open()
        self._ensure_game_focus()
        if self._cancel.is_set():
            raise ActionCancelled("动作已取消")
        snapshot = self._read_snapshot()
        horizontal, vertical = self._range_to_tile(snapshot, tile_x, tile_y)
        if horizontal > max_range_tiles or vertical > max_range_tiles:
            raise OutOfRangeError(
                f"目标格 ({tile_x}, {tile_y}) 超出交互范围: "
                f"水平 {horizontal:.2f} 格 / 垂直 {vertical:.2f} 格，"
                f"上限 {max_range_tiles:.1f} 格"
            )
        world_x = (tile_x + 0.5) * TILE_SIZE_PX
        world_y = (tile_y + 0.5) * TILE_SIZE_PX
        screen_x, screen_y = screen_mapper(world_x, world_y)
        self._backend.mouse_move(int(screen_x), int(screen_y))

    def mine_tile(
        self,
        tile_x: int,
        tile_y: int,
        *,
        screen_mapper: Callable[[float, float], tuple[int, int]],
        button: str = "left",
    ) -> None:
        """在指定网格坐标执行一次挖掘点击。

        ``screen_mapper`` 负责把世界像素坐标转换为屏幕像素坐标，通常由
        GameMonitor 提供（需要相机偏移和缩放）。执行层不猜测屏幕映射。
        """
        self._click_tile(
            tile_x,
            tile_y,
            screen_mapper=screen_mapper,
            button=button,
            max_range_tiles=self._config.max_mine_range_tiles,
            check_player_body=False,
        )

    def place_block(
        self,
        tile_x: int,
        tile_y: int,
        *,
        screen_mapper: Callable[[float, float], tuple[int, int]],
        button: str = "left",
    ) -> None:
        """在指定网格坐标放置方块（默认左键使用手持方块）。

        放置前检查目标是否落在玩家 2x3 身体内，避免把方块放进自己脚下。
        是否持有方块、是否选中了正确物品由技能层负责；若玩家改过键位，
        可通过 ``button`` 覆盖。
        """
        # 放置时必须关闭智能光标，按鼠标指向的格子放。
        self.smart_cursor_up()
        self._click_tile(
            tile_x,
            tile_y,
            screen_mapper=screen_mapper,
            button=button,
            max_range_tiles=self._config.max_place_range_tiles,
            check_player_body=True,
            hold_s=0.40,
        )

    def place_slide(
        self,
        support_x: int,
        support_y: int,
        target_x: int,
        target_y: int,
        *,
        screen_mapper: Callable[[float, float], tuple[int, int]],
    ) -> dict:
        """从已有支撑方块按住鼠标滑到目标格，用于左/右/上建房。

        返回本次点击实际用到的坐标，方便上层回执和排查。
        按下鼠标前会检查“光标真的移到了支撑方块”：如果游戏以管理员运行、
        而本进程权限较低，SendInput 会被系统静默丢弃，鼠标不动但也不报错——
        这种情况在这里明确失败，而不是假装放过了。
        """
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self.smart_cursor_up()
            snapshot = self._read_snapshot()
            horizontal, vertical = self._range_to_tile(snapshot, support_x, support_y)
            if horizontal > self._config.max_place_range_tiles or vertical > self._config.max_place_range_tiles:
                raise OutOfRangeError("支撑方块超出放置范围")
            sx, sy = screen_mapper((support_x + 0.5) * TILE_SIZE_PX, (support_y + 0.5) * TILE_SIZE_PX)
            tx, ty = screen_mapper((target_x + 0.5) * TILE_SIZE_PX, (target_y + 0.5) * TILE_SIZE_PX)
            sx, sy, tx, ty = int(sx), int(sy), int(tx), int(ty)
            self._backend.mouse_move(sx, sy)
            self._sleep(0.18)
            if not self._cursor_near(sx, sy):
                cursor = self._cursor_pos()
                raise InputBackendError(
                    f"鼠标没有移到支撑方块 ({sx},{sy})，现在停在 {cursor}："
                    "可能游戏不在前台，或者游戏以管理员权限运行、本进程权限不够"
                )
            logger.info(
                "place_slide: support=(%s,%s) target=(%s,%s) screen=(%s,%s)->(%s,%s)",
                support_x, support_y, target_x, target_y, sx, sy, tx, ty,
            )
            self._backend.mouse_down("left")
            try:
                steps = 8
                for i in range(1, steps + 1):
                    x = int(round(sx + (tx - sx) * i / steps))
                    y = int(round(sy + (ty - sy) * i / steps))
                    self._backend.mouse_move(x, y)
                    self._sleep(0.06)
                self._sleep(0.20)
            finally:
                self._backend.mouse_up("left")
            self._sleep(0.30)
            return {
                "support": [support_x, support_y],
                "target": [target_x, target_y],
                "screen": [sx, sy],
                "end_screen": [tx, ty],
            }

    def _cursor_pos(self) -> tuple[int, int]:
        try:
            import win32api

            return tuple(int(v) for v in win32api.GetCursorPos())
        except Exception:
            return (-1, -1)

    def _cursor_near(self, x: int, y: int, tolerance: int = 4) -> bool:
        """光标是否真的到了 (x,y) 附近；读不到光标时不阻塞动作。"""
        try:
            import win32api

            cx, cy = win32api.GetCursorPos()
        except Exception:
            return True
        return abs(int(cx) - int(x)) <= tolerance and abs(int(cy) - int(y)) <= tolerance

    def place_wall_at(
        self,
        tile_x: int,
        tile_y: int,
        *,
        screen_mapper: Callable[[float, float], tuple[int, int]],
        hold_s: float = 0.18,
    ) -> None:
        """把手上的背景墙放到指定格：墙不需要支撑，直接点目标格即可。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            snapshot = self._read_snapshot()
            horizontal, vertical = self._range_to_tile(snapshot, tile_x, tile_y)
            if horizontal > self._config.max_place_range_tiles or vertical > self._config.max_place_range_tiles:
                raise OutOfRangeError(
                    f"墙目标 ({tile_x},{tile_y}) 超出范围：水平 {horizontal:.2f} / 垂直 {vertical:.2f} 格"
                )
            world_x = (tile_x + 0.5) * TILE_SIZE_PX
            world_y = (tile_y + 0.5) * TILE_SIZE_PX
            sx, sy = screen_mapper(world_x, world_y)
            sx, sy = int(sx), int(sy)
            self._backend.mouse_move(sx, sy)
            self._sleep(0.12)
            if not self._cursor_near(sx, sy):
                raise InputBackendError(f"鼠标没有移到墙目标 ({sx},{sy})")
            self._backend.mouse_down("left")
            try:
                self._sleep(max(0.05, float(hold_s)))
            finally:
                self._backend.mouse_up("left")
            self._sleep(0.15)

    def place_furniture(self, base_x: int, base_y: int, *, screen_mapper: Callable[[float, float], tuple[int, int]]) -> None:
        """家具放置：鼠标先点地面块，再按住缓慢向上滑一格。"""
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()
            self.smart_cursor_up()
            snapshot = self._read_snapshot()
            horizontal, vertical = self._range_to_tile(snapshot, base_x, base_y)
            if horizontal > self._config.max_place_range_tiles or vertical > self._config.max_place_range_tiles:
                raise OutOfRangeError(f"地面格 ({base_x},{base_y}) 超出放置范围")
            sx, sy = screen_mapper((base_x + 0.5) * TILE_SIZE_PX, (base_y + 0.5) * TILE_SIZE_PX)
            self._backend.mouse_move(int(sx), int(sy))
            self._sleep(0.20)
            self._backend.mouse_down("left")
            try:
                for step in range(1, 9):
                    self._backend.mouse_move(int(sx), int(sy - step * 2))
                    self._sleep(0.06)
                self._sleep(0.25)
            finally:
                self._backend.mouse_up("left")
            self._sleep(0.35)

    def can_mine_tile(self, tile_x: int, tile_y: int) -> bool:
        return self._within_range(
            self._read_snapshot(),
            tile_x,
            tile_y,
            self._config.max_mine_range_tiles,
        )

    def can_place_tile(self, tile_x: int, tile_y: int) -> bool:
        snapshot = self._read_snapshot()
        return (
            not snapshot.body_tiles.contains(tile_x, tile_y)
            and self._within_range(
                snapshot,
                tile_x,
                tile_y,
                self._config.max_place_range_tiles,
            )
        )

    def _click_tile(
        self,
        tile_x: int,
        tile_y: int,
        *,
        screen_mapper: Callable[[float, float], tuple[int, int]],
        button: str,
        max_range_tiles: float,
        check_player_body: bool,
        hold_s: float = 0.0,
    ) -> None:
        with self._action_lock:
            self._ensure_open()
            self._ensure_game_focus()

            if self._cancel.is_set():
                raise ActionCancelled("动作已取消")

            snapshot = self._read_snapshot()
            if check_player_body and snapshot.body_tiles.contains(tile_x, tile_y):
                raise InvalidTargetError(
                    f"目标格 ({tile_x}, {tile_y}) 位于玩家 2x3 碰撞体内"
                )

            horizontal, vertical = self._range_to_tile(snapshot, tile_x, tile_y)
            if horizontal > max_range_tiles or vertical > max_range_tiles:
                raise OutOfRangeError(
                    f"目标格 ({tile_x}, {tile_y}) 超出交互范围: "
                    f"水平 {horizontal:.2f} 格 / 垂直 {vertical:.2f} 格，"
                    f"上限 {max_range_tiles:.1f} 格"
                )

            world_x = (tile_x + 0.5) * TILE_SIZE_PX
            world_y = (tile_y + 0.5) * TILE_SIZE_PX
            try:
                screen_x, screen_y = screen_mapper(world_x, world_y)
            except Exception as exc:
                raise ExecutorError("世界坐标到屏幕坐标转换失败") from exc

            try:
                self._backend.mouse_move(int(screen_x), int(screen_y))
                self._sleep(0.12)
                if hold_s > 0:
                    self._backend.mouse_down(button)
                    try:
                        self._sleep(float(hold_s))
                    finally:
                        self._backend.mouse_up(button)
                else:
                    self._backend.mouse_click(button)
            except Exception as exc:
                raise InputBackendError("鼠标点击发送失败") from exc

    def _within_range(
        self,
        snapshot: PlayerSnapshot,
        tile_x: int,
        tile_y: int,
        max_range_tiles: float,
    ) -> bool:
        horizontal, vertical = self._range_to_tile(snapshot, tile_x, tile_y)
        return horizontal <= max_range_tiles and vertical <= max_range_tiles

    @staticmethod
    def _range_to_tile(
        snapshot: PlayerSnapshot,
        tile_x: int,
        tile_y: int,
    ) -> tuple[float, float]:
        """返回玩家中心到目标格中心的水平/垂直距离（格）。"""
        target_x = (tile_x + 0.5) * TILE_SIZE_PX
        target_y = (tile_y + 0.5) * TILE_SIZE_PX
        horizontal = abs(target_x - snapshot.center_x_px) / TILE_SIZE_PX
        vertical = abs(target_y - snapshot.center_y_px) / TILE_SIZE_PX
        return horizontal, vertical

    # ------------------------------------------------------------------
    # 5.5 原始键鼠状态管理（所有方法必须在动作锁内调用）
    # ------------------------------------------------------------------

    def _set_horizontal(self, direction: Direction | None) -> None:
        """把水平按键切换到指定方向；``None`` 表示全部释放。"""
        if direction is None:
            self._release_horizontal()
            return

        desired_key = LEFT_KEY if direction is Direction.LEFT else RIGHT_KEY
        for key in (LEFT_KEY, RIGHT_KEY):
            if key != desired_key:
                self._release_key(key)

        if desired_key not in self._held_keys:
            self._press_key(desired_key)

        self._horizontal = direction

    def _release_horizontal(self) -> None:
        self._release_key(LEFT_KEY)
        self._release_key(RIGHT_KEY)
        self._horizontal = None

    def _press_key(self, key: str) -> None:
        if key in self._held_keys:
            return
        try:
            self._backend.key_down(key)
        except Exception as exc:
            raise InputBackendError(f"按下按键 {key!r} 失败") from exc
        self._held_keys.add(key)

    def _release_key(self, key: str) -> None:
        if key not in self._held_keys:
            return
        try:
            self._backend.key_up(key)
        except Exception:  # 清理阶段不掩盖主异常
            logger.exception("释放按键 %r 失败", key)
        finally:
            self._held_keys.discard(key)

    def _tap_key(self, key: str) -> None:
        """短按一个键。仅控制“按下时长”，不用于控制角色移动时长。"""
        self._hold_key(key, self._config.jump_key_hold_s)

    def _hold_key(self, key: str, duration_s: float) -> None:
        self._press_key(key)
        try:
            self._sleep(max(0.0, float(duration_s)))
        finally:
            self._release_key(key)


__all__ = [
    "ActionCancelled",
    "ActionExecutor",
    "BodyTiles",
    "Direction",
    "ExecutorConfig",
    "ExecutorError",
    "InputBackend",
    "InputBackendError",
    "InvalidTargetError",
    "JumpResult",
    "JumpTooHighError",
    "MoveResult",
    "OutOfRangeError",
    "PlayerSnapshot",
    "PlayerStateReader",
    "PyDirectInputBackend",
    "StateReadError",
]
