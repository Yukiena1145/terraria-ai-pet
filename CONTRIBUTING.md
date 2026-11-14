# 贡献指南

## 先跑起来

```bat
.venv\Scripts\python -m unittest discover -s tests -q
```

- Python 测试：73 个（技能、背包坐标、地图、跳跃、蓝图等）；
- 插件冒烟：`cd coopanion-extension && node scripts/smoke.mjs`（26 个工具）；
- 需要 Node 20+ 和 CoO 的运行时才能跑 dry-mount。

## 提交 PR 前

1. 跑一遍全部测试；
2. 不要提交 `config.json`、`logs/`、`__pycache__/`、`.venv/`、真实 API key；
3. 新工具要在 `src/world.ts` 里加 spec + handler，并更新 `ENV_PROMPT.md`；
4. 新技能要在 `tests/` 里加至少一个单元测试；
5. 说明你改了什么、怎么测的。

## 最需要的帮助

- **内存/ClrMD**：修 1.4.4 之后的偏移，加更多字段（buff、增益、Boss 血量）；
- **Coopanion 插件**：完善面板、蓝图编辑器、配置项；
- **玩法逻辑**：造房子、跳跃、矿车瞄准；
- **英文文档**：README / SETUP 翻译；
- **试玩反馈**：Issue 里写清楚「回执 + 截图 + 当时在做什么」。

## 代码风格

- Python：类型标注 + 中文注释，函数尽量小；
- TypeScript：严格模式，工具描述写清楚「什么时候用、失败会返回什么」；
- 所有对外文字用中文优先，英文其次。
