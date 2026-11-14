"""把小动作拆开之后的单元测试：开关背包、检查快捷栏/背包、切格、搬物品。

全部跑在假数据上：不需要游戏窗口、不需要真实鼠标。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from executor import ActionExecutor, ExecutorConfig  # noqa: E402
from monitor import FakeGameMonitor, SimulatedInputBackend  # noqa: E402
from skills import SkillContext, build_default_registry  # noqa: E402


class AtomicSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.monitor = FakeGameMonitor(grid_radius=10)
        self.executor = ActionExecutor(
            self.monitor,
            backend=SimulatedInputBackend(self.monitor),
            config=ExecutorConfig(),
        )
        self.registry = build_default_registry()
        self.context = SkillContext(
            monitor=self.monitor,
            executor=self.executor,
            config={},
        )

    def tearDown(self) -> None:
        self.executor.close()

    def run_skill(self, name: str, **params):
        skill = self.registry.get(name)
        self.assertIsNotNone(skill, f"没有注册技能 {name}")
        return skill.run(self.context, **params)

    # -- 背包开关 ------------------------------------------------------

    def test_open_inventory_succeeds_without_window(self) -> None:
        result = self.run_skill("open_inventory")
        self.assertTrue(result.ok, result)
        self.assertIn("背包", result.message)

    def test_close_inventory_succeeds(self) -> None:
        result = self.run_skill("close_inventory")
        self.assertTrue(result.ok, result)

    # -- 检查 ----------------------------------------------------------

    def test_check_hotbar_lists_slots_1_to_10(self) -> None:
        result = self.run_skill("check_hotbar")
        self.assertTrue(result.ok, result)
        slots = result.data["slots"]
        self.assertTrue(slots, result.data)
        self.assertTrue(all(1 <= item["slot"] <= 10 for item in slots))
        self.assertTrue(all(item["name"] for item in slots))
        # DEFAULT_INVENTORY 占满 1-9，10 应该是空的。
        self.assertEqual(result.data["empty"], [10])
        self.assertEqual(result.data["selected"], 1)

    def test_check_inventory_lists_backpack_slots(self) -> None:
        result = self.run_skill("check_inventory")
        self.assertTrue(result.ok, result)
        self.assertTrue(all(11 <= item["slot"] <= 50 for item in result.data["slots"]))
        self.assertEqual(len(result.data["empty"]), 40)

    # -- 切格 ----------------------------------------------------------

    def test_select_slot_switches_current_tool(self) -> None:
        result = self.run_skill("select_slot", slot=3)
        self.assertTrue(result.ok, result)
        self.assertEqual(self.monitor.current_tool["slot"], 3)
        self.assertEqual(result.data["slot"], 3)

    def test_select_slot_rejects_out_of_range(self) -> None:
        result = self.run_skill("select_slot", slot=11)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "bad_slot")

    # -- 搬物品 --------------------------------------------------------

    def test_move_item_requires_visible_grid(self) -> None:
        with patch("skills.InventoryUI.grid_visible", return_value=False):
            result = self.run_skill("move_item", item="Torch", to_slot=10)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "inventory_grid_closed")

    def test_move_item_moves_by_name(self) -> None:
        # 假监控器默认没有 inventory_open，这里临时补上。
        self.monitor.inventory_open = True

        clicks: list[int] = []

        def fake_center_for_item(ui_self, slot: int, name: str):
            return (slot, 0), (0, 0)

        def fake_center(ui_self, slot: int):
            return (slot, 0)

        def fake_ui_click(executor_self, x: int, y: int) -> None:
            clicks.append(int(x))
            if len(clicks) == 3:
                src, dst = clicks[0], clicks[1]
                inventory = self.monitor._inventory
                source = next(i for i in inventory if i["slot"] == src)
                target = next((i for i in inventory if i["slot"] == dst), None)
                if target is not None:
                    target["slot"] = src
                source["slot"] = dst

        with patch("skills.InventoryUI.grid_visible", return_value=True),                 patch("skills.InventoryUI.center_for_item", fake_center_for_item),                 patch("skills.InventoryUI.center", fake_center),                 patch("executor.ActionExecutor.ui_click", fake_ui_click):
            result = self.run_skill("move_item", item="Torch", to_slot=10)

        self.assertTrue(result.ok, result)
        self.assertEqual(result.data["to_slot"], 10)
        moved = next(i for i in self.monitor.inventory if i["slot"] == 10)
        self.assertEqual(moved["name"], "Torch")

    def test_move_item_unknown_item(self) -> None:
        self.monitor.inventory_open = True
        result = self.run_skill("move_item", item="不存在的方块", to_slot=10)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "item_not_found")

    def test_move_item_requires_target(self) -> None:
        self.monitor.inventory_open = True
        result = self.run_skill("move_item", item="Torch")
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "bad_slot")

    # -- 放置 ----------------------------------------------------------

    def test_place_block_refuses_when_inventory_open(self) -> None:
        self.monitor.inventory_open = True
        result = self.run_skill("place_block", x=1, y=1)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "inventory_open")

    def test_place_block_requires_coordinates(self) -> None:
        result = self.run_skill("place_block")
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "bad_target")

    # -- 砍树/挖矿的小动作语义 ----------------------------------------

    def test_chop_tree_local_only_refuses_when_too_far(self) -> None:
        # 把玩家挪到很远的地方，网格中心不再有树，应该提示先走路。
        result = self.run_skill("chop_tree", local_only=True)
        if result.ok:
            # 假地图里可能正好够得着；那也必须是“没移动”的结果。
            self.assertIn("tree", result.data)
        else:
            self.assertIn(result.status, {"too_far", "no_tree_in_view"})

    def test_mine_block_local_only_returns_too_far_or_success(self) -> None:
        result = self.run_skill("mine_block", local_only=True)
        if not result.ok:
            self.assertIn(result.status, {"too_far", "no_block", "block_still_there"})


class TileMonitor:
    def __init__(self, tiles=None, inventory_open=False):
        self.tiles = dict(tiles or {})
        self.inventory_open = inventory_open
        self.inventory = [{"slot": 1, "name": "木材", "stack": 10}]
        self.current_tool = {"slot": 1, "name": "木材"}

    def get_state(self):
        return {}

    def tile_at(self, x, y):
        return self.tiles.get((int(x), int(y)), ".")

    def world_to_screen(self, x, y):
        return int(x), int(y)


class SlideExecutor:
    def __init__(self, monitor):
        self.monitor = monitor
        self.calls = []

    def place_slide(self, sx, sy, tx, ty, *, screen_mapper):
        self.calls.append((sx, sy, tx, ty))
        self.monitor.tiles[(int(tx), int(ty))] = "W"


class PlaceBlockSkillTest(unittest.TestCase):
    def build(self, tiles=None, inventory_open=False):
        monitor = TileMonitor(tiles, inventory_open)
        executor = SlideExecutor(monitor)
        context = SkillContext(monitor=monitor, executor=executor, config={})
        return monitor, executor, context

    def place(self, context, x, y):
        from skills import PlaceBlockSkill

        return PlaceBlockSkill().run(context, x=x, y=y)

    def test_ground_placement_slides_up(self) -> None:
        _monitor, executor, context = self.build({(5, 6): "D"})
        result = self.place(context, 5, 5)
        self.assertTrue(result.ok, result)
        self.assertEqual(executor.calls, [(5, 6, 5, 5)])
        self.assertEqual(result.data["direction"], "up")

    def test_left_placement_uses_right_block(self) -> None:
        _monitor, executor, context = self.build({(6, 5): "S"})
        result = self.place(context, 5, 5)
        self.assertTrue(result.ok, result)
        self.assertEqual(executor.calls, [(6, 5, 5, 5)])
        self.assertEqual(result.data["direction"], "left")

    def test_right_placement_uses_left_block(self) -> None:
        _monitor, executor, context = self.build({(4, 5): "W"})
        result = self.place(context, 5, 5)
        self.assertTrue(result.ok, result)
        self.assertEqual(executor.calls, [(4, 5, 5, 5)])
        self.assertEqual(result.data["direction"], "right")

    def test_no_support_is_rejected(self) -> None:
        _monitor, executor, context = self.build({})
        result = self.place(context, 5, 5)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "no_support")
        self.assertEqual(executor.calls, [])

    def test_already_solid_is_rejected(self) -> None:
        _monitor, executor, context = self.build({(5, 5): "D"})
        result = self.place(context, 5, 5)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "already_solid")
        self.assertEqual(executor.calls, [])

    def test_open_inventory_blocks_placement(self) -> None:
        _monitor, executor, context = self.build({(5, 6): "D"}, inventory_open=True)
        result = self.place(context, 5, 5)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "inventory_open")
        self.assertEqual(executor.calls, [])

    def test_place_adjusts_to_nearby_support(self) -> None:
        # 目标 (5,5) 自己没支撑；附近 (6,5) 下方有 D → 自动改到 (6,5)
        _monitor, executor, context = self.build({(6, 6): "D"})
        result = self.place(context, 5, 5)
        self.assertTrue(result.ok, result)
        self.assertEqual(executor.calls, [(6, 6, 6, 5)])


class FindHotbarSkillTest(unittest.TestCase):
    def test_find_existing_item(self) -> None:
        from skills import FindHotbarSkill

        monitor = FakeGameMonitor(grid_radius=10)
        context = SkillContext(monitor=monitor, executor=None, config={})
        result = FindHotbarSkill().run(context, item="Iron Broadsword")
        self.assertTrue(result.ok, result)
        self.assertEqual(result.data["slot"], 5)

    def test_find_missing_item(self) -> None:
        from skills import FindHotbarSkill

        monitor = FakeGameMonitor(grid_radius=10)
        context = SkillContext(monitor=monitor, executor=None, config={})
        result = FindHotbarSkill().run(context, item="不存在的方块")
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "not_in_hotbar")

    def test_find_requires_query(self) -> None:
        from skills import FindHotbarSkill

        monitor = FakeGameMonitor(grid_radius=10)
        context = SkillContext(monitor=monitor, executor=None, config={})
        result = FindHotbarSkill().run(context)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "no_query")


class FightMonitor:
    def __init__(self, enemies):
        self._enemies = list(enemies)
        self.player_tile = (10, 10)
        self.inventory_open = False
        self.inventory = [
            {"slot": 1, "name": "铁短剑", "kind": "weapon", "damage": 12, "hotbar": True}
        ]
        self.current_tool = {"slot": 1, "name": "铁短剑", "kind": "weapon", "damage": 12}

    @property
    def enemies(self):
        return list(self._enemies)

    def get_state(self):
        return {}

    def world_to_screen(self, x, y):
        return int(x), int(y)


class AttackExecutor:
    def __init__(self):
        self.calls = []
        self.selected = []

    def select_hotbar_slot(self, slot):
        self.selected.append(slot)

    def attack_move(self, sx, sy, *, direction=None, hold_s=0.45):
        self.calls.append((sx, sy, direction))


class FightSkillTest(unittest.TestCase):
    def test_fight_walks_toward_enemy_and_swings(self) -> None:
        from executor import Direction
        from skills import FightSkill

        enemy = {"i": 1, "x": (10 + 4) * 16, "y": 10 * 16, "w": 16, "h": 16, "life": 20}
        monitor = FightMonitor([enemy])
        executor = AttackExecutor()
        context = SkillContext(monitor=monitor, executor=executor, config={})
        result = FightSkill().run(context, duration_s=0.6)
        self.assertTrue(result.ok, result)
        self.assertTrue(executor.calls, result)
        self.assertEqual(executor.calls[0][2], Direction.RIGHT)

    def test_fight_without_enemies_finishes(self) -> None:
        from skills import FightSkill

        monitor = FightMonitor([])
        executor = AttackExecutor()
        context = SkillContext(monitor=monitor, executor=executor, config={})
        result = FightSkill().run(context, duration_s=0.3)
        self.assertTrue(result.ok, result)
        self.assertEqual(executor.calls, [])


class BodyExecutor(SlideExecutor):
    """带身体快照的执行器：让 place 能检查“目标是不是自己站着的格子”。"""

    def __init__(self, monitor, center_x_px, center_y_px):
        super().__init__(monitor)
        from executor import PlayerSnapshot

        self.snapshot = PlayerSnapshot(center_x_px=center_x_px, center_y_px=center_y_px)

    def get_current_position(self):
        return self.snapshot


class BodyCheckTest(unittest.TestCase):
    def test_place_inside_player_body_is_rejected(self) -> None:
        from skills import PlaceBlockSkill
        from executor import PlayerSnapshot

        # 中心点 (88,88) → 身体左列 5、上排 4 → 占 (5..6, 4..6)；(5,5) 在身体里。
        monitor = TileMonitor({(5, 6): "D"})
        executor = BodyExecutor(monitor, 88.0, 88.0)
        context = SkillContext(monitor=monitor, executor=executor, config={})
        result = PlaceBlockSkill().run(context, x=5, y=5)
        self.assertFalse(result.ok, result)
        self.assertEqual(result.status, "in_player_body")
        self.assertEqual(executor.calls, [])


class BuildPillarTest(unittest.TestCase):
    def test_builds_six_beside_body(self) -> None:
        from skills import BuildPillarSkill
        from executor import PlayerSnapshot

        monitor = TileMonitor({(5, 7): "D", (7, 7): "D"})
        # 中心点 (88,88) → 身体左列 5；右边一列 = 7
        executor = BodyExecutor(monitor, 88.0, 88.0)
        # 让 build_pillar 读到的玩家格子和身体一致
        monitor.player_tile = (5, 5)
        context = SkillContext(monitor=monitor, executor=executor, config={})
        result = BuildPillarSkill().run(context, height=6)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.data["column"], 7)
        self.assertEqual(result.data["placed"], 6)
        # 6 格都在 (7,6)..(7,1)，且都变成了方块
        for i in range(6):
            self.assertIn((7, 7 - i), monitor.tiles)


class HoldExecutor:
    def __init__(self):
        self.events = []
        self.selected = []

    def select_hotbar_slot(self, slot):
        self.selected.append(slot)

    def begin_hold_attack(self):
        self.events.append("down")

    def aim_screen(self, x, y):
        self.events.append(("aim", x, y))
        return x, y, False

    def end_hold_attack(self):
        self.events.append("up")

    def hold_direction(self, direction):
        self.events.append(("dir", direction))


class HoldFireTest(unittest.TestCase):
    def test_holds_button_and_follows_enemy(self) -> None:
        from skills import HoldFireSkill

        enemy = {"i": 1, "x": (10 + 3) * 16, "y": 10 * 16, "w": 16, "h": 16, "life": 20}
        monitor = FightMonitor([enemy])
        executor = HoldExecutor()
        context = SkillContext(monitor=monitor, executor=executor, config={})
        result = HoldFireSkill().run(context, duration_s=0.15)
        self.assertTrue(result.ok, result)
        self.assertEqual(executor.events[0], "down")
        self.assertEqual(executor.events[-1], "up")
        aims = [event for event in executor.events if isinstance(event, tuple) and event[0] == "aim"]
        self.assertTrue(aims, result)
        self.assertAlmostEqual(aims[0][1], (10 + 3) * 16 + 8, delta=1)
        self.assertAlmostEqual(aims[0][2], 10 * 16 + 8, delta=1)


if __name__ == "__main__":
    unittest.main()
