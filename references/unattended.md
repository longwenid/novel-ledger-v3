# 无人值守连写（会话子 agent 循环）

唯一形态：宿主会话**主线程一直执行、循环唤起子 agent**（每个 stage 一个空上下文
worker）。`chapter next` → 派子 agent → 核验记账 → 下一阶段/下一章，一直跑到
`complete`、`blocked` 或 `usage_guard` 才停。本文覆盖调度循环、检查点、用量与恢复、
有界批纪律与降级路径。

默认 stage-agent。宿主作为薄调度器，同书串行创建独立空上下文 worker；
每个 worker 只跑一个 action。宿主必须实际提供新模型会话，不能 fork 主会话或
继承聊天历史。

## 调度循环

1. 宿主 `chapter next --card`，只收调度卡（动作、相位、停止位与 worker 派发指针）；
   全量信封只落盘 stage-action 文件，由 worker 按路径自取——宿主上下文不进信封、不粘贴
   stage-action 内容，状态核对用 `status --card`。不读 pack/正文。
2. draft / polish / assemble / ack：按调度卡创建无历史继承的新会话，
   只给 action_path、唯一角色卡与 worker-protocol §二，并注明「不加载 SKILL.md /
   写作模式入口」（worker 与宿主共享 preset 时，入口指令会对每个 worker 重放，
   必须豁免——见 dispatch.md 派发配方第 0 项）。派发为 one-shot：worker 写 staging、
   执行当前提交、回报一行机器行状态卡后即退，不留可续会话。完整报告（verdict/quotes/
   自检明细）在 staging 报告文件里，宿主不收全文；超长回报按 dispatch.md runbook 拒收重派。
   ack 必须由新终审会话读 chapter_path 并摘取引文；CLI ack-read 不会代替模型阅读。
3. 宿主 `status --card` 核对预期 phase；only then 再 next。提交返工按 submit 回执的
   `recovery.route` 分两路：`delta_only` / `one_point_prose` 走零模型宿主路径——按 steps 执行
   （rework-patch 锚定替换或保留稿直接 draft-submit → next 回组装 → 按 violation hint 对齐
   delta 字段 → submit），**不派返工 worker**；`redraft_worker` 才另建会话，传当前失败清单及
   证据来源路径。组装文件修改必须等 `chapter next` 回到 await_assembly 之后（提前改会吃
   wrong_phase）。诊断以 CLI 回执为准（violations 自带近失对照与 remedy hint）——宿主与
   worker 一律不读 skill 源码反推语义。
4. extend_plan：独立 plan worker 按 §三，在已签脊柱与预算授权内候选择优、起草 phase、
   plan extend 原子提交后退出；更改书级方向/红线则提出方案等待总编辑/作者裁决。
5. story_review：卷界与收尾均须派发独立空上下文剧情审稿会话，按协议卡 §六提交带正文证据的回执；
   通过才继续，fix 则停线。批级机器检查与逐章 ack 均不能替代此复核，见 [剧情复核](story-review.md)。
6. blocked / story_review_blocked / usage_guard / 契约或检查点异常停并报告；每 10 章在章界由宿主
   直接跑 `run checkpoint`——零模型调用，报告落盘 checkpoints/，CLI 回执只带结论（review_required
   / blockers / 路径），报告全文留盘上不进宿主上下文；仅 review_required 时才派 triage 子代理处置
   阻断项。triage 子代理预算：模型步 ≤ 30、等待一律 shell sleep 不用模型轮询、按报告路径定向取证，
   不把报告全文或整窗正文读进上下文。未决 UNVERIFIABLE/BLOCKER 必须处置。到 complete 后全书审计
   （`book audit` + `book complete`），向作者报告可完本。

宿主必须从新会话 API 获得真实 session_id，并核对每阶段会话不同、history 为空。
不得向 worker 注入父会话或前阶段的推理记录；原文与证据通过指定文件和来源路径按需读取，
所选内容完整加载，不使用固定字数裁剪。来源不足时定向查询与补读。
会话原语如果只能 fork，不能声称隔离，应使用可新建空 conversation 的宿主或人工逐阶段新任务。
权限隔离另由宿主沙箱提供，共享目录本身无访问隔离；宿主须自行串行化同书写者。

## 批级与卷界机器检查点

每完成 10 章、或下一章进入新卷时，宿主在派发下一章前于章界调用
`run checkpoint`，写 `book/run/checkpoints/ch-NNNN-*.json`：与 last_checkpoint_ch
游标共用（幂等，非到期章返回 skipped），机器部分零模型调用——CLI 回执只带结论与路径
（报告全文留盘上，不回显进宿主上下文），仅 review_required 才派 triage 子代理。
批级检查点另核对**批次候选留痕**：待写批次（首章>3）没有 `plan.batch_select`
治理事件即以 `plan_batch_selection_missing` 暂停（该批扩纲没做候选择优，报告
`batch_plan_review` 节汇总本窗口择优）。检查按当前批窗口读取
正文、meta、摘要与 ack，定位该批完整质量记录与提交事件并核对哈希链；
卷界报告另外引用当前卷的完整滚层摘要、卷纲和下卷合同。
读取不设单文件字节上限，也不按日志尾部字节数漏掉窗口内事件。诊断报告可按章或记录分页，
选中的摘要、资产与证据保留全文和来源路径。`run status` 的 `last_checkpoint`
只保存报告路径与结论；`autopilot-events.jsonl` 记录 `quality_checkpoint`，卷界额外记录
`volume_review`。

缺少已确认章节的文件或质量回执、哈希/字数/引文不一致、快照章号不符、伏笔已逾期，
以及已签卷体系中下卷仍未签脊柱，会使检查点以 `review_required` 暂停运行态。
处置后执行 `run resume`，同一检查点会重新检查；有问题的报告不会推进检查游标。
返工率、人物选择与代价、主支线兑现和世界规则属于卷界复核议题：机器报告提供相关证据与
复核信号。独立剧情审稿会话必须按 [剧情复核](story-review.md) 提交与当前正文及合同绑定的证据回执；
通过才跨卷，fix 自动停线。原有项目首次接入时机器检查只覆盖最近 10 章，并明确标明历史范围；
历史卷的剧情复核仍须补齐，完本须再通过全书终局复核与机器审计。

## 低水位提示的生命周期

`chapter next` 在剩余章拍 ≤ `plan_low_water` 时返回一次 `action=extend_plan`（一次性标记
`plan_low_water_nudged_at` 记录当时的 max_planned）。**扩纲跨度由 `config plan_extend_span`
统一下达**（默认 20；`"volume"`=按卷合同 chapters_budget 一次签满当前卷，当前卷已签满则
整签下一卷，无卷合同回退 20）：回执与 plan 简报以 `extend_through_ch` 给出覆盖契约，
worker 必须从 suggest_from 一路编拍到该章，**不贴水位线补章**——实测贴线补章 63 章烧 13 轮
扩纲 ≈109 万 token（每轮的钱≈写一章）。跨度 >20 章时协议卡规定同一 plan worker 会话内
拆 ≤20 章候选批连续 `select-batch → plan extend`（spawn 一次，候选批机检不变）。
plan worker 失败后处置完再 `run resume`：恢复会在写锁内**重置未兑现的标记**
（标记仍等于当前 max_planned 才清，事件 `plan_nudge_marker_reset`），让下一次
`chapter next` 重新提示扩纲——「扩纲优先」不会被一次性标记静默吞掉；计划已增长过的
旧标记保持原语义。

## 用量与记账

每模型请求立即记录四分量 delta，request-id 唯一，禁止汇总重复计量；缺计量保持 unknown。
完成状态卡只含路径与退出码（一行机器行，见 dispatch.md Lean Transcript），自检/quotes
留在 staging 报告文件；不要复制协议内容制造第二真源。
角色卡与协议卡仍必须读，不能为了提速跳过职责边界。

**批末转录预算核对**：每批收口（`run handoff` 前）宿主只读一次 `status` 的 usage 窗口
（最近 12 章的 `input_per_chapter` / `max_input_per_chapter` / `by_chapter`），把本批
四分量汇总与趋势追加进 unattended-log.md；单章 input 持续走高即先排查超长回报或
重复派发，再继续下一批。窗口外的全量明细留在 `book/usage.jsonl`，不进宿主上下文。
同书只派一个 worker；中断确认旧会话结束后，宿主从 HEAD 恢复未完成 action，创建新空会话。

**宿主拿不到逐请求分量、但能看到整单总量时**（stage-agent 子代理的常态）：按信封
`usage_request.request_id` 改记 `chapter usage-record --total-tokens N`（total-only 合法形状，
回声同一 request_id 即幂等）。它计入章节总量并驱动 `stop_total_per_chapter` 熔断——
telemetry 不再是 unknown，usage_guard 的总量口径从纸面闸门变成实测闸门。记错（换 ID 重记同一笔）时用
`chapter usage-void --request-id <rid> --reason <why>` 作废：append-only 更正通道，
聚合口径剔除被作废样本；禁止「再记一笔负数/直接无视」等其他更正方式。

**宿主连总量都拿不到时**（无头 CLI 常不给 token）：沿用 `telemetry=unknown`，
**不得补零**（补零等于伪造"这章几乎不花钱"）。此时 `usage_budget` /
`usage_guard` 属于**未实测闸门**：它仍然会被 `chapter usage-record` 驱动并正常
拦截，但没有真实数据流过，阈值（默认 warn 300k / stop 500k input per chapter）只是
纸面数字。报告成本结论时注明"telemetry unknown，阈值未实测"，或先补一条真实
`book/run/usage.jsonl` 再谈回归；不要拿"闸门存在"当成"成本已被控制"。

## 协议版本纪律

派发提示词带一行 `协议版本：<版本>`（真源是 `control/pipeline/_dispatch.py` 的
`WORKER_PROMPT_PROTOCOL` 常量，`chapter next` 的 worker 简报 `worker_protocol` 字段
携带同一常量）。worker 开工第一步核对两处版本一致：

- **不一致 = 宿主装了新旧两个 skill 实例混跑**（例如宿主派发用旧安装、worker 解析到
  新安装的 `SKILL_ROOT`，或长跑中途 skill 被升级）。此时立即停止并如实报告，不要继续写作——
  两套派发协议对 staging/简报的理解可能不同，继续写会产生无法解释的半成品。
- 派发协议发生不兼容改动（简报字段语义、hygiene 规则、阶段边界）时，维护者先升
  `WORKER_PROMPT_PROTOCOL`，再改协议；版本行是运行中实例分叉的唯一可见信号。

## 恢复与停止

- `extend_plan` 独占一个新会话；成功后结束，再创建独立 draft 会话。
- 阶段 worker 从当前 action 文件恢复，提交通过或 phase 改变即退出；只有调度器再次 next。
- worker 正常退出但 HEAD/计划无目标进展，记 `no_progress`；基础设施失败最多重试三次。
- `blocked`、`story_review_blocked`、`usage_guard`、契约损坏、重试耗尽均暂停。剧情复核 fix 不能靠重交 pass 清除，
  须先修订正文并重新确认，或显式修订叙事合同，使输入版本改变后重新复核。批级或卷界检查点出现明确问题时以
  `review_required` 暂停；先按报告路径修复，再 `run resume` 重查，不会跳过未通过的检查点。
- `story_review` 每次使用独立空会话；回执已落盘而 HEAD 标记未写完时，可原样重交完成幂等恢复。
- 正常 `complete` 前先补齐卷审和终局复核。HEAD 返回 `complete` 时，宿主跑一次全书审计
  （`book audit`）并将完整报告保存到 `book/run/completion-audit-*.json`，硬问题清洁后调用严格
  `book complete` 同步写入全书 completed 状态。审计命令的 `ok=true` 只表示成功产出报告；只有账本、
  引文、哈希、术语、衔接、数词、派生摘要、正典与大纲等硬问题均清洁才算通过（`book complete`
  本身会再跑同一套阻断核对）。作者提前封笔只走 `book close-early --author-confirmed`，
  记录为 early_close，不报正常完本。
- 会话崩溃或被硬杀后：恢复会话先 `status`（必要时 `ledger verify`），再读交接卡
  （`book/run/handoff.md`，由 `run handoff` 生成）与 `book/run/unattended-log.md` 的交接节，
  然后从盘上 HEAD 续跑，不做聊天考古。

## 宿主单次运行时长上限：有界批纪律

**现象**：交互宿主的托管运行（如闲时任务）有单次最长运行时间，超时被硬杀。
把批目标定成「一次跑到卷末/全书」必然撞墙（百章级 ≈ 数十小时模型时间）。这属于宿主
配额边界，不是本书或工具缺陷。

**安全性**：状态真源全在盘上（HEAD 相位、账本、pack、进度日志）。硬杀点任意——
包括章中——都不腐蚀状态：HEAD 相位即恢复点，staging 半成品可原位续修，
quality.jsonl 的 begin 悬空是良性残留（详见 [dispatch.md](dispatch.md) 第一部分的中断重派 runbook）。
损失只是被杀那半章的算力。

**有界批纪律（每批 = 一个宿主任务，批内由宿主串行派发阶段 worker）**：
1. **批目标有界**：N 章（默认 20）或时间盒（建议 ≤ 观察安全时长的一半），先到为止；
   禁止把「写完一卷/全书」设为单批目标。会话宿主的 goal/任务原语按轮计时：
   每章含返工按 12–15 轮估，goal 轮数 ≥ 15×批章数＋5，轮数不够就把批改小。
1b. **宿主上下文预算触发**：宿主上下文逼近窗口（按 1M 级模型记 **≥600k tokens**；
    其他窗口按 60%）或本批达到章数上限，就在当前章 ack 后立即 `run handoff` 章界
    收口换会话，不等被动压缩——实测 73 章连跑宿主涨到 755k，按每章 +60k 估，压缩
    一旦触发会丢掉全部已建立的 KV 缓存与调度手感。触发即收口是纪律，不是建议。
2. **章边界收口**：每章 `ack-read` 后检查剩余预算；不足下一整章（约 12 轮）就在此处干净停
   （更新进度日志、回报批次摘要），绝不开始下一章的 draft——宁可少写一章，
   不浪费半章算力。
3. **每章更新进度日志**（`book/run/unattended-log.md` 或等价物），不要攒到批末——
   硬杀后下一批完全靠日志恢复。
4. **交接卡**：批末或任何中止点先跑 `run handoff`（机器段：HEAD/下一动作/逾期钩子/
   开放 findings/日志尾部，落盘 `book/run/handoff.md`），宿主再把批目标、剩余轮预算与
   本批裁决摘要追加到 unattended-log.md 的「## 交接」节。恢复会话第一步＝读交接卡 +
   `status` 核对，然后 `chapter next` 续跑。
5. **下一批由作者开启**：新会话（同一书工作区）从盘上 HEAD 续跑即可；被拉起的
   会话不得再创建自动化（平台硬拒定时任务会话内再建定时任务，配额与递归风险
   由作者侧控制）。
6. **extend_plan 同批可做但独立成 worker**：低水位提示幂等（`plan_low_water_nudged_at`
   防重发；失败后 `run resume` 重置未兑现标记），预算不够就留给下一批，无副作用。
7. **批次闸门**：每 10 章的机器检查点核对最近 10 章文件、确认回执、质量日志与当前账本，
  不重跑全量 `book audit`。逾期伏笔触发 `review_required` 后，用 `hooks audit` 查明细，
  核对正文——已演出则 close（reason 注章号），未演出则 defer 或排进下批。关系身份冲突
  由总编辑按 `book audit` 或账本复核结果裁决；卷界按滚层摘要与报告核对人物选择、代价、
  主支线兑现及世界规则，由独立 story_review 会话提交通过回执后再进入下一卷。

批目标有界是**宿主配额边界，不是写作边界**：内循环连写能一直写到完本（卷界自动派发
剧情复核，通过后续写）。

## 宿主开不了空上下文子会话时

宿主必须能新建空上下文模型会话；只能 fork 的会话原语不能声称隔离。三条降级路径
（按成本优先级）：

1. **逐阶段人工新会话**：调度器 next，人在空上下文中完成当前阶段提交后结束，再新开下一阶段。
2. **宿主 stage-agent**：每 action 一个不继承父历史的新会话，同书串行，只派路径。
3. **显式兼容降级**：worker-agent（整章）或 inline（角色切换）只提供职责隔离，报告能力缺口。
   inline-compact 允许压缩后续章，但压缩不等于独立上下文。不能将兼容模式称为隔离。
