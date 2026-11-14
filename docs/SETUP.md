# 安装与运行

## 环境要求

| 需要 | 说明 |
|---|---|
| Windows 10 / 11 | 只在 Windows 上测过 |
| 官方 Terraria **1.4.4** | Steam 正版，**不要用 tModLoader**（内存布局不同） |
| Python 3.10+ | 建议用项目里的 `.venv` |
| .NET Framework 4.8 | 编译 32 位内存读取器（`csc.exe`） |
| Coopanion（CoO） | 桌宠本体，提供大模型和插件宿主 |
| Node.js 20+ | 只有改插件/重新构建面板时才需要 |

## 1. Python 依赖

```bat
cd Terraria-Agent-V1
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## 2. 编译内存读取器（bridge）

```bat
cd bridge
build_bridge.bat
```

成功后应生成 `bridge\clrmd_probe32.exe`。仓库里也带了一份已编译的，但如果你改了 `probe.cs` 就要重新编译。

> 为什么是 32 位：Terraria 是 32 位进程，ClrMD 必须用 x86 才能附加。

## 3. 配置

```bat
copy config.example.json config.json
```

打开 `config.json`：

- `window_title`：`泰拉瑞亚`（或你的窗口标题）
- `exe_path`：你的 `Terraria.exe` 路径
- `deepseek_api_key`：**插件模式下可以留空**；只有网页面板的旧「关键词/本地 LLM」模式需要
- `enemy_range_tiles`：找怪范围，默认 150
- `dev_screenshot_on_failure`：失败自动截图，正式用可以关

## 4. 启动本地服务

```bat
.venv\Scripts\python main.py
```

验证：

```bat
curl http://127.0.0.1:5000/api/health
```

返回 `{"ok": true, ...}` 就说明服务起来了。游戏进世界后 `game_ready` 会变 true。

## 5. 安装 Coopanion 世界插件

1. 把 `coopanion-extension` 整个文件夹复制到 CoO 的扩展目录，例如：
   `F:\dafeiyu\data\extensions\cortico-world-terraria`
2. 在扩展目录的 `package.json` 里加依赖（示例）：
   ```json
   "dependencies": {
     "cortico-world-terraria": "link:F:/dafeiyu/data/extensions/cortico-world-terraria"
   }
   ```
3. 在 `extensions/node_modules` 里建一个指向扩展目录的 junction/软链接
   （Windows：`mklink /J node_modules\cortico-world-terraria ..\cortico-world-terraria`）。
4. 重启 CoO。

## 6. 在 CoO 里启用

打开 CoO 设置 → 泰拉瑞亚（世界插件）→：

- 启用
- 控制面板地址：`http://127.0.0.1:5000`
- Python 解释器：`...\Terraria-Agent-V1\.venv\Scripts\python.exe`
- 项目目录：`...\Terraria-Agent-V1`
- 自动拉起服务：开
- 背包键：默认 `escape`

## 7. 试一下

1. 打开 Terraria，进入一个单人世界；
2. 让桌宠先调用 `terraria_see`，确认能看到坐标/血量/地图；
3. 再试 `terraria_move`、`terraria_chop_tree`、`terraria_open_inventory`；
4. 控制面板里也能看到地图、日志和蓝图编辑器。

## 常见问题

- **动作全失败**：先确认游戏在前台；游戏以管理员运行时，Agent 也要用管理员启动。
- **看不到敌人**：确认 `enemy_range_tiles`（默认 150），并在 `terraria_see` 里看「敌人」那行。
- **放方块提示 not_holding_block**：先 `find_in_hotbar` 找木材/土块，再 `select_slot` 选中。
- **背包关不掉**：只按一次 Esc；如果屏幕上开着箱子/商店，先手动关掉或走开。
- **游戏更新后失效**：内存偏移按 1.4.4 写死，更新后需要重新找偏移（欢迎 PR）。
- **启动报 WindowsGameForm 已释放**：确认没有残留的 `Terraria.exe` 进程，只启动一次游戏。
