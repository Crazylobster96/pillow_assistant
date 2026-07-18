# Pillow Assistant（瞌睡送枕头）

[中文](#中文) | [English](#english)

---

## 中文

一个「弱 UI、强 Agent」的本地桌面 AI 助手：屏幕上只有一枚半透明的枕头图标，所有能力都从它出发。纯 Python（PySide6）单技术栈，数据与执行 100% 在本机，API Key 存操作系统密钥库，绝不落盘明文。

### 安装与启动

作为 pip 包安装（推荐），会得到一个 `pillow-assistant` 命令。基础安装只含运行所需的最小依赖，其余按需通过 extras 选装（缺失时自动降级，不影响其它功能）：

```bash
pip install .                         # 基础：对话 + 本地 Agent
pip install ".[all]"                  # 全功能一次装齐
pip install ".[previews,asr,video]"   # 按需组合（文件预览 / 语音转写 / 视频处理）

pillow-assistant                      # 启动（等同 python -m pillow_assistant）
```

可选 extras：`sandbox`（pandas/matplotlib，Agent 数据与画图）、`voice`（录音）、`asr`（SenseVoice 中文语音转写）、`asr-whisper`（faster-whisper 回退）、`previews`（PDF/Excel/Word/PPT 预览）、`video`（视频切分/压缩/抽帧，也可直接装系统 ffmpeg）、`browser`（Playwright 取网页）、`mcp`（挂载外部 MCP server）、`all`（全部）。

开发态运行（不安装）：

```bash
pip install -r requirements.txt
python -m pillow_assistant
```

应用数据库默认保存在 `~/.pillow/data/assistant.db`。从旧版本升级时，首次启动会从安装目录旁的旧 `data/assistant.db` 安全复制数据，保留旧库并在新目录写入 `migration.json` 迁移记录。Docker、便携版或企业部署可通过环境变量 `PILLOW_DATA_DIR` 指定其它数据目录。

首次运行会弹出「模型 API 配置」窗口：

1. 选择服务提供商：OpenAI / Anthropic / vLLM / Ollama / 自定义（任何 OpenAI 兼容端点）；
2. 填写显示名称（自定，作为唯一标识）、实际模型名（如 `gpt-4o`、`qwen2.5`）、接口地址（如 `https://api.openai.com/v1`）；
3. 模型类型选 `llm`（纯文本）或 `vlm`（多模态，能看图）；
4. API Key 输入后存入系统密钥库（Windows 凭据管理器 / macOS Keychain），不写入数据库。

至少配置一个模型即可使用；以后可随时右键图标 → 配置修改。

可选增强（缺失时自动降级，不影响其它功能）：

| 功能 | 安装 |
| --- | --- |
| 语音转文字（推荐，中文效果好） | `pip install funasr torch torchaudio`（SenseVoice） |
| 语音转文字（轻量回退） | `pip install faster-whisper` |
| 视频切分/压缩/抽帧 | 系统安装 ffmpeg，或 `pip install imageio-ffmpeg` |
| PDF / Excel / Word / PPT 预览 | `pymupdf` / `openpyxl` / `python-docx` + `mammoth` / `python-pptx` |
| 浏览器取网页工具 | `pip install playwright && playwright install chromium` |

### 基本用法

#### 图标手势

| 手势 | 行为 |
| --- | --- |
| 左键点击 | 打开对话输入条；再点一下关闭所有窗口回到图标 |
| 左键长按（>0.4s） | 开始录音，松手自动转文字并预填输入条 |
| 左键拖动 | 移动图标，所有打开的窗口一起跟随（跨屏不漂移） |
| 右键 | 扇形菜单：项目 / 配置 / 关闭展示 / 清除引用 / 退出 |
| 拖入文件或文件夹 | 按类型打开预览面板，并挂为本次会话的引用 |

#### 对话

点击图标弹出输入条：选模型 → 输入问题 → 回车发送，回答流式显示。输入条会预载最近几轮对话历史，不会空白开局。关闭方式：右上角 ×、Esc、或再点一下图标。右下角手柄或任意边缘可拖拽调整大小。

每次提问会自动**三分分诊**：闲聊/简单问题走「单次对话」（不建项目）；复杂工作自动新建**项目**或延续同源的已有项目。项目存放在 `~/.pillow/projects/<id>/`，含元数据、产物目录（workspace）和多会话对话历史；历史自动喂给 Agent，「继续上次的任务」可增量推进。右键 → 项目可浏览项目列表、历史与产物文件夹。

Agent 单次任务默认最多 **50 步**工具调用；打满会暂停并提示，回复「继续」可**保留现场续跑**（包括之前所有工具调用结果，再获完整步数预算）。说「把最大步数调到 100」即可持久调整（1–500）。

#### 项目自动切换与聊天的关系

每轮对话都会自动分诊，决定它属于当前项目、属于过去的某个项目，还是只是一次无关闲聊：

- **属于某个过去的项目**：当判断这句话明显是在延续/追问/修改某个已有项目的工作、且置信度 ≥ 0.8 时，会**自动把对话切换到那个项目**继续（顶部提示「🔀 已切换到项目 X 继续」），这一轮及后续都在该项目里推进。置信度未达 0.8 时不切换，继续当前对话，直到某一轮足够有把握再切。
- **在项目里说无关的话**：当前会话仍**停留在该项目**，不会被切出去；这句无关的话只作为一次性闲聊就地回答（顶部显示「💬 对话」），**不写入项目历史、不影响项目产物**。下一句只要和项目相关，就继续在原项目推进。
- **手动控制**：右键 → 项目，可双击或点「切换到此项目对话」手动切换；也可切到「💬 单次对话」回到不归属任何项目的聊天。

简单说：相关的话自动归并进对应项目，无关的闲聊就地答掉而不离开当前项目，分类拿不准时倾向于稳在当前对话。

#### 文件问答

把文件拖到图标上，按类型打开预览面板：图片（随窗缩放）、表格、Word（富文本）、PPT、PDF、代码/文本、视频（内置播放器：播放/暂停/进度条）、文件夹（目录树）。面板通用操作：

- 边缘/右下角拖拽缩放；Ctrl+左键在任意位置（含表格上）拖动移动；标题栏/空白处直接拖动；
- 面板底部内嵌提问框，所提问题自动携带该文件；
- 文件夹树中可**单选或 Ctrl/Shift 多选**文件，提问只针对所选文件；不选则针对整个目录；
- 输入条里的引用芯片可点击重新打开预览，点 × 移除引用。

#### 多窗口对比

对话中直接说「把这两个文件并排展示」「上下对比这三段结果」，Agent 会调用 `present_windows` 平铺多个窗口（每个窗口按文件类型选查看器，文件夹显示目录树），整组共用一个输入条。单击图标或右键 → 关闭展示即可一键全关。

#### 视频

拖入视频即可播放预览。视频对话示例：「这个视频太大了，帮我处理成模型能接受的输入」——Agent 会自动探测时长/大小，再按需切片段（`split`）、压缩到目标大小（`compress`）或均匀抽帧成图片（`frames`，给只收图片的视觉模型）。产物落在项目 workspace。

#### 让 Agent 配置自己

- 「把 qwen2.5 设为对话模型，看图用 GPT-4o」→ Agent 调 `assign_model_role` 持久化角色（chat/vision/asr），后续请求自动路由；也可在配置窗口选中模型后点「**设为默认对话模型**」；
- 「帮我接入本地 Ollama 的 llama3」→ Agent 调 `configure_model` 新增配置；
- 「把最大步数调到 100」→ `set_max_steps` 持久化步数预算；
- 「switch to English」/「切回中文」→ 界面与提示语言热切换（也可用环境变量 `PILLOW_LANG=zh|en`）。

#### 其它

- **5 秒撤销**：Agent 覆盖/新建文件后，图标旁弹出撤销条，5 秒内可一键恢复；
- **Agent 反问**：信息不足时 Agent 会弹出选择/输入对话框等你拍板，再继续执行；
- **审计**：每次运行的步骤/工具调用记录在项目目录的 `audit.jsonl`。

### 测试

```bash
pytest tests/
```

### 安全说明

沙箱是「防误伤」护栏而非安全边界（强隔离需 Docker）。所有 `SYSTEM` / `NETWORK` 工具每次执行前都必须由用户确认，无 UI 时默认拒绝；确认窗口会隐藏 API Key、Token 等敏感参数。`run_cli` 不再启用命令行 shell，不支持管道、重定向或命令串联，并过滤传给子进程的常见凭据环境变量；HTTP/浏览器工具还会拦截内网与本机地址（防 SSRF）。数据库不存任何凭证，审计日志也会脱敏工具参数。

---

## English

A local desktop AI assistant with a "minimal UI, powerful Agent" design: a single translucent pillow icon on your screen is the entry point to everything. Pure Python (PySide6), all data and execution stay 100% on your machine, and API keys live in the OS keyring — never written to disk in plain text.

### Install & run

Install as a pip package (recommended) — you get a `pillow-assistant` command. The base install carries only the minimal runtime; everything else is opt-in via extras (missing extras degrade gracefully):

```bash
pip install .                         # base: chat + local Agent
pip install ".[all]"                  # everything at once
pip install ".[previews,asr,video]"   # mix and match (previews / speech-to-text / video)

pillow-assistant                      # launch (same as python -m pillow_assistant)
```

Extras: `sandbox` (pandas/matplotlib for Agent data + charts), `voice` (recording), `asr` (SenseVoice Chinese STT), `asr-whisper` (faster-whisper fallback), `previews` (PDF/Excel/Word/PPT), `video` (split/compress/extract frames; system ffmpeg also works), `browser` (Playwright), `mcp` (external MCP servers), `all`.

Run from source without installing:

```bash
pip install -r requirements.txt
python -m pillow_assistant
```

The application database defaults to `~/.pillow/data/assistant.db`. On the first launch after upgrading, an existing legacy `data/assistant.db` beside the installation is safely copied to the new location; the legacy database is preserved and `migration.json` records the migration. Docker, portable, and managed deployments can select another directory with `PILLOW_DATA_DIR`.

The first run opens the **Model API Settings** dialog:

1. Pick a provider: OpenAI / Anthropic / vLLM / Ollama / Custom (any OpenAI-compatible endpoint);
2. Fill in a display name (your unique label), the actual model id (e.g. `gpt-4o`, `qwen2.5`) and the base URL (e.g. `https://api.openai.com/v1`);
3. Choose the model type: `llm` (text) or `vlm` (multimodal, can see images);
4. The API key goes to the OS keyring (Windows Credential Manager / macOS Keychain), never the database.

One model is enough to start; right-click the icon → Settings to change things later.

Optional extras (everything degrades gracefully when missing):

| Feature | Install |
| --- | --- |
| Speech-to-text (recommended) | `pip install funasr torch torchaudio` (SenseVoice) |
| Speech-to-text (light fallback) | `pip install faster-whisper` |
| Video split / compress / frame extraction | system ffmpeg, or `pip install imageio-ffmpeg` |
| PDF / Excel / Word / PPT previews | `pymupdf` / `openpyxl` / `python-docx` + `mammoth` / `python-pptx` |
| Browser page-reading tool | `pip install playwright && playwright install chromium` |

### Usage

#### Icon gestures

| Gesture | Action |
| --- | --- |
| Left click | Open the dialog bar; click again to close every window |
| Long press (>0.4s) | Record voice; release to transcribe into the input |
| Left drag | Move the icon — all open windows follow (multi-monitor safe) |
| Right click | Radial menu: Projects / Settings / Close views / Clear refs / Quit |
| Drop files/folders | Type-appropriate preview panel + session reference |

#### Conversation

Click the icon to open the input bar: pick a model → type → Enter; answers stream in. The bar preloads recent conversation history. Close with the × button, Esc, or another click on the icon. Resize from any edge or the corner grip.

Every request is **triaged three ways**: small talk / simple questions run as one-off chat (no project); complex work creates a **project** or continues a related existing one. Projects live in `~/.pillow/projects/<id>/` with metadata, an artifacts workspace and multi-session history; history is fed back to the Agent so "continue where we left off" just works. Right-click → Projects to browse them.

Each run has a tool-step budget (default **50**). When exhausted, the Agent pauses — reply "continue" to **resume with the full transcript** (all prior tool results kept, fresh budget granted). Say "set max steps to 100" to adjust persistently (1–500).

#### Auto project-switching and its relationship with chat

Every turn is triaged to decide whether it belongs to the current project, to a past project, or is just unrelated small talk:

- **Belongs to a past project**: when a turn clearly continues / follows up on / edits an existing project's work with confidence ≥ 0.8, the conversation **switches into that project automatically** (a "🔀 Switched to project X" note appears) and continues there. Below 0.8 it does not switch — it keeps chatting until a turn is confident enough.
- **Unrelated talk inside a project**: the session **stays in the current project**; the unrelated turn is answered as a one-off chat (a "💬 Chat" note) and is **not written into the project history or artifacts**. The next project-related message simply continues the project.
- **Manual control**: right-click → Projects to double-click or "Switch chat to this project"; or switch to "💬 One-off chat" to leave any project binding.

In short: related turns fold into the right project automatically, unrelated small talk is answered in place without leaving the current project, and when the classification is uncertain it prefers to stay put.

#### Asking about files

Drop a file onto the icon to open its preview: image (fit-to-window), table, Word (rich text), PPT, PDF, code/text, video (built-in player with play/pause/seek), or a folder tree. Panel basics:

- resize from edges / corner grip; move with Ctrl+left-drag anywhere (even over tables), or drag the title/background;
- the embedded prompt box automatically targets the file;
- in a folder tree, select one file or **Ctrl/Shift multi-select** several — questions target the selection (none selected = the whole folder);
- reference chips in the input bar reopen previews on click; × removes a reference.

#### Multi-window compare

Just say "show these two files side by side" — the Agent calls `present_windows` to tile windows (each with the right viewer; folders render as trees), with one shared input bar for the group. One click on the icon (or right-click → Close views) closes them all.

#### Video

Dropped videos play right in the panel. Try: "this video is too big for the model — make it fit". The Agent probes duration/size, then splits into segments, compresses to a target size, or extracts evenly spaced frames for image-only vision models. Outputs land in the project workspace.

#### Let the Agent configure itself

- "Use qwen2.5 for chat and GPT-4o for images" → `assign_model_role` persists purpose roles (chat/vision/asr) and routing follows automatically; or select a model in Settings and click "**Set as default chat model**";
- "Hook up llama3 from my local Ollama" → `configure_model` adds the config;
- "Set max steps to 100" → `set_max_steps` persists the step budget;
- "切回中文" / "switch to English" → hot-swaps the UI/prompt language (or set `PILLOW_LANG=zh|en`).

#### More

- **5-second undo**: after the Agent overwrites/creates a file, an undo toast appears next to the icon;
- **Agent asks back**: when unsure, the Agent pops a choice/input dialog and waits for your call;
- **Auditing**: every run's steps and tool calls are logged to `audit.jsonl` in the project folder.

### Tests

```bash
pytest tests/
```

### Security notes

The sandbox is a guard rail, not a security boundary (use Docker for hard isolation). Every `SYSTEM` / `NETWORK` tool call requires per-execution user confirmation and is denied when no confirmation UI is available; API keys, tokens, and similar arguments are redacted in the prompt. `run_cli` no longer invokes a command shell, does not support pipes, redirects, or command chaining, and filters common credential environment variables before launching a child process. HTTP/browser tools also reject private and loopback addresses (SSRF). The database stores no credentials, and audit logs redact sensitive tool arguments.
