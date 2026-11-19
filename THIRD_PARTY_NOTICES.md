# CueScope 第三方来源与许可说明

本项目代码及自绘品牌图标采用根目录 `LICENSE` 中的 MIT License。品牌 SVG/PNG/ICO 由同一自绘矢量源生成，不依赖图标字体或外部商标。以下模型和依赖的许可证分别适用。

## RapidOCR 3.10.0

- 项目：https://github.com/RapidAI/RapidOCR/tree/v3.10.0
- 代码许可：Apache License 2.0。
- 本项目通过 `params` 显式指定 ONNX Runtime CPU、检测模型、识别模型和字典，不依赖默认模型静默下载。

## PP-OCRv6 small 模型与字典

模型权重版权归 Baidu 和/或相应 PaddleOCR 权利持有人。RapidOCR 转换及再分发工件采用 Apache License 2.0。准确来源 URL 和 SHA-256 见 `models/manifest.json`，加载时由程序再次校验。

- PP-OCRv6 small detection：`PP-OCRv6_det_small.onnx`
- PP-OCRv6 small recognition：`PP-OCRv6_rec_small.onnx`
- 字典：`ppocrv6_dict.txt`
- 上游：https://github.com/PaddlePaddle/PaddleOCR
- 模型许可说明：https://rapidai.github.io/RapidOCRDocs/main/en/model_licenses/
- 许可证全文：https://www.apache.org/licenses/LICENSE-2.0

RapidOCR 的格式转换属于模型表示的机械转换，不转移上游权重所有权。本项目的打包仅收集官方工件，不修改模型内容；模型文件名与哈希保持清单记录。Apache-2.0 不授予相关商标权，也不表示上游对本工具提供背书。


## 其他主要依赖

- PySide6：LGPL-3.0-or-later / GPL-3.0-or-later / 商业许可，由 Qt for Python 项目提供。
- MSS：仅保留旧版桌面兼容代码，0.2.0 自动监控和预览使用独立 WGC 采集组件。
- ONNX Runtime：MIT License。
- NumPy：BSD-3-Clause。
- OpenCV（opencv-python-headless）：Apache-2.0 及 wheel 所附第三方说明。
- Pillow：HPND 及 wheel 所附第三方说明。

各依赖的精确版本见 `requirements.lock`；PyInstaller 便携目录同时保留依赖随 wheel 分发的许可文件。发布前如升级任何依赖，必须重新审查对应许可和模型清单，不得仅依据本说明继续分发。

`tools/finalize_package.py` 从已安装的运行依赖及其传递依赖收集许可证全文，随便携包放入 `licenses/`，清单为 `licenses/inventory.json`。运行时 OCR 固定使用 CPU：默认算子内四线程、跨算子一线程，不修改任何第三方包源码。

FlatBuffers 的已安装 wheel 元数据声明 Apache-2.0，但未包含许可证文件；全文补充自官方项目 https://github.com/google/flatbuffers/blob/master/LICENSE ，保存在 `packaging/licenses/flatbuffers-LICENSE.txt`，打包时复制至对应依赖目录。

## 独立采集组件

冻结的 Windows 采集组件依赖单独锁定在 `capture-requirements.lock`（源码位于 `packaging/`）：

- windows-capture 2.0.1：MIT；许可证取自隔离 wheel 的 `licenses/LICENCE`。
- opencv-python 5.0.0.93：wheel 附带的 `cv2/LICENSE.txt` 与 `cv2/LICENSE-3RD-PARTY.txt` 一并交付；包含绑定代码许可、OpenCV 及所含第三方组件说明。
- NumPy 2.2.6：BSD-3-Clause 及 wheel 附带许可；仅从构建解释器收集，排除 `.capture-runtime` 中的 NumPy，避免混版。

主程序 OCR 环境使用 `opencv-python-headless`，采集组件使用隔离环境的 `opencv-python`，两者不混用。采集许可证复制到 `licenses/capture-runtime/`；`licenses/inventory.json` 区分主程序与采集组件，并记录实际 wheel 元数据版本、来源环境和许可证 SHA-256。依赖版本不符、许可证缺失或为空时，整理交付包会失败。
