# 参与 CueScope

欢迎贡献可以复现的问题、应用兼容性结果和有限范围的改进。一次反馈聚焦一个现象；例如可以说明“两个相同标题的窗口无法区分”，再给出使用合成文字的复现步骤。[选择反馈类型](https://github.com/LP-XXX/CueScope/issues/new/choose)

## 准备源码环境

已验证的开发环境为 **Windows x64 和 Python 3.11**。在仓库根目录运行以下 PowerShell 命令。第一次初始化会联网安装依赖并获取固定模型，请预留本地空间。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe tools/bootstrap.py
.\.venv\Scripts\python.exe tools/bootstrap.py --check
.\.venv\Scripts\python.exe -m deal_watch
```

`requirements.lock` 对应主程序和 OCR 环境，`packaging/capture-requirements.lock` 对应隔离采集组件。模型清单记录来源和哈希，模型权重由下载器获取。更新时同时核对适用许可证。[依赖和模型说明](THIRD_PARTY_NOTICES.md)

## 调试与测试

- **窗口与选区**从 `src/deal_watch/window_capture.py`、`src/deal_watch/capture_pool.py` 开始看。公开测试使用模拟窗口或合成帧。
- **文字到提醒**从 `content.py` 的内容单位，经 `tracking.py` 与 `rules.py`，到 `storage.py` 的事件和出站队列。对应文件位于 `src/deal_watch/`。改规则时也要检查首次基线、静止文字、滚动、识别抖动和去重。
- **通知和界面**分别在 `src/deal_watch/notify.py` 与 `src/deal_watch/ui/`。用模拟客户端检查业务拒绝、限流和超时，在途分片也要遵守关闭外发的选择。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tracking_rules.py -q
.\.venv\Scripts\python.exe tools/verify.py
```

第一条是针对规则的短测试，可替换成与改动有关的模块。第二条运行项目的离线回归和 Ruff，输出位于 `build/validation/`。要试用源码里的合成文字窗口，可单独运行 `.\.venv\Scripts\python.exe tools/demo_window.py`。现场短测要取得目标窗口使用者的授权。长跑和真实群通知均需单独安排。

## 提交改动

1. 从公开仓库当前目标分支新建功能分支，写明要解决的问题和具体使用场景。
2. 复现行为，完成相关短测试并查看差异。提交与修复有关的源码、测试和说明。新版本支持范围应在验证后更新。
3. 在 PR 中说明复现方法、执行了哪些测试，以及哪些行为仍需使用者验证。截图仅使用合成数据，保留明确的来源说明。

公开仓库和 Issue 中请勿放入真实聊天、Windows 用户路径、窗口标题、数据库、运行日志原件、Webhook、推送令牌或签名材料。涉及误投、凭据或隐私的安全问题请使用[私密报告](SECURITY.md)。维护发行包见[发布流程](docs/RELEASING.md)。
