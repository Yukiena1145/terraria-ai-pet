"""DeepSeek planner: compact state -> one high-level Terraria action."""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是泰拉瑞亚机器人的大脑。你可以自由组合小动作，不要只会走路。
每一步只输出一个 JSON，不要 Markdown。

可用小动作：
{"action":"move","direction":"left|right","steps":1-30}
{"action":"jump","height":0-6,"direction":"left|right"}
{"action":"open_inventory"}
{"action":"close_inventory"}
{"action":"select_slot","slot":1-10}
{"action":"move_item","item":"物品名","to_slot":1-10}
{"action":"place_block","x":世界格,"y":世界格}
{"action":"mine_tile","x":世界格,"y":世界格}
{"action":"lookup_item","query":"物品名/编号"}
{"action":"craft","item":"物品名"}
{"action":"fight","duration_s":1-6}
{"action":"chop_tree"}
{"action":"build_house_shell","width":12,"height":6}
{"action":"wait","seconds":0.2-3}
{"action":"stop"}

思考规则：
- 目标需要材料时，先检查背包；材料不够就 craft 或 chop_tree。
- 需要换手上的东西，用 select_slot；从背包拿东西到快捷栏用 open_inventory + move_item。
- 需要放方块，先 select_slot 再 place_block(x,y)。
- 需要挖方块，用 mine_tile(x,y)。
- 怪贴脸时优先 fight，不要先慢慢走。
- 被挡住先 jump，不要连续 move 顶墙。
- 建造前确保附近有空地；没有就继续 move 找。
- 背包里已经给出 slot/name/id；不确定全物品名字时用 lookup_item 查询，不要靠猜。
- 每次只做一个小动作，让执行结果返回后再决定下一步。
"""




class LLMPlanner:
    def __init__(self, *, api_key: str, base_url: str, model: str, timeout_s: float = 30.0):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.model = model or "deepseek-chat"
        self.timeout_s = float(timeout_s)

    def decide(self, goal: str, state: dict[str, Any], last_result: str = "") -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("未配置 DeepSeek API Key")
        player = state.get("player", {})
        inv = state.get("inventory", [])
        names = [
            f"slot{i.get('slot')}: {i.get('name')} x{i.get('stack')} id={i.get('type')}"
            for i in inv
            if int(i.get("stack", 0) or 0) > 0
        ]
        grid = state.get("grid", [])
        grid_text = chr(10).join("".join(row) for row in grid)
        payload_state = {
            "goal": goal,
            "player": {
                "x": player.get("x_tiles"),
                "y": player.get("y_tiles"),
                "hp": player.get("hp"),
                "on_ground": player.get("on_ground"),
            },
            "inventory": names,
            "grid": grid_text,
            "last_result": last_result,
        }
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload_state, ensure_ascii=False)},
                ],
                "temperature": 0.0,
                "max_tokens": 200,
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            content = content.split(chr(10), 1)[1] if chr(10) in content else content
        data = json.loads(content)
        if not isinstance(data, dict) or "action" not in data:
            raise RuntimeError("规划器没有返回 action")
        return data
