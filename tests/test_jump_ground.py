"""跳跃相关测试：落地判定（不能把“跳到最高点”当成落地）和“跳上方块”动作。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from executor import Direction, JumpResult  # noqa: E402
from monitor import MemoryGameMonitor  # noqa: E402
from skills import SkillContext, JumpOntoTileSkill  # noqa: E402


class FakeReader:
    def __init__(self, data):
        self.data = data
        self.is_ready = True

    def latest(self, max_age_s: float = 1.0):
        return self.data

    def ensure_started(self):
        return True


def make_data(*, feet_solid: bool, vy: float) -> dict:
    """构造一帧 bridge 数据：31×31 全是空气，只有 [17][15] 是地面（可选）。"""
    air = {"has_tile": False, "tile_type": 0, "has_wall": False, "wall_type": 0}
    ground = {"has_tile": True, "tile_type": 0, "has_wall": False, "wall_type": 0}
    tiles = [[dict(air) for _ in range(31)] for _ in range(31)]
    if feet_solid:
        tiles[17][15] = dict(ground)
    # raw_y = 16*100-42 → 脚底正好落在 y=1600（第 100 格顶边），中心格 = 98
    return {
        "ok": True,
        "x": 1600.0,
        "y": 1558.0,
        "vx": 0.0,
        "vy": vy,
        "hp": 100,
        "inventory": [],
        "enemies": [],
        "tiles": tiles,
        "ui_scale": 1.0,
        "inventory_open": False,
    }


class OnGroundTest(unittest.TestCase):
    def build(self, *, feet_solid: bool, vy: float) -> MemoryGameMonitor:
        return MemoryGameMonitor(
            FakeReader(make_data(feet_solid=feet_solid, vy=vy)), grid_radius=15
        )

    def test_standing_on_solid_ground(self) -> None:
        monitor = self.build(feet_solid=True, vy=0.0)
        self.assertTrue(monitor.read_player_state().on_ground)

    def test_apex_over_air_is_not_grounded(self) -> None:
        # 跳到最高点：vy≈0，但脚下是空气 → 不能算落地。
        monitor = self.build(feet_solid=False, vy=0.0)
        self.assertFalse(monitor.read_player_state().on_ground)

    def test_moving_vertically_is_not_grounded(self) -> None:
        monitor = self.build(feet_solid=True, vy=-200.0)
        self.assertFalse(monitor.read_player_state().on_ground)


class FakeJumpMonitor:
    def __init__(self, start_tile, block_tile):
        self.tile = start_tile
        self.block = block_tile
        self.jumps = []

    def tile_at(self, x, y):
        return "D" if (x, y) == self.block else "."

    @property
    def player_tile(self):
        return self.tile

    def set_action(self, action):
        pass

    def get_state(self):
        return {}


class FakeJumpExecutor:
    def __init__(self, monitor):
        self.monitor = monitor
        self.moves = []

    def move_to_x(self, target, timeout_s=3.0):
        self.moves.append(target)

        class R:
            success = True
            status = "success"

        return R()

    def get_current_position(self):
        class S:
            center_x_tiles = float(self.monitor.tile[0])

        return S()

    def jump_over_obstacle(self, height, *, direction=None, timeout_s=3.0):
        self.monitor.jumps.append((height, direction))
        bx, by = self.monitor.block
        # 踩上方块顶：中心格 = 方块上方两格
        self.monitor.tile = (bx, by - 2)
        return JumpResult(True, 2.0, 0.6, "")


class JumpOntoTest(unittest.TestCase):
    def test_jump_onto_block_succeeds(self) -> None:
        monitor = FakeJumpMonitor(start_tile=(3, 5), block_tile=(5, 5))
        executor = FakeJumpExecutor(monitor)
        context = SkillContext(monitor=monitor, executor=executor, config={})
        result = JumpOntoTileSkill().run(context, x=5, y=5)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.data["tile"], [5, 5])
        self.assertTrue(monitor.jumps)
        self.assertEqual(monitor.jumps[0][1], Direction.RIGHT)

    def test_jump_onto_missing_block_is_rejected(self) -> None:
        monitor = FakeJumpMonitor(start_tile=(3, 5), block_tile=(5, 5))
        context = SkillContext(monitor=monitor, executor=FakeJumpExecutor(monitor), config={})
        result = JumpOntoTileSkill().run(context, x=9, y=9)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "no_block")


if __name__ == "__main__":
    unittest.main()
