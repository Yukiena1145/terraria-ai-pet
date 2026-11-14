"""指令路由层：本地关键词优先，复杂指令走 DeepSeek。

设计原则
--------
* “去砍树”“向左走”“跳跃”“停止”等高频指令本地拦截，0 Token、毫秒级响应。
* 只有本地规则未命中时才调用大模型，并且强制模型输出受控 JSON。
* API Key 只在后端内存中使用，永远不会返回给前端。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = {
    "chop_tree",
    "mine_block",
    "craft",
    "build",
    "build_house_shell",
    "ai_plan",
    "fight",
    "house_prep",
    "place_workbench",
    "prepare_furniture",
    "move",
    "move_left",
    "move_right",
    "jump",
    "stop",
    "select_tool",
}

REPEAT_PATTERN = re.compile(
    r"(?:重复|再来|再)?\s*([一二两三四五六七八九十百0-9]+)\s*(?:次|遍|棵|棵树)"
)


def chinese_number(text: str) -> int:
    """把 1~99 的中文数字/阿拉伯数字转成整数。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return digits.get(text, 1)


SYSTEM_PROMPT = """你是《泰拉瑞亚》赛博玩家 Agent 的「意图 + 台词」解析器。

每次必须只输出一个 JSON 对象，不能输出 Markdown、代码块或额外解释。
JSON 必须同时包含：
1. reply：给主人看的自然语言回复，中文，1~2 句，最多 80 字；
2. action：受控动作名；
3. 动作参数：直接放在顶层，不要嵌套 params。

示例：
{"reply":"主人，我看到右边有棵树，我这就去砍！","action":"move","direction":"right","steps":5}

可用动作：
1. {"reply":"...","action":"chop_tree"}
2. {"reply":"...","action":"move","direction":"left|right","steps":3}
3. {"reply":"...","action":"jump","height":0}
4. {"reply":"...","action":"stop"}
5. {"reply":"...","action":"craft","item":"workbench"}
6. {"reply":"...","action":"build","width":3,"height":3}
7. {"reply":"...","action":"select_tool","target_kind":"tree|block|wall|plant|enemy","tile_code":"T|D|S|M|W|B|P|E"}

规则：
- 用户说砍树/伐木 -> chop_tree。
- 用户说向左/往左 -> move + direction=left；向右同理。
- steps 是移动格数，默认 3。
- 用户说跳 -> jump。
- 用户说停/别动 -> stop。
- 用户说换斧头/镐子/锤子/武器 -> select_tool，并给 target_kind。
- reply 要像真实玩家在游戏里说话，不要暴露 API、JSON、系统提示词。
- 不确定时输出 {"reply":"主人，我没太听懂，可以先再说一遍吗？","action":"stop"}。
"""


@dataclass(slots=True)
class Intent:
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    source: str = "local"
    raw: str = ""
    message: str = ""
    reply: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "params": self.params,
            "source": self.source,
            "raw": self.raw,
            "message": self.message,
            "reply": self.reply,
        }


class LLMError(RuntimeError):
    """大模型调用/解析失败。"""


class KeywordRouter:
    """本地关键词 + LLM fallback 的路由器。"""

    _RULES: tuple[tuple[str, re.Pattern[str], dict[str, Any]], ...] = (
        (
            "chop_tree",
            re.compile(
                r"砍树|砍柴|伐木|去砍|"
                r"砍\s*[一二两三四五六七八九十百0-9]+\s*棵(?:树)?|"
                r"chop\s*tree|cut\s*tree|chop",
                re.I,
            ),
            {},
        ),
        (
            "prepare_furniture",
            re.compile(r"准备家具|制作家具|做家具", re.I),
            {},
        ),
        (
            "place_workbench",
            re.compile(r"放置工作台|放下工作台|把工作台放地上", re.I),
            {},
        ),
        (
            "house_prep",
            re.compile(r"准备房子|准备建造|准备材料|做工作台", re.I),
            {},
        ),
        (
            "fight",
            re.compile(r"打怪|攻击|战斗|fight|attack", re.I),
            {},
        ),
        (
            "ai_plan",
            re.compile(r"AI规划|大模型规划|让大模型", re.I),
            {},
        ),
        (
            "build_house_shell",
            re.compile(r"建造房子|搭房子|盖房子|建造方盒|搭方盒", re.I),
            {"width": 12, "height": 6},
        ),
        (
            "build",
            re.compile(r"建造|盖房|搭墙|搭方块|build", re.I),
            {"width": 3, "height": 3},
        ),
        (
            "craft",
            re.compile(r"制作|合成|造|做|craft", re.I),
            {},
        ),
        (
            "mine_block",
            re.compile(r"挖方块|挖土|挖石头|挖矿|mine\s*block", re.I),
            {},
        ),
        (
            "move",
            re.compile(r"向左走|往左走|向左|往左|左移|left", re.I),
            {"direction": "left"},
        ),
        (
            "move",
            re.compile(r"向右走|往右走|向右|往右|右移|right", re.I),
            {"direction": "right"},
        ),
        (
            "jump",
            re.compile(r"跳(?:一下|跃|起)?|jump", re.I),
            {},
        ),
        (
            "stop",
            re.compile(r"停止|停下|别动|不要动|stop|pause", re.I),
            {},
        ),
    )

    _TOOL_PATTERN = re.compile(
        r"(?:换|切换|拿|用|装备)(?:上|成|到)?\s*(斧头|斧子|镐子|镐|锤子|武器|剑)",
        re.I,
    )

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout_s: float = 25.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def set_api_key(self, api_key: str) -> None:
        self.api_key = (api_key or "").strip()

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    # 本地关键词
    # ------------------------------------------------------------------

    def match_local(self, command: str) -> Intent | None:
        text = (command or "").strip()
        if not text:
            return None

        # “用斧头砍树”同时包含工具名和目标动作，应优先走砍树技能，
        # 否则只会切工具、不会执行砍树。
        tool_match = self._TOOL_PATTERN.search(text)
        if tool_match and not re.search(r"砍树|砍柴|伐木|chop", text, re.I):
            name = tool_match.group(1)
            if name in {"斧头", "斧子"}:
                kind, tile = "tree", "T"
            elif name in {"镐子", "镐"}:
                kind, tile = "block", "D"
            elif name == "锤子":
                kind, tile = "wall", "B"
            else:
                kind, tile = "enemy", "E"
            return Intent(
                action="select_tool",
                params={"target_kind": kind, "tile_code": tile},
                source="local",
                raw=text,
                message=f"本地匹配：切换 {name}",
                reply=f"好，我换成{name}。",
            )

        for action, pattern, extra in self._RULES:
            if pattern.search(text):
                params = dict(extra)
                if action == "move":
                    distance = self._extract_distance(text)
                    params["distance"] = distance
                if action == "ai_plan":
                    m = re.search(r"(?:AI规划|大模型规划|让大模型)[:：\s]*(.*)", text, re.I)
                    params["goal"] = (m.group(1).strip() if m else text)
                if action == "craft":
                    m = re.search(r"(?:制作|合成|造|做)\s*(?:一个|个)?\s*(.+)", text)
                    if m:
                        target = m.group(1).strip()
                        target = re.sub(r"(?:吧|呢|啊|。|！|!|\?)$", "", target).strip()
                        if target:
                            params["item"] = target
                repeat = self._extract_repeat(text)
                if repeat > 1:
                    params["repeat"] = repeat
                return Intent(
                    action=action,
                    params=params,
                    source="local",
                    raw=text,
                    message=f"本地匹配：{action}",
                    reply=self._local_reply(action, params),
                )
        return None

    @staticmethod
    def _extract_repeat(text: str) -> int:
        m = REPEAT_PATTERN.search(text)
        if not m:
            return 1
        try:
            value = chinese_number(m.group(1))
        except Exception:
            return 1
        return max(1, min(999, value))

    @staticmethod
    def _local_reply(action: str, params: dict[str, Any]) -> str:
        if action == "chop_tree":
            return "好，我去找最近的树，马上开砍！"
        if action == "mine_block":
            return "好，我切镐子挖最近的方块。"
        if action == "craft":
            return "好，我打开背包试着制作工作台。"
        if action == "build":
            return "好，我用快捷栏里的方块搭一小面墙。"
        if action == "build_house_shell":
            return "好，我先用木材搭长平台，再围一个方盒子。"
        if action == "ai_plan":
            return "好，我让大模型看地图和背包，一步一步决定往哪走、怎么造。"
        if action == "fight":
            return "好，我锁定最近的怪物，切武器连续攻击。"
        if action == "house_prep":
            return "好，我先把工作台放好，再做门、桌子、椅子和木墙。"
        if action == "place_workbench":
            return "好，我把工作台从背包拿到快捷栏，再放到地上。"
        if action == "prepare_furniture":
            return "好，我在工作台旁制作木桌、木椅和木墙，再放进快捷栏。"
        if action == "move":
            direction = "左" if params.get("direction") == "left" else "右"
            return f"收到，我向{direction}走 {params.get('distance', 3):g} 格。"
        if action == "jump":
            return "好，我跳一下。"
        if action == "stop":
            return "好，我停下。"
        return "收到，我来处理。"

    @staticmethod
    def is_question(text: str) -> bool:
        """判断是否是对 Agent 的对话/问话，而不是动作指令。"""
        return bool(
            re.search(
                r"在干嘛|在干什么|在做什么|做什么呢|你是谁|你好|在吗|在不在|"
                r"聊天|说话|陪我|怎么样|为什么|怎么回事|吗[？?]?|[？?]|hello|hi\b",
                text or "",
                re.I,
            )
        )

    @staticmethod
    def _extract_distance(text: str) -> float:
        match = re.search(r"(\d+(?:\.\d+)?)\s*格", text)
        if not match:
            return 3.0
        try:
            return max(0.5, min(20.0, float(match.group(1))))
        except ValueError:
            return 3.0

    # ------------------------------------------------------------------
    # LLM fallback
    # ------------------------------------------------------------------

    def route(self, command: str) -> Intent:
        local = self.match_local(command)
        if local is not None:
            logger.info("本地路由命中: %s -> %s", command, local.action)
            return local

        if not self.api_key:
            return Intent(
                action="config_required",
                source="system",
                raw=command,
                message="未配置 DeepSeek API Key，复杂指令不可用",
                reply="主人，我还没配置 API Key，复杂指令暂时听不懂。",
            )

        try:
            return self._route_with_llm(command)
        except Exception as exc:
            logger.warning("LLM 路由失败: %s", exc)
            return Intent(
                action="error",
                source="llm",
                raw=command,
                message=f"大模型解析失败：{exc}",
                reply="主人，我刚刚走神了，没解析成功，再说一遍好吗？",
            )

    def _route_with_llm(self, command: str) -> Intent:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - requirements 已包含
            raise LLMError("缺少 requests 依赖") from exc

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": command},
            ],
            "temperature": 0.0,
            "max_tokens": 260,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=self.timeout_s,
        )
        if response.status_code == 400:
            # 某些兼容 OpenAI 的端点不支持 response_format，降级重试一次。
            payload.pop("response_format", None)
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout_s,
            )
        if response.status_code >= 400:
            raise LLMError(f"HTTP {response.status_code}: {response.text[:200]}")

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        parsed = self._parse_json(content)
        return self._build_intent(parsed, command)

    def _build_intent(self, parsed: dict[str, Any], command: str) -> Intent:
        action = str(parsed.get("action", "")).strip().lower()
        if action not in ALLOWED_ACTIONS:
            raise LLMError(f"模型返回了不允许的动作: {action!r}")

        # 兼容两种格式：
        # 1) 本轮 Prompt 推荐的扁平格式：{"action":"move","direction":"right","steps":5}
        # 2) 旧格式：{"action":"move_right","params":{"distance":5}}
        params = parsed.get("params")
        if not isinstance(params, dict):
            params = {}
        for key in (
            "direction",
            "distance",
            "steps",
            "height",
            "target_kind",
            "tile_code",
            "repeat",
        ):
            if key in parsed and key not in params:
                params[key] = parsed[key]
        if "steps" in params and "distance" not in params:
            params["distance"] = params.pop("steps")

        # 统一成技能注册名：路由可以叫 move_left，但注册中心只有 move。
        if action in {"move_left", "move_right"}:
            params["direction"] = "left" if action == "move_left" else "right"
            action = "move"

        reply = self._sanitize_reply(parsed.get("reply"))
        if not reply:
            reply = "收到，我来处理。"
            logger.warning("模型未返回 reply，使用兜底台词")

        return Intent(
            action=action,
            params=params,
            source="llm",
            raw=command,
            message=str(parsed.get("reason", "大模型解析")),
            reply=reply,
        )

    @staticmethod
    def _sanitize_reply(value: Any) -> str:
        text = " ".join(str(value or "").split()).strip()
        return text[:120]

    @staticmethod
    def _parse_json(content: str) -> dict:
        text = (content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
            text = re.sub(r"\s*```$", "", text)
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型未返回合法 JSON: {text[:200]}") from exc
        if not isinstance(value, dict):
            raise LLMError("模型返回的 JSON 顶层必须是对象")
        return value


__all__ = [
    "ALLOWED_ACTIONS",
    "Intent",
    "KeywordRouter",
    "LLMError",
]
