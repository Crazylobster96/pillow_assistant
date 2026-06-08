"""Lightweight i18n (zh / en).

Usage::

    from pillow_assistant.core.i18n import t
    label = t("menu.projects")
    msg = t("file.write_failed", err=exc)

Language resolution order:
  1. ``PILLOW_LANG`` environment variable ("zh" / "en")
  2. ``~/.pillow/lang`` file containing "zh" or "en"
  3. system locale (Chinese -> zh, everything else -> en)

Missing keys fall back to the zh pack, then to the key itself, so an
incomplete translation never crashes the app. The language is resolved once
at import time (restart to switch).
"""

from __future__ import annotations

import os
from pathlib import Path

_ZH = {
    # -- floating icon / gestures -------------------------------------------
    "icon.tooltip": "左键点击=打字 · 长按=语音 · 拖动=移动 · 右键=菜单 · 拖入文件=引用",
    # -- radial menu ---------------------------------------------------------
    "menu.projects": "项目",
    "menu.config": "配置",
    "menu.close_displays": "关闭展示",
    "menu.clear_refs": "清除引用",
    "menu.quit": "退出",
    # -- voice / ASR ---------------------------------------------------------
    "voice.none": "未捕获到语音，请输入文字",
    "voice.no_asr": "语音已录制；安装 funasr 或 faster-whisper 可自动转文字，或直接输入",
    "voice.transcribing": "转写中…（首次会下载模型，请稍候）",
    "voice.failed": "转写失败或为空，请输入文字",
    "asr.no_backend": "没有可用的 ASR 后端：pip install funasr 或 faster-whisper",
    # -- quick input bar -----------------------------------------------------
    "input.placeholder": "输入问题，回车发送（Esc 关闭）",
    "input.no_models": "尚未配置模型，请先右键 → 配置",
    "input.close_tip": "关闭（Esc）",
    "chip.reopen": "（点击重新打开预览）",
    "role.user": "你",
    "role.assistant": "助手",
    "common.error_prefix": "[错误]",
    # -- file panels ---------------------------------------------------------
    "panel.file": "文件",
    "panel.image": "图片",
    "panel.table": "表格",
    "panel.text": "代码 / 文本",
    "panel.doc": "Word 文档",
    "panel.archive": "目录 / 压缩包",
    "panel.more_items": " 等 {n} 项",
    "panel.ask_placeholder": "针对该文件提问，回车发送（Esc 关闭）",
    "panel.no_models": "尚未配置模型，请右键图标 → 配置。",
    "panel.preview_unavailable": "预览不可用：{err}",
    "panel.no_preview": "（无预览）",
    "panel.image_load_failed": "无法加载图像。",
    "panel.table_need_openpyxl": "预览 xlsx 需要 openpyxl（pip install openpyxl）。仍可直接提问。",
    "panel.table_empty": "（空表）",
    "panel.text_read_failed": "无法读取文件：{err}",
    "panel.truncated": "\n…（已截断）",
    "panel.doc_legacy": "旧版 .doc 暂不支持预览（请另存为 .docx）。仍可直接提问。",
    "panel.doc_need_pkg": "预览 Word 需要 python-docx：pip install python-docx。仍可直接提问。",
    "panel.doc_read_failed": "无法读取文档：{err}。仍可直接提问。",
    "panel.doc_empty": "<p>（空文档）</p>",
    "panel.ppt_legacy": "旧版 .ppt 暂不支持预览（请另存为 .pptx）。仍可直接提问。",
    "panel.ppt_need_pkg": "预览 PPT 需要 python-pptx：pip install python-pptx。仍可直接提问。",
    "panel.ppt_read_failed": "无法读取演示文稿：{err}。仍可直接提问。",
    "panel.ppt_empty": "（空演示文稿）",
    "panel.pdf_need_pkg": "预览 PDF 需要 PyMuPDF：pip install pymupdf。仍可直接提问。",
    "panel.pdf_open_failed": "无法打开 PDF：{err}。仍可直接提问。",
    "panel.pdf_encrypted": "该 PDF 已加密/受保护，无法预览。仍可直接提问。",
    "panel.pdf_pages": "… 共 {total} 页，仅预览前 {shown} 页",
    "panel.pdf_render_failed": "渲染失败：{err}",
    "panel.archive_read_failed": "无法读取内容：{err}",
    "panel.archive_empty": "（空 / 无法列出内容）",
    "panel.archive_first_n": "仅列出前 {n} 项",
    "panel.archive_hint": "选中文件后提问将针对所选文件（按住 Ctrl/Shift 可多选）；不选则针对整个目录。",
    "panel.archive_selected": "已选中：{name}（提问将针对此文件）",
    "panel.archive_selected_n": "已选中 {n} 个文件：{names}（提问将针对这些文件）",
    "panel.archive_whole": "未选中文件，提问针对整个目录。",
    "panel.generic_refs": "已引用 {n} 个文件：",
    "panel.generic_name": "名称：{v}",
    "panel.generic_path": "路径：{v}",
    "panel.generic_size": "大小：{v}",
    "panel.generic_type": "类型：{v}",
    "panel.generic_no_ext": "（无扩展名）",
    # -- surface / multi-window ----------------------------------------------
    "surface.title": "结果",
    "surface.empty": "（无内容）",
    "surface.artifacts": "产物：",
    "surface.open_folder": "打开产物文件夹",
    "multi.content_title": "内容",
    "multi.open_failed": "无法打开预览：{path}\n{err}",
    "multi.open_failed_title": "打开失败",
    # -- projects panel --------------------------------------------------------
    "projects.title": "项目",
    "projects.chat_entry": "💬 单次对话",
    "projects.empty": "还没有项目。提问后会自动创建。",
    "projects.project": "项目：{name}",
    "projects.dir": "目录：{path}",
    "projects.no_history": "（暂无对话历史）",
    "projects.chat_header": "单次对话（不归属任何项目的聊天）",
    "projects.no_chat": "（暂无聊天历史）",
    # -- undo ------------------------------------------------------------------
    "undo.button": "撤销",
    "undo.default_label": "可撤销",
    # -- config dialog ----------------------------------------------------------
    "config.title": "模型 API 配置",
    "config.provider": "服务提供商",
    "config.model_type": "模型类型",
    "config.display_name": "显示名称",
    "config.display_name_ph": "例如：OpenAI GPT-4o",
    "config.model_name": "模型名称",
    "config.model_name_ph": "实际模型名，例如：gpt-4o / llama3 / qwen2.5",
    "config.base_url": "接口地址",
    "config.base_url_ph": "例如：https://api.openai.com/v1",
    "config.api_key_ph": "用于鉴权的 API Key（存入系统密钥库）",
    "config.extra": "额外参数",
    "config.extra_ph": '额外参数（JSON 字符串，可选），例如 {"temperature": 0.7}',
    "config.add": "添加/更新",
    "config.remove": "删除选中",
    "config.set_default": "设为默认对话模型",
    "config.default_done": "已将「{name}」设为默认对话模型",
    "config.default_tag": "（默认）",
    "config.hint": "提示：模型类型用于区分文本模型 (llm) 与多模态模型 (vlm)；API Key 存入系统密钥库，不落数据库。",
    "config.missing_title": "缺少信息",
    "config.missing_name": "请填写显示名称。",
    "config.need_one_title": "缺少配置",
    "config.need_one": "请至少添加一个模型配置。",
    "config.custom_provider": "自定义",
    # -- core / orchestrator ----------------------------------------------------
    "core.no_model": "未找到模型配置",
    "core.project_note": "📂 项目：{name}",
    "core.project_continue": "（延续）",
    "core.project_new": "（新建）",
    "core.chat_note": "💬 对话",
    "core.max_steps": "\n[已达到最大步数，先停一下。回复「继续」可保留现场接着做]",
    "core.answer_sep": "\n\n—— 回答 ——\n",
    "core.unnamed_task": "未命名任务",
    # -- references --------------------------------------------------------------
    "refs.missing": "[引用缺失] {path}",
    "refs.dir_unreadable": "目录 {path}（无法读取：{err}）",
    "refs.dir_truncated": "\n…（共 {n} 项，已截断）",
    "refs.dir": "目录 {path}:\n",
    "refs.image": "图片文件 {path}（已作为图像输入附加）",
    "refs.file": "文件 {path}:\n",
    "refs.docx_unreadable": "文件 {path}（Word 文档，未能读取内容）",
    "refs.pptx_unreadable": "文件 {path}（PPT，未能读取内容）",
    "refs.unreadable": "文件 {path}（无法读取：{err}）",
    "refs.not_inlined": "文件 {path}（{size} 字节，未内联）",
    # -- tools: registry / shared --------------------------------------------------
    "tool.unknown": "未知工具：{name}",
    "tool.error": "工具 {name} 执行出错：{err}",
    "tool.truncated": "\n…（已截断）",
    # -- tools: python ----------------------------------------------------------------
    "tool.py.desc": "在受限沙箱中执行 Python 代码。工作目录是当前项目目录，可读写其中文件；默认禁网。"
                    "用于数据处理、用 matplotlib 画图、读写/转换文件、生成产物等。"
                    "返回 returncode 与 stdout/stderr 以及新增的产物文件名。",
    "tool.py.code": "要执行的完整、可独立运行的 Python 源代码",
    "tool.sandbox_timeout": "\n[sandbox] 执行超过 {n}s 被终止。",
    # -- tools: file -------------------------------------------------------------------
    "tool.fr.desc": "读取文本文件内容。可读：当前项目工作目录内的文件，或本次会话已引用的文件/目录内文件。",
    "tool.fr.path": "文件路径（工作目录内可用相对路径）",
    "tool.fr.denied": "不允许读取该路径：{path}",
    "tool.fr.not_found": "文件不存在：{path}",
    "tool.fr.failed": "读取失败：{err}",
    "tool.fw.desc": "把文本写入当前项目工作目录内的文件（仅限工作目录，不能写到目录之外）。",
    "tool.fw.path": "工作目录内的相对路径，如 report.md",
    "tool.fw.content": "要写入的文本内容",
    "tool.fw.outside": "只能写入当前项目工作目录内。",
    "tool.fw.failed": "写入失败：{err}",
    "tool.fw.overwrote": "已覆盖",
    "tool.fw.wrote": "已写入",
    "tool.fw.result": "{verb} {rel}（{n} 字）",
    "tool.fw.undo_overwrite": "覆盖 {rel}",
    "tool.fw.undo_create": "新建 {rel}",
    "tool.fl.desc": "列出目录内容。可列：工作目录（或其子目录），或已引用的目录。",
    "tool.fl.path": "目录路径，默认当前工作目录",
    "tool.fl.denied": "不允许列出该路径：{path}",
    "tool.fl.not_dir": "不是目录：{path}",
    "tool.fl.failed": "列目录失败：{err}",
    "tool.fl.empty": "（空目录）",
    # -- tools: http / cli / browser ------------------------------------------------------
    "tool.http.desc": "发起 HTTP(S) 请求（GET / POST）。仅允许公网地址（自动拦截内网/本机，防 SSRF）；"
                      "有超时，返回状态码与截断后的响应正文。用于查公开资料、调用公开 API。",
    "tool.http.url": "完整 URL，http 或 https",
    "tool.http.headers": "请求头（可选）",
    "tool.http.body": "POST 请求体（可选）",
    "tool.http.scheme": "仅支持 http/https。",
    "tool.http.not_allowed": "域名不在白名单：{host}",
    "tool.http.private": "拒绝访问内网/本机地址：{host}",
    "tool.http.failed": "请求失败：{err}",
    "tool.cli.desc": "在当前项目工作目录执行一条命令行（调用本机已安装的命令行工具）。"
                     "会拦截明显危险的命令；有 30s 超时。注意：不在沙箱内，仅用于安全的实用命令。",
    "tool.cli.command": "要执行的单条命令行",
    "tool.cli.empty": "命令为空。",
    "tool.cli.disabled": "命令行工具已被禁用（可在配置中开启）。",
    "tool.cli.dangerous": "已拦截可能危险的命令：{cmd}",
    "tool.cli.timeout": "执行超过 {n}s 被终止。",
    "tool.browser.desc": "用无头浏览器打开一个网页并返回其可见文本（会执行 JS，适合普通 http_request 抓不到内容的动态页面）。"
                         "仅公网地址；可选 CSS 选择器只取某区域。",
    "tool.browser.url": "完整 http/https URL",
    "tool.browser.selector": "可选 CSS 选择器，默认 body",
    "tool.browser.need_pkg": "浏览器工具需要 playwright：pip install playwright 后执行 playwright install chromium。",
    "tool.browser.failed": "打开页面失败：{err}",
    # -- tools: skills / mcp -----------------------------------------------------------------
    "tool.skill.desc": "应用一个本地技能（预置的工作流 / 操作指引）；返回该技能的详细指示，你随后据此完成任务。\n可用技能：\n{listing}",
    "tool.skill.none": "（无）",
    "tool.skill.not_found": "未找到技能：{name}",
    "tool.skill.applied": "【技能 {name}】\n{instructions}",
    "tool.mcp.desc": "MCP 工具 {tool}（来自 {server}）",
    "tool.mcp.failed": "MCP 调用失败：{err}",
    "tool.mcp.no_text": "（无文本输出）",
    # -- tools: present_windows ------------------------------------------------------------------
    "tool.pw.desc": "在屏幕上同时打开多个展示窗口（最多 4 个），按用户要求并排（row）或上下（column）排列。"
                    "当用户要求并排/分屏/多窗口对比展示多段内容或多个文件时调用。"
                    "每个窗口给 title；展示【文件】（图片/Word/PDF/表格/代码等）必须用 path 传绝对路径，"
                    "会按类型用对应查看器打开（图片看图、表格成表、Word 富文本），不要把文件内容读出来塞进 text；"
                    "文件夹直接传文件夹路径，会展示其目录树结构，不要把里面的文件逐个展开；"
                    "text 仅用于要展示的纯文字/Markdown 内容。",
    "tool.pw.items": "要展示的窗口列表（1-4 个）",
    "tool.pw.title": "窗口标题",
    "tool.pw.text": "要展示的文字内容",
    "tool.pw.path": "要展示的文件绝对路径",
    "tool.pw.layout": "排列方式：row=左右并排，column=上下排列（默认 row）",
    "tool.pw.no_items": "items 为空：需要至少 1 个要展示的窗口",
    "tool.pw.no_ui": "当前运行环境没有 UI，无法弹出展示窗口",
    "tool.pw.missing": "{path}（文件不存在）",
    "tool.pw.nothing": "没有可展示的内容：{detail}",
    "tool.pw.empty_items": "items 内容为空",
    "tool.pw.done_row": "已按左右并排打开 {n} 个展示窗口",
    "tool.pw.done_column": "已按上下排列打开 {n} 个展示窗口",
    "tool.pw.skipped": "；跳过：",
    # -- textextract -----------------------------------------------------------------------------
    "extract.truncated": "\n…（已截断）",
    "extract.slide": "--- 幻灯片 {i} ---",
    # -- tools: self-configuration (models / language) ---------------------------------------------
    "tool.lm.desc": "列出已配置的模型、当前角色分配（chat=对话主模型，vision=看图，asr=语音转写）和实际生效的 ASR 后端。",
    "tool.lm.none": "尚未配置任何模型。",
    "tool.lm.header_models": "已配置模型：",
    "tool.lm.header_roles": "角色分配：",
    "tool.lm.unset": "（未设置）",
    "tool.lm.asr_now": "当前生效 ASR 后端：{v}",
    "tool.cm.desc": "新增或更新一个模型配置（持久化；API Key 存入系统密钥库）。当用户要求接入/修改某个模型时使用。",
    "tool.cm.display_name": "显示名称（唯一标识，已存在则更新）",
    "tool.cm.provider": "服务提供商：OpenAI / Anthropic / vLLM / Ollama / 自定义",
    "tool.cm.model": "实际模型名，如 gpt-4o / qwen2.5",
    "tool.cm.base_url": "接口地址（可选），如 https://api.openai.com/v1",
    "tool.cm.api_key": "API Key（可选；不填则保留原有）",
    "tool.cm.model_type": "模型类型：llm=文本，vlm=多模态",
    "tool.cm.missing": "display_name 与 model 为必填",
    "tool.cm.no_storage": "当前环境没有配置存储，无法配置模型",
    "tool.cm.saved": "已保存模型配置 {name}（{provider} / {model}，类型 {type}）",
    "tool.ar.desc": "为用途角色指定模型并持久化：chat=日常对话/Agent 主模型，vision=涉及图片时用的多模态模型，"
                    "asr=语音转写后端（sensevoice 或 whisper，可带 whisper_size）。用户要求「用 X 模型做 Y」时使用。",
    "tool.ar.role": "角色：chat / vision / asr",
    "tool.ar.model": "chat/vision 填模型的显示名称；asr 填后端名 sensevoice 或 whisper",
    "tool.ar.size": "仅 asr+whisper 时可选：tiny/base/small/medium",
    "tool.ar.bad_role": "未知角色：{role}（可选 chat / vision / asr）",
    "tool.ar.model_not_found": "未找到模型配置：{name}（先用 configure_model 添加，或用 list_models 查看）",
    "tool.ar.bad_backend": "未知 ASR 后端：{name}（可选 sensevoice / whisper）",
    "tool.ar.saved": "已设置 {role} → {value}，后续请求自动生效",
    "tool.lang.desc": "切换应用界面与提示语言并持久化（zh=中文，en=English）。用户要求换语言时使用。",
    "tool.lang.lang": "目标语言：zh 或 en",
    "tool.lang.bad": "不支持的语言：{lang}（可选 zh / en）",
    "tool.lang.done": "已切换语言为 {lang}；动态文字立即生效，少量界面文字在重启后生效",
    "tool.steps.desc": "设置 Agent 工具循环的最大步数并持久化（1-500，当前默认 50）。"
                       "用户要求调整最大步数/觉得任务总被中断时使用；下个请求即生效。",
    "tool.steps.steps": "新的最大步数（1-500）",
    "tool.steps.bad": "无效的步数：{v}（应为 1-500 的整数）",
    "tool.steps.done": "最大步数已设为 {n}，下个请求生效",
    # -- tools: ask_user -------------------------------------------------------------------------
    "tool.ask.desc": "当你不确定、需要用户确认或补充信息时，弹出对话框向用户提问并等待回答。"
                     "可给 options 让用户从选项中选，或 allow_text 让用户自由输入；不要用它替代你能自己完成的判断。",
    "tool.ask.question": "要问用户的问题",
    "tool.ask.options": "可选项列表（让用户从中选择，可选）",
    "tool.ask.allow_text": "是否允许用户自由输入文字（无选项时默认允许）",
    "tool.ask.no_ui": "当前环境没有 UI，无法向用户提问",
    "tool.ask.empty": "问题为空",
    "tool.ask.timeout": "用户未在限定时间内回答",
    "tool.ask.cancelled": "用户取消了本次提问",
    "tool.ask.answered": "用户回答：{answer}",
    # -- tools: process_video --------------------------------------------------------------------
    "tool.vid.desc": "用 ffmpeg 处理视频，让它符合后台模型对时长/大小的要求："
                     "probe=探测时长/分辨率/大小；split=按时长切成片段（给 segment_seconds，"
                     "或给 max_mb 自动算每段时长；流复制按关键帧对齐，段长为近似值）；"
                     "compress=压缩到目标大小（max_mb 必填，可限 max_height，需精确控制大小时用这个）；"
                     "frames=均匀抽帧成图片（视觉模型只收图片时用）。产物落在项目工作目录。",
    "tool.vid.path": "视频文件路径（工作目录内或已引用的文件）",
    "tool.vid.action": "probe / split / compress / frames",
    "tool.vid.segment_seconds": "split：每段时长（秒），不填且给了 max_mb 时自动计算",
    "tool.vid.max_mb": "目标大小上限（MB）：compress 必填；split 时用于自动算每段时长",
    "tool.vid.max_height": "compress：最大输出高度（默认 720）",
    "tool.vid.frame_count": "frames：抽取帧数（默认 8）",
    "tool.vid.denied": "不允许访问该路径：{path}",
    "tool.vid.not_found": "文件不存在：{path}",
    "tool.vid.no_ffmpeg": "需要 ffmpeg：请安装系统 ffmpeg，或 pip install imageio-ffmpeg",
    "tool.vid.probe_result": "视频信息：{info}",
    "tool.vid.split_done": "已切分为 {n} 段（每段约 {seg}s）：{files}",
    "tool.vid.compress_done": "已压缩为 {name}（{mb} MB，视频码率 {kbps}kbps）",
    "tool.vid.frames_done": "已抽取 {n} 帧：{files}",
    "tool.vid.need_max_mb": "compress 需要 max_mb 参数",
    "tool.vid.no_duration": "无法读取视频时长（需要 ffprobe）",
    "tool.vid.bad_action": "未知 action：{action}",
    "tool.vid.failed": "ffmpeg 执行失败：{err}",
    "tool.vid.timeout": "处理超过 {n}s 被终止",
    # -- video panel / references ------------------------------------------------------------------
    "panel.video": "视频",
    "panel.video_need_ffmpeg": "（安装 ffmpeg 可显示缩略图与时长）",
    "panel.video_need_mm": "（未安装 QtMultimedia 多媒体组件，仅显示缩略图；pip install PySide6 完整包即可播放）",
    "panel.video_play_failed": "播放失败：{err}",
    "refs.video": "视频文件 {path}（{size_mb} MB{extra}）— 如需让模型查看，可用 process_video 探测/切分/压缩/抽帧",
    # -- ask dialog (UI) -------------------------------------------------------------------------
    "ask.title": "需要你确认",
    "ask.input_ph": "在此输入你的回答…",
    "ask.submit": "提交",
    "ask.cancel": "取消",
    "ask.other": "其他（自己输入）",
}

_EN = {
    "icon.tooltip": "Click = type · Hold = voice · Drag = move · Right-click = menu · Drop files = reference",
    "menu.projects": "Projects",
    "menu.config": "Settings",
    "menu.close_displays": "Close views",
    "menu.clear_refs": "Clear refs",
    "menu.quit": "Quit",
    "voice.none": "No speech captured — please type",
    "voice.no_asr": "Voice recorded; install funasr or faster-whisper for auto transcription, or just type",
    "voice.transcribing": "Transcribing… (first run downloads the model)",
    "voice.failed": "Transcription failed or empty — please type",
    "asr.no_backend": "No ASR backend available: pip install funasr or faster-whisper",
    "input.placeholder": "Ask anything, Enter to send (Esc to close)",
    "input.no_models": "No model configured — right-click the icon → Settings",
    "input.close_tip": "Close (Esc)",
    "chip.reopen": "(click to reopen preview)",
    "role.user": "You",
    "role.assistant": "Assistant",
    "common.error_prefix": "[error]",
    "panel.file": "File",
    "panel.image": "Image",
    "panel.table": "Table",
    "panel.text": "Code / Text",
    "panel.doc": "Word document",
    "panel.archive": "Folder / Archive",
    "panel.more_items": " and {n} more",
    "panel.ask_placeholder": "Ask about this file, Enter to send (Esc to close)",
    "panel.no_models": "No model configured — right-click the icon → Settings.",
    "panel.preview_unavailable": "Preview unavailable: {err}",
    "panel.no_preview": "(no preview)",
    "panel.image_load_failed": "Could not load the image.",
    "panel.table_need_openpyxl": "Previewing xlsx needs openpyxl (pip install openpyxl). You can still ask questions.",
    "panel.table_empty": "(empty sheet)",
    "panel.text_read_failed": "Could not read the file: {err}",
    "panel.truncated": "\n… (truncated)",
    "panel.doc_legacy": "Legacy .doc preview is not supported (save as .docx). You can still ask questions.",
    "panel.doc_need_pkg": "Previewing Word needs python-docx: pip install python-docx. You can still ask questions.",
    "panel.doc_read_failed": "Could not read the document: {err}. You can still ask questions.",
    "panel.doc_empty": "<p>(empty document)</p>",
    "panel.ppt_legacy": "Legacy .ppt preview is not supported (save as .pptx). You can still ask questions.",
    "panel.ppt_need_pkg": "Previewing PPT needs python-pptx: pip install python-pptx. You can still ask questions.",
    "panel.ppt_read_failed": "Could not read the presentation: {err}. You can still ask questions.",
    "panel.ppt_empty": "(empty presentation)",
    "panel.pdf_need_pkg": "Previewing PDF needs PyMuPDF: pip install pymupdf. You can still ask questions.",
    "panel.pdf_open_failed": "Could not open the PDF: {err}. You can still ask questions.",
    "panel.pdf_encrypted": "This PDF is encrypted/protected and cannot be previewed. You can still ask questions.",
    "panel.pdf_pages": "… {total} pages total, previewing the first {shown}",
    "panel.pdf_render_failed": "Rendering failed: {err}",
    "panel.archive_read_failed": "Could not read contents: {err}",
    "panel.archive_empty": "(empty / cannot list contents)",
    "panel.archive_first_n": "Showing the first {n} entries only",
    "panel.archive_hint": "Select files to ask about them (hold Ctrl/Shift to multi-select); with none selected, questions target the whole folder.",
    "panel.archive_selected": "Selected: {name} (questions will target this file)",
    "panel.archive_selected_n": "Selected {n} files: {names} (questions will target them)",
    "panel.archive_whole": "No file selected; questions target the whole folder.",
    "panel.generic_refs": "{n} files referenced:",
    "panel.generic_name": "Name: {v}",
    "panel.generic_path": "Path: {v}",
    "panel.generic_size": "Size: {v}",
    "panel.generic_type": "Type: {v}",
    "panel.generic_no_ext": "(no extension)",
    "surface.title": "Result",
    "surface.empty": "(no content)",
    "surface.artifacts": "Artifacts:",
    "surface.open_folder": "Open artifacts folder",
    "multi.content_title": "Content",
    "multi.open_failed": "Could not open preview: {path}\n{err}",
    "multi.open_failed_title": "Open failed",
    "projects.title": "Projects",
    "projects.chat_entry": "💬 One-off chat",
    "projects.empty": "No projects yet. One is created automatically when you ask.",
    "projects.project": "Project: {name}",
    "projects.dir": "Folder: {path}",
    "projects.no_history": "(no conversation history)",
    "projects.chat_header": "One-off chat (not bound to any project)",
    "projects.no_chat": "(no chat history)",
    "undo.button": "Undo",
    "undo.default_label": "Undoable",
    "config.title": "Model API Settings",
    "config.provider": "Provider",
    "config.model_type": "Model type",
    "config.display_name": "Display name",
    "config.display_name_ph": "e.g. OpenAI GPT-4o",
    "config.model_name": "Model name",
    "config.model_name_ph": "actual model id, e.g. gpt-4o / llama3 / qwen2.5",
    "config.base_url": "Base URL",
    "config.base_url_ph": "e.g. https://api.openai.com/v1",
    "config.api_key_ph": "API key for auth (stored in the OS keyring)",
    "config.extra": "Extra params",
    "config.extra_ph": 'extra params (JSON string, optional), e.g. {"temperature": 0.7}',
    "config.add": "Add / Update",
    "config.remove": "Remove selected",
    "config.set_default": "Set as default chat model",
    "config.default_done": "\"{name}\" is now the default chat model",
    "config.default_tag": " (default)",
    "config.hint": "Tip: model type distinguishes text (llm) from multimodal (vlm); API keys go to the OS keyring, never the database.",
    "config.missing_title": "Missing info",
    "config.missing_name": "Please fill in the display name.",
    "config.need_one_title": "Missing config",
    "config.need_one": "Please add at least one model config.",
    "config.custom_provider": "Custom",
    "core.no_model": "No model config found",
    "core.project_note": "📂 Project: {name}",
    "core.project_continue": " (continued)",
    "core.project_new": " (new)",
    "core.chat_note": "💬 Chat",
    "core.max_steps": "\n[max steps reached, pausing — reply \"continue\" to resume with full context]",
    "core.answer_sep": "\n\n—— Answer ——\n",
    "core.unnamed_task": "Untitled task",
    "refs.missing": "[missing reference] {path}",
    "refs.dir_unreadable": "Folder {path} (unreadable: {err})",
    "refs.dir_truncated": "\n… ({n} entries total, truncated)",
    "refs.dir": "Folder {path}:\n",
    "refs.image": "Image file {path} (attached as visual input)",
    "refs.file": "File {path}:\n",
    "refs.docx_unreadable": "File {path} (Word document, could not read)",
    "refs.pptx_unreadable": "File {path} (PPT, could not read)",
    "refs.unreadable": "File {path} (unreadable: {err})",
    "refs.not_inlined": "File {path} ({size} bytes, not inlined)",
    "tool.unknown": "Unknown tool: {name}",
    "tool.error": "Tool {name} failed: {err}",
    "tool.truncated": "\n… (truncated)",
    "tool.py.desc": "Run Python code in a restricted sandbox. The working directory is the current "
                    "project folder (read/write allowed there); network is blocked by default. "
                    "Use it for data processing, matplotlib charts, file conversion, generating artifacts. "
                    "Returns returncode, stdout/stderr and any new artifact filenames.",
    "tool.py.code": "complete, self-contained Python source to run",
    "tool.sandbox_timeout": "\n[sandbox] killed after exceeding {n}s.",
    "tool.fr.desc": "Read a text file: files inside the project workspace, or files under this session's references.",
    "tool.fr.path": "file path (relative allowed inside the workspace)",
    "tool.fr.denied": "Not allowed to read this path: {path}",
    "tool.fr.not_found": "File not found: {path}",
    "tool.fr.failed": "Read failed: {err}",
    "tool.fw.desc": "Write text to a file inside the current project workspace (workspace only, nothing outside).",
    "tool.fw.path": "relative path inside the workspace, e.g. report.md",
    "tool.fw.content": "text content to write",
    "tool.fw.outside": "Can only write inside the current project workspace.",
    "tool.fw.failed": "Write failed: {err}",
    "tool.fw.overwrote": "Overwrote",
    "tool.fw.wrote": "Wrote",
    "tool.fw.result": "{verb} {rel} ({n} chars)",
    "tool.fw.undo_overwrite": "Overwrite {rel}",
    "tool.fw.undo_create": "Create {rel}",
    "tool.fl.desc": "List a directory: the workspace (or a subfolder), or a referenced folder.",
    "tool.fl.path": "directory path, defaults to the workspace",
    "tool.fl.denied": "Not allowed to list this path: {path}",
    "tool.fl.not_dir": "Not a directory: {path}",
    "tool.fl.failed": "Listing failed: {err}",
    "tool.fl.empty": "(empty directory)",
    "tool.http.desc": "Make an HTTP(S) request (GET / POST). Public addresses only (private/loopback "
                      "blocked against SSRF); has a timeout, returns status code and truncated body. "
                      "For public data and public APIs.",
    "tool.http.url": "full URL, http or https",
    "tool.http.headers": "request headers (optional)",
    "tool.http.body": "POST body (optional)",
    "tool.http.scheme": "Only http/https are supported.",
    "tool.http.not_allowed": "Domain not in allowlist: {host}",
    "tool.http.private": "Refusing private/loopback address: {host}",
    "tool.http.failed": "Request failed: {err}",
    "tool.cli.desc": "Run one command line in the current project workspace (uses locally installed CLI tools). "
                     "Obviously dangerous commands are blocked; 30s timeout. Note: NOT sandboxed — "
                     "safe utility commands only.",
    "tool.cli.command": "the single command line to run",
    "tool.cli.empty": "Empty command.",
    "tool.cli.disabled": "The CLI tool is disabled (enable it in settings).",
    "tool.cli.dangerous": "Blocked a potentially dangerous command: {cmd}",
    "tool.cli.timeout": "Killed after exceeding {n}s.",
    "tool.browser.desc": "Open a web page in a headless browser and return its visible text (executes JS — "
                         "for dynamic pages plain http_request can't read). Public addresses only; "
                         "optional CSS selector to scope the extraction.",
    "tool.browser.url": "full http/https URL",
    "tool.browser.selector": "optional CSS selector, defaults to body",
    "tool.browser.need_pkg": "The browser tool needs playwright: pip install playwright, then playwright install chromium.",
    "tool.browser.failed": "Failed to open the page: {err}",
    "tool.skill.desc": "Apply a local skill (a predefined workflow / playbook); returns its detailed "
                       "instructions for you to follow.\nAvailable skills:\n{listing}",
    "tool.skill.none": "(none)",
    "tool.skill.not_found": "Skill not found: {name}",
    "tool.skill.applied": "[Skill {name}]\n{instructions}",
    "tool.mcp.desc": "MCP tool {tool} (from {server})",
    "tool.mcp.failed": "MCP call failed: {err}",
    "tool.mcp.no_text": "(no text output)",
    "tool.pw.desc": "Open several display windows at once (max 4), tiled side-by-side (row) or stacked "
                    "(column) as the user asks. Call when the user wants to compare or view multiple "
                    "pieces of content / files in separate windows. Give each window a title; for FILES "
                    "(image/Word/PDF/table/code) you MUST pass the absolute path in `path` — they open in "
                    "type-appropriate viewers — do not paste file contents into `text`; for a folder pass "
                    "its path to show the directory tree, do not expand its files; `text` is only for "
                    "plain text / Markdown content.",
    "tool.pw.items": "windows to show (1-4)",
    "tool.pw.title": "window title",
    "tool.pw.text": "text content to show",
    "tool.pw.path": "absolute path of the file to show",
    "tool.pw.layout": "arrangement: row = side by side, column = stacked (default row)",
    "tool.pw.no_items": "items is empty: at least 1 window is required",
    "tool.pw.no_ui": "No UI in this environment, cannot open display windows",
    "tool.pw.missing": "{path} (file not found)",
    "tool.pw.nothing": "Nothing to display: {detail}",
    "tool.pw.empty_items": "items are empty",
    "tool.pw.done_row": "Opened {n} display windows side by side",
    "tool.pw.done_column": "Opened {n} display windows stacked",
    "tool.pw.skipped": "; skipped: ",
    "extract.truncated": "\n… (truncated)",
    "extract.slide": "--- Slide {i} ---",
    "tool.lm.desc": "List configured models, current role assignments (chat = main conversation model, "
                    "vision = image model, asr = speech-to-text) and the effective ASR backend.",
    "tool.lm.none": "No models configured yet.",
    "tool.lm.header_models": "Configured models:",
    "tool.lm.header_roles": "Role assignments:",
    "tool.lm.unset": "(unset)",
    "tool.lm.asr_now": "Effective ASR backend: {v}",
    "tool.cm.desc": "Add or update a model config (persisted; the API key goes to the OS keyring). "
                    "Use when the user asks to hook up or modify a model.",
    "tool.cm.display_name": "display name (unique id; updates if it exists)",
    "tool.cm.provider": "provider: OpenAI / Anthropic / vLLM / Ollama / Custom",
    "tool.cm.model": "actual model id, e.g. gpt-4o / qwen2.5",
    "tool.cm.base_url": "base URL (optional), e.g. https://api.openai.com/v1",
    "tool.cm.api_key": "API key (optional; keeps the existing one when omitted)",
    "tool.cm.model_type": "model type: llm = text, vlm = multimodal",
    "tool.cm.missing": "display_name and model are required",
    "tool.cm.no_storage": "No config storage in this environment, cannot configure models",
    "tool.cm.saved": "Saved model config {name} ({provider} / {model}, type {type})",
    "tool.ar.desc": "Assign a model to a purpose role (persisted): chat = main conversation/agent model, "
                    "vision = multimodal model for images, asr = speech backend (sensevoice or whisper, "
                    "optional whisper_size). Use when the user says \"use model X for Y\".",
    "tool.ar.role": "role: chat / vision / asr",
    "tool.ar.model": "for chat/vision: the model's display name; for asr: backend name sensevoice or whisper",
    "tool.ar.size": "asr+whisper only, optional: tiny/base/small/medium",
    "tool.ar.bad_role": "Unknown role: {role} (chat / vision / asr)",
    "tool.ar.model_not_found": "Model config not found: {name} (add it with configure_model, or check list_models)",
    "tool.ar.bad_backend": "Unknown ASR backend: {name} (sensevoice / whisper)",
    "tool.ar.saved": "Set {role} → {value}; takes effect on subsequent requests",
    "tool.lang.desc": "Switch the app's UI/prompt language and persist it (zh = Chinese, en = English). "
                      "Use when the user asks to change language.",
    "tool.lang.lang": "target language: zh or en",
    "tool.lang.bad": "Unsupported language: {lang} (zh / en)",
    "tool.lang.done": "Language switched to {lang}; dynamic texts apply immediately, a few UI labels after restart",
    "tool.steps.desc": "Set and persist the Agent's tool-loop step budget (1-500, default 50). "
                       "Use when the user asks to change the max steps or tasks keep getting cut off; "
                       "applies from the next request.",
    "tool.steps.steps": "new max steps (1-500)",
    "tool.steps.bad": "Invalid steps: {v} (integer 1-500)",
    "tool.steps.done": "Max steps set to {n}; applies from the next request",
    "tool.ask.desc": "When you're unsure or need the user to confirm or supply information, pop up a dialog "
                     "to ask and wait for the answer. Provide options for a multiple-choice pick, or "
                     "allow_text for free input; don't use it to offload judgements you can make yourself.",
    "tool.ask.question": "the question to ask the user",
    "tool.ask.options": "list of choices for the user to pick from (optional)",
    "tool.ask.allow_text": "whether to allow free-text input (default on when there are no options)",
    "tool.ask.no_ui": "No UI in this environment, cannot ask the user",
    "tool.ask.empty": "The question is empty",
    "tool.ask.timeout": "The user did not answer in time",
    "tool.ask.cancelled": "The user cancelled the question",
    "tool.ask.answered": "User answered: {answer}",
    "tool.vid.desc": "Process a video with ffmpeg so it satisfies the backend model's duration/size "
                     "limits: probe = duration/resolution/size; split = cut into time segments "
                     "(give segment_seconds, or max_mb to auto-derive; stream-copy aligns to keyframes "
                     "so lengths are approximate); compress = shrink to a target size (max_mb required, "
                     "optional max_height — use this when the size must be exact); frames = extract evenly spaced "
                     "frames as images (for vision models that only take images). Outputs land in "
                     "the project workspace.",
    "tool.vid.path": "video file path (inside the workspace or a referenced file)",
    "tool.vid.action": "probe / split / compress / frames",
    "tool.vid.segment_seconds": "split: seconds per segment; auto-derived when omitted and max_mb is given",
    "tool.vid.max_mb": "target size cap in MB: required for compress; for split it derives the segment length",
    "tool.vid.max_height": "compress: max output height (default 720)",
    "tool.vid.frame_count": "frames: number of frames to extract (default 8)",
    "tool.vid.denied": "Not allowed to access this path: {path}",
    "tool.vid.not_found": "File not found: {path}",
    "tool.vid.no_ffmpeg": "ffmpeg required: install system ffmpeg, or pip install imageio-ffmpeg",
    "tool.vid.probe_result": "Video info: {info}",
    "tool.vid.split_done": "Split into {n} segments (~{seg}s each): {files}",
    "tool.vid.compress_done": "Compressed to {name} ({mb} MB, video bitrate {kbps}kbps)",
    "tool.vid.frames_done": "Extracted {n} frames: {files}",
    "tool.vid.need_max_mb": "compress requires max_mb",
    "tool.vid.no_duration": "Could not read the video duration (ffprobe needed)",
    "tool.vid.bad_action": "Unknown action: {action}",
    "tool.vid.failed": "ffmpeg failed: {err}",
    "tool.vid.timeout": "Processing killed after {n}s",
    "panel.video": "Video",
    "panel.video_need_ffmpeg": "(install ffmpeg to show a thumbnail and duration)",
    "panel.video_need_mm": "(QtMultimedia not installed — thumbnail only; install the full PySide6 package for playback)",
    "panel.video_play_failed": "Playback failed: {err}",
    "refs.video": "Video file {path} ({size_mb} MB{extra}) — use process_video to probe/split/compress/extract frames for the model",
    "ask.title": "Need your input",
    "ask.input_ph": "Type your answer here…",
    "ask.submit": "Submit",
    "ask.cancel": "Cancel",
    "ask.other": "Other (type your own)",
}

_PACKS = {"zh": _ZH, "en": _EN}


def _detect() -> str:
    lang = os.environ.get("PILLOW_LANG", "").strip().lower()
    if lang in _PACKS:
        return lang
    try:
        cfg = Path.home() / ".pillow" / "lang"
        if cfg.exists():
            v = cfg.read_text("utf-8").strip().lower()
            if v in _PACKS:
                return v
    except OSError:
        pass
    loc = ""
    try:
        import locale
        loc = (locale.getdefaultlocale()[0] or "")
    except Exception:
        pass
    if not loc:
        loc = os.environ.get("LANG", "")
    return "zh" if loc.lower().startswith("zh") else "en"


LANG = _detect()


def set_language(lang: str) -> bool:
    """Hot-switch the active language and persist it to ~/.pillow/lang.

    Texts produced through t() after this call use the new language; strings
    captured at import time (class attributes like panel titles and tool
    descriptions) refresh on next start.
    """
    global LANG
    lang = (lang or "").strip().lower()
    if lang not in _PACKS:
        return False
    LANG = lang
    try:
        cfg = Path.home() / ".pillow" / "lang"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(lang, "utf-8")
    except OSError:
        pass
    return True


def t(key: str, **fmt) -> str:
    """Translate a key in the active language; zh fallback, then the key."""
    s = _PACKS.get(LANG, {}).get(key)
    if s is None:
        s = _ZH.get(key, key)
    if fmt:
        try:
            return s.format(**fmt)
        except (KeyError, IndexError):
            return s
    return s
