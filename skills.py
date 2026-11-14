"""技能层：BaseSkill / SkillRegistry / ChopTreeSkill / 工具匹配。

V1 重点是把“指令 -> 选工具 -> 闭环移动 -> 点击目标”串成完整闭环；
未来 Boss、矿车、建场地等技能只需要继承 ``BaseSkill`` 并注册即可。
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from executor import (
    ActionCancelled,
    ActionExecutor,
    Direction,
    ExecutorError,
    JumpResult,
    JumpTooHighError,
    MoveResult,
    TILE_SIZE_PX,
)
from inventory_ui import InventoryUI
from planner import LLMPlanner
from item_catalog import find_items
from monitor import (
    TARGET_KIND_BY_TILE,
    TILE_REQUIRED_PICKAXE_POWER,
    TILE_TREE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 基础数据结构
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SkillResult:
    ok: bool
    status: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "data": self.data,
        }


def _item_stack(item: dict) -> int:
    """物品堆叠数；假数据里经常没有 stack 字段，按 1 处理。"""
    try:
        return int(item.get("stack", 1) or 0)
    except (TypeError, ValueError):
        return 1


@dataclass(slots=True)
class SkillContext:
    monitor: Any
    executor: ActionExecutor
    config: dict[str, Any]
    log: Callable[[str, str], None] | None = None

    def emit(self, message: str, level: str = "info") -> None:
        # 有运行时回调时由回调统一落日志，避免 console/file 重复两遍。
        if self.log:
            try:
                display = message if message.startswith("[") else f"[执行] {message}"
                self.log(display, level)
            except Exception:
                pass
            return
        level_no = {
            "error": logging.ERROR,
            "warning": logging.WARNING,
            "success": logging.INFO,
        }.get(level, logging.INFO)
        logger.log(level_no, message)


class BaseSkill(ABC):
    """所有技能的基类。"""

    name: str = "base"
    description: str = ""

    @abstractmethod
    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        raise NotImplementedError


class SkillRegistry:
    """技能注册中心。技能名即路由动作名。"""

    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}

    def register(self, skill: BaseSkill) -> BaseSkill:
        if not skill.name or skill.name == "base":
            raise ValueError("技能必须有唯一 name")
        if skill.name in self._skills:
            raise ValueError(f"技能重复注册: {skill.name}")
        self._skills[skill.name] = skill
        logger.info("注册技能: %s", skill.name)
        return skill

    def get(self, name: str) -> BaseSkill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return sorted(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills


# ---------------------------------------------------------------------------
# 工具匹配：严格遵循泰拉瑞亚机制
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolDecision:
    ok: bool
    status: str
    item: dict[str, Any] | None
    reason: str


class ToolMatcher:
    """根据目标类型选择工具。

    规则来源（V1 需求）：
    * 树/仙人掌/巨型发光蘑菇 -> 斧头/链锯；没有则镐子/武器兜底；
    * 物块（含放置后的木材块）-> 镐子/钻头；木材块不能用斧头；
    * 背景墙 -> 锤子，锤子不能采物块/砍树；
    * 花草藤蔓 -> 当前手持工具优先，斧/镐/剑均可；
    * 敌对生物 -> 攻击力最高的武器；没有武器时宁可拒绝，不拿斧镐送；
    * 物块有镐力门槛，当前镐子不够时返回 insufficient_pickaxe_power。
    """

    AXE_KINDS = {"axe", "chainsaw"}
    PICK_KINDS = {"pickaxe", "drill"}
    WEAPON_KINDS = {"weapon"}
    HAMMER_KINDS = {"hammer"}
    PLANT_OK_KINDS = AXE_KINDS | PICK_KINDS | WEAPON_KINDS

    def select(
        self,
        inventory: list[dict],
        current_slot: int,
        *,
        target_kind: str = "",
        tile_code: str = "",
        required_power: int | None = None,
    ) -> ToolDecision:
        kind = target_kind or TARGET_KIND_BY_TILE.get(tile_code, "")
        current = self._current(inventory, current_slot)

        if kind == "tree":
            return self._select_tree(inventory, current)
        if kind == "block":
            required = (
                int(required_power)
                if required_power is not None
                else TILE_REQUIRED_PICKAXE_POWER.get(tile_code or "", 0)
            )
            return self._select_block(inventory, current, required)
        if kind == "wall":
            return self._select_wall(inventory, current)
        if kind == "plant":
            return self._select_plant(inventory, current)
        if kind == "enemy":
            return self._select_enemy(inventory)
        return ToolDecision(
            ok=bool(current),
            status="no_rule" if current else "no_tool",
            item=current,
            reason="未知目标类型，保持当前手持工具",
        )

    # ------------------------------------------------------------------
    # 各目标规则
    # ------------------------------------------------------------------

    def _select_tree(self, inventory: list[dict], current: dict | None) -> ToolDecision:
        if current and current.get("kind") in self.AXE_KINDS:
            return ToolDecision(True, "ok", current, "当前手持斧头/链锯，直接砍树")

        axe = self._best(inventory, self.AXE_KINDS, key="power")
        if axe:
            return ToolDecision(True, "ok", axe, "树木目标：切换斧头/链锯")

        fallback = self._best(inventory, self.PICK_KINDS, key="power") or self._best(
            inventory, self.WEAPON_KINDS, key="damage"
        )
        if fallback:
            return ToolDecision(
                True,
                "no_axe_fallback",
                fallback,
                "没有斧头，使用镐子/武器兜底，砍树效率极低",
            )
        return ToolDecision(False, "no_tool", None, "没有斧头，也没有可用的兜底工具")

    def _select_block(
        self,
        inventory: list[dict],
        current: dict | None,
        required_power: int,
    ) -> ToolDecision:
        # 关键：木材块也必须用镐子；当前拿斧头时不能将就。
        if (
            current
            and current.get("kind") in self.PICK_KINDS
            and int(current.get("power", 0)) >= required_power
        ):
            return ToolDecision(
                True, "ok", current, f"当前镐子镐力 {current.get('power', 0)} 足够"
            )

        pick = self._best(inventory, self.PICK_KINDS, key="power")
        if not pick:
            return ToolDecision(False, "no_pickaxe", None, "物块目标需要镐子/钻头")
        if int(pick.get("power", 0)) < required_power:
            return ToolDecision(
                False,
                "insufficient_pickaxe_power",
                pick,
                f"目标需要 {required_power} 镐力，当前最高只有 {pick.get('power', 0)}",
            )
        return ToolDecision(
            True, "ok", pick, f"物块目标：切换镐子（镐力 {pick.get('power', 0)}）"
        )

    def _select_wall(self, inventory: list[dict], current: dict | None) -> ToolDecision:
        if current and current.get("kind") in self.HAMMER_KINDS:
            return ToolDecision(True, "ok", current, "当前手持锤子，可拆除背景墙")
        hammer = self._best(inventory, self.HAMMER_KINDS, key="power")
        if hammer:
            return ToolDecision(True, "ok", hammer, "背景墙目标：切换锤子")
        return ToolDecision(False, "no_hammer", None, "背景墙只能用锤子拆除")

    def _select_plant(self, inventory: list[dict], current: dict | None) -> ToolDecision:
        if current and current.get("kind") in self.PLANT_OK_KINDS:
            return ToolDecision(True, "ok", current, "花草目标：保持当前手持工具")
        fallback = (
            self._best(inventory, self.AXE_KINDS, key="power")
            or self._best(inventory, self.PICK_KINDS, key="power")
            or self._best(inventory, self.WEAPON_KINDS, key="damage")
        )
        if fallback:
            return ToolDecision(True, "ok", fallback, "花草目标：切换可用工具")
        return ToolDecision(False, "no_tool", None, "没有可清除花草的工具")

    def _select_enemy(self, inventory: list[dict]) -> ToolDecision:
        weapon = self._best(inventory, self.WEAPON_KINDS, key="damage")
        if not weapon:
            # 明确禁止用斧头/镐子主动打敌人
            return ToolDecision(False, "no_weapon", None, "没有武器，拒绝用斧镐攻击敌人")
        return ToolDecision(
            True,
            "ok",
            weapon,
            f"敌人目标：选择攻击力最高的武器（{weapon.get('damage', 0)}）",
        )

    # ------------------------------------------------------------------
    # 工具函数
    # ------------------------------------------------------------------

    @staticmethod
    def _current(inventory: list[dict], current_slot: int) -> dict | None:
        for item in inventory:
            if int(item.get("slot", -1)) == int(current_slot):
                return item
        return inventory[0] if inventory else None

    @staticmethod
    def _best(
        inventory: list[dict],
        kinds: set[str],
        *,
        key: str,
    ) -> dict | None:
        candidates = [
            item for item in inventory
            if item.get("kind") in kinds
            and item.get("hotbar", int(item.get("slot", 99)) <= 10)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (int(item.get(key, 0)), -int(item.get("slot", 99))),
        )


class SelectToolSkill(BaseSkill):
    """独立技能：根据目标类型切换工具。"""

    name = "select_tool"
    description = "根据树木/物块/背景墙/植物/敌人选择正确工具，并检查镐力"

    def __init__(self) -> None:
        self.matcher = ToolMatcher()

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        target_kind = str(params.get("target_kind", ""))
        tile_code = str(params.get("tile_code", ""))
        required_power = params.get("required_power")

        decision = self.matcher.select(
            context.monitor.inventory,
            int(context.monitor.current_tool.get("slot", 1)),
            target_kind=target_kind,
            tile_code=tile_code,
            required_power=required_power,
        )
        if not decision.ok or not decision.item:
            return SkillResult(
                False,
                decision.status,
                decision.reason,
                {"target_kind": target_kind, "tile_code": tile_code},
            )

        item = decision.item
        if int(item["slot"]) != int(context.monitor.current_tool.get("slot", 1)):
            context.executor.select_hotbar_slot(int(item["slot"]))
        context.emit(f"选工具: {item['name']}（{decision.reason}）", "info")
        return SkillResult(
            True,
            decision.status,
            decision.reason,
            {"tool": item, "target_kind": target_kind, "tile_code": tile_code},
        )


# ---------------------------------------------------------------------------
# V1 核心技能：砍树
# ---------------------------------------------------------------------------


class ChopTreeSkill(BaseSkill):
    """读取 21x21 网格，找最近的树，闭环走过去并挥斧。"""

    name = "chop_tree"
    description = "寻找最近的树，自动选斧头，闭环移动后砍倒它"

    def __init__(self) -> None:
        self.matcher = ToolMatcher()

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        local_only = bool(params.get("local_only", False))
        context.monitor.set_action("寻找树木")
        grid = context.monitor.get_grid()
        if not grid:
            return SkillResult(False, "no_grid", "地形网格为空")

        target = self._find_nearest_tree(grid)
        if target is None and not local_only and bool(params.get("search", True)):
            target = self._search_for_tree(context)
            if target is not None:
                grid = context.monitor.get_grid()
        if target is None:
            if local_only:
                return SkillResult(
                    False,
                    "no_tree_in_view",
                    "视野里没有树：先 move 往有树的方向走，再 chop_tree",
                    {"hint": "先走路找树；chop_tree 只砍看得见、够得着的树"},
                )
            return SkillResult(False, "no_tree", "扩大搜索后仍未找到树")

        dx, dy = target
        center = len(grid) // 2
        player_tx, player_ty = context.monitor.player_tile
        tree_world_x = player_tx + (dx - center)
        tree_world_y = player_ty + (dy - center)
        context.emit(
            f"发现树木: 世界格=({tree_world_x}, {tree_world_y})，"
            f"横向 {abs(tree_world_x - player_tx)} 格 / 纵向 {abs(tree_world_y - player_ty)} 格",
            "info",
        )

        stand_x: float | None = None
        if local_only:
            horizontal = abs(tree_world_x - player_tx)
            vertical = abs(tree_world_y - player_ty)
            if horizontal > 4 or vertical > 4:
                side = (
                    "右边"
                    if tree_world_x > player_tx
                    else ("左边" if tree_world_x < player_tx else "原地")
                )
                return SkillResult(
                    False,
                    "too_far",
                    f"最近的树在{side} {horizontal} 格、上下 {vertical} 格，超出 4 格砍伐范围",
                    {
                        "tree": [tree_world_x, tree_world_y],
                        "dx": tree_world_x - player_tx,
                        "dy": tree_world_y - player_ty,
                        "hint": "先用 move 走近到 4 格内再 chop_tree",
                    },
                )
        else:
            # 2) 计算站位：站在树左侧/右侧 1.5 格，保证在 5 格交互范围内
            if tree_world_x > player_tx:
                stand_x = tree_world_x - 1.5
            elif tree_world_x < player_tx:
                stand_x = tree_world_x + 1.5
            else:
                stand_x = float(player_tx)

            context.monitor.set_action(f"走向树木 x={stand_x:.1f}")
            move: MoveResult = context.executor.move_to_x(stand_x, timeout_s=10.0)
            if not move.success:
                context.emit("走不过去，尝试边跳边移动", "warning")
                try:
                    context.executor.jump_over_obstacle(
                        1.5,
                        direction=Direction.RIGHT if stand_x > move.final_x_tiles else Direction.LEFT,
                        timeout_s=3.0,
                    )
                    move = context.executor.move_to_x(stand_x, timeout_s=6.0)
                except (ExecutorError, JumpTooHighError) as exc:
                    context.emit(f"跳跃越障失败: {exc}", "warning")
            if not move.success:
                return SkillResult(
                    False,
                    f"move_{move.status}",
                    f"走到树木附近失败：{move.status}",
                    {"move": asdict(move)},
                )
            context.emit(
                f"到达砍树站位: {move.final_x_tiles:.2f} 格，误差 {move.error_tiles:.2f} 格",
                "info",
            )

        return self._chop_at(context, tree_world_x, tree_world_y, stand_x=stand_x)

    def _chop_at(
        self,
        context: SkillContext,
        tree_world_x: int,
        tree_world_y: int,
        *,
        stand_x: float | None = None,
    ) -> SkillResult:
        """只负责“看向并砍倒这棵树”，不移动、不寻找。"""
        decision = self.matcher.select(
            context.monitor.inventory,
            int(context.monitor.current_tool.get("slot", 1)),
            target_kind="tree",
            tile_code=TILE_TREE,
        )
        if not decision.ok or not decision.item:
            return SkillResult(False, decision.status, decision.reason)
        tool = decision.item
        # 无论当前监控器认为拿的是什么，都主动切到正确工具；
        # 玩家手动换成剑时也能恢复斧头。
        context.executor.select_hotbar_slot(int(tool["slot"]))
        context.emit(
            f"使用工具: {tool['name']}（{decision.reason}）",
            "warning" if decision.status == "no_axe_fallback" else "info",
        )

        # 先正脸朝向树，避免倒退/用背对着树
        current = context.executor.get_current_position()
        if tree_world_x > current.center_x_tiles + 0.4:
            context.executor.face_direction(Direction.RIGHT)
        elif tree_world_x < current.center_x_tiles - 0.4:
            context.executor.face_direction(Direction.LEFT)

        # 按住鼠标左键；Terraria 的斧头是持续挥动，不是点三下
        context.monitor.set_action("砍树（长按）")
        removed = False
        context.executor.smart_cursor_down()
        context.executor.auto_select_down()
        try:
            # 只瞄准树干底部一次，然后一直按住。
            # 不再左右扫鼠标：第一次位置是对的，乱扫反而点到树叶。
            context.executor.aim_tile(
                tree_world_x,
                tree_world_y,
                screen_mapper=context.monitor.world_to_screen,
            )
            removed = context.executor.hold_mouse(
                "left",
                until=lambda: context.monitor.tile_at(tree_world_x, tree_world_y) != TILE_TREE,
                timeout_s=8.0,
            )
        except ActionCancelled as exc:
            return SkillResult(
                False,
                "cancelled",
                f"动作已取消：{exc}",
                {"tree": [tree_world_x, tree_world_y]},
            )
        except ExecutorError as exc:
            return SkillResult(
                False,
                "mine_failed",
                f"挥斧失败：{exc}",
                {"tree": [tree_world_x, tree_world_y]},
            )
        finally:
            context.executor.auto_select_up()
            context.executor.smart_cursor_up()

        if removed:
            context.emit("砍树完成：树木已消失", "success")
            data: dict[str, Any] = {
                "tree": [tree_world_x, tree_world_y],
                "tool": tool,
            }
            if stand_x is not None:
                data["stand_x"] = stand_x
            return SkillResult(
                True,
                "success",
                f"砍倒树木 @ ({tree_world_x}, {tree_world_y})",
                data,
            )
        return SkillResult(
            False,
            "tree_still_alive",
            "长按后树仍在：检查工具/距离/是否被遮挡，或者挪近一点再试",
            {"tree": [tree_world_x, tree_world_y]},
        )

    def _search_for_tree(self, context: SkillContext) -> tuple[int, int] | None:
        """选定一个方向后一直走，不来回折返。"""
        direction = "right" if random.random() < 0.5 else "left"
        sign = 1.0 if direction == "right" else -1.0
        context.emit(f"视野内没有树，向{direction}连续搜索", "info")
        for leg in range(6):
            start = context.executor.get_current_position()
            target_x = start.center_x_tiles + sign * 12.0
            result = context.executor.move_to_x(target_x, timeout_s=8.0, allow_backoff=False)
            for jump_try in range(3):
                if result.success:
                    break
                context.emit(f"被挡住，尝试第 {jump_try + 1} 次跳跃", "warning")
                try:
                    context.executor.jump_over_obstacle(
                        2.5,
                        direction=Direction.RIGHT if sign > 0 else Direction.LEFT,
                        timeout_s=3.0,
                    )
                    result = context.executor.move_to_x(
                        target_x, timeout_s=6.0, allow_backoff=False
                    )
                except (ExecutorError, JumpTooHighError) as exc:
                    context.emit(f"跳跃越障失败: {exc}", "warning")
            if not result.success:
                context.emit("跳跃后仍然过不去，停止这次搜索", "warning")
                return None
            tree = self._find_nearest_tree(context.monitor.get_grid())
            if tree is not None:
                return tree
            context.emit(f"已走 {12 * (leg + 1)} 格，仍没树", "info")
        return None

    @staticmethod
    def _find_nearest_tree(grid: list[str]) -> tuple[int, int] | None:
        """找一棵树的树干底部，而不是树冠/树枝。

        Terraria 的树是“竖直树干 + 横向树枝/树叶”。同一列可能有很多 T，
        只有最下面那段连续的树干才是砍伐点。这里按列找最长的竖直连续段，
        取它的底端。
        """
        center = len(grid) // 2
        best: tuple[int, int] | None = None
        best_score = 10**9
        for x in range(len(grid)):
            ys = sorted(y for y, row in enumerate(grid) if x < len(row) and row[x] == TILE_TREE)
            if not ys:
                continue
            # 切分成竖直连续段
            runs: list[list[int]] = []
            run = [ys[0]]
            for y in ys[1:]:
                if y == run[-1] + 1:
                    run.append(y)
                else:
                    runs.append(run)
                    run = [y]
            runs.append(run)
            # 树干通常是最长的竖直段；树叶/树枝段更短
            trunk = max(runs, key=len)
            base_y = trunk[-1]
            dx = abs(x - center)
            dy = abs(base_y - center)
            # 太高/太低的树现在够不到，先跳过
            if dy > 5:
                continue
            # 树干长度低于 3 的，优先度降低，但不完全排除小树
            score = dx + dy * 3 + (0 if len(trunk) >= 3 else 6)
            if score < best_score:
                best_score = score
                best = (x, base_y)
        return best

class CraftSkill(BaseSkill):
    """控制台合成：扫描制作菜单 tooltip，找到目标物品后点击。"""

    name = "craft"
    description = "按物品名在制作菜单里找配方并制作"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        item = str(params.get("item", "")).strip()
        if not item:
            return SkillResult(False, "no_target", "没有指定要制作的东西")
        require_open = bool(params.get("require_open", False))
        if require_open:
            probe = InventoryUI(context.executor, context.monitor)
            if not probe.grid_visible():
                # 只开着箱子/商店时 playerInventory 也是 True，但格子没画出来；
                # 这时扫配方只会扫到空气。
                return SkillResult(
                    False,
                    "inventory_grid_closed",
                    "看不到背包格子（可能只开着箱子/商店）：先 open_inventory 再 craft",
                    {"hint": "先调 open_inventory；它会按一次 Esc 并确认格子出现"},
                )
        ui = InventoryUI(context.executor, context.monitor)
        try:
            # 插件传 require_open=True：背包已经由大模型打开，这里绝不按 Esc——
            # 再按一次就把它关上了。只有网页面板的旧复合流程（没传 require_open）才自己开。
            if not require_open:
                ui.force_open()
            before = {
                int(i.get("slot", 0)): (int(i.get("type", 0)), int(i.get("stack", 0)))
                for i in context.monitor.inventory
            }
            before_count = sum(
                int(i.get("stack", 0) or 0)
                for i in context.monitor.inventory
                if str(i.get("name", "")) == item
            )
            point = ui.scroll_to_recipe(item)
            if point is None:
                return SkillResult(
                    False,
                    "recipe_not_found",
                    f"制作菜单里没有「{item}」的可见配方：做东西要站在对应工作站旁边"
                    "（工作台/熔炉/铁砧），工作台还在背包里就先 terraria_place 把它放出来",
                    {
                        "hint": "先放工作台：select_slot 选中「工作台」→ terraria_place 放在脚边空地；"
                        "靠近后再 craft。放完看 terraria_see 的「附近工作台」提示",
                    },
                )
            # 金色配方格本身就是制作入口。
            # count=1 点一次；count>1 按住不放——泰拉瑞亚会连续制作，
            # 一次就能把几十上百个木墙堆到鼠标上，而不是“做 4 个放一次”。
            try:
                count = max(1, min(500, int(params.get("count", 1) or 1)))
            except (TypeError, ValueError):
                count = 1
            if count <= 1:
                context.executor.ui_click(*point)
                time.sleep(0.6)
            else:
                hold_s = min(6.0, 0.10 * count + 0.25)
                hold = getattr(context.executor, "ui_hold", None)
                if callable(hold):
                    hold(point[0], point[1], hold_s=hold_s)
                else:
                    for _ in range(count):
                        context.executor.ui_click(*point)
                        time.sleep(0.06)
                time.sleep(0.5)
            # 先刷新一次背包，再找当前为空的格子。
            context.monitor.get_state()
            empty = ui.empty_inventory_slot()
            if empty is not None:
                ex, ey = ui.center(empty)
                context.executor.ui_click(ex, ey)
                time.sleep(0.5)
            context.monitor.get_state()
            after = {
                int(i.get("slot", 0)): (int(i.get("type", 0)), int(i.get("stack", 0)))
                for i in context.monitor.inventory
            }
        except Exception as exc:
            return SkillResult(False, "craft_failed", f"合成操作失败: {exc}")
        after_names = [str(i.get("name", "")) for i in context.monitor.inventory]
        after_count = sum(
            int(i.get("stack", 0) or 0)
            for i in context.monitor.inventory
            if str(i.get("name", "")) == item
        )
        if item not in after_names:
            return SkillResult(
                False,
                "craft_not_in_inventory",
                f"点了「{item}」配方，但背包里没有出现该物品（成品可能还在鼠标上）",
                {"item": item, "after_names": after_names},
            )
        if before == after:
            return SkillResult(
                False,
                "craft_no_change",
                f"点到了「{item}」配方，但背包没有变化，可能材料不足",
                {"before": before, "after": after},
            )
        if after_count <= before_count:
            return SkillResult(
                False,
                "craft_not_increased",
                f"点击了「{item}」配方，但数量没有增加（制作前 {before_count}，制作后 {after_count}）",
                {"item": item, "before_count": before_count, "after_count": after_count},
            )
        return SkillResult(
            True,
            "success",
            f"已按配方制作：{item}，数量 {before_count} -> {after_count}",
            {"item": item, "before": before, "after": after,
             "before_count": before_count, "after_count": after_count},
        )


def ensure_workbench_slot(context: SkillContext) -> int | None:
    """随时重新查找工作台：快捷栏优先，其次背包，最后自动补做一个。"""
    ui = InventoryUI(context.executor, context.monitor)
    ui.force_open()
    try:
        context.monitor.get_state()
    except Exception:
        pass
    workbench = next(
        (i for i in context.monitor.inventory
         if str(i.get("name", "")) == "工作台" and 1 <= int(i.get("slot", 0) or 0) <= 10),
        None,
    )
    if workbench is None:
        workbench = next(
            (i for i in context.monitor.inventory if str(i.get("name", "")) == "工作台"),
            None,
        )
    if workbench is None:
        made = CraftSkill().run(context, item="工作台")
        context.emit(f"背包没有工作台，补做: {made.message}", "info" if made.ok else "warning")
        if not made.ok:
            return None
        try:
            context.monitor.get_state()
        except Exception:
            pass
        workbench = next(
            (i for i in context.monitor.inventory if str(i.get("name", "")) == "工作台"),
            None,
        )
    if workbench is None:
        return None
    slot = int(workbench.get("slot", 0) or 0)
    if 1 <= slot <= 10:
        return slot
    return ui.ensure_hotbar_item("工作台")


class BuildSkill(BaseSkill):
    """先在地面方块上放第一块，再沿着它往上/往旁边搭。"""

    name = "build"
    description = "找地面支撑，先放第一块，再逐格向上搭"

    def _solid(self, context: SkillContext, x: int, y: int) -> bool:
        return context.monitor.tile_at(x, y) not in {".", " "}

    def _find_support(self, context: SkillContext, px: int, py: int) -> tuple[int, int] | None:
        """找真正的“地面正上方一格”，不是在半空的树枝/石头上乱点。"""
        grid = context.monitor.get_grid()
        center = len(grid) // 2
        candidates = []
        for x in range(px - 4, px + 5):
            dx = x - px
            gx = center + dx
            if not (0 <= gx < len(grid)):
                continue
            # 从下往上找该列最低的实心地面
            # 找“上方是空气”的地面，不能取地下深处的最底实心块。
            surface_ys = [
                y for y in range(1, len(grid))
                if 0 <= gx < len(grid[y])
                and grid[y][gx] not in {".", " "}
                and grid[y - 1][gx] in {".", " "}
            ]
            if not surface_ys:
                continue
            base_map_y = max(surface_ys)
            base_world_y = py + (base_map_y - center)
            target_y = base_world_y - 1
            # 目标必须是空气，且不能是玩家身体
            if context.monitor.tile_at(x, target_y) not in {".", " "}:
                continue
            if abs(dx) <= 1 and abs(target_y - py) <= 2:
                continue
            # 优先离玩家近、地面平的地方
            score = abs(dx) + abs(base_world_y - py) * 2
            candidates.append((score, x, target_y, base_world_y))
        if not candidates:
            return None
        _, x, target_y, _ = min(candidates)
        return x, target_y
    def _find_surface(self, context: SkillContext, px: int, py: int) -> tuple[int, int] | None:
        grid = context.monitor.get_grid()
        center = len(grid) // 2
        candidates = []
        for x in range(px - 4, px + 5):
            dx = x - px
            gx = center + dx
            if not (0 <= gx < len(grid)):
                continue
            surface_ys = [
                y for y in range(1, len(grid))
                if 0 <= gx < len(grid[y])
                and grid[y][gx] not in {".", " "}
                and grid[y - 1][gx] in {".", " "}
            ]
            if not surface_ys:
                continue
            base_map_y = max(surface_ys)
            base_world_y = py + (base_map_y - center)
            if abs(dx) <= 1 and abs(base_world_y - py) <= 2:
                continue
            score = abs(dx) + abs(base_world_y - py) * 2
            candidates.append((score, x, base_world_y))
        if not candidates:
            return None
        _, x, y = min(candidates)
        return x, y

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        width = max(1, min(4, int(params.get("width", 1) or 1)))
        width = max(1, min(4, int(params.get("width", 1) or 1)))
        height = max(1, min(4, int(params.get("height", 3) or 3)))
        inventory = context.monitor.inventory
        candidates = [i for i in inventory if i.get("hotbar") and str(i.get("name", "")) in {"土块", "木材", "石块", "泥块"}]
        if not candidates:
            return SkillResult(False, "no_block_material", "快捷栏里没有土块/木材/石块")
        block = candidates[0]
        context.executor.select_hotbar_slot(int(block["slot"]))
        px, py = context.monitor.player_tile
        base = self._find_support(context, px, py)
        if base is None:
            return SkillResult(False, "no_support", "附近找不到“下面是实心、上面是空气”的放置点")
        context.emit(f"找到支撑点: ({base[0]},{base[1]})，材料 {block['name']}", "info")
        placed = 0
        # 先只搭一列：从地面方块往上
        for level in range(height):
            x = base[0]
            y = base[1] - level
            if self._solid(context, x, y):
                continue
            try:
                before = context.monitor.tile_at(x, y)
                context.executor.place_block(
                    x, y, screen_mapper=context.monitor.world_to_screen
                )
                time.sleep(0.20)
                after = context.monitor.tile_at(x, y)
                if after != before and after not in {".", " "}:
                    placed += 1
                    context.emit(f"放置成功: ({x},{y}) -> {after}", "info")
                else:
                    context.emit(f"放置未生效: ({x},{y}) 仍为 {after}", "warning")
                    break
            except ExecutorError as exc:
                context.emit(f"放置 ({x},{y}) 失败: {exc}", "warning")
                break
        if placed == 0:
            return SkillResult(False, "place_failed", "第一块没有放上去")
        return SkillResult(
            True,
            "success",
            f"放置了 {placed} 个方块（从地面向上）",
            {"placed": placed, "base": base, "material": block["name"]},
        )


class PlaceBlockSkill(BaseSkill):
    """把手上的东西放到指定世界格：先点旁边的支撑方块，再慢慢滑到目标格。

    这是 Terraria 里最稳的放法（也就是主人说的“先点地面再往上滑”）：
    * 往地上放：先点目标下方那块实心地面，再往上滑一格；
    * 往左放：先点目标右边那块，再往左滑；
    * 往右放：先点目标左边那块，再往右滑。
    不挑材料、不开背包、不关背包——那些是别的小动作。
    """

    name = "place_block"
    description = "把手上的东西放到指定格：自动找支撑方块，先点它再滑到目标"

    SUPPORT_CHARS = {"D", "S", "W", "M", "?"}

    @classmethod
    def _solid(cls, context: SkillContext, x: int, y: int) -> bool:
        return context.monitor.tile_at(x, y) in cls.SUPPORT_CHARS

    def _find_support(
        self, context: SkillContext, x: int, y: int
    ) -> tuple[int, int, str] | None:
        # 地面：目标下方是实心 → 点地面，往上滑。
        if self._solid(context, x, y + 1):
            return x, y + 1, "up"
        # 往左放：右边有实心 → 点右边，往左滑。
        if self._solid(context, x + 1, y):
            return x + 1, y, "left"
        # 往右放：左边有实心 → 点左边，往右滑。
        if self._solid(context, x - 1, y):
            return x - 1, y, "right"
        return None

    def _find_nearby(
        self, context: SkillContext, x: int, y: int, radius: int = 2
    ) -> tuple[int, int, int, int, str] | None:
        """目标格没有支撑时，在周围 2 格内找一个“空气 + 有支撑”的格子。"""
        best = None
        best_score = 10**9
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx == 0 and dy == 0:
                    continue
                tx, ty = x + dx, y + dy
                if self._solid(context, tx, ty):
                    continue
                support = self._find_support(context, tx, ty)
                if support is None:
                    continue
                score = abs(dx) + abs(dy)
                if score < best_score:
                    best_score = score
                    best = (tx, ty, support[0], support[1], support[2])
        return best

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        try:
            x = int(params.get("x"))
            y = int(params.get("y"))
        except (TypeError, ValueError):
            return SkillResult(False, "bad_target", "place_block 需要整数 x 和 y")

        opened = bool(getattr(context.monitor, "inventory_open", False))
        if opened:
            return SkillResult(
                False,
                "inventory_open",
                "背包还开着，鼠标会点到 UI 上：先 close_inventory 再 place_block",
                {"hint": "先调 close_inventory"},
            )

        try:
            context.monitor.get_state()
        except Exception:
            pass

        held_tool = context.monitor.current_tool or {}
        try:
            create_tile = int(held_tool.get("create_tile", 0) or 0)
        except (TypeError, ValueError):
            create_tile = 0
        if create_tile < 0:
            return SkillResult(
                False,
                "not_holding_block",
                f"手上是「{held_tool.get('name', '空手')}」，不是方块：先切到木材/土块再放",
                {
                    "held": held_tool.get("name"),
                    "hint": "先 find_in_hotbar {name:'木材'} 找到槽位，再 select_slot 选中，再 place",
                },
            )

        if self._solid(context, x, y):
            return SkillResult(
                False,
                "already_solid",
                f"({x},{y}) 已经有方块了，不用再放",
                {"x": x, "y": y},
            )

        # 不能放在自己站着的 2x3 身体里——那格子被你自己占着，游戏不会让你放。
        try:
            body = context.executor.get_current_position().body_tiles
        except Exception:
            body = None
        if body is not None and body.contains(x, y):
            return SkillResult(
                False,
                "in_player_body",
                f"({x},{y}) 是你自己站着的格子，放不上去；要放到身体左边或右边一格",
                {
                    "x": x,
                    "y": y,
                    "body": {"left": body.left, "top": body.top, "right": body.right, "bottom": body.bottom},
                    "hint": "放到 (x=左列-1) 或 (x=左列+2)，再一格一格往上叠",
                },
            )

        requested = (x, y)
        support = self._find_support(context, x, y)
        if support is None:
            # 模型给的坐标常常差 1~2 格（尤其是 y）：自动在附近找一个能放的点。
            nearby = self._find_nearby(context, x, y)
            if nearby is not None:
                x, y, support_x0, support_y0, direction0 = nearby
                support = (support_x0, support_y0, direction0)
                context.emit(
                    f"({requested[0]},{requested[1]}) 没有支撑，改用附近的 ({x},{y})",
                    "warning",
                )
            else:
                below = context.monitor.tile_at(x, y + 1)
                right = context.monitor.tile_at(x + 1, y)
                left = context.monitor.tile_at(x - 1, y)
                return SkillResult(
                    False,
                    "no_support",
                    f"({x},{y}) 周围两格都找不到可依靠的方块",
                    {
                        "x": x,
                        "y": y,
                        "around": {"下": below, "右": right, "左": left},
                        "hint": "先走到地面旁边；往地上放时目标要定在“地面格的正上方一格”",
                    },
                )

        support_x, support_y, direction = support
        tool = context.monitor.current_tool
        held = str(tool.get("name", "")) or "空手"
        before = context.monitor.tile_at(x, y)
        context.emit(
            f"放置: 目标({x},{y}) 支撑({support_x},{support_y}) 往{'上' if direction == 'up' else ('左' if direction == 'left' else '右')}滑",
            "info",
        )
        slide_info: dict = {}
        try:
            slide_info = context.executor.place_slide(
                support_x,
                support_y,
                x,
                y,
                screen_mapper=context.monitor.world_to_screen,
            )
        except ExecutorError as exc:
            return SkillResult(
                False,
                "place_failed",
                f"从 ({support_x},{support_y}) 滑到 ({x},{y}) 失败: {exc}",
                {
                    "x": x,
                    "y": y,
                    "support": [support_x, support_y],
                    "held": held,
                    "hint": "如果提示鼠标没移动：把游戏窗口点到前台；如果游戏以管理员运行，Agent 也要用管理员启动",
                },
            )
        time.sleep(0.40)
        after = context.monitor.tile_at(x, y)
        if after == before and after in {".", " "}:
            return SkillResult(
                False,
                "not_placed",
                f"滑过去之后 ({x},{y}) 还是空气：手上拿的可能是工具，或者支撑块不是实心方块",
                {
                    "x": x,
                    "y": y,
                    "support": [support_x, support_y],
                    "direction": direction,
                    "held": held,
                    "slide": slide_info,
                    "hint": "先 move_item 把方块放进快捷栏，再 select_slot；确认支撑块是泥土/石头/木块",
                },
            )
        return SkillResult(
            True,
            "success",
            f"从 ({support_x},{support_y}) 往{'上' if direction == 'up' else ('左' if direction == 'left' else '右')}滑，"
            f"在 ({x},{y}) 放下了 {held}",
            {
                "x": x,
                "y": y,
                "support": [support_x, support_y],
                "direction": direction,
                "held": held,
                "tile": after,
                "slide": slide_info,
            },
        )


class BuildPillarSkill(BaseSkill):
    """在人物身体旁边竖着叠 N 格（默认 6 格），搭完可以用 jump_onto 跳上去。

    关键：只放在身体左边或右边一列，绝不放在人物站着的 2x3 身体里。
    从地面往上、一格一格叠：每一格的支撑就是下面那一块（第一格是地面）。
    """

    name = "build_pillar"
    description = "在身体旁边竖着叠 N 格（默认 6），从地面往上，搭完可跳上去"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        try:
            height = int(params.get("height", 6) or 6)
        except (TypeError, ValueError):
            height = 6
        height = max(1, min(8, height))
        side = str(params.get("side", "") or "").strip().lower()

        if bool(getattr(context.monitor, "inventory_open", False)):
            return SkillResult(
                False,
                "inventory_open",
                "背包还开着：先 close_inventory 再搭",
                {"hint": "先调 close_inventory"},
            )

        px, py = context.monitor.player_tile
        try:
            body = context.executor.get_current_position().body_tiles
        except Exception:
            body = None
        if body is None:
            return SkillResult(False, "no_body", "读不到人物身体位置，先 terraria_see 再看")

        ground_y = None
        for dy in range(1, 6):
            if context.monitor.tile_at(px, py + dy) in PlaceBlockSkill.SUPPORT_CHARS:
                ground_y = py + dy
                break
        if ground_y is None:
            return SkillResult(
                False,
                "no_ground",
                "脚下找不到实心地面，先站到地面上再搭",
                {"player": [px, py]},
            )

        candidates: list[tuple[str, int]] = []
        if side in ("", "right"):
            candidates.append(("right", body.left + 2))
        if side in ("", "left"):
            candidates.append(("left", body.left - 1))
        column = None
        chosen_side = ""
        for name, x in candidates:
            if 0 <= ground_y - height - 1:
                blocked = False
                for i in range(height):
                    ch = context.monitor.tile_at(x, ground_y - 1 - i)
                    if ch not in {".", " ", "P"}:
                        blocked = True
                        break
                if not blocked:
                    column = x
                    chosen_side = name
                    break
        if column is None:
            return SkillResult(
                False,
                "no_space",
                "身体左右两边都放不下这么高的柱子",
                {"height": height, "player": [px, py], "sides": [c[1] for c in candidates]},
            )

        placed = 0
        last = None
        for i in range(height):
            ty = ground_y - 1 - i
            result = PlaceBlockSkill().run(context, x=column, y=ty)
            if not result.ok:
                return SkillResult(
                    False,
                    result.status,
                    f"第 {i + 1}/{height} 格失败：{result.message}",
                    {
                        "placed": placed,
                        "column": column,
                        "side": chosen_side,
                        "top": [column, ground_y - max(1, placed)],
                    },
                )
            placed += 1
            last = result
            time.sleep(0.05)

        top_y = ground_y - height
        context.emit(f"搭好 {height} 格柱子：({column},{ground_y - 1}) 到 ({column},{top_y})", "success")
        return SkillResult(
            True,
            "success",
            f"在人物{('右边' if chosen_side == 'right' else '左边')}竖着搭了 {height} 格，柱顶是 ({column},{top_y})；要上去就 jump_onto 它",
            {
                "placed": placed,
                "column": column,
                "side": chosen_side,
                "base": [column, ground_y - 1],
                "top": [column, top_y],
                "last": last.data if last else {},
            },
        )


class PlaceWallSkill(BaseSkill):
    """把手上的背景墙刷到指定格；墙不需要支撑，但格子不能被方块占着。"""

    name = "place_wall"
    description = "把手上的背景墙放到指定格（不需要支撑）"

    SOLID = {"D", "S", "W", "M", "?"}

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        try:
            x = int(params.get("x"))
            y = int(params.get("y"))
        except (TypeError, ValueError):
            return SkillResult(False, "bad_target", "place_wall 需要整数 x 和 y")

        if bool(getattr(context.monitor, "inventory_open", False)):
            return SkillResult(
                False,
                "inventory_open",
                "背包还开着：先 close_inventory 再放墙",
                {"hint": "先调 close_inventory"},
            )

        def has_wall(wx: int, wy: int) -> bool:
            fn = getattr(context.monitor, "wall_at", None)
            if callable(fn):
                try:
                    return bool(fn(wx, wy))
                except Exception:
                    return False
            return context.monitor.tile_at(wx, wy) == "B"

        held_tool = context.monitor.current_tool or {}
        try:
            create_wall = int(held_tool.get("create_wall", 0) or 0)
        except (TypeError, ValueError):
            create_wall = 0
        if create_wall < 0:
            return SkillResult(
                False,
                "not_holding_wall",
                f"手上是「{held_tool.get('name', '空手')}」，不是背景墙：先切到木墙再刷",
                {"held": held_tool.get("name"), "hint": "先 select_slot 选中「木墙」再 place_wall"},
            )

        before = context.monitor.tile_at(x, y)
        anchored = any(
            has_wall(x + dx, y + dy)
            or context.monitor.tile_at(x + dx, y + dy) in self.SOLID
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
        )
        if has_wall(x, y) or before == "B":
            return SkillResult(
                True, "already_walled", f"({x},{y}) 已经有背景墙了", {"x": x, "y": y, "tile": "B"}
            )
        if before in self.SOLID:
            return SkillResult(
                False,
                "block_occupied",
                f"({x},{y}) 被方块占着，墙只能刷在空气/背景格",
                {"x": x, "y": y, "tile": before},
            )
        if not anchored:
            return SkillResult(
                False,
                "needs_anchor",
                f"({x},{y}) 悬空：墙要先贴着实心方块或已有墙放，再往外扩",
                {"x": x, "y": y, "hint": "从地面/已有墙旁边开始刷，一格一格往外扩"},
            )

        held = str(context.monitor.current_tool.get("name", "")) or "空手"
        try:
            context.executor.place_wall_at(
                x, y, screen_mapper=context.monitor.world_to_screen
            )
        except ExecutorError as exc:
            return SkillResult(
                False, "wall_place_failed", f"放墙 ({x},{y}) 失败: {exc}", {"x": x, "y": y, "held": held}
            )
        time.sleep(0.42)
        try:
            context.monitor.get_state()
        except Exception:
            pass
        after = context.monitor.tile_at(x, y)
        if not has_wall(x, y):
            return SkillResult(
                False,
                "not_walled",
                f"点完 ({x},{y}) 还是没有墙：手上拿的是不是「木墙」？距离是不是太远？",
                {"x": x, "y": y, "held": held, "tile": after},
            )
        return SkillResult(
            True, "success", f"在 ({x},{y}) 刷了墙（{held}）", {"x": x, "y": y, "held": held}
        )


class PlaceWallAreaSkill(BaseSkill):
    """矩形区域批量刷墙：逐格验证，连续失败就停。"""

    name = "place_wall_area"
    description = "在一块矩形区域里批量放背景墙（最多 60 格）"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        try:
            x1 = int(params.get("x1"))
            y1 = int(params.get("y1"))
            x2 = int(params.get("x2"))
            y2 = int(params.get("y2"))
        except (TypeError, ValueError):
            return SkillResult(False, "bad_target", "place_wall_area 需要 x1,y1,x2,y2")

        left, right = min(x1, x2), max(x1, x2)
        top, bottom = min(y1, y2), max(y1, y2)
        count = (right - left + 1) * (bottom - top + 1)
        if count > 60:
            return SkillResult(
                False,
                "area_too_big",
                f"区域有 {count} 格，一次最多 60 格",
                {"count": count},
            )

        def has_wall(wx: int, wy: int) -> bool:
            fn = getattr(context.monitor, "wall_at", None)
            if callable(fn):
                try:
                    return bool(fn(wx, wy))
                except Exception:
                    return False
            return context.monitor.tile_at(wx, wy) == "B"

        px, py = context.monitor.player_tile
        placed = skipped = failed = 0
        out_of_range: list[list[int]] = []
        failures: list[list[Any]] = []
        blocked: list[list[int]] = []
        remaining = [(x, y) for y in range(top, bottom + 1) for x in range(left, right + 1)]
        while remaining:
            progress = False
            still: list[tuple[int, int]] = []
            for x, y in remaining:
                # 已经有墙：直接算“已有”，不点鼠标，也不算失败
                if has_wall(x, y):
                    skipped += 1
                    continue
                # 超出 5 格：不点，记成“太远”，别打断后面的格子
                if max(abs(x - px), abs(y - py)) > 5:
                    out_of_range.append([x, y])
                    continue
                result = PlaceWallSkill().run(context, x=x, y=y)
                if result.ok and result.status != "already_walled":
                    placed += 1
                    progress = True
                elif result.status == "already_walled":
                    skipped += 1
                elif result.status == "needs_anchor":
                    still.append((x, y))
                else:
                    failed += 1
                    failures.append([x, y, result.status])
                    if failed >= 3:
                        break
            if failed >= 3:
                break
            if not progress:
                blocked = [[x, y] for x, y in still]
                break
            remaining = still

        parts = [f"成功 {placed}", f"已有 {skipped}"]
        if out_of_range:
            parts.append(f"太远没点 {len(out_of_range)}")
        parts.append(f"失败 {failed}")
        text = "刷墙：" + "，".join(parts)
        if blocked:
            text += f"；还有 {len(blocked)} 格悬空（先从有方块/墙的边上一格开始）"
        hint = ""
        if out_of_range:
            hint = "“太远”的格子离你超过 5 格：站到区域中间，或者把区域分成几小块再刷"
        return SkillResult(
            failed == 0 and not blocked,
            "success" if failed == 0 and not blocked else "partial",
            text,
            {
                "placed": placed,
                "skipped": skipped,
                "failed": failed,
                "out_of_range": out_of_range[:20],
                "failures": failures,
                "blocked": blocked[:20],
                "hint": hint,
                "area": [left, top, right, bottom],
            },
        )


class CheckHouseSkill(BaseSkill):
    """检查附近的房子是不是合格的 NPC 住房（粗略但够用）。"""

    name = "check_house"
    description = "检查附近房子的空间/墙/门/桌椅/光源/地板"

    # 门（O）算封闭边界：关着的门能挡洪水填充；平台（=）仍算可通行。
    PASSABLE = {".", " ", "B", "L", "A", "C", "=", "H"}
    SOLID = {"D", "S", "W", "M", "?"}

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        grid = context.monitor.get_grid()
        if not grid:
            return SkillResult(False, "no_grid", "地形网格为空")
        radius = len(grid) // 2
        rows = len(grid)
        cols = len(grid[0]) if grid else 0

        def char_at(r: int, c: int) -> str:
            if 0 <= r < rows and 0 <= c < cols:
                return grid[r][c]
            return "?"

        # 从人物所在格开始洪水填充“可通行”的格子
        start = (radius, radius)
        if char_at(*start) not in self.PASSABLE:
            # 找附近一个可通行的起点（人物可能站在家具格）
            found = None
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    if char_at(radius + dr, radius + dc) in self.PASSABLE:
                        found = (radius + dr, radius + dc)
                        break
                if found:
                    break
            if found is None:
                return SkillResult(False, "no_space", "人物附近没有可通行空间，看不到房子内部")
            start = found

        seen = {start}
        stack = [start]
        touches_border = False
        while stack:
            r, c = stack.pop()
            if r in (0, rows - 1) or c in (0, cols - 1):
                touches_border = True
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nr, nc = r + dr, c + dc
                if not (0 <= nr < rows and 0 <= nc < cols):
                    continue
                if (nr, nc) in seen:
                    continue
                if char_at(nr, nc) in self.PASSABLE:
                    seen.add((nr, nc))
                    stack.append((nr, nc))

        interior = len(seen)
        px, py = context.monitor.player_tile
        missing_walls = [
            (px + (c - radius), py + (r - radius))
            for r, c in seen
            if char_at(r, c) in {".", " "}
        ]
        has_door = any(char_at(r, c) in {"O", "="} for r, c in seen)
        has_table = any(char_at(r, c) == "A" for r, c in seen)
        has_chair = any(char_at(r, c) == "C" for r, c in seen)
        has_light = any(char_at(r, c) == "L" for r, c in seen)
        floor_cells = [
            (r, c) for r, c in seen
            if char_at(r + 1, c) in self.SOLID
        ]
        checks = {
            "enclosed": not touches_border,
            "space_30": interior >= 30,
            "walls": len(missing_walls) == 0,
            "door_or_platform": has_door,
            "table": has_table,
            "chair": has_chair,
            "light": has_light,
            "floor": len(floor_cells) >= 1,
        }
        ok = all(checks.values())
        missing = [name for name, good in checks.items() if not good]
        return SkillResult(
            ok,
            "success" if ok else "not_valid_house",
            ("房子合格：空间 %d 格，墙/门/桌椅/光/地板都齐" % interior)
            if ok
            else ("房子还不合格，缺：" + "、".join(missing) + f"（内部 {interior} 格，缺墙 {len(missing_walls)} 格）"),
            {
                "checks": checks,
                "interior": interior,
                "missing_walls": missing_walls[:20],
                "floor_cells": len(floor_cells),
            },
        )


class BuildFrameSkill(BaseSkill):
    """按图纸搭悬空方框：先搭 6 格柱子 → 跳上柱顶 → 往右 11 格 →
    右端往上 5 格 → 顶部再往左 11 格回来，最后补左边一列围成方框。

    每一步都按“先放一格、再往目标方向走一格”的节奏，保证支撑块始终在 5 格内。
    """

    name = "build_frame"
    description = "按图纸搭悬空方框（6 格柱子 → 右 11 → 上 5 → 左 11 回来）"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        def clamp(value: Any, low: int, high: int, default: int) -> int:
            try:
                return max(low, min(high, int(value)))
            except (TypeError, ValueError):
                return default

        width = clamp(params.get("width", 11), 3, 20, 11)
        height = clamp(params.get("height", 5), 2, 10, 5)
        pillar = clamp(params.get("pillar", 6), 2, 6, 6)
        side_name = str(params.get("direction", "right") or "right").strip().lower()
        direction = 1 if side_name != "left" else -1

        if bool(getattr(context.monitor, "inventory_open", False)):
            return SkillResult(False, "inventory_open", "背包还开着：先 close_inventory 再搭", {})

        px, py = context.monitor.player_tile
        try:
            body = context.executor.get_current_position().body_tiles
        except Exception:
            body = None
        if body is None:
            return SkillResult(False, "no_body", "读不到人物身体位置，先 terraria_see 再看")

        ground_y = None
        for dy in range(1, 6):
            if context.monitor.tile_at(px, py + dy) in PlaceBlockSkill.SUPPORT_CHARS:
                ground_y = py + dy
                break
        if ground_y is None:
            return SkillResult(False, "no_ground", "脚下找不到实心地面，先站到平地上", {"player": [px, py]})

        # 柱子放在身体右侧/左侧一列；方框从柱子顶往该方向延伸。
        # 柱子底下必须是实地，否则第一格就放不上；优先选朝向那一侧，不行再换另一边。
        preferred = body.left + 2 if direction > 0 else body.left - 1
        other = body.left - 1 if direction > 0 else body.left + 2
        x0 = None
        for cand in (preferred, other):
            if context.monitor.tile_at(cand, ground_y) not in PlaceBlockSkill.SUPPORT_CHARS:
                continue
            if all(
                context.monitor.tile_at(cand, ground_y - 1 - i) in {".", " ", "P"}
                for i in range(pillar)
            ):
                x0 = cand
                break
        if x0 is None:
            return SkillResult(
                False,
                "no_space",
                "身体左右两边都没有“脚下实地 + 上方空”的柱子位置：先走到平地上",
                {"player": [px, py], "ground": ground_y},
            )
        top_y = ground_y - pillar

        placed = 0

        def place(x: int, y: int) -> str | None:
            nonlocal placed
            result = PlaceBlockSkill().run(context, x=x, y=y)
            if not result.ok:
                return result.status
            placed += 1
            time.sleep(0.05)
            return None

        def walk(x: float) -> None:
            try:
                context.executor.move_to_x(float(x), timeout_s=2.0)
            except ExecutorError:
                pass
            time.sleep(0.1)

        # 1) 搭 6 格柱子
        context.emit(f"搭柱子：({x0},{ground_y - 1}) 往上 {pillar} 格", "info")
        for i in range(pillar):
            err = place(x0, ground_y - 1 - i)
            if err:
                return SkillResult(
                    False, err, f"柱子第 {i + 1} 格失败", {"placed": placed, "column": x0}
                )

        # 2) 跳上柱顶；6 格是跳跃极限，直接跳不稳，就在身体外侧垫一级台阶再跳
        context.emit(f"跳上柱顶 ({x0},{top_y})", "info")
        jump = JumpOntoTileSkill().run(context, x=x0, y=top_y)
        if not jump.ok:
            step_x = x0 - 3 * direction
            context.emit(f"直接跳不上，先搭一级垫脚台阶 ({step_x},{ground_y - 1})", "warning")
            err = place(step_x, ground_y - 1)
            if not err:
                walk(step_x + 0.5 * direction)
                time.sleep(0.2)
                hop = getattr(context.executor, "hop_toward", None)
                try:
                    if callable(hop):
                        hop(
                            x0 + 0.5,
                            hold_s=min(0.8, 0.12 + 5 * 0.11),
                            timeout_s=3.0,
                        )
                except ExecutorError:
                    pass
                time.sleep(0.2)
                px2, py2 = context.monitor.player_tile
                if abs(px2 - x0) <= 1 and py2 == top_y - 2:
                    jump = SkillResult(True, "success", "从垫脚台阶跳上柱顶", {})
        if not jump.ok:
            return SkillResult(
                False,
                "jump_to_pillar_failed",
                f"柱顶跳不上去：{jump.message}",
                {"placed": placed, "column": x0, "hint": "把 pillar 降到 5 再试，或先手动站上柱顶"},
            )

        # 3) 往右（或左）一格一格铺地板：放一格 → 走一格
        context.emit(f"铺地板：往{'右' if direction > 0 else '左'} {width} 格", "info")
        for i in range(1, width + 1):
            tx = x0 + i * direction
            err = place(tx, top_y)
            if err:
                return SkillResult(
                    False, err, f"地板第 {i} 格失败", {"placed": placed, "at": [tx, top_y]}
                )
            walk(tx + 0.5 * direction)

        x_end = x0 + width * direction
        # 4) 右端往上 5 格：先往回退一格，别把自己站的位置堵住
        context.emit(f"右端往上 {height} 格：({x_end})", "info")
        walk(x_end - 0.5 * direction)
        for i in range(1, height + 1):
            err = place(x_end, top_y - i)
            if err:
                return SkillResult(
                    False, err, f"右端第 {i} 格失败", {"placed": placed, "at": [x_end, top_y - i]}
                )

        # 5) 顶部往左（回）width 格：放一格 → 走一格
        context.emit(f"顶部往{'左' if direction > 0 else '右'}铺 {width} 格", "info")
        for i in range(1, width + 1):
            tx = x_end - i * direction
            err = place(tx, top_y - height)
            if err:
                return SkillResult(
                    False, err, f"顶行第 {i} 格失败", {"placed": placed, "at": [tx, top_y - height]}
                )
            walk(tx - 0.5 * direction)

        # 6) 左边一列补到顶（顶行最左那格已经放过了）
        context.emit("补左边一列，围成方框", "info")
        for i in range(1, height):
            err = place(x0, top_y - i)
            if err:
                return SkillResult(
                    False, err, f"左列第 {i} 格失败", {"placed": placed, "at": [x0, top_y - i]}
                )

        return SkillResult(
            True,
            "success",
            f"方框搭好了：柱子 ({x0},{ground_y - 1})~({x0},{top_y})，"
            f"地板 ({x0},{top_y})→({x_end},{top_y})，右端到 ({x_end},{top_y - height})，"
            f"顶部回到 ({x0},{top_y - height})，共 {placed} 格",
            {
                "placed": placed,
                "column": x0,
                "top": top_y,
                "end": x_end,
                "width": width,
                "height": height,
                "pillar": pillar,
                "bounds": [min(x0, x_end), top_y - height, max(x0, x_end), top_y],
            },
        )


class BuildBlueprintSkill(BaseSkill):
    """按主人画的蓝图施工：rows 是从下往上的 '0/1' 字符串（'1' = 放方块）。

    蓝图底边贴着你脚下的地面，从身体旁边一列开始往 direction 方向铺。
    施工顺序从下往上；被自己身体挡住的格子会先跳过，人物挪一格后再补。
    """

    name = "build_blueprint"
    description = "按主人画的蓝图施工（严格照图，不自由发挥）"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        rows = params.get("rows")
        if not isinstance(rows, list) or not rows:
            return SkillResult(False, "bad_blueprint", "蓝图是空的：让主人在面板里画好并保存")
        rows = [str(r) for r in rows]
        width = max(len(r) for r in rows)
        rows = [r.ljust(width, "0") for r in rows]
        direction = -1 if str(params.get("direction", "right")).lower() == "left" else 1

        if bool(getattr(context.monitor, "inventory_open", False)):
            return SkillResult(False, "inventory_open", "背包还开着：先 close_inventory 再施工", {})

        px, py = context.monitor.player_tile
        try:
            body = context.executor.get_current_position().body_tiles
        except Exception:
            body = None
        if body is None:
            return SkillResult(False, "no_body", "读不到人物位置，先 terraria_see 再看")

        ground_y = None
        for dy in range(1, 6):
            if context.monitor.tile_at(px, py + dy) in PlaceBlockSkill.SUPPORT_CHARS:
                ground_y = py + dy
                break
        if ground_y is None:
            return SkillResult(False, "no_ground", "脚下找不到实心地面，先站到平地上", {"player": [px, py]})

        anchor_x = body.left + 2 if direction > 0 else body.left - 1
        # 以“最下面有格子的一行”贴地：主人在网格底部留空也没关系。
        base_row = 0
        for ri, row in enumerate(rows):
            if "1" in row:
                base_row = ri
                break
        anchor_y = ground_y - 1 + base_row
        cells: list[tuple[int, int]] = []
        for ri, row in enumerate(rows):
            for ci, ch in enumerate(row):
                if ch == "1":
                    cells.append((anchor_x + ci * direction, anchor_y - ri))
        if not cells:
            return SkillResult(False, "empty_blueprint", "蓝图里没有格子")
        # 从下往上，同一行从离人物近的往远处
        cells.sort(key=lambda c: (-c[1], abs(c[0] - px)))

        placed = 0
        skipped = 0
        failures: list[list[Any]] = []
        remaining = cells
        for _pass in range(8):
            deferred: list[tuple[int, int]] = []
            for (x, y) in remaining:
                if context.monitor.tile_at(x, y) in PlaceBlockSkill.SUPPORT_CHARS:
                    skipped += 1
                    continue
                # 太远就先往目标走，保持支撑在 5 格内
                if abs(x - px) > 4:
                    try:
                        context.executor.move_to_x(x - 2 * direction, timeout_s=2.0)
                    except ExecutorError:
                        pass
                    px, py = context.monitor.player_tile
                result = PlaceBlockSkill().run(context, x=x, y=y)
                if result.ok:
                    placed += 1
                elif result.status in {"in_player_body", "needs_anchor", "no_support"}:
                    deferred.append((x, y))
                else:
                    failures.append([x, y, result.status])
            if not deferred:
                break
            # 挪一格再补：身体挡住的格子，走开后就能放
            try:
                context.executor.move_to_x(px + direction, timeout_s=1.5)
            except ExecutorError:
                pass
            px, py = context.monitor.player_tile
            remaining = deferred

        total = len(cells)
        ok = placed >= total
        return SkillResult(
            ok,
            "success" if ok else "partial",
            f"按蓝图施工：计划 {total} 格，放下 {placed}，已有 {skipped}，还剩 {len(deferred) if not ok else 0} 格"
            + ("" if ok else "（剩下的可能被身体/地形挡住，把人物挪开再调一次）"),
            {
                "total": total,
                "placed": placed,
                "skipped": skipped,
                "remaining": [[x, y] for x, y in (deferred if not ok else [])][:30],
                "failures": failures[:20],
                "bounds": [min(x for x, _ in cells), min(y for _, y in cells), max(x for x, _ in cells), max(y for _, y in cells)],
            },
        )


class FindHotbarSkill(BaseSkill):
    """在快捷栏 1-10 里按名字找物品，只返回槽位，不搬东西。"""

    name = "find_hotbar"
    description = "在快捷栏 1-10 里按名字找物品，返回第几格"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        query = str(params.get("item") or params.get("query") or "").strip()
        if not query:
            return SkillResult(False, "no_query", "find_hotbar 需要 item（物品名）")
        try:
            context.monitor.get_state()
        except Exception:
            pass
        inventory = context.monitor.inventory
        hotbar = [
            {
                "slot": int(item.get("slot", 0) or 0),
                "name": str(item.get("name", "")),
                "stack": _item_stack(item),
            }
            for item in inventory
            if 1 <= int(item.get("slot", 0) or 0) <= 10 and _item_stack(item) > 0
        ]
        hit = next((item for item in hotbar if item["name"] == query), None)
        if hit is None:
            hit = next((item for item in hotbar if query in item["name"]), None)
        if hit is None:
            return SkillResult(
                False,
                "not_in_hotbar",
                f"快捷栏里没有「{query}」",
                {
                    "query": query,
                    "hotbar": [f"{item['slot']}:{item['name']}" for item in hotbar],
                    "hint": "先在背包里查，再用 move_item 搬进快捷栏",
                },
            )
        selected = int(context.monitor.current_tool.get("slot", 1) or 1)
        suffix = "（当前选中）" if int(hit["slot"]) == selected else ""
        return SkillResult(
            True,
            "success",
            f"「{hit['name']}」在快捷栏 slot {hit['slot']}{suffix}",
            {"slot": hit["slot"], "name": hit["name"], "selected": selected},
        )


class InventoryToggleMixin:
    """开关背包共用：读“最新一帧”状态，读不到就返回 None（模拟模式/没窗口）。"""

    @staticmethod
    def inventory_state(context: SkillContext) -> bool | None:
        reader = getattr(context.monitor, "reader", None)
        if reader is None:
            return None
        try:
            data = reader.latest(max_age_s=0.8)
        except Exception:
            return None
        if "inventory_open" in data:
            return bool(data.get("inventory_open"))
        return None


class OpenInventorySkill(BaseSkill, InventoryToggleMixin):
    """打开背包：先读状态，已经开着就绝不按键；关着才按一次，然后验证。"""

    name = "open_inventory"
    description = "打开背包（按一次验一次；已经开着就不按）"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        key = str(params.get("key", "") or "").strip() or None
        before = self.inventory_state(context)
        if before is True:
            return SkillResult(
                True,
                "success",
                "背包本来就开着，没有再按（避免误关）",
                {"before": True, "after": True, "pressed": False, "changed": False},
            )
        try:
            pressed = context.executor.press_inventory(key)
        except ExecutorError as exc:
            return SkillResult(
                False,
                "game_not_focused",
                f"游戏不在前台，背包键没按出去：{exc}",
                {"hint": "点一下游戏窗口，或者把挡在前面的 QQ/设置窗口移开，再试"},
            )
        time.sleep(0.55)
        try:
            context.monitor.get_state()
        except Exception:
            pass
        after = self.inventory_state(context)
        if after is None:
            return SkillResult(
                True,
                "sent",
                f"背包键已发送（按了 {1 if pressed else 0} 次，读不到状态无法验证）",
                {"before": before, "after": None, "pressed": pressed, "changed": None},
            )
        if after:
            return SkillResult(
                True,
                "success",
                f"背包已打开（按了 1 次{key or 'Esc'}）",
                {"before": before, "after": True, "pressed": pressed, "changed": True},
            )
        return SkillResult(
            False,
            "toggle_ineffective",
            f"按了 1 次{key or 'Esc'}，背包还是关着：按键可能没送进游戏",
            {
                "before": before,
                "after": False,
                "pressed": pressed,
                "changed": False,
                "hint": "确认游戏在前台；如果改过背包键，到插件配置里把「背包键」改成游戏里的那个键",
            },
        )


class CloseInventorySkill(BaseSkill, InventoryToggleMixin):
    """关闭背包：先读状态，关着就不动；开着只按一次，然后验证。

    Esc 是“开/关”同一个键，连按会来回开关。所以这里一次调用最多按一次，
    并且把“按之前 / 按之后”都写进回执，方便主人看清到底是谁把它开开的。
    """

    name = "close_inventory"
    description = "关闭背包（只按一次；把前后状态都报给你）"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        key = str(params.get("key", "") or "").strip() or None
        before = self.inventory_state(context)
        if before is False:
            return SkillResult(
                True,
                "success",
                "背包本来就是关着的，没有再按",
                {"before": False, "after": False, "pressed": False, "changed": False},
            )
        try:
            pressed = context.executor.press_inventory(key)
        except ExecutorError as exc:
            return SkillResult(
                False,
                "game_not_focused",
                f"游戏不在前台，背包键没按出去：{exc}",
                {"before": before, "hint": "点一下游戏窗口，或者把挡在前面的 QQ/设置窗口移开，再试"},
            )
        time.sleep(0.55)
        try:
            context.monitor.get_state()
        except Exception:
            pass
        after = self.inventory_state(context)
        if after is None:
            return SkillResult(
                True,
                "sent",
                f"背包键已发送（按了 {1 if pressed else 0} 次，读不到状态无法验证）",
                {"before": before, "after": None, "pressed": pressed, "changed": None},
            )
        if after:
            return SkillResult(
                False,
                "still_open",
                f"按了 1 次{key or 'Esc'}，背包还是开着（before={before}, after=True）",
                {
                    "before": before,
                    "after": True,
                    "pressed": pressed,
                    "changed": False,
                    "hint": "如果屏幕上有箱子/商店，Esc 可能关不掉它：先手动关一下，或者换个背包键再试",
                },
            )
        return SkillResult(
            True,
            "success",
            f"背包已关闭（按了 1 次{key or 'Esc'}，before={before}, after=False）",
            {"before": before, "after": False, "pressed": pressed, "changed": True},
        )


class CheckHotbarSkill(BaseSkill):
    """只看快捷栏 1-10 格，不看主背包。"""

    name = "check_hotbar"
    description = "查看快捷栏 1-10 格里有什么"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        try:
            context.monitor.get_state()
        except Exception:
            pass
        inventory = context.monitor.inventory
        filled = [
            {
                "slot": int(item.get("slot", 0) or 0),
                "name": str(item.get("name", "")),
                "stack": _item_stack(item),
                "type": int(item.get("type", 0) or 0),
            }
            for item in inventory
            if 1 <= int(item.get("slot", 0) or 0) <= 10
            and _item_stack(item) > 0
        ]
        used = {item["slot"] for item in filled}
        empty = [slot for slot in range(1, 11) if slot not in used]
        selected = int(context.monitor.current_tool.get("slot", 1) or 1)
        return SkillResult(
            True,
            "success",
            f"快捷栏 {len(filled)} 格有东西，当前选中 slot {selected}",
            {"slots": filled, "empty": empty, "selected": selected},
        )


class CheckInventorySkill(BaseSkill):
    """只看主背包 11-50 格。"""

    name = "check_inventory"
    description = "查看主背包 11-50 格里有什么"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        try:
            context.monitor.get_state()
        except Exception:
            pass
        inventory = context.monitor.inventory
        filled = [
            {
                "slot": int(item.get("slot", 0) or 0),
                "name": str(item.get("name", "")),
                "stack": _item_stack(item),
                "type": int(item.get("type", 0) or 0),
            }
            for item in inventory
            if 11 <= int(item.get("slot", 0) or 0) <= 50
            and _item_stack(item) > 0
        ]
        used = {item["slot"] for item in filled}
        empty = [slot for slot in range(11, 51) if slot not in used]
        return SkillResult(
            True,
            "success",
            f"主背包 {len(filled)} 格有东西，空位 {len(empty)} 个",
            {"slots": filled, "empty": empty},
        )


class SelectSlotSkill(BaseSkill):
    """切换快捷栏 1-10。"""

    name = "select_slot"
    description = "切换快捷栏到指定格（1-10）"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        try:
            slot = int(params.get("slot"))
        except (TypeError, ValueError):
            return SkillResult(False, "bad_slot", "select_slot 需要 slot=1..10")
        if not 1 <= slot <= 10:
            return SkillResult(False, "bad_slot", "slot 必须在 1..10 之间")
        context.executor.select_hotbar_slot(slot)
        time.sleep(0.2)
        actual = 0
        try:
            data = context.monitor.reader.latest(max_age_s=0.8)
            actual = int(data.get("selected_slot", 0) or 0)
        except Exception:
            actual = 0
        try:
            context.monitor.get_state()
        except Exception:
            pass
        tool = context.monitor.current_tool
        if actual and actual != slot:
            return SkillResult(
                False,
                "switch_not_applied",
                f"按了数字键但游戏还停在 slot {actual}：可能游戏没前台，或者键位改了",
                {"wanted": slot, "actual": actual},
            )
        return SkillResult(
            True,
            "success",
            f"切到 slot {slot}：{tool.get('name', '空手')}",
            {"slot": slot, "tool": tool, "actual": actual or slot},
        )


class MoveItemSkill(BaseSkill):
    """把背包里的东西搬到另一个格子（通常是搬进快捷栏）。"""

    name = "move_item"
    description = "把物品从背包搬到指定格子（例如放进快捷栏），可指定物品名或源格子"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        try:
            to_slot = int(params.get("to_slot"))
        except (TypeError, ValueError):
            return SkillResult(False, "bad_slot", "move_item 需要 to_slot=1..50")
        if not 1 <= to_slot <= 50:
            return SkillResult(False, "bad_slot", "to_slot 必须在 1..50 之间")

        ui = InventoryUI(context.executor, context.monitor)
        if not ui.grid_visible():
            # 只开着箱子/商店时 playerInventory 也是 True，但格子没画出来；
            # 这时候点坐标会点到别的东西上。
            return SkillResult(
                False,
                "inventory_grid_closed",
                "看不到背包格子（可能只开着箱子/商店）：先 open_inventory 把物品栏打开",
                {"hint": "先调 open_inventory；它会按一次 Esc 并确认格子出现"},
            )

        item_name = str(params.get("item", "") or "").strip()
        try:
            from_slot = int(params.get("from_slot", 0) or 0)
        except (TypeError, ValueError):
            from_slot = 0

        try:
            context.monitor.get_state()
        except Exception:
            pass
        inventory = context.monitor.inventory

        source: dict | None = None
        if item_name:
            source = next(
                (
                    i
                    for i in inventory
                    if str(i.get("name", "")) == item_name
                    and _item_stack(i) > 0
                    and int(i.get("slot", 0) or 0) != to_slot
                ),
                None,
            )
            if source is None:
                return SkillResult(
                    False,
                    "item_not_found",
                    f"背包里没有「{item_name}」（或者它已经在 slot {to_slot} 了）",
                    {"item": item_name},
                )
        elif from_slot:
            source = next(
                (i for i in inventory if int(i.get("slot", 0) or 0) == from_slot),
                None,
            )
            if source is None:
                return SkillResult(
                    False, "slot_empty", f"slot {from_slot} 是空的", {"from_slot": from_slot}
                )
        else:
            return SkillResult(
                False, "no_source", "move_item 需要 item（物品名）或 from_slot"
            )

        source_slot = int(source.get("slot", 0) or 0)
        name = str(source.get("name", ""))
        if source_slot == to_slot:
            return SkillResult(
                True,
                "success",
                f"「{name}」已经在 slot {to_slot}",
                {"from_slot": source_slot, "to_slot": to_slot, "name": name},
            )

        ui = InventoryUI(context.executor, context.monitor)
        try:
            # 先悬停校准源格子：如果算出来的点上不是这个东西，就在 ±1 格内挪着找，
            src_point, (dx, dy) = ui.center_for_item(source_slot, name)
            dst_x, dst_y = ui.center(to_slot)
            dst_point = (dst_x + dx, dst_y + dy)
            context.executor.ui_click(*src_point)
            time.sleep(0.30)
            context.executor.ui_click(*dst_point)
            time.sleep(0.30)
            # 目标格原本有东西时会被换到光标上，再点回源格，保证不把东西丢在光标上。
            context.executor.ui_click(*src_point)
            time.sleep(0.30)
        except Exception as exc:
            return SkillResult(False, "move_failed", f"搬动失败: {exc}")
        try:
            context.monitor.get_state()
        except Exception:
            pass
        moved = next(
            (i for i in context.monitor.inventory if int(i.get("slot", 0) or 0) == to_slot),
            None,
        )
        moved_name = str(moved.get("name", "")) if moved else ""
        if moved is None or moved_name != name:
            return SkillResult(
                False,
                "move_not_verified",
                f"点完没确认到 slot {to_slot} 上是「{name}」，实际是「{moved_name or '空'}」",
                {"from_slot": source_slot, "to_slot": to_slot, "name": name, "now": moved_name},
            )
        return SkillResult(
            True,
            "success",
            f"已把「{name}」从 slot {source_slot} 搬到 slot {to_slot}",
            {"from_slot": source_slot, "to_slot": to_slot, "name": name},
        )


class HousePrepSkill(BaseSkill):
    """准备 NPC 房屋：工作台拿出来/补做、放地上、做门桌椅子木墙。"""

    name = "house_prep"
    description = "准备并放置工作台，然后制作门、桌子、椅子、木墙"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        crafted: list[str] = []
        slot = ensure_workbench_slot(context)
        if slot is None:
            return SkillResult(False, "no_workbench", "找不到工作台，也补做失败")
        InventoryUI(context.executor, context.monitor).close()
        context.executor.select_hotbar_slot(slot)
        px, py = context.monitor.player_tile
        support = BuildSkill()._find_support(context, px, py)
        if support is None:
            return SkillResult(False, "no_support", "附近找不到放工作台的地面")
        try:
            context.executor.place_block(
                support[0], support[1], screen_mapper=context.monitor.world_to_screen
            )
            time.sleep(0.4)
        except ExecutorError as exc:
            return SkillResult(False, "place_workbench_failed", f"放工作台失败: {exc}")
        context.monitor.set_action("工作台已放置，开始制作家具")
        for name in ("木门", "木桌", "木椅", "木墙"):
            result = CraftSkill().run(context, item=name)
            if result.ok:
                crafted.append(name)
                context.emit(f"已制作: {name}", "success")
            else:
                context.emit(f"制作 {name} 失败: {result.message}", "warning")
        return SkillResult(
            True,
            "partial" if len(crafted) < 5 else "success",
            f"工作台已放置，已制作: {', '.join(crafted) or '无'}",
            {"workbench_at": support, "crafted": crafted},
        )


class PlaceWorkbenchSkill(BaseSkill):
    """把工作台从背包拿到快捷栏，并放到地面。"""

    name = "place_workbench"
    description = "工作台入快捷栏并放到地面"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        slot = ensure_workbench_slot(context)
        if slot is None:
            return SkillResult(False, "no_workbench", "背包里和工作台配方都不可用")
        # 关键：放世界方块前必须关背包，否则鼠标点的是 UI。
        InventoryUI(context.executor, context.monitor).close()
        context.executor.select_hotbar_slot(slot)
        px, py = context.monitor.player_tile
        base = BuildSkill()._find_surface(context, px, py)
        if base is None:
            return SkillResult(False, "no_support", "附近找不到放工作台的地面")
        try:
            context.executor.place_furniture(
                base[0], base[1], screen_mapper=context.monitor.world_to_screen
            )
            time.sleep(0.4)
        except ExecutorError as exc:
            return SkillResult(False, "place_failed", f"放工作台失败: {exc}")
        tile = context.monitor.tile_at(base[0], base[1] - 1)
        if tile in {".", " "}:
            return SkillResult(False, "not_placed", f"工作台没有放到 ({base[0]},{base[1]-1})")
        return SkillResult(True, "success", f"工作台已放到 ({base[0]},{base[1]-1})", {"slot": slot, "at": [base[0], base[1]-1], "ground": base})


class PrepareFurnitureSkill(BaseSkill):
    """在工作台旁制作木桌、木椅、木墙，并放进快捷栏。"""

    name = "prepare_furniture"
    description = "制作木桌/木椅/木墙并放入快捷栏"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        ui = InventoryUI(context.executor, context.monitor)
        made: list[str] = []
        for name in ("木桌", "木椅", "木墙"):
            result = CraftSkill().run(context, item=name)
            if not result.ok:
                context.emit(f"{name} 制作失败: {result.message}", "warning")
                continue
            slot = ui.ensure_hotbar_item(name)
            if slot is None:
                context.emit(f"{name} 已制作，但快捷栏替换失败", "warning")
            else:
                context.emit(f"{name} 已进快捷栏 slot {slot}", "success")
            made.append(name)
            try:
                ui.close()
            except Exception:
                pass
        if not made:
            return SkillResult(False, "no_furniture", "木桌/木椅/木墙都没有制作成功")
        return SkillResult(True, "success", f"已准备家具: {', '.join(made)}", {"made": made})


class BuildHouseShellSkill(BaseSkill):
    """自动找一块平地，先搭长平台，再围 12×6 木屋外壳。"""

    name = "build_house_shell"
    description = "找空地，搭长平台和 12×6 木屋外壳"

    def _surface_map_y(self, grid: list[str], gx: int) -> int | None:
        for y in range(len(grid)):
            if 0 <= gx < len(grid[y]) and grid[y][gx] not in {".", " "}:
                return y
        return None

    def _find_area(self, context: SkillContext, width: int, height: int):
        grid = context.monitor.get_grid()
        center = len(grid) // 2
        px, py = context.monitor.player_tile
        best = None
        best_score = 10**9
        for start_gx in range(2, len(grid) - width - 2):
            surfaces = [self._surface_map_y(grid, start_gx + i) for i in range(width)]
            if any(v is None for v in surfaces):
                continue
            if max(surfaces) - min(surfaces) > 2:
                continue
            top = min(surfaces) - (height + 1)
            clear = True
            for i in range(width):
                for y in range(max(0, top), min(surfaces[i], len(grid))):
                    if grid[y][start_gx + i] not in {".", " "}:
                        clear = False
                        break
                if not clear:
                    break
            if not clear:
                continue
            world_x = px + (start_gx - center)
            score = abs(world_x - px) + (max(surfaces) - min(surfaces)) * 10
            if score < best_score:
                best_score = score
                best = (world_x, py + (max(surfaces) - center), surfaces)
        return best

    def _travel_for_area(self, context: SkillContext, width: int, height: int):
        """选一个方向连续走远，直到找到空地，不原地左右折返。"""
        direction = Direction.RIGHT if random.random() < 0.5 else Direction.LEFT
        sign = 1 if direction == Direction.RIGHT else -1
        for step in range(8):
            start = context.executor.get_current_position()
            target_x = start.center_x_tiles + sign * 12.0
            result = context.executor.move_to_x(
                target_x, timeout_s=8.0, allow_backoff=False
            )
            if not result.success:
                try:
                    context.executor.jump_over_obstacle(
                        2.0, direction=direction, timeout_s=3.0
                    )
                    context.executor.move_to_x(
                        target_x, timeout_s=6.0, allow_backoff=False
                    )
                except (ExecutorError, JumpTooHighError):
                    pass
            distance = 12 * (step + 1)
            if distance < 36:
                context.emit(f"继续向{'右' if sign > 0 else '左'}走，已走 {distance} 格", "info")
                continue
            area = self._find_area(context, width, height)
            if area is not None:
                context.emit(f"连续向{'右' if sign > 0 else '左'}走了 {distance} 格，找到空地", "success")
                return area
            context.emit(f"继续向{'右' if sign > 0 else '左'}走，已走 {distance} 格", "info")
        return None

    def _ensure_wood(self, context: SkillContext) -> int | None:
        ui = InventoryUI(context.executor, context.monitor)
        ui.force_open()
        slot = ui.ensure_hotbar_item("木材")
        ui.close()
        if slot is None:
            return None
        context.executor.select_hotbar_slot(slot)
        return slot

    def _solid(self, context: SkillContext, x: int, y: int) -> bool:
        return context.monitor.tile_at(x, y) not in {".", " "}

    def _slide(self, context: SkillContext, sx: int, sy: int, tx: int, ty: int) -> bool:
        try:
            context.executor.place_slide(
                sx, sy, tx, ty, screen_mapper=context.monitor.world_to_screen
            )
            time.sleep(0.15)
            return self._solid(context, tx, ty)
        except ExecutorError:
            return False

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        width = max(6, min(12, int(params.get("width", 12) or 12)))
        height = max(3, min(6, int(params.get("height", 6) or 6)))
        if self._ensure_wood(context) is None:
            return SkillResult(False, "no_wood", "快捷栏里没有木材")
        area = self._travel_for_area(context, width, height)
        if area is None:
            return SkillResult(False, "no_area", f"连续走了 96 格仍未找到 {width} 格宽的空地")
        start_x, surface_y, surfaces = area
        base_y = surface_y - 1
        end_x = start_x + width - 1
        top_y = base_y - height + 1
        px, py = context.monitor.player_tile
        center = len(context.monitor.get_grid()) // 2
        # 1) 人站在平台左侧外面，先搭 12 格平台
        context.executor.move_to_x(start_x - 2, timeout_s=8.0)
        placed = 0
        start_ground_y = py + (surfaces[0] - center)
        try:
            context.executor.place_furniture(
                start_x, start_ground_y, screen_mapper=context.monitor.world_to_screen
            )
            time.sleep(0.2)
        except ExecutorError:
            pass
        if self._solid(context, start_x, base_y):
            placed += 1
        for i in range(1, width):
            tx = start_x + i
            context.executor.move_to_x(tx - 2, timeout_s=5.0)
            if self._slide(context, tx - 1, base_y, tx, base_y):
                placed += 1
        # 2) 跳上平台，走到平台内部再开始造
        context.executor.move_to_x(start_x - 1, timeout_s=5.0)
        try:
            context.executor.jump_over_obstacle(1.0, direction=Direction.RIGHT, timeout_s=3.0)
        except (ExecutorError, JumpTooHighError):
            pass
        context.executor.move_to_x(start_x + 2, timeout_s=5.0)
        # 3) 左墙 6 格：人在平台内部，从平台往上滑
        for level in range(1, height):
            ty = base_y - level
            if self._solid(context, start_x, ty):
                continue
            if self._slide(context, start_x, ty + 1, start_x, ty):
                placed += 1
        # 4) 屋顶/长平台：人留在平台内部，从左往右滑
        for i in range(1, width):
            tx = start_x + i
            context.executor.move_to_x(min(tx - 1, end_x - 1), timeout_s=5.0)
            if self._slide(context, tx - 1, top_y, tx, top_y):
                placed += 1
        # 5) 右墙：从屋顶往下滑，人物仍在屋内
        context.executor.move_to_x(end_x - 2, timeout_s=5.0)
        for level in range(1, height):
            ty = top_y + level
            if self._solid(context, end_x, ty):
                continue
            if self._slide(context, end_x, ty - 1, end_x, ty):
                placed += 1
        if placed == 0:
            return SkillResult(False, "place_failed", "没有放下任何木材方块")
        return SkillResult(
            True,
            "partial",
            f"先搭平台并进入内部，已放 {placed} 块；背景墙/门未做",
            {"placed": placed, "width": width, "height": height,
             "base": [start_x, base_y], "top": [start_x, top_y]},
        )


class AIPlanSkill(BaseSkill):
    """让 DeepSeek 根据状态逐步选择移动/建造，并执行结果。"""

    name = "ai_plan"
    description = "大模型根据当前地图和背包决定下一步"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        goal = str(params.get("goal", "")).strip()
        if not goal:
            return SkillResult(False, "no_goal", "没有给大模型目标")
        cfg = context.config
        planner = LLMPlanner(
            api_key=str(cfg.get("deepseek_api_key", "")),
            base_url=str(cfg.get("deepseek_base_url", "https://api.deepseek.com")),
            model=str(cfg.get("deepseek_model", "deepseek-chat")),
        )
        last = ""
        steps: list[dict[str, Any]] = []
        result: SkillResult | None = None
        for index in range(8):
            try:
                state = context.monitor.get_state()
                plan = planner.decide(goal, state, last)
            except Exception as exc:
                return SkillResult(False, "planner_failed", f"大模型规划失败: {exc}")
            action = str(plan.get("action", ""))
            context.emit(f"大模型第 {index + 1} 步: {action}", "info")
            try:
                if action == "move":
                    result = MoveSkill().run(
                        context,
                        direction=str(plan.get("direction", "right")),
                        distance=int(plan.get("steps", 5) or 5),
                    )
                elif action == "build_house_shell":
                    result = BuildHouseShellSkill().run(
                        context,
                        width=int(plan.get("width", 12) or 12),
                        height=int(plan.get("height", 6) or 6),
                    )
                elif action == "place_workbench":
                    result = PlaceWorkbenchSkill().run(context)
                elif action == "chop_tree":
                    result = ChopTreeSkill().run(context)
                elif action in {"fight", "attack"}:
                    result = FightSkill().run(context, duration_s=float(plan.get("duration_s", 3.0) or 3.0))
                elif action == "jump":
                    result = JumpSkill().run(
                        context,
                        height=float(plan.get("height", 0) or 0),
                        direction=str(plan.get("direction", "") or ""),
                    )
                elif action == "open_inventory":
                    InventoryUI(context.executor, context.monitor).open()
                    result = SkillResult(True, "success", "背包已打开")
                elif action == "close_inventory":
                    InventoryUI(context.executor, context.monitor).close()
                    result = SkillResult(True, "success", "背包已关闭")
                elif action == "select_slot":
                    slot = int(plan.get("slot", 1) or 1)
                    context.executor.select_hotbar_slot(slot)
                    result = SkillResult(True, "success", f"已选快捷栏 {slot}")
                elif action == "move_item":
                    ui = InventoryUI(context.executor, context.monitor)
                    # 如果上一步已经 open_inventory，这里绝不能再次 Esc 把背包关掉。
                    ui.open()
                    src = int(plan.get("from_slot", 0) or 0)
                    dst = int(plan.get("to_slot", 0) or 0)
                    wanted = str(plan.get("item", "") or "")
                    if wanted:
                        found = next(
                            (i for i in context.monitor.inventory if str(i.get("name", "")) == wanted),
                            None,
                        )
                        if found is None:
                            result = SkillResult(False, "item_not_found", f"背包里没有 {wanted}")
                            continue
                        src = int(found.get("slot", 0) or 0)
                    ui.move_item(src, dst)
                    ui.close()
                    context.monitor.get_state()
                    at_dst = next(
                        (i for i in context.monitor.inventory if int(i.get("slot", 0) or 0) == dst),
                        None,
                    )
                    ok = at_dst is not None and (not wanted or str(at_dst.get("name", "")) == wanted)
                    result = SkillResult(ok, "success" if ok else "move_not_verified",
                                         f"移动 slot {src} -> {dst}，目标格={at_dst.get('name') if at_dst else '空'}")
                elif action == "place_block":
                    InventoryUI(context.executor, context.monitor).close()
                    bx = int(plan.get("x", 0) or 0)
                    by = int(plan.get("y", 0) or 0)
                    before = context.monitor.tile_at(bx, by)
                    context.executor.place_block(bx, by, screen_mapper=context.monitor.world_to_screen)
                    time.sleep(0.2)
                    after = context.monitor.tile_at(bx, by)
                    result = SkillResult(after != before, "success" if after != before else "place_failed",
                                         f"放置 ({bx},{by}) -> {after}")
                elif action == "mine_tile":
                    mx = int(plan.get("x", 0) or 0)
                    my = int(plan.get("y", 0) or 0)
                    before = context.monitor.tile_at(mx, my)
                    context.executor.aim_tile(mx, my, screen_mapper=context.monitor.world_to_screen)
                    context.executor.hold_mouse(
                        "left",
                        until=lambda: context.monitor.tile_at(mx, my) != before,
                        timeout_s=2.0,
                    )
                    result = SkillResult(True, "success", f"挖掘 ({mx},{my})")
                elif action == "lookup_item":
                    hits = find_items(str(plan.get("query", "") or ""), 12)
                    detail = ", ".join(f"{h['name']}#{h['id']}" for h in hits) or "没有匹配"
                    result = SkillResult(True, "success", f"物品查询: {detail}")
                    # 查询是回答型动作，查到就结束，避免模型连续查 8 次。
                    last = f"{result.status}: {result.message}"
                    steps.append({"plan": plan, "result": last})
                    break
                elif action == "craft":
                    result = CraftSkill().run(context, item=str(plan.get("item", "") or ""))
                elif action == "wait":
                    time.sleep(max(0.2, min(3.0, float(plan.get("seconds", 0.5) or 0.5))))
                    result = SkillResult(True, "success", "等待完成")
                elif action == "stop":
                    result = SkillResult(True, "stopped", "大模型选择停止")
                    break
                else:
                    result = SkillResult(False, "bad_action", f"大模型返回未知动作: {action}")
            except Exception as exc:
                result = SkillResult(False, "action_failed", str(exc))
            last = f"{result.status}: {result.message}"
            steps.append({"plan": plan, "result": last})
            if not result.ok:
                # 让模型看到失败后重新规划，而不是在外层直接放弃。
                continue
        if result is None:
            return SkillResult(False, "no_action", "大模型没有执行动作")
        return SkillResult(result.ok, result.status, result.message, {"steps": steps})
class FightSkill(BaseSkill):
    """拿武器追着最近的怪打：一边朝它走，一边把鼠标压在它身上按住左键挥砍。"""

    name = "fight"
    description = "拿武器追着最近的怪：边靠近边挥砍（按住左键），直到打不到或超时"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        duration = max(0.5, min(12.0, float(params.get("duration_s", 4.0) or 4.0)))
        deadline = time.monotonic() + duration
        swings = 0
        moved = 0.0
        last_error = ""
        while time.monotonic() < deadline:
            try:
                context.monitor.get_state()
            except Exception:
                pass
            enemies = context.monitor.enemies
            if not enemies:
                return SkillResult(
                    True,
                    "success",
                    f"附近没有敌人了（挥砍 {swings} 次，追了 {moved:.1f} 格）",
                    {"swings": swings, "moved": round(moved, 2)},
                )

            player = context.monitor.player_tile
            px = (player[0] + 0.5) * 16
            py = (player[1] + 0.5) * 16

            def enemy_center(enemy: dict) -> tuple[float, float]:
                return (
                    float(enemy.get("x", 0)) + float(enemy.get("w", 16)) / 2,
                    float(enemy.get("y", 0)) + float(enemy.get("h", 16)) / 2,
                )

            def distance(enemy: dict) -> float:
                ex, ey = enemy_center(enemy)
                return ((ex - px) ** 2 + (ey - py) ** 2) ** 0.5

            try:
                enemy_range = float(context.config.get("enemy_range_tiles", 150) or 150)
            except (TypeError, ValueError):
                enemy_range = 150.0
            enemies = [
                e for e in enemies
                if abs(float(e.get("x", 0)) + float(e.get("w", 16)) / 2 - px) <= enemy_range * 16
                and abs(float(e.get("y", 0)) + float(e.get("h", 16)) / 2 - py) <= enemy_range * 16
            ]
            if not enemies:
                return SkillResult(
                    True, "success", f"{enemy_range:.0f} 格内没有敌人了", {"swings": swings}
                )
            target = min(enemies, key=distance)
            dist_px = distance(target)

            # 切攻击力最高的武器
            decision = ToolMatcher().select(
                context.monitor.inventory,
                int(context.monitor.current_tool.get("slot", 1)),
                target_kind="enemy",
            )
            if not decision.ok or not decision.item:
                return SkillResult(False, decision.status, decision.reason)
            slot = int(decision.item["slot"])
            if slot != int(context.monitor.current_tool.get("slot", 1)):
                context.executor.select_hotbar_slot(slot)

            ex, ey = enemy_center(target)
            direction = None
            # 离得远就边追边砍；离得近就原地砍（避免贴脸穿过去）。
            if dist_px > 2.2 * 16:
                direction = Direction.RIGHT if ex > px else Direction.LEFT
                moved += 0.45

            try:
                sx, sy = context.monitor.world_to_screen(ex, ey)
                context.executor.attack_move(
                    sx, sy, direction=direction, hold_s=0.45
                )
                swings += 1
            except ExecutorError as exc:
                last_error = str(exc)
                break
            time.sleep(0.05)

        if last_error:
            return SkillResult(
                False,
                "fight_failed",
                f"战斗中出错：{last_error}",
                {"swings": swings, "moved": round(moved, 2)},
            )
        return SkillResult(
            True,
            "success",
            f"追着砍 {swings} 次（大约追了 {moved:.1f} 格）",
            {"swings": swings, "moved": round(moved, 2)},
        )


class HoldFireSkill(BaseSkill):
    """锁定最近的怪，鼠标一直贴着它，左键全程按住不放（矿车/站桩持续输出）。

    和 fight 的区别：fight 是“跳一下砍一刀”，会松手；这个是按下左键后不再松开，
    只移动准星，所以没有点击间隔、没有停顿。
    """

    name = "hold_fire"
    description = "锁定最近的怪，鼠标一直跟着它并按住左键不放（矿车战用）"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        try:
            duration = max(1.0, min(30.0, float(params.get("duration_s", 10.0) or 10.0)))
        except (TypeError, ValueError):
            duration = 10.0
        direction_name = str(params.get("direction", "") or "").strip().lower()
        direction = Direction.LEFT if direction_name == "left" else (
            Direction.RIGHT if direction_name == "right" else None
        )

        try:
            enemy_range = float(context.config.get("enemy_range_tiles", 150) or 150)
        except (TypeError, ValueError):
            enemy_range = 150.0
        context.monitor.get_state()
        player = context.monitor.player_tile
        px = (player[0] + 0.5) * TILE_SIZE_PX
        py = (player[1] + 0.5) * TILE_SIZE_PX
        enemies = [
            e for e in context.monitor.enemies
            if abs(float(e.get("x", 0)) + float(e.get("w", 16)) / 2 - px) <= enemy_range * 16
            and abs(float(e.get("y", 0)) + float(e.get("h", 16)) / 2 - py) <= enemy_range * 16
        ]
        if not enemies:
            return SkillResult(
                True, "success", f"{enemy_range:.0f} 格内没有敌人，不用开火", {"enemies": 0}
            )

        def center(enemy: dict) -> tuple[float, float]:
            return (
                float(enemy.get("x", 0)) + float(enemy.get("w", 16)) / 2,
                float(enemy.get("y", 0)) + float(enemy.get("h", 16)) / 2,
            )

        def distance(enemy: dict) -> float:
            ex, ey = center(enemy)
            return ((ex - px) ** 2 + (ey - py) ** 2) ** 0.5

        target = min(enemies, key=distance)
        target_id = target.get("i")
        start_life = float(target.get("life", 0) or 0)

        decision = ToolMatcher().select(
            context.monitor.inventory,
            int(context.monitor.current_tool.get("slot", 1)),
            target_kind="enemy",
        )
        if not decision.ok or not decision.item:
            return SkillResult(False, decision.status, decision.reason)
        slot = int(decision.item["slot"])
        if slot != int(context.monitor.current_tool.get("slot", 1)):
            context.executor.select_hotbar_slot(slot)

        context.emit(f"持续开火：锁定 {decision.item['name']}，左键按住 {duration:.0f} 秒", "info")
        ticks = 0
        empty_reads = 0
        offscreen = 0
        context.executor.begin_hold_attack()
        try:
            if direction is not None:
                context.executor.hold_direction(direction)
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                try:
                    context.monitor.get_state()
                except Exception:
                    pass
                current = [
                    e for e in context.monitor.enemies
                    if abs(float(e.get("x", 0)) + float(e.get("w", 16)) / 2 - px) <= enemy_range * 16
                    and abs(float(e.get("y", 0)) + float(e.get("h", 16)) / 2 - py) <= enemy_range * 16
                ]
                if not current:
                    empty_reads += 1
                    # 数据偶尔闪空一帧不算打完，连续空 10 帧才停，避免“打两枪就停”
                    if empty_reads >= 10:
                        break
                    time.sleep(0.03)
                    continue
                empty_reads = 0
                # 优先继续打同一只怪，死了再换最近的
                same = next((e for e in current if target_id is not None and e.get("i") == target_id), None)
                target = same or min(current, key=distance)
                ex, ey = center(target)
                try:
                    sx, sy = context.monitor.world_to_screen(ex, ey)
                    _, _, clamped = context.executor.aim_screen(sx, sy)
                    if clamped:
                        offscreen += 1
                except ExecutorError as exc:
                    return SkillResult(False, "aim_failed", str(exc), {"ticks": ticks})
                ticks += 1
                time.sleep(0.03)
        finally:
            context.executor.end_hold_attack()
            if direction is not None:
                context.executor.hold_direction(None)

        try:
            context.monitor.get_state()
        except Exception:
            pass
        remaining = next(
            (e for e in context.monitor.enemies if target_id is not None and e.get("i") == target_id),
            None,
        )
        end_life = float(remaining.get("life", 0) or 0) if remaining else 0.0
        if offscreen > max(10, ticks // 3):
            text_extra = "（目标大部分时间在屏幕外，鼠标被夹在窗口边缘，先让矿车靠近）"
        else:
            text_extra = ""
        if remaining is None:
            text = f"目标已经被打死（开火 {ticks} 次瞄准）{text_extra}"
        else:
            text = (
                f"持续开火 {ticks} 次瞄准：目标血量 {start_life:.0f} → {end_life:.0f}"
                + ("（没掉血：确认武器够得着/子弹够用）" if end_life >= start_life else "")
                + text_extra
            )
        return SkillResult(
            True,
            "success",
            text,
            {
                "ticks": ticks,
                "start_life": start_life,
                "end_life": end_life,
                "killed": remaining is None,
                "weapon": decision.item.get("name"),
                "offscreen_ticks": offscreen,
            },
        )


class MineBlockSkill(BaseSkill):
    """找最近的石块/泥土，切镐子，走过去按住左键挖掉。"""

    name = "mine_block"
    description = "自动选择镐子，走到最近方块旁长按挖掉"

    def __init__(self) -> None:
        self.matcher = ToolMatcher()

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        local_only = bool(params.get("local_only", False))
        player_tx, player_ty = context.monitor.player_tile
        explicit = None
        try:
            if params.get("x") is not None and params.get("y") is not None:
                explicit = (int(params.get("x")), int(params.get("y")))
        except (TypeError, ValueError):
            explicit = None
        if explicit is not None:
            # 指定格子：挡路时直接挖面前那一格，不用再“找最近的方块”
            bx, by = explicit
            context.monitor.set_action(f"挖 ({bx},{by})")
        else:
            context.monitor.set_action("寻找方块")
            grid = context.monitor.get_grid()
            target = self._find_block(grid)
            if target is None:
                return SkillResult(False, "no_block", "附近没有可挖的方块：先 move 换地方再挖")
            dx, dy = target
            center = len(grid) // 2
            bx = player_tx + (dx - center)
            by = player_ty + (dy - center)
        block = context.monitor.tile_at(bx, by)
        if block not in {"D", "S", "W", "M", "?"}:
            return SkillResult(
                False,
                "no_block",
                f"({bx},{by}) 不是可挖方块（是 {block}）",
                {"block": [bx, by], "tile": block},
            )

        if local_only:
            horizontal = abs(bx - player_tx)
            vertical = abs(by - player_ty)
            if horizontal > 4 or vertical > 4:
                side = (
                    "右边"
                    if bx > player_tx
                    else ("左边" if bx < player_tx else "原地")
                )
                return SkillResult(
                    False,
                    "too_far",
                    f"最近的方块在{side} {horizontal} 格、上下 {vertical} 格，超出 4 格挖掘范围",
                    {
                        "block": [bx, by],
                        "dx": bx - player_tx,
                        "dy": by - player_ty,
                        "hint": "先用 move 走近到 4 格内再 mine",
                    },
                )

        decision = self.matcher.select(
            context.monitor.inventory,
            int(context.monitor.current_tool.get("slot", 1)),
            target_kind="block",
            tile_code=block,
        )
        if not decision.ok or not decision.item:
            return SkillResult(False, decision.status, decision.reason)
        pick = decision.item
        context.executor.select_hotbar_slot(int(pick["slot"]))
        context.emit(f"使用工具: {pick['name']}（镐子挖方块）", "info")

        if not local_only:
            stand_x = bx - 1.5 if bx > player_tx else bx + 1.5
            move = context.executor.move_to_x(stand_x, timeout_s=8.0)
            if not move.success:
                try:
                    context.executor.jump_over_obstacle(
                        1.5,
                        direction=Direction.RIGHT if stand_x > move.final_x_tiles else Direction.LEFT,
                    )
                    move = context.executor.move_to_x(stand_x, timeout_s=5.0)
                except (ExecutorError, JumpTooHighError) as exc:
                    return SkillResult(False, "move_failed", f"走不到方块旁：{exc}")
            if not move.success:
                return SkillResult(False, "move_failed", f"走不到方块旁：{move.status}")

        context.executor.smart_cursor_down()
        try:
            context.executor.aim_tile(bx, by, screen_mapper=context.monitor.world_to_screen)
            removed = context.executor.hold_mouse(
                "left",
                until=lambda: context.monitor.tile_at(bx, by) != block,
                timeout_s=4.0,
            )
        except ExecutorError as exc:
            return SkillResult(False, "mine_failed", str(exc))
        finally:
            context.executor.smart_cursor_up()
        if removed:
            return SkillResult(
                True,
                "success",
                f"挖掉方块 @ ({bx}, {by})",
                {"block": [bx, by], "tool": pick},
            )
        return SkillResult(
            False,
            "block_still_there",
            "长按后还是没挖掉：可能镐力不够或距离太远，挪近一点再试",
            {"block": [bx, by], "tool": pick},
        )

    @staticmethod
    def _find_block(grid: list[str]) -> tuple[int, int] | None:
        center = len(grid) // 2
        best = None
        best_d = 10**9
        for y, row in enumerate(grid):
            for x, ch in enumerate(row):
                if ch not in {"D", "S", "W", "M"}:
                    continue
                if abs(y - center) <= 1 and abs(x - center) <= 1:
                    continue
                d = abs(x - center) + abs(y - center) * 2
                if d < best_d:
                    best_d = d
                    best = (x, y)
        return best


# ---------------------------------------------------------------------------
# 辅助技能
# ---------------------------------------------------------------------------


class MoveSkill(BaseSkill):
    name = "move"
    description = "向指定方向闭环移动若干格"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        direction = str(params.get("direction", "right"))
        distance = float(params.get("distance", 3.0))
        distance = max(0.5, min(20.0, distance))
        context.monitor.set_action(f"向{'左' if direction == 'left' else '右'}移动")

        start = context.executor.get_current_position()
        target_x = start.center_x_tiles + (-distance if direction == "left" else distance)
        result = context.executor.move_to_x(target_x, timeout_s=10.0)
        if result.success:
            return SkillResult(
                True,
                "success",
                f"移动到 x={result.final_x_tiles:.2f}",
                {"move": asdict(result)},
            )
        return SkillResult(
            False,
            f"move_{result.status}",
            f"移动失败：{result.status}",
            {"move": asdict(result)},
        )


class JumpSkill(BaseSkill):
    name = "jump"
    description = "跳跃并等待落地"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        height = float(params.get("height", 0.0))
        direction_name = str(params.get("direction", ""))
        direction = None
        if direction_name == "left":
            direction = Direction.LEFT
        elif direction_name == "right":
            direction = Direction.RIGHT

        context.monitor.set_action("跳跃")
        try:
            result: JumpResult = context.executor.jump_over_obstacle(
                height, direction=direction
            )
        except JumpTooHighError as exc:
            return SkillResult(False, "jump_too_high", str(exc))
        if result.success:
            return SkillResult(
                True,
                "success",
                f"跳跃落地，实测高度 {result.height_tiles:.2f} 格",
                {"jump": asdict(result)},
            )
        return SkillResult(
            False,
            "jump_failed",
            result.reason,
            {"jump": asdict(result)},
        )


class JumpOntoTileSkill(BaseSkill):
    """跳到指定方块格的顶上：先走到旁边，再朝它蓄力跳，落点不对就微调重试。"""

    name = "jump_onto"
    description = "跳到指定方块格上（站到它顶上），失败会自动微调重试"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        try:
            x = int(params.get("x"))
            y = int(params.get("y"))
        except (TypeError, ValueError):
            return SkillResult(False, "bad_target", "jump_onto 需要整数 x 和 y")

        if context.monitor.tile_at(x, y) in {".", " "}:
            return SkillResult(
                False,
                "no_block",
                f"({x},{y}) 没有方块，跳上去也没地方站",
                {"x": x, "y": y},
            )

        context.monitor.set_action(f"跳上 ({x},{y})")
        last_error = ""
        for attempt in range(3):
            px, py = context.monitor.player_tile
            if abs(px - x) > 12:
                return SkillResult(
                    False,
                    "too_far",
                    f"离 ({x},{y}) 太远了（现在在格({px},{py})）：先走近再跳",
                    {"tile": [x, y], "now": [px, py]},
                )
            # 站到方块左边或右边约 1 格：优先站在人物当前所在的一侧，
            # 免得为了绕到另一边先被方块挡住。
            side = 1 if px >= x else -1
            stand_x = x + side * (1.0 + 0.3 * attempt)
            try:
                context.executor.move_to_x(stand_x, timeout_s=3.0)
            except ExecutorError as exc:
                last_error = str(exc)
            current = context.executor.get_current_position()
            direction = Direction.RIGHT if x > current.center_x_tiles else Direction.LEFT
            # 方块顶比脚底高多少格
            step = 2.0
            try:
                step = (current.foot_y_px - y * TILE_SIZE_PX) / TILE_SIZE_PX
            except Exception:
                pass
            # 1 格台阶不用跳：泰拉瑞亚走路会自动上一格台阶。
            if step <= 1.2:
                try:
                    context.executor.move_to_x(
                        x + (0.5 if current.center_x_tiles < x else -0.5),
                        timeout_s=2.0,
                    )
                except ExecutorError as exc:
                    last_error = str(exc)
                time.sleep(0.2)
                px2, py2 = context.monitor.player_tile
                if abs(px2 - x) <= 1 and py2 == y - 2:
                    return SkillResult(
                        True,
                        "success",
                        f"走上 ({x},{y}) 了（1 格台阶不用跳）",
                        {"tile": [x, y], "attempts": attempt + 1, "walked": True},
                    )
                last_error = "1 格台阶没走上去（可能被挡/离太远）"
                continue
            # 头顶有方块就别跳：会撞头，跳多少次都上不去。
            try:
                head = context.monitor.tile_at(
                    int(current.center_x_tiles), int(current.center_y_tiles) - 2
                )
            except Exception:
                head = "."
            if head in PlaceBlockSkill.SUPPORT_CHARS:
                return SkillResult(
                    False,
                    "blocked_above",
                    f"头顶正上方有方块（{head}），跳不上 ({x},{y})：先挖掉/绕开头顶的方块",
                    {"tile": [x, y], "head": head, "hint": "先把头顶那格挖掉，或者从旁边绕上去"},
                )
            # 按“柱顶比脚底高多少”动态蓄力；跳到目标上方后由 hop_toward 松开方向键。
            need = max(1.0, min(5.8, step + 0.3))
            context.emit(f"跳上 ({x},{y})：需要抬高约 {need:.1f} 格", "info")
            try:
                hop = getattr(context.executor, "hop_toward", None)
                if callable(hop):
                    hop(x + 0.5, hold_s=min(0.8, 0.12 + need * 0.11), timeout_s=2.5)
                else:
                    context.executor.jump_over_obstacle(
                        need, direction=direction, timeout_s=2.5
                    )
            except (ExecutorError, JumpTooHighError) as exc:
                last_error = str(exc)
                continue
            time.sleep(0.15)
            px2, py2 = context.monitor.player_tile
            # 站在方块顶上时，角色中心格 = 方块格上方两格。
            if abs(px2 - x) <= 1 and py2 == y - 2:
                return SkillResult(
                    True,
                    "success",
                    f"跳上 ({x},{y}) 了（第 {attempt + 1} 次）",
                    {"tile": [x, y], "attempts": attempt + 1},
                )
        px2, py2 = context.monitor.player_tile
        return SkillResult(
            False,
            "not_on_block",
            f"试了 3 次没站上 ({x},{y})，现在在格({px2},{py2})"
            + (f"：{last_error}" if last_error else ""),
            {"tile": [x, y], "now": [px2, py2]},
        )


class StopSkill(BaseSkill):
    name = "stop"
    description = "立即停止移动并释放按键"

    def run(self, context: SkillContext, **params: Any) -> SkillResult:
        context.executor.stop()
        context.monitor.set_action("Idle")
        return SkillResult(True, "stopped", "已停止并释放所有按键")


def build_default_registry() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(SelectToolSkill())
    registry.register(ChopTreeSkill())
    registry.register(MineBlockSkill())
    registry.register(HoldFireSkill())
    registry.register(BuildSkill())
    registry.register(PlaceBlockSkill())
    registry.register(BuildPillarSkill())
    registry.register(BuildFrameSkill())
    registry.register(BuildBlueprintSkill())
    registry.register(PlaceWallSkill())
    registry.register(PlaceWallAreaSkill())
    registry.register(CheckHouseSkill())
    registry.register(OpenInventorySkill())
    registry.register(CloseInventorySkill())
    registry.register(CheckHotbarSkill())
    registry.register(CheckInventorySkill())
    registry.register(FindHotbarSkill())
    registry.register(SelectSlotSkill())
    registry.register(MoveItemSkill())
    registry.register(HousePrepSkill())
    registry.register(PrepareFurnitureSkill())
    registry.register(BuildHouseShellSkill())
    registry.register(AIPlanSkill())
    registry.register(FightSkill())
    registry.register(PlaceWorkbenchSkill())
    registry.register(CraftSkill())
    registry.register(MoveSkill())
    registry.register(JumpSkill())
    registry.register(JumpOntoTileSkill())
    registry.register(StopSkill())
    return registry


__all__ = [
    "BaseSkill",
    "ChopTreeSkill",
    "MineBlockSkill",
    "HoldFireSkill",
    "CraftSkill",
    "BuildSkill",
    "PlaceBlockSkill",
    "BuildPillarSkill",
    "BuildFrameSkill",
    "BuildBlueprintSkill",
    "PlaceWallSkill",
    "PlaceWallAreaSkill",
    "CheckHouseSkill",
    "OpenInventorySkill",
    "CloseInventorySkill",
    "CheckHotbarSkill",
    "CheckInventorySkill",
    "FindHotbarSkill",
    "SelectSlotSkill",
    "MoveItemSkill",
    "HousePrepSkill",
    "PlaceWorkbenchSkill",
    "PrepareFurnitureSkill",
    "BuildHouseShellSkill",
    "AIPlanSkill",
    "FightSkill",
    "JumpSkill",
    "JumpOntoTileSkill",
    "MoveSkill",
    "SelectToolSkill",
    "SkillContext",
    "SkillRegistry",
    "SkillResult",
    "StopSkill",
    "ToolDecision",
    "ToolMatcher",
    "build_default_registry",
]
