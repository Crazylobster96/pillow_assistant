# Pillow Assistant

Pillow Assistant 是一个基于 PySide6 的桌面浮动工具，提供以下能力：

- 屏幕上常驻半透明“枕头”按钮，鼠标悬停可展开菜单；
- 支持拖拽图片到枕头按钮，弹出图像预览并提供基于图像的提问输入框；
- 麦克风按钮可录制语音输入并保存为 wav；
- 键盘按钮弹出文本输入对话框，向所选模型发起真实推理（经事件总线 + LiteLLM 流式返回）；
- 拖入图片可向多模态模型提问，结果实时流式显示；
- 使用 SQLite 保存常见大语言模型与多模态模型的 API 配置，第 1 次运行会自动弹出配置窗口；
- API Key 存入操作系统密钥库（Keychain / Credential Manager），不落数据库。

## 环境准备

1. 创建并激活虚拟环境（可选）：
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

   如果需要录音功能，请确保系统已正确安装 `sounddevice` 依赖对应的底层 PortAudio 库。

## 运行

在仓库根目录执行：

```bash
python -m pillow_assistant.main
```

首次运行将弹出“模型 API 配置”对话框。至少配置一个模型（文本或多模态），随后主界面会显示浮动枕头按钮。拖拽图片或点击菜单中的按钮即可体验对应功能。录音文件与 SQLite 数据库存放在 `data/` 目录下。

## 代码结构

- `pillow_assistant/app.py`：应用入口，装配 Storage / Vault / 事件总线 / UI；
- `pillow_assistant/contracts.py`：UI 与执行层之间的契约（AppRequest / AgentEvent / SurfaceSpec）；
- `pillow_assistant/core/`：执行层 —— `bus.py`（Qt↔asyncio 事件总线）、`llm.py`（LiteLLM 封装）、`handlers.py`（LLM 处理器）；
- `pillow_assistant/ui/`：界面组件（浮动按钮、配置窗口、语音/文本输入、图像预览）；
- `storage/db.py`：SQLite 读写封装（不再存明文 Key）；`storage/vault.py`：OS 密钥库封装；
- `data/assistant.db`：默认的配置数据库路径（首次运行自动创建）。

## 测试

```bash
python tests/test_r0.py
```

## 重构进度

- **R0（解耦骨架 + 真实模型 + 凭证迁移）**：契约层 + 事件总线、模型调用桩 → LiteLLM 真实流式调用、API Key 迁入系统密钥库（首启自动迁移明文 Key）。
- **R0.5（手势 FSM + 拖放引用）**：浮动图标改为状态机交互——左键点击打字、长按语音（ASR 待 R1）、拖动移图标、右键扇形菜单；拖任意文件/文件夹到图标即挂为当前会话引用（仅存路径、不复制），并弹出文本输入条；引用在会话结束 / 手动移除前持续生效。
- **R1（Agent 核心 + 沙箱 + 项目）**：内置轻量工具循环 Agent（复用 LiteLLM 的 function calling），可自主写代码并在**子进程沙箱**里执行（工作目录限定项目 workspace、超时、默认软禁网、POSIX 资源限额）；每次会话绑定一个**项目**（`~/.pillow/projects/<id>/`），产物落在项目 workspace 里。复杂任务（如「CSV→折线图」）可端到端跑通：自动建项目 → 沙箱执行 → 产物落盘。语音转文字（ASR）仍留待后续。

- **R1 补全（语义归类 + 历史 + ASR + 项目浏览）**：每次请求经 triage 按项目索引自动关联到已有项目或新建（FR-11/15）；项目对话历史持久化并喂给 Agent，使「继续上次的任务」可增量执行（FR-13）；长按语音经 faster-whisper（可选）本地转文字后预填输入条；右键「项目」按需打开项目浏览（列表/历史/打开产物文件夹，FR-14）。
- **R2（文件类型自适应面板）**：拖文件/文件夹到图标，按类型弹出对应预览面板——图片、PDF（PyMuPDF 可选）、表格（csv/xlsx）、代码/文本、目录/压缩包、通用信息卡；每个面板内嵌模型选择 + 提问 + 流式回答，所提问题携带该文件作为引用走 Agent。可选依赖缺失时优雅降级。

- **工具体系 T0–T3**：可插拔工具注册表 + Agent 工具集——`run_python`（沙箱）、`file_read/write/list`（限工作区/引用）、`http_request`（SSRF 防护）、`run_cli`（危险命令拦截）、`browser_read`（Playwright 取页）、`apply_skill`（本地技能库 ~/.pillow/skills）、`mcp:*`（挂载外部 MCP server）；多模型路由（图像自动用 vlm）；审计日志（audit.jsonl）。
- **R3 5 秒撤销**：高危可逆动作（覆盖/新建文件）先执行 + 5 秒可撤销——图标旁弹出「撤销」条，点击即恢复原内容/删除新建文件；超时自动确认。

仍可做：R3 的 Surface L0–L5 分级与本地 RAG（本轮未做）。

> 提示：R1 任务常用 `pandas`/`matplotlib`，已加入 requirements；纯对话不需要。沙箱是「防误伤」的护栏而非安全边界，强隔离需 Docker（可选，后续）。

