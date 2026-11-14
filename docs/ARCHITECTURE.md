# 架构说明

## 1. 三层结构

```
Coopanion 桌宠（大模型）  →  cortico-world-terraria 插件（26 个工具）  →  Python 服务  →  Terraria
```

### 插件层（TypeScript，运行在 CoO 里）

- `src/world.ts`：定义世界 `terraria`，声明 26 个工具、面板 invoke、配置项、提示词变量；
- `src/agent.ts`：HTTP 客户端，负责连 `panelUrl`，连不上时可自动 spawn `main.py`；
- `dist/webui.js`：原生 DOM 控制面板（地图、血量、背包、日志、蓝图编辑器）；
- `ENV_PROMPT.md`：给桌宠的系统提示词，写清动作配方和禁忌。

插件只做三件事：**参数校验、调用 Python `/api/action`、把回执翻译成人话**。它不调用任何大模型。

### 服务层（Python）

- `app.py`：Flask 路由（`/api/state`、`/api/action`、`/api/items`、`/api/health`），装配 monitor/executor/skills；
- `skills.py`：26 个原子技能，每个只做一件事，返回 `SkillResult(ok, status, message, data)`；
- `executor.py`：键鼠执行层，负责移动/跳跃/点击/范围检查/松开所有键；
- `monitor.py`：内存读取层，把 bridge 的 JSON 变成坐标/血量/背包/地形/敌人/格子；
- `inventory_ui.py`：背包与制作界面的固定像素坐标 + 悬停校准；
- `clrmd_reader.py`：管理 32 位 bridge 进程，watchdog 在进程退出时重连。

### 读取层（C# / ClrMD）

`bridge/probe.cs` 用 `DataTarget.AttachToProcess(..., AttachFlag.Passive)` 附加到 Terraria，每 50ms 输出一帧 JSON：

- 玩家：position、velocity、life/maxLife、screenPosition、playerInventory、UIScale；
- 背包：50 格（slot/type/stack/name/axe/pick/hammer/damage/createTile/createWall）；
- 地形：31×31 格（tile/wall 标志）；
- 敌人：所有 active && !friendly && !townNPC 的 NPC。

## 2. 坐标系

| 坐标 | 含义 |
|---|---|
| 世界像素 | `position + offset`（offset ≈ (10, 21)） |
| 世界格 | `floor(世界像素 / 16)` |
| 角色碰撞体 | **严格 2×3 格**：中心点所在格 = 左列/中行 |
| 屏幕坐标 | `世界像素 - Main.screenPosition + 客户端左上角` |
| 背包/制作界面 | 固定像素：第一格 (41,43)，列距 47.5，行距 47（UIScale=1） |

## 3. 26 个工具

`see / move / jump / jump_onto / chop_tree / mine / open_inventory / close_inventory / check_hotbar / check_inventory / find_in_hotbar / move_item / select_slot / craft / place / build_pillar / build_frame / build_blueprint / place_wall / place_wall_area / check_house / fight / hold_fire / stop / lookup_item / launch_game`

每个工具的 handler 只负责把参数转成 `/api/action` 调用，例如：

```ts
terraria_place: (args) => this.place(args)
// → POST /api/action {action:"place_block", params:{x,y}}
```

## 4. 关键安全设计

- **Esc 只按一次**：背包键是开关，连按会来回切换；`press_inventory` 有 1 秒防连按，开/关都先读状态再按；
- **不放在自己身上**：`place` 会检查目标是否在 2×3 碰撞体里，是就返回 `in_player_body`；
- **手上不是方块就拒绝**：`createTile < 0` 返回 `not_holding_block`（避免拿剑乱点）；
- **范围检查**：放置/挖掘都限制在 5 格内，超了返回明确状态；
- **松开所有键**：每个动作结束（含异常）都会 `release_all`，避免卡键卡鼠标；
- **鼠标夹在窗口内**：持续开火时把准星夹在游戏客户区内，不会飞到任务栏；
- **失败截图**：开发阶段每次失败存 `logs/failures/*.png`，回执里带路径。

## 5. 蓝图格式

`blueprint.json`：

```json
{ "rows": ["0011111111111100", "0010000000000100", "..."] }
```

- 从**下往上**，`1` = 放方块；
- 施工时以「最下面有格子的一行」贴地，从身体旁边往指定方向铺；
- 优先继续打同一只怪/同一块墙，被身体挡住的格子挪一格再补。

## 6. 为什么不用独立的 LLM

独立 Agent 需要自己维护对话、人设、记忆、语音，还要和玩家抢窗口；CoO 已经有这些，所以让 CoO 的大模型当大脑，Python 只做「手眼」。这也是项目后面所有设计的前提。
