"""ActionExecutor 的离线单元测试（仅依赖 Python 标准库）。

不依赖 Windows、不依赖 pydirectinput、不移动真实鼠标。用一个极简的
一维物理模拟器驱动闭环控制器，验证“到达目标、不过冲、松键、2x3 碰撞体、
跳跃上限”等 V1 核心约束。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from executor import (  # noqa: E402
    ActionExecutor,
    Direction,
    ExecutorConfig,
    InputBackendError,
    InvalidTargetError,
    JumpTooHighError,
    PlayerSnapshot,
    TILE_SIZE_PX,
)


class FakePhysics:
    """一维简化角色物理，用于验证控制律。"""

    ACCEL = 2000.0
    FRICTION = 3000.0
    MAX_SPEED = 240.0

    def __init__(self) -> None:
        self.x_px = 0.0
        self.vx_px_s = 0.0
        self.on_ground = True
        self.held: set[str] = set()

    def step(self, dt: float) -> None:
        if "a" in self.held and "d" not in self.held:
            self.vx_px_s -= self.ACCEL * dt
        elif "d" in self.held and "a" not in self.held:
            self.vx_px_s += self.ACCEL * dt
        else:
            if self.vx_px_s > 0:
                self.vx_px_s = max(0.0, self.vx_px_s - self.FRICTION * dt)
            else:
                self.vx_px_s = min(0.0, self.vx_px_s + self.FRICTION * dt)

        self.vx_px_s = max(-self.MAX_SPEED, min(self.MAX_SPEED, self.vx_px_s))
        self.x_px += self.vx_px_s * dt


class FakeClock:
    def __init__(self, physics: FakePhysics, start: float = 100.0) -> None:
        self.now = start
        self.physics = physics

    def __call__(self) -> float:
        return self.now

    def sleep(self, dt: float) -> None:
        self.now += dt
        self.physics.step(dt)


class FakeReader:
    def __init__(self, physics: FakePhysics, clock: FakeClock) -> None:
        self.physics = physics
        self.clock = clock

    def read_player_state(self) -> PlayerSnapshot:
        return PlayerSnapshot(
            center_x_px=self.physics.x_px,
            center_y_px=64.0,
            velocity_x_px_s=self.physics.vx_px_s,
            on_ground=self.physics.on_ground,
            timestamp=self.clock(),
        )


class FakeBackend:
    def __init__(self, physics: FakePhysics) -> None:
        self.physics = physics
        self.clicks: list[tuple[str, int, int]] = []
        self.typed: list[str] = []
        self.mouse_position = (0, 0)

    def key_down(self, key: str) -> None:
        self.physics.held.add(key)

    def key_up(self, key: str) -> None:
        self.physics.held.discard(key)

    def mouse_move(self, x: int, y: int) -> None:
        self.mouse_position = (x, y)

    def mouse_click(self, button: str = "left") -> None:
        self.clicks.append((button, *self.mouse_position))

    def release_all(self, keys) -> None:
        self.physics.held.difference_update(keys)

    def type_text(self, text: str) -> None:
        self.typed.append(text)

    def ensure_game_focus(self) -> None:
        """测试中不做窗口操作。"""
        return None


class ExecutorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.physics = FakePhysics()
        self.clock = FakeClock(self.physics)
        self.reader = FakeReader(self.physics, self.clock)
        self.backend = FakeBackend(self.physics)
        self.executor = ActionExecutor(
            self.reader,
            self.backend,
            ExecutorConfig(),
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def tearDown(self) -> None:
        self.executor.close()

    def test_move_to_x_reaches_target_and_releases_keys(self) -> None:
        result = self.executor.move_to_x(10.0, timeout_s=5.0)

        self.assertTrue(result.success, result)
        self.assertLessEqual(
            abs(result.error_tiles),
            self.executor._config.position_tolerance_tiles + 1e-6,
        )
        self.assertLessEqual(
            abs(self.physics.vx_px_s),
            self.executor._config.settle_speed_px_s,
        )
        self.assertFalse({"a", "d"} & self.physics.held)
        self.assertGreater(result.elapsed_s, 0)

    def test_move_to_x_works_in_negative_direction(self) -> None:
        result = self.executor.move_to_x(-6.0, timeout_s=5.0)

        self.assertTrue(result.success, result)
        self.assertLessEqual(abs(result.final_x_tiles - (-6.0)), 0.16)

    def test_move_stall_returns_status_without_stuck_keys(self) -> None:
        class FrozenReader(FakeReader):
            def read_player_state(self) -> PlayerSnapshot:
                snapshot = super().read_player_state()
                # 模拟被墙挡住：坐标不动、速度始终为 0。
                return PlayerSnapshot(
                    center_x_px=0.0,
                    center_y_px=snapshot.center_y_px,
                    velocity_x_px_s=0.0,
                    on_ground=True,
                    timestamp=snapshot.timestamp,
                )

        executor = ActionExecutor(
            FrozenReader(self.physics, self.clock),
            self.backend,
            ExecutorConfig(),
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        result = executor.move_to_x(10.0, timeout_s=2.0)

        self.assertFalse(result.success)
        self.assertIn(result.status, {"stalled", "timeout"})
        self.assertFalse({"a", "d"} & self.physics.held)

    def test_body_tiles_are_exactly_2x3(self) -> None:
        snapshot = PlayerSnapshot(center_x_px=32.0, center_y_px=48.0)
        body = snapshot.body_tiles

        # x=32 恰好是两格边界：按约定归入左列（第 1 列）。
        self.assertEqual(body.right - body.left + 1, 2)
        self.assertEqual(body.bottom - body.top + 1, 3)
        self.assertEqual(body.center, (1, 3))
        self.assertTrue(body.contains(1, 3))
        self.assertEqual(len(list(body.iter_tiles())), 6)

    def test_jump_above_six_tiles_is_rejected(self) -> None:
        with self.assertRaises(JumpTooHighError):
            self.executor.jump_over_obstacle(6.5, direction=Direction.RIGHT)
        self.assertFalse({"a", "d"} & self.physics.held)

    def test_place_block_rejects_player_body(self) -> None:
        body = self.executor.get_current_position().body_tiles
        with self.assertRaises(InvalidTargetError):
            self.executor.place_block(
                body.left + 1,
                body.top + 1,
                screen_mapper=lambda x, y: (0, 0),
            )
        self.assertEqual(self.backend.clicks, [])

    def test_focus_failure_sends_no_keys(self) -> None:
        class FailingFocusBackend(FakeBackend):
            def ensure_game_focus(self) -> None:
                raise InputBackendError("窗口未聚焦")

        backend = FailingFocusBackend(self.physics)
        executor = ActionExecutor(
            self.reader,
            backend,
            ExecutorConfig(),
            clock=self.clock,
            sleep=self.clock.sleep,
        )

        with self.assertRaises(InputBackendError):
            executor.move_to_x(10.0, timeout_s=1.0)

        self.assertFalse({"a", "d"} & self.physics.held)

    def test_send_chat_message_releases_keys_and_types_chinese(self) -> None:
        # 模拟执行器当前确实持有方向键（_held_keys 是执行器的真实状态）
        self.physics.held.update({"a", "d"})
        self.executor._held_keys.update({"a", "d"})
        self.executor.send_chat_message("主人，我到啦！")
        self.assertEqual(self.backend.typed, ["主人，我到啦！"])
        self.assertFalse({"a", "d"} & self.physics.held)
        self.assertFalse({"enter"} & self.physics.held)

    def test_range_uses_tile_centers(self) -> None:
        snapshot = PlayerSnapshot(center_x_px=0.0, center_y_px=0.0)

        horizontal, vertical = ActionExecutor._range_to_tile(snapshot, 4, 0)

        self.assertTrue(math.isclose(horizontal, 4.5))
        self.assertTrue(math.isclose(vertical, 0.5))
        self.assertEqual(TILE_SIZE_PX, 16.0)


if __name__ == "__main__":
    unittest.main()
