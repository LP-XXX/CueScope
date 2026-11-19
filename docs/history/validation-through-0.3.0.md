# 验证记录

更新日期：2026-11-17

当前版本：**CueScope 0.3.0 候选版**

验证环境：Windows x64，项目内隔离 Python 3.11.9（`.runtime/python311`），ONNX Runtime CPU，RapidOCR 3.10.0。离线、真实微信、界面及通知证据按下文分别列出；真实飞书和手机送达没有验证。

> 本文件只记录**已实际执行并留下产物**的验证。标记「待用户操作」的项目不得当作已通过。

## 0.3.0 候选版验证（2026-11-17）

- 覆盖通用逐行/显式文本块/微信预设、基础多规则、作用域去重、严格文本跟踪、任务间隔、窗口身份/标题/DPI、v4 迁移备份、可选飞书与本地记录、健康状态隔离及 CueScope 前端。
- **1022 个唯一用例最终通过，零失败/错误/跳过**：全量首次 1017 通过、3 失败，均为同一历史预览测试替身未增加设置保存状态字段；补齐替身后该模块 5 项通过，该次仅补测试替身。随后修复停止/停用后新故障复用旧事件编号的问题，62 项相关回归通过（含 2 个新增用例）。原始全量、定向补测及有效结果 XML 分别保留；这是全量加定向补测，不是一次全量零失败。最终汇总为 `build/validation/automated-tests.json`，有效 XML 为 `pytest-effective-0.3.0.xml`。已显式启用微基准，并使用 Git 自带 GNU sha256sum 验证产物清单，未跳过对应项目。Ruff 通过。
- 新健康逻辑在纯内存、模拟客户端中阻止故障/恢复自反馈；同一原模拟条件下只产生一次故障通知。首次请求和各分片前的外发开关、暂停后 TTL、持久事件及幂等写入均有回归；不使用真实飞书。
- 采集池启动预算边界修前 5 项失败，修后新增 7 项恢复回归通过；子进程协议测试只传输合成 PNG，覆盖初始化迟到、启动卡住、传输迟到与备用恢复。没有实际启动 WGC 采集会话，不能据此认定历史 Windows/驱动根因已解决。
- 20 张主页面、12 张向导合成截图覆盖浅深主题、1000×720/760×520 及向导尺寸；最小尺寸未被撑大。原生短检查约 1.6 秒，严格核对 DISPLAY1/MNG007DA5-1、完整窗口边界、无前台激活、浅深 DWM 标题栏与正常退出。没有更改系统显示设置。
- 源码隔离自检和冻结主程序自检均通过：3 个模型哈希、实际 OCR 的合成文字输入、4 个主页面、2 个记录视图及干净退出。冻结采集组件依赖自检、双组件预热/关闭自检通过，零真实帧、退出码均 0、残留进程 0。42 项冻结代码（含入口与采集组件）与当前源码一致。
- 干净独立 Python 3.11.9 环境成功安装主锁与采集锁，原生模块导入、pip check、真实 wheel 构建与安装、品牌资源和模型清单加载均通过。该 wheel 是中间安装/资源验证件，未重新下载模型或执行真实窗口/通知验收，详见 `clean-environment-check-0.3.0.json`。
- 打包采用独立 CueScope 目录，包含许可证、依赖锁、模型来源/哈希、用户说明和脱敏验证摘要；排除个人配置、数据库、原始日志、聊天帧及协作计划。旧 DealWatch 包不覆盖，内部数据目录/单实例标识兼容保留。

| 场景 | 0.3.0 证据与边界 |
|---|---|
| 微信聊天 | 既有规则/气泡/跟踪离线回归通过；本轮未做真实窗口验收 |
| 通用日志、文字列表、显式文本块 | 合成图、OCR 路由、单位边界和规则/跟踪验证通过；具体真实软件仍待逐个短测 |
| 窗口标题变化、进程重启、路径失败、DPI/尺寸变化 | 模拟身份与 UI 代数回归通过；真实跨软件变换未完成 |
| 飞书异常/恢复、超时、开关和分片 | 模拟通知回归通过；真实平台与手机送达未验证 |
| 长时间连续运行 | 未启动长跑，用户此前停止长跑的决定保持有效 |

0.2.5 的真实五窗短测与遮挡证据保留在历史章节，不作为 0.3.0 的现场通过证明；不承诺零漏报、任意软件兼容或锁屏持续监测。


## 0.2.6 UI 候选版验证（2026-11-17）

- 四个主导航及记录子页、独立外观偏好、三步区域向导、稳定ID区域操作、记录详情与阅读位置保持已实现。采集/OCR/规则/去重/通知后端和核心配置/数据库结构不变。
- 相关UI、配置保存与打包契约共247个唯一用例最终通过，0失败/错误/跳过。先执行主批次，针对旧布局断言和新增状态文案修正后补测；这是分段验证，不是一次全后端全量回归。原始XML与最终汇总保存在 `build/validation/ui-regression-0.2.6*.xml`、`automated-tests.json`。
- Ruff通过。原生字体已通过QFontInfo确认解析为Segoe UI，离屏渲染显式加载本机字体；不分发字体文件。
- 两套主题在1000×720、760×520下共20张主页面合成截图；三步向导共12张合成截图，尺寸不被内容撑大。另用进程内QT_SCALE_FACTOR=2检查4张监控页，实际DPR=2、逻辑尺寸保持不变；没有改Windows显示设置。原生短显示检查实际位于DISPLAY1/MNG007DA5-1，整个窗口边界在内屏内，未成为前台窗口；最终验证耗时约1.5秒，正常退出。
- 原生DWM标题栏在浅色为false、深色为true，修正了Qt后续样式事件覆盖过早设置的问题。颜色、输入状态和页面控件原地更新，不重建页面。
- 未操作真实微信、未发送真实通知、未更改系统主题/显示/睡眠设置、未启动长跑。图片全部使用合成内容，不代表真实识别或通知送达结果。
- 冻结主程序自检通过：版本0.2.6、4个主页面、2个记录子页、3个OCR模型校验和干净退出；依赖检查通过。
- 冻结采集组件依赖自检及仅预热/关闭的协议自检通过，没有采集真实帧；两进程正常退出，无残留。原有关键模块与新增/重构UI模块的编译代码均与源码一致（合计14项），证据见 `capture-pool-self-test-0.2.6.json`、`build-info.json`。
- 与0.2.5改版前快照逐字节比较，采集、OCR、跟踪、规则、记录、通知及核心配置等15个后端文件没有变化。0.2.5回退包保留。
- 最终包包含依赖锁、模型来源/哈希、许可证、使用说明及 `preview/` 中的浅深合成界面图；不包含真实聊天帧、用户数据库或凭据。

## 0.2.5 候选版验证（历史，2026-11-16）

- 变更前工作区、差异和114个源码/文档文件已保存于 `build/baseline-025/20261110-233128/`，未覆盖原有未提交工作。
- 已实现常驻采集协议v2、两个全生命周期进程额度、阻塞隔离、预热门禁、原生会话完整预算与故障诊断。
- 中间候选首次冻结A/B出现一次4500ms故障，最后阶段为wgc_start；中间五窗服务测试出现一次worker_ready超时。两份失败报告保留于 `build/validation025/ab-frozen.json` 和 `real-service-025.json`，不得记为通过。后者揭示启动预热与首请求预算交接缺口，现增加独立的就绪门禁；前者原始阶段证据不足，不强行归因。
- 12次重新启动组件的定向冷启动复测通过，完整阶段与启动等待保存在 `cold-probe.json`；它不代替最终候选长跑。
- 用户在手机文件传输助手发送唯一测试文字，完全遮挡下已识别命中：`occluded-new-text-0.2.5.json`，11帧、0采集错误、conclusive=true。该证据为冻结采集组件+源码OCR+用户确认ROI；不代表飞书或手机投递。
- 2026-11-16复核补齐：共享客户端从请求到解码的完整互斥；排队后派发前和返回后的窗口身份复核；日志延迟后的截止检查；版本/依赖错误明确传播且不循环重启；页脚、状态栏和托盘预热状态一致。
- 2026-11-16首次审计发现5个旧HWND失效；用户随后重新校准并预览确认。复审5个不同窗口身份和ROI均有效、完整位于DISPLAY1（`reconfirmed-audit-20261116.json`）。没有按标题自动重绑。
- 当前候选与0.2.4冻结组件完成10轮、五窗A/B：双方各50次采集，失败均0。预热另计时，采集P50 734→297ms，P95 812→375ms，最大3641→500ms；候选10次预热全部通过，中位297ms、最大328ms。测试期间用户正常GUI监控也在运行；这是当时本机负载下的短测，不能推断零故障或长期无泄漏。
- 源码生产MonitorService + 当前冻结组件 + 真实OCR + 本机模拟飞书完成五窗10轮30秒；8项检查通过，各区域处理序号均推进到10，硬失败/暂停/丢帧均0，同区域相邻提交最大间隔30109ms。没有真实飞书投递。
- 用户已明确要求停止长跑及长时间检测；验收进程和资源观察已停止。长跑未完成，不记为通过，不再自动重启。已完成的短时A/B、五窗十轮调度和此前遮挡新文字证据保留；窗口变换场景尚未完成，不依据用户操作说明直接记为通过。
- 最终离线回归：793项通过，0失败/错误/跳过，Ruff及pip依赖检查通过。结果：`build/validation/automated-tests.json` 与 `pytest.xml`。
- 冻结主程序隔离自检通过：0.2.5、3个OCR模型哈希校验、5页签、合成文字识别及干净退出。结果：`package-smoke-0.2.5.json`。
- 冻结采集组件依赖自检通过（NumPy2.2.6 / OpenCV5.0.0）；双组件常驻协议预热与关闭自检通过，0真实采集、2进程正常退出、剩余进程0。一次冷启动就绪3656ms，双空闲进程私有提交合计约47.8MiB；仅一次离线资源快照，不是实采峰值或长期无泄漏证明。
- 从EXE归档提取编译代码逐项与当前源码比较：capture_pool、window_capture、service、models、diagnostics、ui.main、ui.region及capture_worker全部一致。证据：`capture-pool-self-test-0.2.5.json` 与 `build-info.json`。
- 当前交付只能标记“候选版”，不能标记最终现场稳定性验收通过。未自动替换用户正在使用的版本，未改用户窗口配置或凭据。
- 下文全部为历史版本记录；历史参数、测试数和包信息不代表0.2.5结果。

## 0.2.4 当前变更与验收边界（2026-11-10）

- 区域数量不设固定上限；默认每区域30秒，可在设置中选择10–300秒，运行中保存即生效。
- 采集与OCR维持单工作线程，等待队列最多4帧；到期公平调度、采前背压和带代数的busy所有权防止同区重叠。
- 采集启动交接改为原子所有权状态；全生命周期最多2个进程，未退出进程持续登记和回收，读槽/异常句柄路径均有回归。
- 反馈学习、标注、通知模型和训练组件已删除；OCR模型、字符规则、跟踪和通知保留。
- 识别记录移至独立recognition.db；新旧统一默认最新20000条，数量可改，无按天清理，按200条分页浏览。
- 迁移覆盖WAL快照、失败留源、删除失败重试、保留集摘要校验；程序在后台准备完成后才允许启动监控。
- 全量离线回归：666项收集，663通过、0失败、0错误、3跳过；随后用本机已有Git GNU sha256sum并显式启用微基准，3个跳过项全部补测通过。合计666个唯一用例均通过，Ruff与pip依赖检查通过；主回归日志和补测XML分别留存，不把两次运行混成一次。
- 主程序和采集组件EXE版本资源均为0.2.4，采集组件依赖自检与冻结主程序隔离自检通过。
- 本机迁移已执行：23035条普通识别记录中保留最新20000条，按授权删除较旧3035条，旧反馈库已移除；配置v3默认30秒，凭据文件元数据未变；重复prepare保持20000条。
- 源码隔离自检已通过：版本0.2.4、3个OCR模型校验、合成文字识别、5页签及干净退出。
- 合成650×300文字帧：首次OCR墙钟2707.84ms；5次稳定推理P50 197.88ms/max235.59ms；5次静止帧复用P50 1.39ms/max2.27ms。样本小且为合成输入，不代表真实微信耗时，不构成与旧版本的同机A/B速度提升声明。
- 离屏界面已检查1000×720和760×520，设置可滚动，保留条数/采样控件可见，识别页无标注入口。
- 现场只读检查：现有4个已配置HWND均已失效。未自动按标题重绑、未采集其他窗口；真实微信十轮30秒、多于4个真实窗口、遮挡新文字及真实飞书手机送达均未验收，需用户重校后补齐。
- 已生成dist/DealWatch-0.2.4/及独立ZIP；旧0.2.0/0.2.1/0.2.2便携包保留。新包只带当前版本脱敏摘要与离线证据，原始聊天探针和旧参考报告不入包。
- 下文0.2.3及更早内容为历史记录，历史60秒参数和旧反馈学习策略不适用于本版。


# 0.2.3 验证记录（区域添加闭环 + 主窗口缩放）

范围声明：只改界面流程与布局。字符规则（按字符计数、不跨消息拼字）、五分钟跨群去重、60 秒采样、通知与投递行为**没有改动**，相关既有回归全部保持通过。

## 一、根因证据（修复前实测）

| 项目 | 修复前实测 |
|---|---|
| 添加流程断点 | 用**真实** `RegionEditDialog` + 真实 `exec()` + 真实按钮连接复现：点“截取”后外层 `exec()` 立刻以 `Rejected` 返回，`add_region` 在截取结果回来之前就结束，没有任何 `add` 提交。新增用例 `test_fourth_region_saved_through_real_dialog` 在修复前失败（`3 != 4`），修复后通过 |
| 隐藏位置 | 旧 `choose_region()` 沿父链 `hide()` 自身与主窗口；Qt `QDialog::hideEvent` 的既有语义就是退出模态事件循环 |
| 残留对象 | 截取完成后旧代码把已失效的对话框重新 `show()`，以主窗口为父残留；此时再点“保存区域”无人接收结果（与“日志里没有 add 请求”一致） |
| 最小宽度 | 修复前离屏量测（正式样式）：`window.minimumSizeHint = 2180×278`。主导项：设置页版本/路径单行标签 **2158**、识别记录说明 1664、运行数据路径 1365、通知记录说明 1105、关键词说明 611 —— 全部是**不换行**的长文本，且多数在隐藏页签 |
| 测试口径 | 既有添加流程用例全部 `patch("deal_watch.ui.main.RegionEditDialog", Fake…)`，真实对话框生命周期无覆盖 |

## 二、修复后（离线）

| 项目 | 实测结果 |
|---|---|
| 全量离线回归 | **656 项收集：655 通过 / 0 失败 / 0 错误 / 1 跳过**（跳过项仍是默认关闭的取证微基准）；日志 `build/tmp/full_run2.log` |
| 新增回归 | `tests/test_region_add_lifecycle.py` 8 项（真实对话框生命周期）；`tests/test_main_window_layout.py` 10 项（正式样式布局） |
| 静态检查 | Ruff 0.11.7：`src` / `tests` / `tools` 全部通过 |
| 最小宽度 | 修复后 `window.minimumSizeHint = 537×295`，声明最小 `760×520`；五个页签最大最小提示宽度 **533**（监控区域、识别记录），低于 760 |
| 缩放档位 | 760×520 / 900×600 / 1180×780 三档：窗口能缩到声明最小宽度、切换任意页签不再撑宽、开始监控/全部暂停/重试保存/区域动作按钮均在窗口可见范围内 |
| 缩放因子 | `QT_SCALE_FACTOR=1.0 / 1.5 / 2.0` 测得逻辑最小提示**完全相同**（537×295）。原因是 Qt 逻辑像素本身已做 DPI 归一：150% 下变化的是物理像素与**可用逻辑面积**，后者由首次显示的可用区收缩处理。真机 150% 下的观感仍需现场确认 |
| 截取失败与重试 | 合成失败 `PRINTWINDOW_BLANK_FRAME` → 界面保留别名与窗口选择、状态写明原因，再次截取后成功保存第 4 条 |
| 取消路径 | 取消圈选回到添加界面且不写盘；截取进行中点“取消”后，迟到的采集结果不再弹圈选窗口（无幽灵弹窗） |
| 包内自检（源码） | `--self-check`：`passed=true`、`version=0.2.3`、`models_verified=3`、`ui_tabs=5`、`clean_shutdown=true`、`no_real_desktop_or_notification=true`（`build/tmp/selfcheck-0.2.3.json`） |

## 三、本轮**未**验证（现场 / 待用户操作）

- 最终 EXE 上的“第 4 个真实窗口：添加 → 保存 → 重启 → 预览确认”。
- 主窗口左右边缘、四角拖动与最大化恢复（真实桌面，屏幕 1）。
- 四区域就绪后 10 轮真实 60 秒调度，覆盖移动、缩放、最小化、遮挡与恢复。
- 真实飞书投递与手机送达（只有用户点“发送测试消息”才会触发）。
- 真实微信窗口在 150% / 200% 系统缩放下的实际观感。

以上任一项不得用离屏结果替代。

# 0.2.2 集成阶段验证记录（A/B/C → D，历史）

## 一、离线回归与静态检查

| 项目 | 实际结果 |
|---|---|
| 定向回归 | **96 项通过**：`test_service` / `test_ui_runtime` / `test_metrics` / `test_stage_fields` / `test_packaging_specs` / `test_packaging_checksums` / `test_packaging_capture_stage`；修改前基线 79 项通过 |
| 全量离线回归 | **638 项收集：637 通过 / 0 失败 / 0 错误 / 1 跳过**（跳过项为默认关闭的 40×40 取证微基准，`DEALWATCH_BENCH=1` 时单独执行且已通过） |
| 静态检查 | Ruff 全部通过（`src`、`tests`、`tools`、`packaging`） |
| 运行日志 | `build/g0/d-baseline-targeted.log`（79）、`d-targeted-after.log`（81）、`d-targeted3.log`（96）、`d-full*.log` |

## 二、集成点与离线证据

| 集成点 | 实现 | 离线证据 |
|---|---|---|
| **共享 `TimingRecorder`** | `MainWindow.__init__` 创建 `self.metrics`；`start_monitoring()` 传给 `LocalOcr(observer=OcrStageRecorder(self.metrics))` 与 `MonitorService(metrics=self.metrics)`；`NotificationDispatcher(metrics=NotifyMetrics(self.metrics))` | `test_shared_timing_recorder_wired_to_ocr_service_capture_and_notifier` 断言四条通路指向同一对象；`build/d/stage_bench.py` 用同一记录器读出 capture / OCR / 规则 / 存储各段样本 |
| **ROI 开关下传** | `WindowFrameCapturer(capture_roi_mode=self.config.capture_roi_mode)` 保留并提交 | 同一界面级用例把配置设为 `False`，断言采集器构造参数为 `False` |
| **候选与判断单事务** | `service.py` 在记录候选处构造 `decisions_by_tracking`（仅 `normal_text`，缺键保持 `observed`），调用 `record_candidates(..., decisions_by_tracking=...)`，删除紧随其后的 `mark_decisions` | `test_candidates_and_decisions_written_in_one_store_call`：每帧只一次候选写入、`mark_calls == 0`，判断为 `not_matched` / `queued` 且逐条对齐 |
| **写入内容不变** | `decision_state()` 与旧逻辑同口径：`queue_state != not_applicable` 用其值，否则用 `reason` | 与 `4f67f7b` 同输入对照：5 条候选落库判断均为 `queued`，逐条一致；事务数 2 → 1 |
| **监控语义不变** | 恢复只建基线、故障/恢复各通知一次、去重与规则判定未改 | `test_service.py` 既有恢复/暂停/in-flight 作废用例全部通过 |

## 三、性能与资源（离线合成，不代表真机）

### 3.1 显式微基准（`DEALWATCH_BENCH=1 pytest tests/test_perf_t24.py -s`）

| 指标 | 改前（参考实现） | 改后（当前实现） |
|---|---|---|
| 40×40 匹配 P50 | **46.181 ms** | **8.148 ms**（5.67×） |
| `SequenceMatcher` 调用 | 1600 | 304 |

### 3.2 真实 OCR 冒烟（`tools/smoke_ocr.py`，1000×480 合成图）

| 轮次 | 耗时 | 结果 |
|---|---|---|
| 第 1 轮（含引擎初始化） | **3259.2 ms** | 4 行文字全部识别，未覆盖块 0 |
| 第 2 轮 | **335.7 ms** | 同上 |
| 第 3 轮 | **332.0 ms** | 同上 |

### 3.3 阶段基准（`build/d/stage_bench.py`：合成帧 + 真实 PP-OCRv6 + 共享记录器，10 帧）

| 段 | 样本 | P50 | P95 | max |
|---|---|---|---|---|
| `capture` | 10 | 54.9 ms | 62.9 ms | 66.5 ms |
| `ocr_queue` | 9 | 0.3 ms | 505.0 ms | 565.5 ms |
| `preprocess` | 3（冷启动单列） | 45.9 ms | 47.2 ms | 47.3 ms |
| `ocr_engine` | 3（冷启动单列） | 347.6 ms | 368.3 ms | 370.5 ms |
| `ocr_infer` | 4 | 435.1 ms | 1577.5 ms | 1776.8 ms |
| `postprocess` | 12 | 0.1 ms | 29.4 ms | 30.3 ms |
| `tracking_rules` | 9 | 0.8 ms | 1.5 ms | 1.7 ms |
| `storage_write` | 9 | 1.8 ms | 11.6 ms | 12.0 ms |

- 真实推理 **4 次**；静止画面复用 **5 次 / 9 次查询（55.56%）**；缓存失效 1 次（恢复/状态变化路径）。
- 单帧端到端（采集开始 → 健康快照）**P50 1045.2 ms / P95 1773.5 ms / max 1834.6 ms**，`peak_in_flight = 2`。
- CPU：**8.672 s / 5.905 s 墙钟 = 147% 单核**（ORT 多线程）；进程 RSS **37.8 MB → 131.3 MB**（含模型加载）。
- 数据传输：合成帧共 **14,400,000 字节**（10 × 1000×480×3，进程内）；**网络传输量未观测**（离线路径无飞书请求）。
- 口径限制：`ocr_infer` 由服务层按 `observation.inference_ms` 记录，未区分冷启动；`preprocess` / `ocr_engine` 的冷启动样本单独计数。`model_init` 仅 1 次（871.1 ms），已归入 cold 桶。

### 3.4 与 `4f67f7b` 的同输入对照（`git worktree` + `build/d/match_bench.py`）

| 指标 | `4f67f7b` | 当前 HEAD + 集成 |
|---|---|---|
| 40×40 匹配 P50 | 8.048 ms | 7.962 ms（噪声级） |
| 一帧 5 条候选事务数 | **2** | **1** |
| 写入耗时 | 2.960 ms | 1.371 ms |
| 落库判断 | 5 × `queued` | 5 × `queued` |

未做真实四窗链路的前后对照；上表只覆盖同输入的匹配与事务两处。

## 四、真实三窗 60 秒 × 十轮源码探针（2026-11-05）

报告：`build/validation/real-wechat-60s-0.2.2.json`。`source_commit=c0dc75729809a25a1f3e813b6383be44ae162142`、`source_dirty=false`、`version=0.2.2`。运行方式为 `.runtime` **源码**，不是最终 EXE；3 个用户已配置的真实微信窗口、只读采集、真实 OCR，通知只发 `127.0.0.1` 模拟飞书（不发送微信消息、不写用户配置）。

| 指标 | 实测 |
|---|---|
| 区域 | **3**（`region_count=3`；`capture_mode=user_configured`、`roi_source=user_config`、`user_confirmed_roi=true`；三个区域均 `previewed=true`、`persisted=true`）+ `all_regions_on_display1=true` |
| 参数 | `production_params=true`、`diagnostic_params=false`；`capture_timeout_seconds=5.0`、`wgc_timeout_seconds=3.0`、`poll_interval_seconds=60.0` |
| 轮数 / 时长 | 10 轮；墙钟 **542.1 秒** |
| 检查 | **8/8 PASS**：`rounds_completed`、`every_region_submitted_each_round`、`round_settled_by_sequence`、`capture_failures_zero`、`round_within_interval`、`region_cadence_within_interval`、`queue_depth_at_most_4`、`no_thread_death` |
| 采集 | 硬失败 **0**、暂停 **0**（`failure_codes: {}`）、丢帧 **0**；队列深度 max **2**（`queue_depth_at_most_4` 上限 4）；`peak_in_flight=3` |
| 调度 | 每区域提交 10 次；同区域相邻提交最大 **60062 ms**；轮时长 P50 59922 / P95 60125 ms；收敛 P50 59922 / P95 60787 ms |
| OCR | P50 **831 ms** / P95 **1613 ms** |
| 静止画面复用 | 查询 **30** / 命中 **27** / 复用 **24** / 真实推理 **6**；`content_hit_rate=90%`、`reuse_rate=80%`（每区域 10/9/8/2） |
| 轮末等待 | `settle_wait_ms` P50 **0** / P95 **1658.8** / max **2031** ms |
| 通知 | `notify.requests=0`（当帧无关键词命中）；**没有真实飞书请求**，不能证明飞书投递或手机送达 |
| 源码指纹 | 9 个文件（`service.py`、`window_capture.py`、`capture_worker.py`、`ocr.py`、`ocr_cache.py`、`notify.py`、`metrics.py`、`diagnostics.py`、`tools/real_schedule_60s.py`），`combined_sha256=ca9cb441…6414` |

口径与边界：

- 这是**源码探针**，不是最终 EXE 验收；源码指纹用于证明运行代码，不依赖之后变化的 `HEAD`。
- 只证明 3 个已配置区域在静态窗口下的 60 秒调度与识别链路稳定；**不证明**第 4 个区域配置正确、不证明窗口移动/缩放/最小化/完全遮挡/恢复、不证明八小时稳定性。
- 同目录 `real-wechat-60s-0.2.2-run1-failed.json` 与 `-run2.json` 是诊断过程文件，**不得**作为验收证据，也不得入包。

## 五、候选包与自检

| 项目 | 实际结果 |
|---|---|
| 构建目录 | 最终候选包重新构建到 `build/candidate-final/DealWatch-0.2.2/`（独立 PyInstaller work 目录；不覆盖 `dist/DealWatch-0.2.0`、`0.2.1`、`0.2.2`，也不覆盖上一轮 `build/candidate`） |
| 包自检 | 包内 `DealWatch.exe --self-check --report build/validation/package-smoke-0.2.2.json`：交付门禁要求 `passed=true`、`frozen=true`、`version=0.2.2`、`models_verified=3`、`ui_tabs=5`、`capture_component=true`、`clean_shutdown=true`、`no_real_desktop_or_notification=true` |
| 采集组件自检 | 包内 `capture-component/DealWatchCapture.exe --self-test`：交付门禁要求 `ok=true`、`numpy-*.dist-info` 恰好 1 份；构建脚本把结果写入 `build/validation/capture-component-self-test-0.2.2.json` |
| 包规模与完整性 | 交付目录文件数 = ZIP 条目数；`SHA256SUMS.txt` 覆盖除自身外的全部文件且逐文件校验 0 不一致；`zipfile.testzip()` 返回 `None`。具体数字与 ZIP SHA-256 见构建输出与交付消息 |
| 源码指纹 | `build/validation/source-snapshot.json`（含 `PLAN_STATUS.md`、`VALIDATION.md`）；finalize 时逐文件复核，与当前工作区不一致即中止 |
| ZIP 门禁 | `tools/finalize_package.py --dist-root build/candidate-final --require-evidence`：缺离线回归、源码指纹、包自检或采集自检时不生成正式 ZIP |
| 构建身份 | `build/validation/build-info.json` 随包分发：commit、已暂存/未暂存差异指纹、未跟踪文件、Python 与依赖版本、模型与字典哈希、构建编号 `build_id`；要求 `commit` 等于当前 `HEAD` 且 `worktree.dirty=false` |
| 上一轮候选包（仅历史） | `build/candidate/DealWatch-0.2.2/`（`47681d6`）889 个文件 / 889 条目 / 约 245.3 MiB；其源码与报告口径均早于 `c0dc757`，**不是**本次交付产物 |

包的提交号、`build_id`、文件数与 ZIP SHA-256 由构建输出与交付消息给出；本文件不写自引用哈希。

## 六、未完成（E 阶段待办，不得据此推断已达标）

1. 第 4 个区域仍需用户配置并逐个预览确认；四个真实 ROI 是否圈对未被证明（现有正式报告只覆盖 3 个区域）。
2. 探针未覆盖窗口移动、缩放、最小化、完全遮挡与恢复；完全遮挡 + 手机发送指定编号（`--expect`）的端到端证据仍缺。
3. 真实四窗十轮 60 秒生产节奏（`production_params: true`）与八小时资源记录；现有三窗探针墙钟约 542 秒。
4. 真实飞书群与手机送达耗时；三窗探针通知请求为 0，平台接受也不等于手机送达。
5. 现场“约 43 秒没有结果”的现象**未复现、未定论**：它不是“单次采集耗时 43 秒”，也不能由本轮离线埋点结果推断为已修复；需要现场 `logs/capture-stages.log`。
6. 最终 EXE 的四窗现场验收未做；本轮最终 EXE 只完成隔离自检、采集组件自检与包完整性校验。

---

# 0.2.1 历史验证记录（下列各节）

## 一、离线回归与静态检查

| 项目 | 实际结果 |
|---|---|
| 完整离线回归 | **273 项通过**，0 失败、0 错误、0 跳过（266 项 + QA 复验两个缺陷的 3 条界面级回归 + 4 条打包约束回归）。QA 的 70 条独立复验用例是一次性证据，不留在仓库，不计入本项统计 |
| 静态检查 | Ruff 全部通过（`src`、`tests`、`tools`） |
| 产物 | `build/validation/automated-tests.json`、`pytest.xml`、`pytest.log`、`ruff.log`、`source-snapshot.json`（本轮刷新，见文件 mtime） |

本轮新增覆盖（按主题归并；逐条用例名见 `build/validation/pytest.xml`）：

1. 气泡分割重写：同气泡多余修饰仍命中、同气泡多行只命中一次、空行分段不切断、4px 间隔不拼字、0px 粘连需“行距 + 左右边界 + 上下底色”三项证据才切分、无法判定进未覆盖。
2. 图片/卡片排除：65×60 彩色缩略图整卡遮罩、灰度照片卡片、无边框浅色卡片、纯文字样式卡片；普通文字提到“聊天记录”仍必须通知。
3. 窗口绑定：切换窗口/后端即失效并阻止保存、迟到采集结果按代数丢弃、保存前复核拒绝不一致身份。
4. PrintWindow：`user32.SetProcessDpiAwarenessContext` + BOOL 语义、实际 awareness 查询、均匀边框检测 + 内部墨水占比判空白帧。
5. 暂停语义：最小化/锁屏进入 `PAUSED` 且不计故障、健康通知只发一次、恢复帧只建基线不补发。
6. 去重：跨群同内容五分钟一次、`1.99/199` 与 `AB-12/AB12` 视为实质差异。
7. 交付门禁：`finalize_package.py` 报告清单防串版（旧名 `package-smoke.json` 与 `version≠0.2.1` 均中止，0.2.0 参考件必须改名）。
8. **区域就绪门禁统一**：`readiness.region_readiness_problems()` 覆盖旧版坐标、未预览确认、句柄/坐标模式/后端、区域尺寸、参考帧、窗口身份（PID/路径/类名/标题）；四个入口（开始监控、启用区域、恢复采集、保存区域）同口径；`resume_region()` 不再因区域不存在抛 `StopIteration`，未就绪一律保持暂停。
9. **故障事件化**：`active_faults` 记录故障事件，`ERROR` 只在“进入故障”时推送一次，`BASELINING` 不算恢复，`PAUSED`（最小化/锁屏）不结束故障事件，`RUNNING` 才发一次恢复，`DISABLED` 静默结束；托盘按全部启用区域汇总（`N 个区域异常` / `已暂停` / `监控中（N 个区域暂停）`）。
10. **生产采集参数统一**：`WindowCaptureClient` 5 秒、`capture_wgc` 3 秒、`poll_interval_ms` 60000；五个真机探针默认 5 秒并新增 `--capture-timeout`，放宽时报告写 `production_params: false` + `diagnostic_params: true`，禁止诊断数据冒充生产证据。
11. **遮挡工具证据强度**：新增 `--expect`（可重复），成功条件改为“**同一次采样**既完全覆盖、又字面命中全部期望项”（`match_normalize`，不用字符集），每轮重做覆盖校验，报告新增 `expect_sha256`、`expected_text_matched`、`expected_matched_at_seconds`、`success_basis`、`conclusive`、`coverage_rechecks`、`coverage_lost_at_seconds`、`detection_seconds_is_not_notification_latency`。
12. **两个自动化验收工具**：`acceptance_synthetic` 迁移到生产路径（`WindowFrameCapturer` + WGC 子进程 + 帧内 ROI + 导出子进程 `PYTHONPATH`），`acceptance_wgc_service` 同步；两者在 `service.start()` 后加**就绪门禁硬闸**（`definitive_gate_failures` 立即中止 → 退出码 2；仅“从未上报” → 退出码 1），报告写 `readiness_gate_passed`、`region_gate_failures`、`gate_wait_seconds`、`uses_legacy_desktop_capture: false`。
13. **启动瞬间状态可见性（QA 复验发现）**：视图状态在构造 `MonitorService` 之前清空；启动时被门禁挡下的区域在 `health_states`/`health_details` 中保持 `paused`，托盘显示「已暂停」，状态栏点名「N 个区域未就绪、不会被采集：…」。回归：`test_blocked_region_at_startup_is_visible_in_tray_and_status_bar`（真实 `MonitorService` + 真实门禁，断言 `service._regions == {}`、`tray_summary()` 为「已暂停」、状态栏含「未就绪/不会被采集」）。
14. **预览确认必须有识别证据（QA 复验发现）**：`PreviewDialog.show_result()` 按 `len(observation.blocks)` 启用「确认此区域」，0 块时状态文案说明“未识别到任何文字块”；`confirm_region()` 二次复核，0 块时 `QMessageBox.warning` 并拒绝写入 `previewed`。回归（真实 `PreviewDialog` + `PreviewWorker`，仅替换 `LocalOcr` 返回值）：`test_confirm_stays_disabled_when_nothing_was_recognized`、`test_confirm_enabled_and_sets_previewed_when_text_recognized`。

## 二、WGC 与 PrintWindow 同窗对照（真实微信，150% 缩放）

报告：`build/validation/backend-compare-0.2.1.json`。目标窗口为一个真实微信聊天窗口，只读采集 + 真实 OCR，不发送任何微信消息。

| 指标 | WGC | PrintWindow |
|---|---|---|
| 采集结果 | `ok=true` | `ok=false`，`PRINTWINDOW_BLANK_FRAME`（明确报错，无帧返回） |
| 帧尺寸 | **800×1280** | 无（错误路径不返回帧） |
| 客户区矩形 | 796×1278 | — |
| 帧 = 客户区 + 2px 描边 | **是**（800×1282，容差 2px 内一致） | — |
| 均匀边框带（上/下/左/右） | 2/2/2/2 px | — |
| 实际生效 DPI awareness | **per_monitor_v2** | **per_monitor_v2** |
| 识别文字块 / 字符 | 20 / 164（底部 20% 区域 5 字） | 0 / 0 |
| 与 WGC 的字符 Jaccard | — | **0.0** |
| `printwindow_compatible` | — | **false** |

真机复测错误路径（报告内原文）：

```
capture_failed: CapturePayloadError: PRINTWINDOW_BLANK_FRAME: PrintWindow 无法取得当前微信内容（空白帧），请改用 WGC
```

结论：PrintWindow 对当前微信不可用，且**不会静默退回桌面截图**；WGC 是默认与推荐后端。

## 三、四个真实微信窗口的只读识别探针

报告：`build/validation/real-window-probe-0.2.1.json`。探针 ROI 由工具按版式计算（顶部内缩 8%、底部内缩 32%），**不是用户校准的 ROI**，也不写入配置。

| 窗口 | 帧 | 文字块 | 气泡面板 | 其中不确定 | 消息条数 | OCR 耗时 |
|---|---|---|---|---|---|---|
| 布丁跑鞋A3 | 800×1280 | 13 | 4 | 1 | 3 | 938.7 ms |
| 橘子户外 | 800×1280 | 11 | 4 | 0 | 5 | 970.9 ms |
| 毛豆鞋服 | 800×1280 | 12 | 5 | 1 | 6 | 800.9 ms |
| 橘子双11 | 800×1280 | 12 | 6 | 0 | 9 | 706.8 ms |
| 合计 | — | 48 | 19 | 2 | 23 | — |

- 2 个“不确定”面板为卡片区域（商品/笔记卡），文字被丢弃并计入未覆盖（`uncovered=4`）。
- 当帧内容不含配置关键词，命中消息 0 条，符合预期。
- 该探针证明真实浅色版式可采集、可识别、气泡面板可分割；**不代表**用户圈选 ROI 下的验收结论。

## 四、真实四窗 60 秒 × 十轮调度（**诊断参数数据，不是生产 5 秒参数的验收证据**）

报告：`build/validation/real-wechat-60s-0.2.1.json`。四个真实微信窗口、WGC、真实 OCR，通知只发 `127.0.0.1` 模拟飞书，不发送微信消息、不写用户配置。

| 指标 | 实测 |
|---|---|
| 轮数 / 采样间隔 | 10 轮 / 60.0 秒 |
| 墙钟总时长 | **544.5 秒**（`finished_at - started_at`） |
| 每轮四区域提交 | 10/10 轮均为 `[1,1,1,1]` |
| 同源相邻提交间隔 | 每区域 9 个间隔；四区域最大 **60250 ms（60.250 秒）** |
| 轮次开始间隔 | 9 个间隔，P50 **59828 ms** / P95 **60794 ms**；首个间隔 5141 ms（首轮无等待直接提交，属工具口径，不是调度漂移） |
| 硬采集失败 / 暂停 | **0 / 0**（`failure_codes: {}`） |
| 丢弃帧 | **0** |
| OCR 队列深度 | 最大 **3**（上限 4） |
| 单窗 OCR | P50 **994 ms** / P95 **1466 ms** |
| 每轮调度耗时（四区提交完毕） | P50 57414 ms / P95 **58340 ms（58.34 秒）** |
| 每轮收敛（含等 OCR 队列排空） | P50 59828 ms / P95 60788 ms |

检查项 `rounds_completed`、`every_region_submitted_each_round`、`capture_failures_zero`、`round_within_interval`、`region_cadence_within_interval`、`queue_depth_at_most_4`、`no_thread_death` 全部 PASS。

> **参数口径（必须按此引用）**：该报告产出于「探针父进程采集超时硬编码 10 秒」的版本，报告里**没有** `capture_timeout_seconds` 字段，也没有 `production_params` 字段。因此它只能证明"60 秒调度与识别链路在 10 秒诊断超时下稳定"，**不能**用于宣称生产 5 秒参数达标。要作为生产证据，必须在当前版本用默认参数重跑：
>
> ```
> .runtime\python311\python.exe tools\real_schedule_60s.py --rounds 10 --interval-seconds 60 --output build\validation\real-wechat-60s-0.2.1.json
> ```
>
> 重跑后报告应出现 `capture_timeout_seconds: 5.0`、`wgc_timeout_seconds: 3.0`、`poll_interval_seconds: 60.0`、`production_params: true`；若曾用 `--capture-timeout` 放宽，则必须显示 `production_params: false` + `diagnostic_params: true`。

**该记录不含**：完全遮挡下的新消息、窗口移动/缩放/最小化恢复、真实飞书投递。

## 五、合成四窗闭环（自建窗口 + 生产 WGC 路径 + 模拟飞书）

报告：`build/validation/synthetic-four-window-0.2.1.json`（`tools/acceptance_synthetic.py`，本机模拟飞书，`--soak-seconds 0`）。

| 指标 | 实测 |
|---|---|
| 检查项 | **13/13 PASS**，`success: true`，`aborted: null` |
| 就绪门禁 | `readiness_gate_passed: true`，`region_gate_failures: {}`，`gate_wait_seconds: **2.91**` |
| 采集路径 | `capture_backend: wgc`、`coordinate_space: frame`、`uses_legacy_desktop_capture: **false**`、`capture_timeout_seconds: 5.0` |
| WGC 帧尺寸 | 四个区域均 **690×450**（460×300 逻辑 @1.5 DPR） |
| 采集 → 模拟飞书延迟 | P50 **606.36 ms** / P95 **2266.29 ms**（阈值 5000 ms） |
| HTTP 接受条数 / 事件记录 | 6 / 9（跨群同内容只入队一次、价格变化照常提醒） |
| 墙钟 | **39.06 秒** |
| 丢弃帧 | **0**（四区域 `dropped_frames: 0`） |
| 识别环节证据 | `recognition_fixture_markers_only` **78 条**、`emitted_fixture_markers_only` 6 条 |

跑通的阶段（报告 `checks`）：基线不通知、四窗各自新内容、跨群同内容只一次、价格变化提醒、**帧尺寸变化丢弃在途 OCR 结果**、尺寸恢复后只重建基线、**窗口移动不影响帧内 ROI**、**完全覆盖不暂停生产采集且不多发提醒**、四窗恢复 running、线程存活、发送端无错误、P95 ≤ 5 秒。

边界（必须一并引用）：

- 自建合成窗口 + 合成文本 + 本机 HTTP 模拟飞书；**不代表**真实微信群、真实飞书网络或手机送达。
- OCR 使用**真实本地引擎**（`LocalOcr`，PP-OCRv6 small）；`GatedOcr` 只是在结果返回时加确定性闸门（用于验证在途结果作废），不是识别替身。
- 采样间隔为 500 ms 的加速验收，**不代表**生产 60 秒节奏。

## 六、0.2.1 便携包与自检

| 项目 | 实际结果 |
|---|---|
| 主程序 `DealWatch.exe` / 采集组件 | **最终发布构建（2026-11-05 12:44）**：主程序 **12:44:25**、`capture-component/DealWatchCapture.exe` **12:43:08**；`find src -name "*.py" -newer dist/DealWatch-0.2.1/DealWatch.exe` 为空 —— 产物新于全部源码 |
| ⚠ 已废弃的中间构建 | **01:17:31** 那份不可信（为验证“修复前必红”曾临时还原源码，构建分析落在该窗口内）；**01:20:53** 那份虽然源码正确，但采集组件存在下述 numpy 混版缺陷，无法在便携包内圈选，同样作废。发布只认 **12:44:25 / 12:43:08** 这一组 |
| **采集组件 numpy 混版（2026-11-05 现场故障）** | 现象：便携包点“截取窗口并圈选消息区域”弹 `WindowCaptureError: ImportError … DLL load failed while importing _multiarray_umath`，而源码模式同一窗口正常。根因：`packaging/capture_worker.spec` 的 `pathex` 含 `.capture-runtime`，纯 Python 模块取自该目录的 numpy **2.4.6**，而 C 扩展与 dist-info 由 hook 从构建解释器 site-packages 收集（**2.2.6**），ABI 不一致导致 `_multiarray_umath` 加载失败。修复：构建时生成只含 cv2 / windows_capture 的暂存目录（排除全部 numpy），使组件内 numpy 只有一个来源；并新增 `--self-test` 与构建后强制校验 |
| **修复验证（不依赖“文件存在”）** | ① 包内组件 `--self-test` → `ok: true`、`numpy 2.2.6`、`cv2 5.0.0`，`_internal/numpy-*.dist-info` 恰好 1 份；② 对真实微信窗口实采 WGC → `ok: true`、801×1280、PNG 306 KB、`dpi_awareness: per_monitor_v2`；③ 旧版同样命令返回 `ImportError` 与空帧，可复现对照 |
| 构建日志 | `build-final.log`：`Capture stage … (excluded numpy, numpy-2.4.6.dist-info, numpy.libs)` → `Capture component self-test OK` → 两次 `Build complete` |
| 产物规模 | 构建产出 **823** 个文件（`_internal` 708 + `capture-component` 113 + 主程序与校验清单 2）；`finalize_package.py` 再注入 7 份文档、52 份第三方许可证、9 份验证报告后，**交付目录 891 个文件**，与 ZIP 条目数一致（891/891 逐文件 CRC 一致） |
| EXE 内嵌版本 | FileVersion **0.2.1** |
| 隔离离线自检 | `package-smoke-0.2.1.json` 在**最终 EXE**（12:44:25）上重跑（`started_at` 12:45:38）实测：`passed: true`、`version: 0.2.1`、`frozen: true`、`models_verified: 3`、`ui_tabs: 5`、`capture_component: true`、`clean_shutdown: true`、`no_real_desktop_or_notification: true`、`learning_enabled: false`、`ocr_ms: 1189.58` |
| **EXE 内含修复代码（二进制级）** | 直接从最终 `DealWatch.exe` 的 PyInstaller 归档取出应用模块反查字符串常量（`CArchiveReader('…/DealWatch.exe').extract('PYZ.pyz')` → `ZlibArchiveReader.extract('deal_watch.ui.region' / 'deal_watch.ui.main')` → 递归扫描 `co_consts`）：`未识别到任何文字块`、`不能确认此区域`、`请调整圈选范围` 出现在 `deal_watch.ui.region`（缺陷 2 修复文案）；`个区域未就绪`、`区域未就绪、不会被采集` 出现在 `deal_watch.ui.main`（缺陷 1 状态栏文案）。这是**不依赖 mtime、也不依赖自检脚本**的产物内证据，直接证明发布的 EXE 就是修复后的代码 |
| 训练组件 | 明确跳过（未准备独立环境），未伪装为已包含 |
| 0.2.0 回退包 | `dist/DealWatch-0.2.0/` **891** 个文件；`SHA256SUMS.txt` mtime 21:39:32、自身 sha256 `931975429bfb9a1f…` 均未变 |

> `dist/DealWatch-0.2.1-win-x64.zip` 已由 `tools/finalize_package.py` 产出：条目 **891**、大小约 **245 MB**，`SHA256SUMS.txt` 覆盖 **890** 个文件（不含自身），逐文件校验 0 不一致；**解压后实测**：包内 `capture-component/DealWatchCapture.exe --self-test` → `ok: true`（numpy 2.2.6 / cv2 5.0.0），对真实微信窗口 WGC 实采 → `ok: true`、801×1280；包内 `DealWatch.exe --self-check` → `passed: true`。校验值见同目录 `DealWatch-0.2.1-win-x64.zip.sha256`（不写进正文，避免哈希自引用）。

构建后 `validation/` 目录由 `finalize_package.py` 按显式清单注入。清单与门禁：

- 入包（源名 = 包内名或显式改名）：`package-smoke-0.2.1.json`、`automated-tests.json`、`source-snapshot.json`、`backend-compare-0.2.1.json`、`real-window-probe-0.2.1.json`、`real-wechat-60s-0.2.1.json`、**`synthetic-four-window-0.2.1.json`**、`feishu-real-send-0.2.1.json`、`attachment-token-probe.json`，以及改名后的 0.2.0 参考件 `synthetic-schedule-0.2.0-reference.json`。（`feishu-real-send-0.2.1.json` 为**条件项**：仅在用户显式完成真实试发后才存在并入包；缺失时工具跳过、不中断打包，本次包内实际为 9 份。）
- 禁止入包：`package-smoke.json`（0.2.0 旧名）与 **`synthetic-four-window.json`**（0.2.0 时代不带版本号的合成闭环）；残留即中止，不生成 zip 与 `SHA256SUMS.txt`。
- 源报告 `version` 与 0.2.1 不符时中止；0.2.0 参考件只允许自证 0.2.0。

## 七、待用户操作（未完成，不得记为通过）

| 项目 | 为什么必须由用户做 | 判定标准 |
|---|---|---|
| 四窗逐个预览确认 | ROI 必须由用户确认，程序有就绪门禁 | 四个区域状态不再是“待检查确认”，`resume`/开始监控不再被门禁拦下 |
| 完全遮挡 + 指定编号端到端 | 需用手机向该群发送含唯一编号（如 `DW-A3F19C`）的消息 | `tools/occluded_new_text_check.py --expect DW-A3F19C` 报告 `expected_text_matched: true`、`success_basis: "expected_text"`、`conclusive: true`、`coverage_rechecks ≥ 1` |
| 生产 5 秒参数的四窗 60 秒记录 | 需在真实桌面重跑（现记录是 10 秒诊断参数） | 报告含 `capture_timeout_seconds: 5.0`、`production_params: true` |
| 真实飞书与手机送达 | 只能由用户点击“发送测试消息”或显式 `--confirm-send` | 群内出现消息 + 手机收到提醒；平台接受不等于手机送达 |

> 现有 `build/validation/occluded-new-text-0.2.1.json` 是**旧口径**记录：`new_text_detected_while_covered: true`、`first_detection_seconds: 172.5`、`frames_captured: 29`、`fully_covered: true`，但没有 `--expect`、没有 `success_basis`/`conclusive`/`coverage_rechecks` 字段，因此**只能证明“覆盖期间出现了基线外新行”**，不能证明“用户发出的指定消息被识别”。需要按上表用新工具重跑。

## 八、仍未完成（后续稳定性验收）

1. 八小时连续运行：未开始；已有十轮 60 秒记录只覆盖 **544.5 秒**。
2. 不少于 200 个**人工标注**事件回放；95% 召回率、98% 精确率仍为目标。
3. 四窗在“移动、缩放、最小化、完全遮挡、恢复”全场景下的现场长跑（合成闭环已覆盖等价分支，真实窗口仍需现场验证）。
4. 学习模型、训练环境、权重下载与候选启用：0.2.1 不在运行路径内。

## 九、复现命令

```powershell
# 离线回归 + 静态检查 + 源码指纹（写 automated-tests.json / source-snapshot.json）
.runtime\python311\python.exe tools\verify.py

# 同窗 WGC / PrintWindow 对照（只读）
.runtime\python311\python.exe tools\backend_compare.py --title-contains 布丁跑鞋 --output build\validation\backend-compare-0.2.1.json

# 四窗只读识别探针（--capture-timeout 默认 5.0，与生产一致）
.runtime\python311\python.exe tools\real_window_probe.py --output build\validation\real-window-probe-0.2.1.json

# 四窗 60 秒 × 十轮（默认 5 秒采集超时；放宽会标 diagnostic_params）
.runtime\python311\python.exe tools\real_schedule_60s.py --rounds 10 --interval-seconds 60 --output build\validation\real-wechat-60s-0.2.1.json

# 合成四窗闭环（自建窗口 + 生产 WGC 路径 + 本机模拟飞书；会占用内屏）
.runtime\python311\python.exe tools\acceptance_synthetic.py --output build\validation\synthetic-four-window-0.2.1.json

# 完全遮挡 + 指定编号（需用户用手机发消息；不给 --expect 只能得到 new_line_only 弱证据）
.runtime\python311\python.exe tools\occluded_new_text_check.py --title-contains 文件传输助手 --expect DW-A3F19C --timeout 180

# 真实飞书试发（仅用户执行）
.runtime\python311\python.exe tools\feishu_real_send.py --confirm-send

# 打包
.runtime\python311\python.exe tools\build.py
.\dist\DealWatch-0.2.1\DealWatch.exe --self-check --report build\validation\package-smoke-0.2.1.json
.runtime\python311\python.exe tools\finalize_package.py
```
