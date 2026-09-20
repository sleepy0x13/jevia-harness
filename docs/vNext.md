# JEVia vNext · 交付说明

依据：`JEVia_vNext_开发需求_决策动效与可靠性.md`（v1.0，2026-09-19）。本文说明做了什么、没做什么、哪些没有在线验证，以及事件协议、迁移方式和测试命令。

> **没有真实调用。** 本轮所有验收都在离线环境完成：单元测试用假传输层，浏览器检查用 `scripts/offline_server.py`（所有引擎都是本地假实现）和 Demo replay（来自 fixture）。`.env` 里的密钥没有用于任何验收调用。下文凡是涉及线上质量、真实延迟、真实费用的地方，一律标注为**未在线验证**。

## 1. 基线

| | 开发前 | 开发后 |
| --- | --- | --- |
| Python 测试 | 326 通过（`.baseline/tests-before.log`） | 380 通过 |
| 界面测试（node） | — | 24 通过（`scripts/ui_tests.mjs`） |
| 文案审计 | 263 个键通过 | 通过 |

改动了的旧测试，每一处都在测试里写了原因：

- `test_roster`：没有模型达标时，以前会退回到最强模型，并把 `required` 降下来。现在 `required` 保持不变，状态是 `blocked`；只有用户允许后才会带标注降级（B03 / T10）。
- `test_agent`：`__enough__` 不再和段落判断放在同一批里，每轮变成 3 次 Jev 调用（B01）。测试夹具的文本加入了 URL，避免文本去重把不同页面合并掉。
- `test_planner`：缓存键从"内容词集合"改成保守键。原来"改写措辞也命中缓存"的断言，换成了"数字、方向、否定不会碰撞，完全重复仍然命中"（B02 / T12）。

## 2. P0 完成情况

| 项 | 状态 | 位置 |
| --- | --- | --- |
| 真实决策事件（`decision_event`） | ✅ | `jevharness/events.py`，执行器全程接入 |
| started 先于阻塞请求到达浏览器 | ✅ T01 | 工作线程 + 单一队列出口；浏览器里已确认可以看到等待态 |
| 观察器按运行隔离 | ✅ | `JevClient.decide(observer=...)`，客户端上不存运行 ID |
| seq 在出口统一分配、每批只有一个终态 | ✅ T13 | `RunEvents.stamp` |
| M01–M06 六类动效 | ✅ | `ui/js/decision-panel.js` + `motion.js` + `app.css` |
| 执行状态条 / 决策摘要 / Decisions 视图 | ✅ | 运行卡片放在回答顶部，Decisions 视图复用右侧面板 |
| B01 证据充分性 | ✅ T03–T07 | `agent.Researcher` |
| B02 保守缓存键 | ✅ T12 | `planner.plan_cache_key`，旧缓存加载时丢弃 |
| B03 原始判断与策略分离、禁止静默降级、价格未知 | ✅ T10 | `roster.Selection`、`_selected` |
| B04 call_id 与费用来源 | ✅ | `providers.Usage.source`、`Ledger.cost_summary` |
| B05 日志安全 | ✅ T20 | `redact.py`、事件白名单、`public_dict`、同源检查 |
| 持久化 / 取消 / 回放 | ✅ T15 T18 | `<workspace>/.jevia/runs/<run_id>.jsonl`、`/api/runs` |
| 离线 fixture ≥ 5 组 | ✅ 6 组 | `jevharness/ui/fixtures/`，由 `scripts/make_fixtures.py` 用真实执行器离线生成 |
| 回归用例 T01–T20 | ✅ | `tests/test_vnext.py`（后端）+ `scripts/ui_tests.mjs`（界面） |

T01–T20 各用例落在哪个测试：

| ID | 后端（Python） | 界面（node） |
| --- | --- | --- |
| T01 started 先到 | `TestT01StartedArrivesFirst` | 浏览器实测（离线服务器，Jev 延迟 3 秒） |
| T02 一次调用 8 题 | `TestT02OneCallManyQuestions` | `T02 …` |
| T03 全部低于保留阈值，旧评分 0.9 | `TestT03SufficiencyUsesWhatWasKept` | |
| T04 null / 缺项 / 异常 | `TestT04UnknownIsNotYes` | |
| T05 74 段只有部分进入请求 | `TestT05NotAssessedIsCounted` | `M03 T05 …` |
| T06 累计证据 | `TestT06CumulativeSnapshot` | |
| T07 预算截断 | `TestT07TruncatedDeliveryIsFlagged` | |
| T08 Noul 方向 | `TestT08T09AnswerFields` | `T08 …` |
| T09 Choice 与 Confidence 分开 | `TestT08T09AnswerFields` | `T09 …` |
| T10 无合格模型 | `TestT10NoQualifiedModel` | `T10 …` |
| T11 规划用了 LLM，生成被跳过 | `TestT11SkippedStepAfterAPlanningCall` | `T11 …` |
| T12 缓存碰撞 | `TestT12CacheKeys`，以及 `test_planner.TestCacheKey` | |
| T13 重复 / 迟到 / 缺序 / 并行 | `TestT13Ordering` | `T13 …` ×2 |
| T14 token 流不重播动画 | | `T14 …` |
| T15 历史与 Demo 不联网 | `test_server.TestRecordedRuns` | `T15 …` |
| T16 动效关闭 / 减少 / 后台 | | `T16 …` |
| T17 输出前失败 / 输出中失败 | `TestT17FailureBeforeAndDuringOutput` | |
| T18 取消 | `TestT18Cancel` | 浏览器实测 |
| T19 英文界面、中文内容 | | `T19 …` |
| T20 HTML 标题、模拟密钥 | `TestT20Redaction` | `T20 …` |

浏览器实测中还发现并修复了一个真实 bug：多步骤任务里，"整体答案"的选模被判为"无合格模型"后，会把本来各自达标的子任务全部拦住。回归测试为 `TestStepsAreNotBlockedByTheWholeAnswer`。

## 3. P1 完成情况

| 项 | 状态 | 说明 |
| --- | --- | --- |
| M07 组件组装状态 | ✅ | 新增 `component.slots/filled/failed` 事件。schema 校验失败时显示真实错误，不再把空页面当成品；没有材料时标注 `Example data`。 |
| M08 循环与局部修订 | ✅（基础） | 循环历史逐条列出条件及检查者（代码 / Jev）；`Limit reached` 与 `Goal met` 分开。局部修订产生新的 `operation_id` 和新产出版本。 |
| R01 多维判断 | ✅ 放在开关后面，默认关闭 | 设置 → Behaviour → Advanced → Extra routing dimensions。开启后判断"材料是否完整""材料是否矛盾"：不完整就先补资料，矛盾就升一档。**未在线验证是否带来改善。** |
| R02 产出版本与 `Needs sync` | ✅ | 子任务修订后版本号 +1。直接拼接的整体原地替换；重写过的整体标为 `Needs sync`，点 `Sync now` 只重跑组装这一步（`/api/sync`）。目前没有手工编辑，所以不存在冲突处理。 |
| R03 目标循环条件 | ✅ | `jevharness/goals.py`：字数、词数、包含某词由代码检查，并写明计数规则；其余交给 Jev 判断。所有条件都满足才算 `Goal met`。 |

## 4. P2（只有接口和待办）

`jevharness/evaluation.py`：

- `RunRecord`：对照评测的记录格式。`record_from_events` 和 `records_from_workspace` 从已记录的运行离线生成，未知费用保持 None。
- `Replanner`、`CapabilityCalibration`、`DecisionEngine`：三个 Protocol，未接入，也未实现。

本轮没有实现动态重规划、按领域校准，也没有做付费对照评测。

## 5. 事件协议（`event_version = 1`）

每个 `decision_event` 包含以下字段：

| 字段 | 含义 |
| --- | --- |
| `event_id` | `<run_id>-e<n>`，全局唯一，用于去重 |
| `seq` | 本次运行内单调递增，在队列出口统一分配 |
| `run_id` / `operation_id` / `attempt_id` | 运行 / 初始执行或 `revise-*`、`sync-*` / 同一操作的第几次尝试 |
| `batch_id` / `question_id` / `subtask_id` | 批次（`batch-<stage>-<n>`）/ 题目 / 子任务的稳定 ID（`step-<hash>`） |
| `kind` / `actor` / `stage` | 事件种类 / `jev`、`policy`、`llm` 或 `tool` / 所处阶段 |
| `state_version` / `evidence_set_id` | 判断时看到的状态版本，以及证据快照（`evidence-<n>-<hash>`） |
| `elapsed_ms` | 单调时钟计时，从运行开始算起 |
| `usage` | `{call_id, cost_usd (null 表示未知), cost_source, price_table}` |
| `payload` | 按 `kind` 白名单过滤后的内容，经过截断和脱敏 |

事件种类（`KIND_FIELDS` 定义了每种事件允许的字段）：

- 运行：`run.started`、`run.completed`、`run.failed`、`run.cancel_requested`、`run.cancelled`、`stage.entered`
- 批次：`batch.started`、`batch.completed`、`batch.failed`
- 语言模型：`llm.started`、`llm.completed`、`llm.failed`
- 证据：`evidence.filtered`、`evidence.checked`、`evidence.stopped`
- 路由：`routing.assessed`、`routing.selected`、`routing.unavailable`
- 策略：`generation.skipped`、`policy.applied`、`policy.review_required`
- P1：`output.version`、`component.slots`、`component.filled`、`component.failed`、`loop.iteration`

**兼容映射**：旧事件（`plan`、`assessed`、`research`、`evidence`、`subtasks`、`subtask`、`subtask_delta`、`subtask_log`、`decisions`、`selection`、`composed`、`app_*`、`delta`、`retried`、`done`、`saved`、`error`）全部保留，含义不变。新事件只负责决策状态，不再额外累加费用，也不重复追加产出。旧客户端会忽略 `decision_event`。新客户端遇到没有事件记录的旧回合时，显示原有摘要，并标注 `Detailed trace unavailable`。

## 6. 新接口

| 接口 | 作用 |
| --- | --- |
| `POST /api/run` 增加 `run_id` | 由浏览器生成，服务器按 `^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$` 校验，不合法则重新生成 |
| `POST /api/runs` `action=cancel` | 协作式取消。只对同一工作区内运行中的任务生效。执行器在发起下一次模型调用或工具步骤前检查取消标记；已发出的请求可能仍会计费，界面会这样说明。 |
| `POST /api/runs` `action=list/get/export` | 只读 `<workspace>/.jevia/runs`，run_id 按正则校验，从不当作路径使用；读取时再做一次脱敏，不调用任何模型 |
| `POST /api/sync` | 重写一个 `Needs sync` 的整体：只运行组装这一步 |
| 所有 POST | 跨站（`Sec-Fetch-Site: cross-site`）或 Origin 与 Host 不一致时，一律返回 403 |

## 7. 迁移说明

- **计划缓存**：`<workspace>/.jevia/plans.json` 换成带版本的格式（`{"version": "plans/v2", "plans": {...}}`）。旧文件加载时整份丢弃，因为旧键会丢失数字和顺序，不安全；下次保存时覆盖。代价是每类任务第一次运行会多一次规划调用。
- **浏览器存储**：沿用 `jevia.v5`。新增字段 `motion`、`allow_downgrade`、`routing_extra`，以及回合上的 `run_id`、`ev`、`ev_truncated`、`run_status`。只有最近 8 个对话在浏览器里保存事件；更早的对话只保存 run_id，打开 Decisions 视图时点 `Load full record`，从工作区读回。
- **模型价格**：没有价格的模型现在记为 `priced: false`（未知），不再按免费处理。设置里已有价格的模型不受影响。
- **旧工作区**：旧对话照常打开，显示原摘要并标注 `Detailed trace unavailable`，不会根据旧摘要反推逐题概率。
- **定时任务**：`schedule.json` 格式不变。循环历史新增 `conditions` 字段；旧记录没有这个字段时，仍按原来的单个分数显示。
- **不再做的事**：没有模型达标时不再静默降级；账本不再显示"比直连便宜 N×"，也不再显示对照估算，只显示实际发生的费用、调用次数和耗时。

## 8. 性能（离线合成数据，不是线上测量）

环境：Claude 桌面应用内置浏览器（Chromium 152），8 核，视口 1440×900。

| 1015 条事件（290 个批次，每批 8 题） | 耗时 |
| --- | --- |
| reducer 处理全部事件 | 2.2 ms |
| 构建 Decisions 视图 HTML | 13.7 ms |
| 首次写入 DOM | 8.1 ms，约 3,600 个节点 |
| 状态没有变化时重新打补丁 | 3.2 ms |
| 运行卡片每次重建 | 0.09 ms |

分页之前，同样的数据会生成 6.2 万个节点，现在默认只显示最近 12 个批次、每批最多 24 题，其余点一下再展开。材料列表每轮最多 200 条（后端截断，并显示剩余数量）。**没有测帧率，也不声称 60 fps。**

## 8.5 交付后按你的要求做的调整

- `/demo` 从输入框的斜杠菜单里去掉了，改到 **设置 → Behaviour → Demo replay**（需求要求保留一个用户主动开启的入口）。
- 账本里去掉了"反事实估算"整块，缓存命中为 0 时不再占一列；指标改成自动换行，侧边栏打开时不会重叠。
- 为了"每次都能顺利跑完"，补了三处：
  - 低置信度时上调一档只是安全边际。如果没有模型达到上调后的档位，就放弃这个边际、退回 Jev 实际给出的档位，并在"为什么采用这个结果"里写明。Jev 自己就要求 4 档而你最高只有 3 档时，仍然停下来问你。
  - 运行过程中每隔几秒保存一次。中途关掉页面后重新打开，会显示 **Interrupted**，而不是停在"等待中"或假装已完成。
  - 单步任务的路由卡片现在也显示 Jev 对整体答案的原始评分，不再是 Unavailable。

## 8.6 安全检查

单独做了一次面向用户使用风险的审计，修了 7 处（对话 ID 路径穿越、静态文件前缀判断、重定向后才校验的 SSRF、应用预览可外联、记录文件权限、明文 http 密钥提示、压缩包解压限制），结论和仍然存在的风险写在 [docs/security.md](security.md)。

## 9. 未完成与未在线验证

- **未在线验证**：Jev 真实的延迟和计费字段、真实供应商对 `usage.cost` 的报告方式、R01 是否真有改善、提醒阈值是否合适（这些阈值只是配置，没有校准）。
- **取消**：执行器在下一次调用前停止调度。正在进行的 LLM 流会在下一个 token 到达时断开连接；服务商那边是否停止计费无法确认，界面照实说明。
- **没有录像**：交付物是可以复现的 Demo replay（6 组 fixture，含 1 组失败分支）和本地浏览器检查。需要录像的话，可以用 `scripts/offline_server.py` 或 `/demo` 自己录。
- **P2**：只有接口，见第 4 节。

## 10. 命令

```bash
python3 -m unittest discover -s tests -t .      # 后端，离线
node scripts/ui_tests.mjs                        # 界面 reducer / 动效 / 渲染
node scripts/copy_audit.mjs                      # 中英文案对齐
python3 scripts/make_fixtures.py                 # 重新生成 Demo fixture（离线）
python3 scripts/offline_server.py                # 假引擎服务器，http://127.0.0.1:8766
python3 app.py                                   # 正式服务器，http://127.0.0.1:8765
```
