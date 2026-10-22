# nonebot-plugin-pixiv-illust

pixiv 插画/漫画订阅推送插件（NoneBot2 / OneBot V11）：按群订阅画师新作自动推送（含多页插画与漫画多页），R18 可打码。

[![Python](https://img.shields.io/badge/python-3.10--3.13-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-blue)](pyproject.toml)

> **qbot 插件** · 宿主仓库：[xinvxueyuan/qbot](https://github.com/xinvxueyuan/qbot)

> - 命令裸词触发（qbot 的 `COMMAND_START=[""]`）：`订阅插画` / `退订插画` / `插画订阅列表`。
> - 订阅**按群隔离**；R18 有「推送 / 打码」两根轴 + 一个非轴开关，默认偏保守。
> - 多页作品：文案 + 第 1 页一条消息；剩余页 **≤3 张直发多图、>3 张走合并转发**（集成 `nonebot-plugin-merge-forward`，库缺失自动降级为多图一条）。
> - 只通过 GitHub Release 分发，不发布到 PyPI。

## 功能特性

- **新作推送**：按群订阅画师，定时轮询该作者的**插画 + 漫画**新作并推送到群；过滤不可见 / 仅 mypixiv 可见的作品。
- **订阅播种**：订阅时取作者当前最新作品 ID 作为基线，**历史作品不会被一次性补推**。
- **多页支持**：一篇作品可能是多页（漫画尤其常见），一次发前 `PIXIV_ILLUST_MAX_PAGES` 页；每页都取 **master1200 原图**（去掉 CDN 的 `/c/WxH/` 缩放段，不做的话会发缩略图）。
- **订阅列表**：渲染成图片（`nonebot-plugin-htmlkit`），每条显示 **画师名 · 头像 · 订阅时间 · 画师id**（外带主页链接）。
  - 画师名/头像在**订阅时**顺带存进库；取不到时降级成显示 ID + 首字占位块。
  - 头像走**磁盘缓存**（`localstore` 的 cache 目录），不会每次重新下载。
  - 渲染失败会自动**回退成纯文本列表**，命令不会「没反应」。
- **被动 URL hook**：消息里出现 pixiv **插画链接**（`artworks/`、老式 `member_illust.php`、短链 `i/`）时自动回图（可开关）；QQ 把链接转成的**分享卡片**也能认（读 json 消息段）；同一会话同一作品有去重窗口防刷屏；机器人自己的消息不触发（避免自我循环）。
- **群白名单 / 黑名单**：白名单为空 = 全放行（默认）；黑名单优先于白名单；两者只管群，私聊不受影响。
- **表态反馈**：慢操作（拉图）打「处理中 → 完成 / 失败」表情；由**可选**依赖 `nonebot-plugin-message-reaction` 提供，库缺失时插件照常工作。
- **权限闸门**：命令与被动 hook 先过群白名单，再过管理员闸门；被拒会明确回文案（不用 NoneBot 的静默 `permission`）。
- 订阅数据按 `(group_id, author_id)` 存在 SQLite（`localstore` 数据目录），各群之间不共享。

## 安装

### 依赖

- 运行依赖：`nonebot2 >= 2.5.0`、`nonebot-adapter-onebot >= 2.4.0`、`nonebot-plugin-apscheduler >= 0.5.0`、`nonebot-plugin-localstore >= 0.7.4`、`nonebot-plugin-htmlkit >= 0.1.0rc5`、`pixivpy3 >= 3.7.5`、`httpx >= 0.28.0`、`pillow >= 12.0.0`；Python `>=3.10,<3.14`。
- 可选依赖（缺失只降级，不影响插件加载）：
  - `nonebot-plugin-htmlkit` 加载失败 → 「插画订阅列表」回退纯文本；
  - `nonebot-plugin-message-reaction`（表态）没装 → 没有表情反馈；
  - `nonebot-plugin-merge-forward`（合并转发）没装 → 多页作品不再合并转发、退化为多图一条。
  - 后两个库都不在 PyPI，由宿主以 git 子模块提供。
- 前置条件：
  1. pixiv 账号的「国・地域」设为**日本**（否则 R18 内容受地区限制；本服务器出口在 US，尤其要注意）
  2. pixiv 账号打开「显示 R18 内容」
  3. 服务器能访问 `127.0.0.1:1081` 代理（pixiv 全域名国内直连不通）
  4. 系统有 CJK 字体（渲染订阅列表用；Debian 上装 `fonts-noto-cjk`）

### 接入宿主

- 安装（宿主以 git 子模块引入）：

```bash
git submodule add -b main https://github.com/xinvxueyuan/nonebot-plugin-pixiv-illust.git vendor/nonebot-plugin-pixiv-illust
git -C vendor/nonebot-plugin-pixiv-illust checkout <钉定 SHA>
```

- 宿主 `[tool.nonebot] plugin_dirs` 加一行 `vendor/nonebot-plugin-pixiv-illust/src/plugins`

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `PIXIV_ILLUST_REFRESH_TOKEN` | `""` | pixiv refresh_token（用 `gppt login` 取得）；为空时**回退读全局 `PIXIV_REFRESH_TOKEN`**；两者都空则只注册命令、不轮询 |
| `PIXIV_ILLUST_PROXY` | `http://127.0.0.1:1081` | 出网代理，pixiv 域名国内直连不通；留空 = 直连 |
| `PIXIV_ILLUST_POLL_INTERVAL` | `600` | 轮询间隔（秒），下限 60（防触发限流） |
| `PIXIV_ILLUST_MAX_PUSH_PER_POLL` | `3` | 单轮每个订阅最多推几条（1–20），防刷屏 |
| `PIXIV_ILLUST_MAX_PAGES` | `4` | 一篇多页作品一次最多发几页图（1–30） |
| `PIXIV_ILLUST_IMAGE_MAX_WIDTH` | `0` | 页图最大宽度：`0` = 发 master1200 原图（默认）；`>0` 时缩到该宽度再发（0–2400） |
| `PIXIV_ILLUST_BLUR_R18` | `true` | R18 页图是否高斯模糊（`false` = 完全不打码） |
| `PIXIV_ILLUST_BLUR_RADIUS` | `9` | 高斯模糊半径（**固定像素**，允许 6–12，不随图片尺寸缩放） |
| `PIXIV_ILLUST_R18_PUSH_ENABLED` | `true` | **新作推送**是否包含 R18 作品（`false` = 遇到 R18 静默跳过，高水位照常推进） |
| `PIXIV_ILLUST_ADMIN_ONLY` | `false` | 指令是否仅管理员可用（默认所有人） |
| `PIXIV_ILLUST_ADMIN_IDS` | `[]` | 管理员 QQ 名单；空 = 回退 NoneBot 全局 `SUPERUSERS` |
| `PIXIV_ILLUST_GROUP_WHITELIST` | `[]` | 群白名单：**只**在列出的群里启用；空 = 关闭白名单（全放行） |
| `PIXIV_ILLUST_GROUP_BLACKLIST` | `[]` | 群黑名单：列出的群**永不**启用（优先级高于白名单）；空 = 关闭 |
| `PIXIV_ILLUST_URL_HOOK_ENABLED` | `true` | 消息里出现 pixiv 插画链接时自动回图 |
| `PIXIV_ILLUST_URL_HOOK_COOLDOWN` | `60` | 同一会话同一作品的去重窗口（秒，0–3600；`0` = 不去重） |

列表字段在 `.env` 里写 JSON，如 `PIXIV_ILLUST_GROUP_WHITELIST=[868258211]`；群号名单里转不成 int 的项会被**丢弃并记 warning**（不会因此让插件起不来）。

### 「R18」相关的**两根轴 + 一个开关**（别混）

| 轴 | 配置键 | 管什么 | 默认 |
|---|---|---|---|
| 1. R18 推送 | `PIXIV_ILLUST_R18_PUSH_ENABLED` | **新作推送**要不要带 R18 作品 | 要带 |
| 2. R18 打码 | `PIXIV_ILLUST_BLUR_R18` / `PIXIV_ILLUST_BLUR_RADIUS` | R18 的**页图**模不模糊 | 模糊 |

- 两根轴**互相独立**：轴 1 决定「发不发」，轴 2 决定「糊不糊」（不发就谈不上糊）。
- 被动 URL hook 是**用户主动贴链接**触发的，照发；R18 图按轴 2 打码。
- 想看未打码的 R18：把 `PIXIV_ILLUST_BLUR_R18` 设为 `false` —— 这是刻意的显式动作，默认模糊。

## 使用 / 命令

默认**所有人可用**（`PIXIV_ILLUST_ADMIN_ONLY=false`）；设为 `true` 后仅管理员（`PIXIV_ILLUST_ADMIN_IDS`，名单为空则回退 NoneBot 全局 `SUPERUSERS`）可用。

| 命令 | 别名 | 说明 |
|---|---|---|
| `订阅插画 <画师id>` | — | 仅群聊。本群订阅该画师新作（订阅时播种基线 + 顺带存画师名/头像） |
| `退订插画 <画师id>` | — | 仅群聊。取消订阅 |
| `插画订阅列表` | — | 仅群聊。查看本群订阅（渲染成图片，失败回退纯文本） |

参数支持直接粘贴 pixiv 链接（`pixiv.net/artworks/123`、`pixiv.net/member_illust.php?...&illust_id=123`、`pixiv.net/i/123`）。

### 多页作品的推送形态

| 剩余页数（第 2..N 页） | 第二条消息 |
|---|---|
| ≤ 3 张 | 一条消息里多个图片段（直发） |
| > 3 张 | **合并转发**（`nonebot-plugin-merge-forward`，依赖缺失或转发失败自动降级为多图一条） |

第 1 页永远跟文案在同一条消息里。`PIXIV_ILLUST_MAX_PAGES` 之外的页不发图，只在文案里写「共 X 页，本条发送前 N 页」，链接永远带。

### 其他约定

- **不引用**：回复不引用原消息，直接发。
- **不共享**：订阅是**按群**隔离的，A 群的订阅不会推到 B 群。
- **分发**：只通过 GitHub Release 发布版本，不发 PyPI。

## 开发与测试

```bash
uv run python -m pytest tests/ -q
uv run ruff check .
uv run ruff format --check .
```

- 测试目录 `tests/` 覆盖 collect / client / policy / poller / render / store / handlers / urls 与接线（含 `test_wiring.py`、`test_reaction_wiring.py`、`test_merge_forward_wiring.py`、`test_smoke.py`）；`pyproject.toml` 里 `testpaths = ["tests"]`、`asyncio_mode = "auto"`、`pythonpath = ["src/plugins"]`。
- 本机 Windows 注意：跑 pytest 前 `export PYTEST_ADDOPTS="--basetemp=$TMPDIR/pytest-x"`（默认临时目录 ACL 坑）；uv 命令建议套 `env -u UV_PYTHON -u UV_PROJECT_ENVIRONMENT -u SSL_CERT_FILE -u VIRTUAL_ENV -u PYTHONPATH`。
- `scripts/smoke_plugin_load.py`：用真 NoneBot 驱动加载本插件、打印已注册命令与可选依赖降级状态（改动接线后先跑它）。
- 门禁：`ruff`（`line-length = 120`、`target-version = "py310"`，显式 `select` 8 组规则）；本仓 `[tool.uv] package = false`，不构建发行包。

## 许可证与相关仓库

MIT —— 全文见 [`LICENSE`](LICENSE)；`pyproject.toml` 的 `license` 字段同为 MIT。

- 宿主仓库：[xinvxueyuan/qbot](https://github.com/xinvxueyuan/qbot)（以 git 子模块挂在 `vendor/nonebot-plugin-pixiv-illust`）
- [xinvxueyuan/nonebot-plugin-merge-forward](https://github.com/xinvxueyuan/nonebot-plugin-merge-forward)：剩余页 > 3 张时用它合并转发，缺失 / 失败自动降级为多图一条
- [xinvxueyuan/nonebot-plugin-message-reaction](https://github.com/xinvxueyuan/nonebot-plugin-message-reaction)：慢操作「处理中 → 完成 / 失败」的表情反馈，缺失只是没有表情
- `nonebot-plugin-htmlkit`：「插画订阅列表」出图依赖（失败回退纯文本）
