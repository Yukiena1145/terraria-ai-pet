"""V1 核心逻辑测试：路由、工具匹配、砍树闭环。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from executor import ActionExecutor, ExecutorConfig  # noqa: E402
from monitor import (  # noqa: E402
    DEFAULT_INVENTORY,
    FakeGameMonitor,
    SimulatedInputBackend,
    TILE_TREE,
)
from router import KeywordRouter  # noqa: E402
from skills import SkillContext, ToolMatcher, build_default_registry  # noqa: E402


class RouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.router = KeywordRouter(api_key="")

    def test_chop_tree_is_local(self) -> None:
        intent = self.router.route("去砍树")
        self.assertEqual(intent.action, "chop_tree")
        self.assertEqual(intent.source, "local")

    def test_move_left_parses_distance(self) -> None:
        intent = self.router.route("向左走 4 格")
        self.assertEqual(intent.action, "move")
        self.assertEqual(intent.params["direction"], "left")
        self.assertEqual(intent.params["distance"], 4.0)

    def test_tool_phrase_with_chop_goes_to_chop_skill(self) -> None:
        intent = self.router.route("用斧头砍树")
        self.assertEqual(intent.action, "chop_tree")

    def test_local_intent_has_game_chat_reply(self) -> None:
        intent = self.router.route("去砍树")
        self.assertIn("树", intent.reply)

    def test_question_detection(self) -> None:
        self.assertTrue(self.router.is_question("小鱼你在干嘛？"))
        self.assertTrue(self.router.is_question("你好呀"))
        self.assertFalse(self.router.is_question("去砍树"))

    def test_flat_llm_json_uses_reply_and_steps(self) -> None:
        intent = self.router._build_intent(
            {
                "reply": "主人，我这就向右走！",
                "action": "move",
                "direction": "right",
                "steps": 5,
            },
            "向右走 5 格",
        )
        self.assertEqual(intent.action, "move")
        self.assertEqual(intent.params["direction"], "right")
        self.assertEqual(intent.params["distance"], 5)
        self.assertEqual(intent.reply, "主人，我这就向右走！")

    def test_complex_without_key_needs_config(self) -> None:
        intent = self.router.route("帮我规划一个打Boss的场地")
        self.assertEqual(intent.action, "config_required")
        self.assertTrue(intent.reply)


class ToolRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = ToolMatcher()

    def test_tree_prefers_axe(self) -> None:
        decision = self.matcher.select(
            DEFAULT_INVENTORY, 1, target_kind="tree", tile_code="T"
        )
        self.assertTrue(decision.ok)
        self.assertEqual(decision.item["kind"], "axe")

    def test_wood_block_uses_pickaxe_not_axe(self) -> None:
        decision = self.matcher.select(
            DEFAULT_INVENTORY, 1, target_kind="block", tile_code="W"
        )
        self.assertTrue(decision.ok)
        self.assertEqual(decision.item["kind"], "pickaxe")

    def test_wall_requires_hammer(self) -> None:
        decision = self.matcher.select(
            DEFAULT_INVENTORY, 1, target_kind="wall", tile_code="B"
        )
        self.assertTrue(decision.ok)
        self.assertEqual(decision.item["kind"], "hammer")

    def test_enemy_prefers_highest_damage_weapon(self) -> None:
        decision = self.matcher.select(
            DEFAULT_INVENTORY, 1, target_kind="enemy", tile_code="E"
        )
        self.assertTrue(decision.ok)
        self.assertEqual(decision.item["name"], "Iron Broadsword")

    def test_meteorite_power_rejected(self) -> None:
        decision = self.matcher.select(
            DEFAULT_INVENTORY, 2, target_kind="block", tile_code="M"
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.status, "insufficient_pickaxe_power")

    def test_enemy_without_weapon_is_rejected(self) -> None:
        inv = [item for item in DEFAULT_INVENTORY if item["kind"] != "weapon"]
        decision = self.matcher.select(
            inv, 1, target_kind="enemy", tile_code="E"
        )
        self.assertFalse(decision.ok)
        self.assertEqual(decision.status, "no_weapon")


class ChopTreeFlowTest(unittest.TestCase):
    def test_chop_tree_removes_tree_and_releases_keys(self) -> None:
        monitor = FakeGameMonitor(grid_radius=10, move_speed=1.0)
        executor = ActionExecutor(
            monitor,
            backend=SimulatedInputBackend(monitor),
            config=ExecutorConfig(),
        )
        registry = build_default_registry()
        skill = registry.get("chop_tree")
        self.assertIsNotNone(skill)

        context = SkillContext(
            monitor=monitor,
            executor=executor,
            config={"agent": {"chop_clicks": 3}},
        )
        result = skill.run(context)
        executor.close()

        self.assertTrue(result.ok, result)
        tree = result.data["tree"]
        self.assertNotEqual(monitor.tile_at(*tree), TILE_TREE)

    def test_grid_is_21x21(self) -> None:
        monitor = FakeGameMonitor(grid_radius=10)
        grid = monitor.get_grid()
        self.assertEqual(len(grid), 21)
        self.assertTrue(all(len(row) == 21 for row in grid))

    def test_select_tool_local_command_switches_pickaxe(self) -> None:
        monitor = FakeGameMonitor()
        executor = ActionExecutor(
            monitor,
            backend=SimulatedInputBackend(monitor),
            config=ExecutorConfig(),
        )
        registry = build_default_registry()
        router = KeywordRouter(api_key="")
        intent = router.route("换镐子")
        self.assertEqual(intent.action, "select_tool")

        context = SkillContext(monitor=monitor, executor=executor, config={})
        result = registry.get(intent.action).run(context, **intent.params)
        executor.close()

        self.assertTrue(result.ok, result)
        self.assertEqual(monitor.current_tool["kind"], "pickaxe")


if __name__ == "__main__":
    unittest.main()
