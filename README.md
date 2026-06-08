# Pillow Assistant（瞌睡送枕头 · 开源版）

一个「弱 UI、强 Agent」的本地桌面 AI 助手：屏幕上只有一枚半透明的枕头图标，
所有能力都从它出发。纯 Python（PySide6）单技术栈，数据与执行 100% 在本机，
API Key 存操作系统密钥库，绝不落盘明文。

## 交互模型

| 手势 | 行为 |
| --- | --- |
| 左键点击 | 弹出/收起对话输入条（带最近对话历史） |
| 左键长按 | 录音，松手本地转文字（SenseVoice / faster-whisper） |
| 左键拖动 | 移动图标（所有已开窗口跟随，跨屏不漂移） |
| 右键 | 扇形菜单：项目 / 配置 / 关闭展示 / 清除引用 / 退出 |
| 拖入文件/文件夹 | 按类型弹预览面板，文件自动挂为会话引用（仅存路径不复制） |

### 文件预览面板

图片（随窗缩放）、表格（csv/xlsx）、Word（富文本）、PPT、PDF、代码/文本、
**视频（QtMultimedia 播放器：播放/暂停/进度条，降级缩略图）**、
目录树（可单选/Ctrl/Shift 多选文件，针对所选文件提问）。
所有面板：边缘/角拖拽缩放、Ctrl+左键拖动移动、内嵌提问框流式回答。

### 多窗口展示

对话中说「把这两个文件并排展示」，Agent 调用 `present_windows` 按类型平铺多个
窗口（左右/上下），整组共用一个输入条；单击图标一键恢复单一对话。

## Agent 能力

内置多步工具循环（LiteLLM function calling），工具集：

- `run_python` — 子进程沙箱执行代码（限项目工作区、超时、默认禁网、资源限额）
- `file_read / file_write / file_list` — 限工作区与引用；写文件带 **5 秒撤销**
- `http_request`（SSRF 防护）/ `run_cli`（危险命令拦截）/ `browser_read`（Playwright）
- `present_windows` — 多窗口对比展示文字/文件/文件夹树
- `process_video` — 探测/按时长或大小切片段/压缩到目标大小/均匀抽帧，
  让视频满足后台模型的长度与大小要求（需 ffmpeg 或 imageio-ffmpeg）
- `ask_user` — Agent 不确定时弹选择/输入对话框，等你回答再继续
- `list_models / configure_model / assign_model_role` — Agent 按需求自配模型：
  chat（对话）/ vision（看图）/ asr（语音）三个用途角色，持久化并自动路由
- `set_language` — 对话切换界面语言（zh/en，热生效）
- `apply_skill`（~/.pillow/skills 技能库）/ `mcp:*`（挂载外部 MCP server）

每次请求先**三分分诊**：单次对话（不建项目）/ 延续已有项目 / 新建项目。
项目位于 `~/.pillow/projects/<id>/`（元数据 + workspace 产物 + 多会话历史），
对话历史持久化并自动喂给 Agent；聊天记忆重启不丢。审计日志 audit.jsonl。

## 安装与运行

```bash
pip install -r requirements.txt   # 可选依赖均已注释说明，缺失时优雅降级
python -m pillow_assistant.main
```

首次运行弹出「模型 API 配置」：支持 OpenAI / Anthropic / vLLM / Ollama / 自定义
（OpenAI 兼容）端点，区分 llm / vlm 类型。语音转写推荐 `pip install funasr torch torchaudio`
（SenseVoice，中文效果好）；视频处理需系统 ffmpeg 或 `pip install imageio-ffmpeg`。

语言：跟随系统，或 `PILLOW_LANG=zh|en`，或对话里直接说「switch to English」。

## 代码结构

```
pillow_assistant/
  contracts.py          # UI↔核心契约：AppRequest / AgentEvent / SurfaceSpec
  app.py                # 装配：Storage/Vault/总线/编排器/撤销/AskBroker/UI
  core/
    bus.py              # Qt 主线程 ↔ 后台 asyncio 事件总线
    orchestrator.py     # 分诊→项目→Agent 循环→历史落盘
    agent/              # 工具循环 + 中英系统提示词
    tools/              # 沙箱、注册表、内置工具、MCP 客户端
    triage.py / project_manager.py / model_router.py / model_roles.py
    asr.py / ask.py / undo.py / surface_router.py / i18n.py / references.py
  ui/
    floating_widget.py  # 浮动图标手势 FSM 主控
    panels/             # 类型自适应预览面板（含视频播放）
    quick_input.py / radial_menu.py / ask_dialog.py / surface_window.py ...
storage/                # SQLite（无明文 Key）+ OS 密钥库 + 项目存储
tests/                  # 全量单测（pytest）
```

## 测试

```bash
pytest tests/
```

## 安全说明

沙箱是「防误伤」护栏而非安全边界（强隔离需 Docker）；`run_cli` 默认开启但有
危险命令黑名单；HTTP/浏览器工具拦截内网与本机地址；数据库不存任何凭证。
