# astrbot_plugin_sd_draw

AstrBot 群聊文生图插件：群里发一句 `/draw 描述`，出图回群。支持本地 SD WebUI、ComfyUI 和任何 OpenAI 兼容的图片接口；带提示词润色确认、每日配额、禁词过滤和一个可视化管理页。

> 许可证：本项目使用自定义《非商业使用许可》，允许免费使用、修改、再分发（须保留版权与许可），**禁止商业用途**。详见 [LICENSE](LICENSE)。

## 目录

- [功能](#功能)
- [依赖](#依赖)
- [安装](#安装)
- [配置项](#配置项)
- [命令用法](#命令用法)
- [管理页](#管理页)
- [常见问题](#常见问题)
- [目录结构](#目录结构)

## 功能

- `/draw 描述`（别名 `/画`）出图，四选一菜单决定「润色 / 直接」与「默认风格 / 选风格」。
- 三后端可切：SD WebUI（A1111 / Forge）、ComfyUI、OpenAI 兼容图片接口；主后端探测失败会按 `webui → comfyui` 顺序回退（`openai` 不参与回退链）。
- 提示词润色：借用 AstrBot 已配置的对话模型做中文润色、中译英与微调；润色结果贴回群里让发起人确认。
- 配额：每人每日额度 + 全群每日总额度，提交即扣（出图失败也扣）。
- 禁词过滤：原始描述与润色稿各查一次；NSFW 总闸默认关闭，可加白名单群。
- 管理页：改配置、改口令、编辑风格表与禁词表、下载模型、一键安装 ComfyUI。

## 依赖

| 依赖 | 说明 |
| --- | --- |
| AstrBot v4 | 插件基于 AstrBot 插件 API（`astrbot.api`），见 `metadata.yaml` 的 `depends: astrbot.core` |
| aiohttp | AstrBot 运行时自带，插件用它请求后端接口与管理页 |
| py7zr >= 0.20 | 见 `requirements.txt`；只在管理页「一键安装 ComfyUI」解压官方 7z 包时用到，不装不影响出图 |
| 7-Zip（可选，当前未启用） | 代码里保留了 `7z.exe` 探测函数，但解压流程目前只调用 py7zr，装了 7-Zip 也不会被用到 |
| 一个出图后端 | 三选一：① SD WebUI / Forge，须以 `--api` 启动；② ComfyUI；③ OpenAI 兼容的 `POST /v1/images/generations`（返回 `b64_json`） |
| 一个对话模型 | 润色 / 中译英 / 微调要用 AstrBot 里配置好的对话模型（Provider）；没有则自动跳过润色直接出图 |

`requirements.txt` 全文只有 `py7zr>=0.20` 这一条依赖声明，其余均为 AstrBot 运行时自带。

## 安装

### 插件市场

1. 先把 `metadata.yaml` 里的 `repo` 换成真实仓库地址，推上 GitHub 后，再到 AstrBot 面板「插件市场」按仓库名或插件名 `astrbot_plugin_sd_draw` 搜索安装。
2. 点安装，等待拉取与依赖安装。
3. 重启 AstrBot（或重载插件），日志里会出现管理页地址与初始口令。

### 手动安装

1. 把整个 `astrbot_plugin_sd_draw` 目录放进 AstrBot 的 `data/plugins/` 下：

   ```
   AstrBot/
   └─ data/plugins/astrbot_plugin_sd_draw/
   ```

2. 若需要「一键安装 ComfyUI」，先装依赖：

   ```bash
   pip install -r requirements.txt
   ```

3. 重启 AstrBot。插件首次启动会在自己的目录下生成 `data/`（配置、配额、风格表、日志、临时图），无需手工创建。

4. 去 AstrBot 面板「插件配置」或直接用管理页（默认 `http://127.0.0.1:7865/`）把后端地址、模型名填好。

## 配置项

以下字段与 `_conf_schema.json` 逐项对应，可在 AstrBot 面板的插件配置里改；同名项也能在管理页改（管理页改动会同步回面板配置，面板重启后生效）。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `backend_primary` | string | `webui` | 主后端引擎，可选 `webui` / `comfyui` / `openai` |
| `webui_url` | string | `http://127.0.0.1:7860` | WebUI 地址（A1111 / Forge，需 `--api` 启动） |
| `comfyui_url` | string | `http://127.0.0.1:8188` | ComfyUI 地址 |
| `openai_api_key` | string | 空 | OpenAI 兼容接口的密钥，选 `openai` 后端时必填 |
| `openai_base_url` | string | `https://api.openai.com/v1` | OpenAI 兼容接口的基准地址 |
| `openai_model` | string | `gpt-image-1` | OpenAI 生图模型名，中转站可按其别名填 |
| `default_model` | string | 空 | 默认 checkpoint 文件名（本地引擎用；留空用后端当前加载的模型） |
| `width` | int | `1024` | 生图宽度（像素） |
| `height` | int | `1216` | 生图高度（像素） |
| `steps` | int | `28` | 采样步数 |
| `cfg_scale` | float | `6.0` | CFG 引导强度 |
| `sampler` | string | `DPM++ 2M Karras` | 采样器名称 |
| `negative_prompt` | text | `lowres, bad anatomy, bad hands, blurry, worst quality` | 负面提示词 |
| `hires` | bool | `true` | 是否自动高清修复（hires fix） |
| `hires_upscaler` | string | `R-ESRGAN 4x+ Anime6B` | 高清修复放大器 |
| `quota_per_user_per_day` | int | `1` | 每人每日额度（张） |
| `quota_total_per_day` | int | `10` | 全群每日总额度（次） |
| `desc_max_len` | int | `500` | 描述字数上限，超出直接打回 |
| `confirm_timeout` | int | `30` | 菜单 / 选风格等待秒数，超时按「③ 直接 + 默认风格」出图 |
| `cancel_timeout` | int | `120` | 确认 / 微调环节等待秒数，超时取消任务并解锁 |
| `pollish_on` | bool | `true` | 润色总开关（中文润色 + 翻译 + 微调） |
| `pollish_model` | string | 空 | 润色 / 翻译 / 微调用的模型名，留空用当前默认对话模型 |
| `allow_private` | bool | `false` | 是否允许私聊使用 |
| `nsfw_on` | bool | `false` | NSFW 许可总闸（默认关；只作用于群聊链路，API 直连不受限） |
| `nsfw_whitelist` | list[string] | `[]` | NSFW 白名单群号，逗号分隔；白名单群即使总闸关闭也放行 |
| `download_dir` | string | 空 | 模型下载目录，留空 = 插件 `data/downloads` |
| `download_source` | string | `hf-mirror` | 模型下载源，可选 `official` / `hf-mirror` / `hf-mirror2` / `custom` |
| `engine_dir` | string | 空 | 引擎部署目录，留空 = 插件 `data/engines` |
| `admin_port` | int | `7865` | 管理页端口（改完需重启生效） |

补充：`config.py` 里还有几个字段**没有**写进 `_conf_schema.json`，因此 AstrBot 面板看不到，需要直接改插件目录下的 `data/config.json`（`admin_token` 也能在管理页改口令）：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `trigger` / `aliases` | `/draw` / `["画"]` | 触发命令与别名。**目前仅存配置，命令仍是硬编码的 `/draw`、`/画`，改这两项不生效**（见常见问题） |
| `backend_timeout` | `300` | 出图超时（秒） |
| `seed` | `-1` | 随机种子，`-1` 为随机 |
| `hires_scale` | `1.4` | 高清放大倍率（8GB 显存建议 ≤ 1.4） |
| `hires_denoise` | `0.4` | 二遍重绘强度（0.35~0.5） |
| `custom_download_url` | 空 | `download_source = custom` 时的地址模板，`{file}` 替换为文件名 |
| `hf_token` | 空 | HuggingFace 令牌，下载 gated / 受限仓库时用 |
| `admin_token` | 空 | 管理页口令，首次启动随机生成并写进日志 |
| `log_dir` | 空 | 日志目录，留空 = 插件 `data/logs` |

## 命令用法

| 输入 | 作用 |
| --- | --- |
| `/draw <描述>` / `/画 <描述>` | 发起一次出图。不带描述会提示「请带上描述哦」；超过 `desc_max_len` 会提示精简 |
| `1` / `2` / `3` / `4` | 主菜单四选一：① 润色 + 默认风格　② 润色 + 选风格　③ 直接 + 默认风格　④ 直接 + 选风格。`confirm_timeout`（默认 30 秒）不选按 ③ 处理 |
| `数字` | 选风格阶段：回复风格序号（序号来自 `data/style_table.json`，至少 2 个风格才会让你选） |
| `1` | 确认阶段：确认出图 |
| `2` | 确认阶段：让对话模型重写一版（最多 2 次） |
| `3` | 确认阶段：进入微调模式 |
| `4` | 确认阶段：取消任务（配额不退） |
| 任意文字 | 微调模式下发修改要求（如「删掉宝宝相关内容」），200 字以内；回 `1` 用当前稿出图，回 `4` 或发「取消」退出 |

流程细节：

1. 原始描述先查禁词（`nsfw_on` 关闭且群不在白名单时），撞词打回，不扣配额、不占锁。
2. 同一时间只跑一张图，忙时直接拒收，不排队。
3. 抢到锁就扣配额，出图失败也照扣。
4. 选「润色」分支：调 AstrBot 当前对话模型润色（必要时中译英），结果贴回群里，回 `1` 确认、`2` 重写、`3` 微调、`4` 取消。
5. 选「直接」分支：直接出图；中文描述在出图前会尝试自动英文化，失败则提示本次效果可能不佳。
6. 出图结果以图片形式回群，并附后端名（如 `[via webui]`）。

## 管理页

- 地址：`http://127.0.0.1:<admin_port>/`，默认 <http://127.0.0.1:7865/>。
- 口令：首次启动随机生成，写进 AstrBot 日志（搜 `管理页口令已生成`），进页面后请立即修改。
- 接口鉴权：`/api/*` 需要请求头 `X-Sd-Token` 或 Cookie `sd_token`。
- 功能：基本配置、改口令、风格表、禁词表、提示词预设、模型参数预设、模型下载（`official` / `hf-mirror` / `custom` 三源）、引擎状态 / 启动 / 停止 / 一键安装 ComfyUI、日志、配置备份与恢复。
- 忘了口令：编辑插件目录下 `data/config.json`，清空 `admin_token` 字段后重启 AstrBot，会重新随机生成并打进日志。

## 常见问题

**Q：日志报连不上 WebUI / 502？**
A：A1111 / Forge 必须带 `--api` 启动，且 `webui_url` 要能从 AstrBot 所在机器访问。默认端口 7860，ComfyUI 默认 8188。

**Q：出图失败但配额被扣了？**
A：这是设计行为——提交即扣费，出图失败也照扣。想重试就直接再发一次 `/draw`，或用管理页调大 `quota_per_user_per_day`。

**Q：私聊发 `/draw` 被拒？**
A：`allow_private` 默认 `false`，去面板或管理页打开即可。

**Q：明明选了「润色」却没看到润色稿？**
A：AstrBot 里没有可用的对话模型（Provider）时，插件会跳过润色直接出图，日志里有「润色不可用，直接出图」。去 AstrBot 配好默认对话模型，或在 `pollish_model` 指定模型。

**Q：想把命令改成别的词，改了 `trigger` / `aliases` 没反应？**
A：目前未实现——命令由 `@filter.command("draw", alias={"画"})` 硬编码注册，配置项只存值、不参与注册。需要改命令请直接改 `main.py` 的这行。（待补）

**Q：管理页 7865 打不开？**
A：先确认 `admin_port` 没被别的程序占用，改端口后要重启 AstrBot；管理页只监听 `127.0.0.1`，从其他机器访问请走 SSH 隧道或反代。

**Q：「一键安装 ComfyUI」能用吗？**
A：只能装官方 Windows 便携包（`ComfyUI_windows_portable_nvidia.7z`，约 1GB+），所以实际上只适用于 Windows。解压依赖 `py7zr`，没装会直接报错；代码里虽然探测了 `7z.exe`，但当前解压流程没接它（待补）。WebUI 没有官方便携包，插件不代装，管理页只给教程链接。

**Q：`data/` 目录会不会越积越大？**
A：每次出图会在 `data/tmp/` 留一张 PNG，插件不会自动清理，需要定期手动删。该目录已被 `.gitignore` 忽略，不会进仓库。

**Q：为什么仓库里没有 `data/`？**
A：`data/` 是运行期生成的（用户配置、配额账本、风格表、临时图、日志），含隐私与图片，按设计不入库；插件缺文件时会用内置默认值兜底。

## 目录结构

```
astrbot_plugin_sd_draw/
├─ main.py                 主入口：命令注册、抢锁、配额、状态机、超时轮巡
├─ config.py               配置模型与落盘（ConfigStore）
├─ __init__.py             包声明
├─ metadata.yaml           AstrBot 插件元数据
├─ _conf_schema.json       AstrBot 面板配置 schema
├─ requirements.txt        Python 依赖
├─ LICENSE                 非商业使用许可（中英双语）
├─ README.md               本文件
├─ admin/
│  ├─ __init__.py
│  ├─ webapp.py            管理页路由与 API（独立 aiohttp 服务）
│  └─ static/              管理页前端（index.html / login.html）
├─ services/
│  ├─ __init__.py
│  ├─ backend.py           WebUI / ComfyUI / OpenAI 三后端适配
│  ├─ pollish.py           润色 / 翻译 / 微调（借用 AstrBot LLM Provider）
│  ├─ quota.py             每日配额账本
│  ├─ badwords.py          禁词表
│  ├─ downloader.py        模型下载（断点续传 + 多源）
│  └─ engine.py            ComfyUI 一键安装
└─ data/                   运行期自动生成，不入库
```

## 说明

- 本项目由 `sd_draw` 整理而来，目录名与插件名统一为 `astrbot_plugin_sd_draw`。
- 使用前请自行确认所在群规与当地法律，生图内容由使用者负责。

## 免责声明

- 本项目**仅供个人自用与学习**，没有针对生产环境做加固，也不承诺长期维护。
- 按现状（AS IS）提供，**不保证适用于任何特定用途**；因使用产生的数据丢失、服务异常、账号风险，作者不承担责任。
- 发现 bug 欢迎提 issue，但不承诺修复时限；急用请自行 fork 修改（改完保留原版权声明即可）。
- 涉及第三方服务的部分（AstrBot / LivingMemory / NapCat / SD WebUI / ComfyUI / OpenAI 兼容接口等）请遵守各自的服务条款。
