# 无人值守连写（默认：会话子 agent 循环）

两部分：① 默认形态——宿主主线程一直执行、循环唤起子 agent（无定时器，批界默认关）；② worker 结果契约、恢复与分批接力。备选的常驻 supervisor 见 [unattended-runner.md](unattended-runner.md)（跨断电存活）。

<!-- 一、默认形态：会话子 agent 循环 -->

默认 stage-agent。宿主作为薄调度器，同书串行创建独立空上下文 worker；
每个 worker 只跑一个 action。两种宿主都必须实际提供新模型会话，不能 fork 主会话或
继承聊天历史。

**形态定位（作者裁决）**：本文就是无人值守连写的**默认形态**——宿主会话主线程一直
执行，循环唤起子 agent（每个 stage 一个空上下文 worker），**不需要定时器**：
`chapter next` → 派子 agent → 核验记账 → 下一阶段/下一章，一直跑到 `complete`、
`blocked` 或 `usage_guard` 才停。批界闩锁默认关闭（`host_batch_chapters` 默认 0，
调度卡不再点 boundary、不要求收口换会话）；只有配额按会话计、必须分批的托管平台才
`config set host_batch_chapters 5` 走「批末 handoff + 定时器接力」的兜底结构。
需要跨断电/脱离会话宿主独立存活时改用 `run start` 的常驻 supervisor
（unattended-runner.md）。

## 调度循环

1. 宿主 `chapter next --card`，只收调度卡（动作、相位、停止位、worker 派发指针与记账句柄）；
   全量信封只落盘 stage-action 文件，由 worker 按路径自取——宿主上下文不进信封、不粘贴
   stage-action 内容，状态核对用 `status --card`。不读 pack/正文。
   卡内 `host_batch` 节是宿主上下文的生命周期闸门（**默认关闭**：host_batch_chapters=0
   时整节缺席，主线程直接连跑，无 boundary、无 handoff 义务）；开启时**新会话第一件事**先
   `run handoff --host-session-start` 立批界基线（缺基线时卡带 baseline_missing）；
   acks_since_boundary ≥ host_batch_chapters 时 boundary 在 draft/extend_plan
   决策点亮且**闩锁**——批中普通 `run handoff` 只做收口留痕，不重置计数也不解除 boundary，
   只有接力新会话（run resume / --host-session-start）才开新计数。boundary 点亮后宿主必须
   跑 handoff、追加交接节、然后**结束本会话**（停止调度，由定时器/接力从盘上 HEAD 续跑）；
   禁止在 boundary 下再开新章（宿主上下文单调增长、每阶段全量重读的平方税，实测单会话
   连写 50 章吃掉全 session 六成 token 体量；「handoff 了但继续跑」等于计数自缴械）。
2. draft / polish / assemble / ack：按调度卡创建无历史继承的新会话，
   只给 action_path、唯一角色卡与 worker-protocol §二。worker 写 staging、执行当前提交后退出。
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
   直接跑 `run checkpoint`——与 supervisor 环内同一条确定性检查路，零模型调用，报告落盘
   checkpoints/，CLI 回执只带结论（review_required / blockers / 路径），报告全文留盘上不进
   宿主上下文；仅 review_required 时才派 triage 子代理处置阻断项。triage 子代理预算：模型步 ≤ 30、
   等待一律 shell sleep 不用模型轮询、按报告路径定向取证，不把报告全文或整窗正文读进上下文。
   未决 UNVERIFIABLE/BLOCKER 必须处置。到 complete 后全书审计，向作者报告可完本。

宿主必须从新会话 API 获得真实 session_id，并核对每 job 不同、history 为空。
不得向 worker 注入父会话或前阶段的推理记录；原文与证据通过指定文件和来源路径按需读取，
所选内容完整加载，不使用固定字数裁剪。来源不足时定向查询与补读。
会话原语如果只能 fork，不能声称隔离，应使用可新建空 conversation 的 Driver 或人工逐阶段新任务。
权限隔离另由宿主沙箱提供，共享目录本身无访问隔离。

## 用量与恢复

每模型请求立即记录四分量 delta，request-id=job-id:request序号；缺计量保持 unknown。
完成状态卡只含路径与退出码，自检/quotes 留在 staging；不要复制协议内容制造第二真源。
角色卡与协议卡仍必须读，不能为了提速跳过职责边界。
同书只派一个 worker；中断确认旧会话结束后，宿主从 HEAD 恢复未完成 action，创建新空会话。
显式 worker-agent 兼容模式才按整章派发，其单会话角色切换不提供阶段上下文隔离。

宿主形态不自动拥有进程 driver 的 lease/fence；宿主须自行串行化并核验阶段会话 ID。
需要机器强制 job/action 写能力与会话回执时使用无人值守运行器。仅有 LOCK 不能隔离任意文件读写。
跨会话接力自动化仅在作者要求时创建；托管 worker 不自行创建调度器。

## 写到完本与接力自动化

**平台铁律**：定时任务拉起的会话**不能**再创建定时任务（平台硬拒："Cannot create a
scheduled task inside a session that already belongs to a scheduled task"）。因此
「一次性接力棒 + 会话内自建下一棒」的链式结构**不成立**——被拉起的会话批末
CronCreate 必被拒，链必断。被拉起会话的禁用清单：CronCreate 一律被拒；
CronList 只读轮询无意义（实测曾连发 50+ 次空转烧上下文）。

**唯一成立的结构**：作者主会话只建**一个 recurring 定时器**；每次触发跑**一批**
（批目标有界，见本文第二部分分批接力协议），从盘上 HEAD 续跑。定时器的下一次
触发就是「下一棒」——接力住在宿主侧的排程里，不住在会话内。

**归属语义（实测）**：定时器的每次触发**回到创建它的那个会话**，不是新开一个。
因此「在哪个会话建自动化」决定写作会话落在哪个工作区：书接力自动化必须由
**书工作区的会话**创建；skill 工程/其他工程会话不得代建（fire 会把写作带进错误
的工作区）。每火之间的工作量与上下文增长由批界闩锁控制（handoff 基线 /
acks_since_boundary），批末收口即停线，会话本身长期存在。

- **prompt 配方不手写**：作者会话跑 `run relay-prompt`（可加 `--every-hours N`），从当前
  运行态（HEAD/相位/计划余量/批大小/项目与 CLI 路径）确定性生成自包含接力 prompt，
  落盘 `book/run/relay-prompt.md` 并给出排程建议。手写模板曾混入非法的自建下一棒步骤，
  正是断链根因。
- **每火约束**（已写死在 relay-prompt 里）：开工先核对上一棒——locked 或活跃 job 未清即
  原样退出不写盘；批界 `acks_since_boundary ≥ host_batch_chapters` 即 `run handoff` 收口并
  **停止写作**（下一次触发回到本会话续跑下一批）；禁止 boundary 下开新章；每章进度追加
  unattended-log.md。
- **收尾**：某火跑到 complete 时跑全书审计并报告可完本；此后触发读到 completed
  即空转退出，作者删除定时器。

---

<!-- 二、worker 结果、恢复与接力 -->

Worker 结果契约、协议版本纪律、恢复与停止、宿主时长上限的分批接力、无模型 CLI 降级。run 配置与 Driver 协议见 [unattended-runner.md](unattended-runner.md)。

每次恢复都按当前阶段及未决问题读取任务相关来源。合同、卷纲、摘要和正文证据不设固定字数上限；
较多材料按来源与章节分次读取，保留完整选中证据。运行时长、阶段职责和实际 usageGuard 仍按已签配置执行。
## Worker 结果

stage-agent 宿主必须在 NOVEL_LEDGER_RESULT_FILE 写当前 job 的会话证明：

```json
{
  "status": "success",
  "job_id": "current-job-id",
  "action": "draft",
  "session_id": "host-provided-session-id",
  "context_origin": "empty",
  "usage_records": [
    {"request_id": "current-job-id:1", "usage": {
      "uncached_input_tokens": 1200, "cache_read_input_tokens": 800,
      "cache_write_input_tokens": 0, "output_tokens": 900
    }}
  ]
}
```

session_id 必须来自宿主新会话 API，不得由模型编造。缺会话证明或复用 ID 会暂停；
已迁移 phase 的 job 保留 fence/current_job，恢复也先验回执，不能跳到下一阶段。
usage_records 可省略，但不能带 usage 会话总量。每请求已通过 usage-record 记录时，只重放
相同 ID 和计数/元数据幂等去重；预算保护依赖即时 delta，最终补传不能冒充即时计量。
缺计量保持 telemetry=unknown。legacy worker-agent 的 usage 总量仍仅作兼容统计。

**宿主拿不到逐请求分量、但能看到整单总量时**（stage-agent 子代理的常态）：改记
`chapter usage-record --total-tokens N`（total-only 合法形状，信封 `usage_request.request_id`
回声即幂等）。它计入章节总量并驱动 `stop_total_per_chapter` 熔断——telemetry 不再是
unknown，usage_guard 的总量口径从纸面闸门变成实测闸门。记错（换 ID 重记同一笔）时用
`chapter usage-void --request-id <rid> --reason <why>` 作废：append-only 更正通道，
聚合口径剔除被作废样本；禁止「再记一笔负数/直接无视」等其他更正方式。

**宿主连总量都拿不到时**（无头 CLI 常不给 token）：沿用 `telemetry=unknown`，
**不得补零**（补零等于伪造"这章几乎不花钱"）。此时 `usage_budget` /
`usage_guard` 属于**未实测闸门**：它仍然会被 `chapter usage-record` 驱动并正常
拦截，但没有真实数据流过，阈值（默认 warn 300k / stop 500k input per chapter）只是
纸面数字。报告成本结论时注明"telemetry unknown，阈值未实测"，或先补一条真实
`book/run/usage.jsonl` 再谈回归；不要拿"闸门存在"当成"成本已被控制"。

## 协议版本纪律

派发提示词带一行 `协议版本：<版本>`（真源是 `control/pipeline.py` 的 `WORKER_PROMPT_PROTOCOL`
常量，`chapter next` 的 worker 简报 `worker_protocol` 字段与 supervisor 的
`build_worker_prompt` 共用同一常量）。worker 开工第一步核对两处版本一致：

- **不一致 = 宿主装了新旧两个 skill 实例混跑**（例如 supervisor 用旧安装派发、worker 解析到
  新安装的 `SKILL_ROOT`，或长跑中途 skill 被升级）。此时立即停止并如实报告，不要继续写作——
  两套派发协议对 staging/简报的理解可能不同，继续写会产生无法解释的半成品。
- 派发协议发生不兼容改动（简报字段语义、hygiene 规则、阶段边界）时，维护者先升
  `WORKER_PROMPT_PROTOCOL`，再改协议；版本行是运行中实例分叉的唯一可见信号。

## 恢复与停止

- `extend_plan` 独占一个新会话；成功后结束，再创建独立 draft 会话。
- 阶段 worker 从当前 action 文件恢复，提交通过或 phase 改变即退出；只有调度器再次 next。
- worker 正常退出但 HEAD/计划无目标进展，记 `no_progress`；基础设施失败最多重试三次。
- supervisor 崩溃后，已迁移 phase 的 job 先校验 session 证明，再确认完成；确认旧 command 进程已死才从当前 phase 新建恢复会话。
- 无法确认是否仍存活的 Sidecar job 一律暂停，避免并发写同章。
- `blocked`、`story_review_blocked`、`usage_guard`、契约损坏、重试耗尽均暂停。剧情复核 fix 不能靠重交 pass 清除，
  须先修订正文并重新确认，或显式修订叙事合同，使输入版本改变后重新复核。批级或卷界检查点出现明确问题时以
  `review_required` 暂停；先按报告路径修复，再 `run resume` 重查，不会跳过未通过的检查点。
- `story_review` 每次使用独立空会话；回执已落盘而 HEAD 标记未写完时，可原样重交完成幂等恢复。
- 正常 `complete` 前先补齐卷审和终局复核。HEAD 返回 `complete` 时，supervisor 跑一次全书审计并将完整报告保存到
  `book/run/completion-audit-*.json`。审计命令的 `ok=true` 只表示成功产出报告；只有账本、
  引文、哈希、术语、衔接、数词、派生摘要、正典与大纲等硬问题均清洁，运行态才记
  `completed`。否则以 `completion_audit_failed` 暂停，`run resume` 后重审；运行态只保存
  报告路径和短问题清单。正常连写在合同、卷审、终局审与机检均通过后调用严格 `book complete`，
  同步写入全书 completed 状态。作者提前封笔只返回 author_early_close，不报正常完本。

## 宿主单次运行时长上限：分批接力协议

**现象**：交互宿主的托管运行（如 ZCode 闲时任务）有单次最长运行时间，超时被硬杀——
报「已超过单次最长运行时间，请创建新的闲时任务继续」。把批目标定成「一次跑到卷末/
全书」必然撞墙（百章级 ≈ 数十小时模型时间）。这属于宿主配额边界，不是本书或工具缺陷。

**安全性**：状态真源全在盘上（HEAD 相位、账本、pack、进度日志）。硬杀点任意——
包括章中——都不腐蚀状态：HEAD 相位即恢复点，staging 半成品可原位续修，
quality.jsonl 的 begin 悬空是良性残留（详见 [dispatch.md](dispatch.md) 第一部分的中断重派 runbook）。
损失只是被杀那半章的算力。

**分批协议（每批 = 一个宿主任务，批内由调度器串行派发阶段 worker）**：
1. **批目标有界**：N 章（建议 5）或时间盒（建议 ≤ 观察安全时长的一半），先到为止；
   禁止把「写完一卷/全书」设为单批目标。会话宿主的 goal/任务原语按轮计时：
   每章含返工按 12–15 轮估，goal 轮数 ≥ 15×批章数＋5，轮数不够就把批改小。
2. **章边界收口**：每章 `ack-read` 后检查剩余预算；不足下一整章（约 12 轮）就在此处干净停
   （更新进度日志、回报批次摘要），绝不开始下一章的 draft——宁可少写一章，
   不浪费半章算力。
3. **每章更新进度日志**（`book/run/unattended-log.md` 或等价物），不要攒到批末——
   硬杀后下一批完全靠日志恢复。
4. **交接卡**：批末或任何中止点先跑 `run handoff`（机器段：HEAD/下一动作/逾期钩子/
   开放 findings/日志尾部，落盘 `book/run/handoff.md`），宿主再把批目标、剩余轮预算与
   本批裁决摘要追加到 unattended-log.md 的「## 交接」节。恢复会话第一步＝读交接卡 +
   `status` 核对，然后 `chapter next` 续跑。
5. **接力首选会话内循环连写**：宿主主线程一直执行、循环唤起子 agent（批界默认关、
   无定时器；即本文第一部分），跑到 complete/blocked
   为止；会话崩溃就从盘上 HEAD 续跑。**次选** `run start` 常驻 supervisor（跨断电存活，
   见 unattended-runner.md）。**兜底**才是定时器接力——只用于配额按会话计、必须分批的
   托管平台：下一批由用户手动新建，或由**书工作区会话**自建的 **recurring** 定时器接力
   ——实测触发回到创建定时器的那个会话，所以「在哪建」决定写作落在哪个工作区；
   闲时任务/托管运行自身不得再创建闲时任务或自动化（配额与递归风险；且平台硬拒
   定时任务会话内再建定时任务——「自续命接力链」不成立）。
   （relay prompt 用 `run relay-prompt` 从运行态确定性生成，配方与平台约束见
   本文第一部分「写到完本与接力自动化」节。）
6. **extend_plan 同批可做但独立成 worker**：低水位提示幂等（`plan_low_water_nudged_at`
   防重发），预算不够就留给下一批，无副作用。
7. **批次闸门**：每 10 章的机器检查点核对最近 10 章文件、确认回执、质量日志与当前账本，
  不重跑全量 `book audit`。逾期伏笔触发 `review_required` 后，用 `hooks audit` 查明细，
  核对正文——已演出则 close（reason 注章号），未演出则 defer 或排进下批。关系身份冲突
  由总编辑按 `book audit` 或账本复核结果裁决；卷界按滚层摘要与报告核对人物选择、代价、
  主支线兑现及世界规则，由独立 story_review 会话提交通过回执后再进入下一卷。

批目标有界是**宿主配额边界，不是写作边界**：默认的会话内循环连写（或备选 supervisor /
兜底定时器接力）都能一直写到完本（卷界自动派发剧情复核，通过后续写，见
本文第一部分）。

## 本机没有任何可无头调用的模型 CLI 时

Driver 接不上线（`zcode/codex/claude/gemini/agentapi` 全部不在 PATH）时，`run start` 无法接线，
先别硬凑。三条正规降级路径（按成本优先级）：

1. **逐阶段人工新会话**：调度器 next，人在空上下文中完成当前阶段提交后结束，再新开下一阶段。
2. **宿主 stage-agent**：每 action 一个不继承父历史的新会话，同书串行，只派路径。
3. **显式兼容降级**：worker-agent（整章）或 inline（角色切换）只提供职责隔离，报告能力缺口。
   inline-compact 允许压缩后续章，但压缩不等于独立上下文。不能将兼容模式称为隔离。

判别命令：对每个候选 CLI 跑 `command -v <cli>`，全部落空即命中本节；不要用半接线的 driver
空转重试。
