# 怎么上传到 GitHub（小白版）

## 0. 上传前检查（重要！）

- `config.json` **已经在 .gitignore 里，不要上传**（里面可能有你的 API key）；
- 确认目录里没有 `logs/`、`.venv/`、`__pycache__/`；
- 打开 `config.example.json` 看一眼：里面只有示例值，没有真实 key。

如果之前不小心把 key 传上去过：马上去 DeepSeek 控制台**作废并重新生成** key，再改代码。

## 方法一：网页上传（最简单，不用装 git）

1. 打开 https://github.com ，登录后点右上角 **+** → **New repository**；
2. Repository name 填 `terraria-ai-pet`（或你喜欢的名字）；
3. Description 可以写：
   `一个 17 岁高中生做的泰拉瑞亚 AI 桌宠：Coopanion 世界插件 + Python 内存读取 + 键鼠模拟`
4. 选 **Public**，**不要**勾 "Add a README file"（我们已经有 README 了）；
5. 点 **Create repository**；
6. 在空仓库页面点 **uploading an existing file**；
7. 把 `terraria-ai-pet` 文件夹里的**所有文件和文件夹**拖进去（注意：是文件夹里面的内容，不是外面那层文件夹）；
8. 底下 Commit changes 填 `Initial commit`，点 **Commit changes**。

> 网页上传对文件数量有限制，如果拖不上去，用下面的 git 方法。

## 方法二：命令行（推荐，以后改代码方便）

先安装 [Git for Windows](https://git-scm.com/download/win)，然后在项目目录打开终端：

```bat
cd C:\Users\19802\Documents\deepseek-harness\default-workspace\terraria-ai-pet

git init
git add .
git commit -m "Initial commit: Terraria AI pet (unfinished)"
git branch -M main
git remote add origin https://github.com/你的用户名/terraria-ai-pet.git
git push -u origin main
```

- 第一次 push 会要求登录：用浏览器授权，或者用 **Personal Access Token** 当密码（GitHub 设置 → Developer settings → Tokens）。
- 如果提示 `remote origin already exists`：
  `git remote set-url origin https://github.com/你的用户名/terraria-ai-pet.git`

以后改了代码：

```bat
git add .
git commit -m "修了 xx"
git push
```

## 上传后建议做的

1. 仓库右侧 **About** → 填一句简介 + Topics：
   `terraria` `ai-agent` `coopanion` `game-automation` `python` `clrmd`
2. 确认首页能看到 README（会自动渲染）；
3. 在 README 里把「联系我」改成你的 GitHub 主页或 Issue 链接；
4. 想加 LICENSE 就保留现在的 MIT；想换成自己的名字，改 `LICENSE` 里的 Copyright 行；
5. 如果项目还没完善，可以在仓库名或 README 顶部保留 **「未完成版 / WIP」**，避免别人误解。

## 写 README 的小技巧

- 第一段就说清楚「这是什么、给谁用、现在什么状态」；
- 放一张截图或动图（GIF 最直观）；
- 写「怎么装、怎么跑、常见问题」；
- 老实写「还没做好的地方」，别人反而更愿意帮忙；
- 最后写「欢迎 Issue / PR」，并列出最需要的帮助方向。

## 常见问题

- **push 被拒（rejected）**：先 `git pull --rebase origin main` 再 push；
- **文件太大**：不要提交 `.venv/`、`logs/`、`*.zip`；
- **想删掉已上传的 config.json**：`git rm --cached config.json && git commit -m "remove config" && git push`，然后**立刻换 key**。
