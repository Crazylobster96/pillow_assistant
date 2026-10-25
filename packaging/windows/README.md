# Windows 离线发布包

一个解压即用的 Windows x64 完整包：内置 Python 运行时和全部依赖，无需安装 Python、pip 或联网；联网时会自动按 GitHub 上的最新代码升级。

## 包里有什么

```
PillowAssistant/
  PillowAssistant.exe          双击启动（无控制台窗口）
  PillowAssistant-console.bat  带控制台启动，排查问题用
  launcher.pyw                 启动器 + 自动更新
  update.json                  自动更新设置
  python/                      独立 CPython 3.12 + 全部依赖（PySide6、litellm、PyTorch CPU、
                               funasr、faster-whisper、pandas、matplotlib、PyMuPDF、Playwright、
                               imageio-ffmpeg、mcp ……）
  app/                         程序源码 + VERSION.json（当前版本的 commit）
  ms-playwright/               浏览器工具用的 Chromium
  node-runtime/                便携版 Node.js，供 Anatomy 等轴测讲解图使用（没有时首次使用自动下载，走国内镜像并校验哈希）
  cad-runtime/                 text-to-cad 的 cadgen 运行时（uv 缓存 + Python 3.13），可离线建模；
                               上游升级后新版本在首次使用时自动下载到这里（build.ps1 -SkipCad 可不预装）
  models/                      （可选）离线语音模型，来自 PillowAssistant-models.zip
```

## 使用

1. 从 Releases 下载 `PillowAssistant-windows-x64-*.zip`，解压到一个**可写**目录（如 `D:\PillowAssistant`，不要放在 `C:\Program Files`，否则无法自动升级）。
2. 想让语音转文字完全离线：再下载 `PillowAssistant-models.zip`，解压到同一位置（会合并出 `PillowAssistant\models\`）。不装模型也能用，首次语音识别时会联网下载。
3. 双击 `PillowAssistant.exe`。首次启动会自动在桌面和开始菜单创建「Pillow Assistant」快捷方式，之后从快捷方式启动即可。删掉的快捷方式不会被再次创建；把文件夹挪到别处后再启动一次，快捷方式会自动指向新位置；也可以随时用 `PillowAssistant-console.bat --create-shortcuts` 重新生成。
4. 数据和配置仍保存在 `%USERPROFILE%\.pillow\`，升级或重新解压都不会丢失。

## 自动升级

- 启动约 20 秒后在后台检查 GitHub，此后运行期间每 6 小时检查一次；离线时静默跳过，不影响使用。
- 发现新版本就下载源码并暂存到 `updates\staged`，**下次启动时**自动替换 `app\`。如果新代码的 `requirements.txt` 有变化，会先用内置 pip 安装新依赖，安装失败则继续使用旧版本，下次再试。
- **出问题可回滚**：旧版本保留在 `updates\previous`。
  - 自动回滚：新版本处于「试用期」，直到正常运行满 60 秒或正常退出。期间如果启动报错，会立刻恢复上一版并重新打开；如果连续两次启动都没撑过试用期（比如闪退），第三次启动时自动恢复上一版。
  - 手动回滚：`PillowAssistant-console.bat --rollback`。
  - 回滚过的版本会被记住（`updates\state.json` 的 `skipped_shas`），自动升级不会再把它装回来；等仓库里出现更新的提交（修复版）才继续升级。想重新允许安装它：`--clear-skipped`。
  - text-to-cad 的技能和 cadgen 版本锁定都在 `app\` 里，所以回滚 app 也会一起退回旧的 text-to-cad，旧版 cadgen 仍在 `cad-runtime\` 缓存中，离线也能用。
- 立即检查并升级：`PillowAssistant-console.bat --update-now`；查看当前版本：`--version`；本次不升级：`--no-update`。
- 日志：`%USERPROFILE%\.pillow\logs\launcher.log`（启动器/升级）、`app-output.log`（程序输出）。

`update.json` 设置：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 关闭自动升级设为 `false`（或设置环境变量 `PILLOW_NO_UPDATE=1`） |
| `channel` | `"branch"` | `branch` 跟随分支最新提交；`release` 只跟随最新 GitHub Release |
| `branch` | `"main"` | `channel=branch` 时跟随的分支 |
| `check_interval_hours` | `6` | 检查间隔 |
| `desktop_shortcut` | `true` | 首次启动时创建桌面和开始菜单快捷方式 |
| `repo` | `Crazylobster96/pillow_assistant` | 源仓库；私有仓库可设置环境变量 `PILLOW_UPDATE_TOKEN` |

## 构建

GitHub Actions（`.github/workflows/windows-release.yml`）在 `windows-latest` 上构建：

- 推送 `v*` 标签：构建主包 + 模型包并发布 Release；
- 手动运行（Actions → Windows offline release → Run workflow）：可选是否发布、是否附带模型包；
- 修改打包相关文件的 PR：只构建并自检，产物作为 Actions artifact 保留 7 天。

本地构建（Windows，需要 git 和 7-Zip）：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
# 附带离线语音模型：
pip install modelscope huggingface_hub
python packaging\windows\prefetch_models.py C:\pillow-models
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1 -ModelsDir C:\pillow-models
```

构建会运行 `launcher.pyw --self-test`：逐个导入全部依赖、启动一次 Chromium，任何一项失败都会让构建失败。
