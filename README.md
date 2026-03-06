# Pillow Assistant

Pillow Assistant 是一个基于 PySide6 的桌面浮动工具，提供以下能力：

- 屏幕上常驻半透明“枕头”按钮，鼠标悬停可展开菜单；
- 支持拖拽图片到枕头按钮，弹出图像预览并提供基于图像的提问输入框；
- 麦克风按钮可录制语音输入并保存为 wav；
- 键盘按钮弹出文本输入对话框，用于快速向大模型发送提示词（示例中保留对接接口入口）；
- 使用 SQLite 保存常见大语言模型与多模态模型的 API 配置，第 1 次运行会自动弹出配置窗口。

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

- `pillow_assistant/app.py`：应用入口与初始化逻辑；
- `pillow_assistant/ui/`：界面组件（浮动按钮、配置窗口、语音/文本输入等）；
- `storage/db.py`：SQLite 读写封装；
- `data/assistant.db`：默认的配置数据库路径（首次运行自动创建）。

目前对接大模型的网络请求部分留有接口入口，您可以在 `SearchDialog` 与 `ImagePreviewDialog` 中接入实际的 API 调用逻辑，实现文本及多模态的推理流程。

