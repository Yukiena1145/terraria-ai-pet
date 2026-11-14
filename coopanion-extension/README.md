# cortico-world-terraria（Coopanion 世界插件）

把本机的 **Terraria-Agent** 变成 [Coopanion](https://github.com/Pal-AI-Lab/Coopanion) 桌宠自己的工具：桌宠用自己的大模型看地图、走路、砍树、挖矿、放方块、刷墙、打怪。

> [Coopanion](https://github.com/Pal-AI-Lab/Coopanion) 是一个用 Cortico 构建的小型 AI 桌面宠物，
> 有自己的大模型、人格、语音和形象包。本插件是它的**非官方**泰拉瑞亚世界插件。

> 这是 [Terraria AI 桌宠](../README.md) 项目的插件部分。完整安装步骤见 [docs/SETUP.md](../docs/SETUP.md)。

## 设计原则

**只提供小动作**。砍树不会自己跑过去，造房子不会自己决定形状；"看 → 走 → 开背包 → 查 → 制作 → 搬格子 → 关背包 → 选格 → 放" 全部由桌宠的大模型一步一步决定。

## 工具（26 个）

| 类别 | 工具 |
|---|---|
| 看 | `terraria_see`、`terraria_check_hotbar`、`terraria_check_inventory`、`terraria_lookup_item`、`terraria_check_house` |
| 走 | `terraria_move`、`terraria_jump`、`terraria_jump_onto` |
| 采集 | `terraria_chop_tree`、`terraria_mine`（可指定 x,y 挖面前那格） |
| 背包 | `terraria_open_inventory`、`terraria_close_inventory`、`terraria_find_in_hotbar`、`terraria_move_item`、`terraria_select_slot`、`terraria_craft`（可 `count` 长按批量） |
| 放置 | `terraria_place`、`terraria_build_pillar`、`terraria_build_frame`、`terraria_build_blueprint` |
| 墙 | `terraria_place_wall`、`terraria_place_wall_area` |
| 战斗 | `terraria_fight`、`terraria_hold_fire` |
| 其他 | `terraria_stop`、`terraria_launch_game` |

## 结构

```
src/world.ts      # 世界定义：26 个工具 + 面板 invoke + 配置 + 提示词变量
src/agent.ts      # 连本地 Python 服务，服务没起来时可自动拉起
src/index.ts      # 默认导出
dist/webui.js     # 原生控制面板：地图、血量、背包、日志、蓝图编辑器
dist/webui.css    # 面板样式
ENV_PROMPT.md     # 给桌宠的系统提示词（动作配方 + 禁忌）
scripts/smoke.mjs # 26 个工具的离线冒烟测试
blueprint.json    # 示例蓝图（面板里画好保存的格式）
```

## 配置

在 CoO 设置里：

- `worlds.terraria.panelUrl`：本地服务地址，默认 `http://127.0.0.1:5000`
- `worlds.terraria.pythonExe` / `projectDir`：Python 和项目目录
- `worlds.terraria.autoStart`：服务没起来时自动拉起
- `worlds.terraria.inventoryKey`：背包键，默认 `escape`
- `worlds.terraria.enemyRangeTiles`：找怪范围（默认 150）

## 许可

MIT。
