# 维护者发布流程

项目同时存在维护者日常开发工作区和从 [LP-XXX/CueScope](https://github.com/LP-XXX/CueScope) 单独克隆的公开仓库工作区。贡献者使用公开仓库即可；已发布的标签与附件在 [Releases](https://github.com/LP-XXX/CueScope/releases) 中查询。

## 用公开工作区准备版本

1. 从公开仓库当前目标分支创建发行分支。按文件清单从开发工作区带入选定改动，逐项审查差异，保留公开仓库已有变更。开发用 `build/`、私有现场材料、用户数据、内部协作记录和旧便携包留在原处。
2. 在公开工作区检查源码锁、模型清单、许可证、版本、用户文档、PR 流程与测试是否完整。确认首页的下载链接指向已经存在的标签，图片是合成素材。按明确的路径暂存和提交文件，检查差异和行尾，向公开仓库提交 PR。
3. 通过 Windows 离线检查并合并后，在要发布的已审查提交上打标签。以 0.3.1 为例，候选标签形如 `v0.3.1-rc.1`，稳定标签形如 `v0.3.1`。保留既有标签与已发布附件，后续修订使用新的候选号或版本号。

## 在干净 checkout 验证交付

使用 Windows x64、Python 3.11 和新的虚拟环境。在仓库根目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe tools/bootstrap.py
.\.venv\Scripts\python.exe tools/build_release.py --dist-root build/release-0.3.1
.\.venv\Scripts\python.exe tools/check_release.py --archive build/release-0.3.1/CueScope-0.3.1-win-x64.zip
```

发布脚本负责离线回归、冻结主程序和采集组件、自检并整理发行包。校验时核对实际 ZIP、旁挂 SHA-256、包内文件清单以及记录的源码提交。人工检查解压目录内的说明、相对链接、许可证、合成 OCR 及首次启动；PR 或发布流水线只使用模拟通知。

## 发布草稿与验收范围

从经过验证的同一公开提交生成发布草稿。上传便携 ZIP 和 SHA-256。检查草稿中的版本号、目标架构、已知限制、数据兼容与回退说明；候选版勾选 **Pre-release**。正常的候选下载链接指向具体标签页，避免依赖“最新稳定版”的筛选。

确认发布包和校验值相符后发布草稿，再检查公开首页到下载、解压到首次打开的实际路径。需要现场证据的项目按软件版本、后端、布局和桌面状态记录。群内可见、平台受理和手机送达分别说明。[当前验证摘要](../VALIDATION.md) · [历史记录](history/validation-through-0.3.0.md)
