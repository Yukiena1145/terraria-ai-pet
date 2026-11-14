"""插件直连接口测试：POST /api/action 与 GET /api/items。

桌宠（Coopanion）通过这两个接口调用本地技能，不再经过关键词路由和大模型。
测试跑在 simulation_mode 上，不碰真实游戏、不需要 API Key。
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import DEFAULT_CONFIG, create_app  # noqa: E402


class PluginApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["simulation_mode"] = True
        config["deepseek_api_key"] = ""
        config["server"]["open_browser"] = False
        self.config_path = Path(self._tmp.name) / "config.json"
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )
        self.app = create_app(self.config_path)
        self.client = self.app.test_client()
        self.runtime = self.app.extensions["runtime"]

    def tearDown(self) -> None:
        self.runtime.shutdown()
        self._tmp.cleanup()

    def test_move_action_runs_skill_directly(self) -> None:
        response = self.client.post(
            "/api/action",
            json={"action": "move", "params": {"direction": "right", "distance": 3}},
        )
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["action"], "move")
        self.assertEqual(body["result"]["status"], "success")

    def test_jump_action_runs_skill_directly(self) -> None:
        response = self.client.post(
            "/api/action", json={"action": "jump", "params": {"height": 2}}
        )
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body["ok"], body)

    def test_unknown_action_is_rejected(self) -> None:
        response = self.client.post("/api/action", json={"action": "fly_to_moon"})
        body = response.get_json()
        self.assertEqual(response.status_code, 400, body)
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["result"]["status"], "unknown_action")

    def test_ai_plan_is_not_allowed_for_plugins(self) -> None:
        response = self.client.post(
            "/api/action", json={"action": "ai_plan", "params": {"goal": "造房子"}}
        )
        body = response.get_json()
        self.assertEqual(response.status_code, 400, body)
        self.assertEqual(body["result"]["status"], "not_allowed")

    def test_missing_action_is_rejected(self) -> None:
        response = self.client.post("/api/action", json={})
        self.assertEqual(response.status_code, 400)

    def test_params_must_be_object(self) -> None:
        response = self.client.post(
            "/api/action", json={"action": "move", "params": [1, 2, 3]}
        )
        self.assertEqual(response.status_code, 400)

    def test_stop_action(self) -> None:
        response = self.client.post("/api/action", json={"action": "stop"})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["result"]["status"], "stopped")

    def test_item_lookup(self) -> None:
        response = self.client.get("/api/items?q=铁剑")
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["items"], body)
        self.assertTrue(all("name" in item and "id" in item for item in body["items"]))

    def test_item_lookup_empty_query(self) -> None:
        response = self.client.get("/api/items")
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body["items"], [])


if __name__ == "__main__":
    unittest.main()
