# 派发手册（总则 · 各角色要点）

两部分：① 派发配方、贯穿判据、单线/瘦身铁律、worker 重派 runbook、返工断路器与模型档位；② 各角色手工派发要点。

<!-- 一、派发总则 -->

派发配方、判据、路径约定、单线/瘦身铁律、worker 重派 runbook、返工断路器与模型档位。各角色派发要点见本文第二部分。

> **派发 prompt 的配方（照这五项写，就不会漏、也不会越界）**：
> 1. **读取哪些路径**——手册与工作包是唯一真源；
> 2. **内容冻结边界**——本阶段动什么、不动什么；
> 3. **输出路径与输出格式**——产物写到哪、长什么样；
> 4. **完工前自检要回什么**——本卡自检清单的逐项结论；
> 5. **完成回复给哪些证据路径**——命令退出码、字数、逐字 quote、verdict JSON。

> **三条贯穿全角色的判据**：
> - **职责域**：执笔只写情节、文风只改文风、事实只提结构、终审只核完整性——各域只判本域。
> - **引用真子串**：凡需机检校验的引用（`quote` / `quotes` / `target`）必须是正文中的精确连续子串
>   （Exact Substring），一字不差。
> - **证据优先**：审校角色的判据只来自被审交付物的直接核验，前序角色的完成汇报不作为放行依据。
>
> 若起草 prompt 时准备写下手感、禁词、句长、标点、收尾或任何指标数值，**停下**——
> 那些条目的真源是**当前阶段的手册字段**：执笔读 `voice_content_text`，润色读
> `voice_writing_text`。随包文风的 `<id>.md` 同时用于文风解析与润色，
> `<id>.content.md` 给执笔；两个阶段各读自己的视图。
> 文风硬红线由 `chapter polish-submit` 的机检收口，已没有独立文风审校视图。

> 路径约定：以下示例中的 `$PROJECT` 是目标小说项目根目录（其下才有 `book/pack`、`book/staging`、
> `book/chapters` 等），`$SKILL_ROOT` 是本 skill 目录（随包文风手册位于 `references/voices/<id>.md`）；
> 章号、项目与文风按实际替换，**切勿原样照抄**示例里的项目专属路径或条目。

> **阶段隔离铁律**：服从 execution 信封。默认 stage-agent：每 action 一个空上下文 worker，
> 禁止 fork/history inheritance/前阶段摘要；只派 action_path、当前角色卡和协议卡。
> 同书串行；阶段通过或 phase 改变即退出，由调度器再次 next。兼容 worker-agent/inline
> 只提供职责隔离，使用时记录降级。文件访问隔离需要宿主沙箱。

> **正文落盘与回复瘦身铁律（Lean Transcript Protocol）**：
> - **正文在盘不在窗**：草稿与终稿正文直接写入目标 staging 文件，**严禁在任何回复或 tool 结果中输出/回显正文全文**！
> - **汇报只给状态卡（≤100 字）**：仅汇报 1) 产物路径与字数；2) 拍点/must 覆盖状态；3) CLI 退出码；4) 自检结论。违反此项回显全文者，总编辑一律拒收！

> **worker 中断重派 runbook**：
> - 章节 worker 中途死掉（超时/错峰票过期/宿主重启）：**从 HEAD 恢复当前阶段并创建新空会话**。新 worker 的第一个动作
>   就是 `chapter next`——HEAD 落盘相位是唯一真源，控制面会按当前 phase 重发对应 action
>   （await_draft/await_polish/await_assembly 重发任务；submitted 先补确定性 commit）。
> - `book/run/quality.jsonl` 里 begin 无后续是**良性残留**（schema 本就没有 end 事件，
>   重复 begin 合法），不影响重派与闸门。
> - staging 半成品不清理：polish/assemble 阶段明确允许在原文件上继续修。
> - **不要**调 `retry-authorize`（只适用于 blocked 或宿主批准的末章整章重写）、
>   **不要**调 `book reopen`（只适用于撤销 book complete）。
> - skill 升级后（control_fingerprint 变化）首次 `chapter next` 若报 stale_pack：按返回
>   action 的提示重建 pack 后 `--force` 走完机器门（机器门一条不少）。
> - 同一本书同一时刻仍只允许一个会话动刀：重派前确认旧 worker 会话确已终止。

---

## 8. 返工硬断路器与完成前验证（Hard Circuit Breaker & Verification）

### 1. 完成前验证铁律（Verification Before Completion）
- **核心原则**：`NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE`。
- 子角色回复“已完成”、“已修复”前，必须在当前步骤输出最新实测证据（如：正文实际字数、逐字提取自正文的 quote 精确子串、自检 checklist 无违约声明）。
- 总编辑严禁接受“应该通过”、“按理已经满足”等无证据假设，必须验证机器命令返回的 `{ok: true}` 与退出码为 0。

### 2. 返工硬断路器与降级机制（Hard Circuit Breaker）
长跑或写章中若某一环节验收失败（如情节不合理、文风机检违规、事实字段缺失）：
- **返工硬上限 ≤ 2 轮**：单章重修严禁超过 2 轮，严禁循环往复消耗 Token；
- **局部微调强制走 `chapter patch`**：
  凡属台词微调、漏带 must 关键词、单句禁词消除、字数微调，**一律强制派快速编辑执行 `chapter patch` 在终稿就地修润并自愈**，严禁推翻草稿重跑整链！
- **plot_fix 返工轮单句修走 `chapter rework-patch`（零模型轮次）**：
  组装 BLOCKER 触发 plot_fix（轮转旧稿、回 await_draft）后，若修复只是单句/局部口径，
  宿主直接 `chapter rework-patch --target ... --replacement ...`（恢复轮转稿+锚定替换，确定性
  操作），再 `chapter draft-submit`（阶段闸一个不少照跑）→ `chapter next` 回组装——
  **不开返工 draft worker、不重读写作简报**。只有结构性重写才派返工 draft worker。
  但 prose 一经改动，旧组装件即作废：其 `plot_findings` 是组装 worker 对改动前正文的
  一次性判断，补 quote 后重提会被 `stale_plot_findings` 拒收（prose_hash 不符）——
  `chapter next` 后必须**重派一个全新组装 worker** 重建 submit JSON，严禁恢复/手改
  旧 assembly JSON 重提（实测：过期 BLOCKER 无限回收，章死锁）。仅 delta 字段形状
  类拒绝（prose 未动）才允许只改 delta 后重提。
- **超限三段式裁决（Rulings, not loops）**：
  若第 2 轮仍未收敛且不违背作者红线，总编辑必须执行三段式留痕裁决（Ruling）收口，任何硬闸未通过则 blocked；记录理由不能绕过机检，严禁进入第 3 轮重试；若涉及不可调和硬伤，则直接标记 `blocked` 挂起；
- **物理执行唯一**：同书串行，阶段交接时换新空会话，worker 不创建其它 worker。

### 3. 总编辑三段式裁决留痕（Rulings, not stalls）
面对不影响主线逻辑的细微瑕疵或边界分歧，总编辑敢于决策，以标准化格式记录到 `$PROJECT/book/editorial/decisions.jsonl`：
```text
Ruling: <裁决内容> — <依据条款与理由> — <若裁决错误的代价与回滚成本>
```
例：
`Ruling: 将结尾外景补成一场有后果的戏 — 本章原稿低于字数硬闸且转折缺少旁人反应 — 若节奏变慢，后续用 patch 删去重复说明。`

---

## 9. 派发模型分级与成本纪律（Model Tiering）

**每个 job 派发时确定模型档位。** 不指定等于继承主会话的默认模型——通常是最强、最贵的一档，
成本与墙钟时间会静默失控；而绝大多数生产角色的活是"按工作包把该写的写出来"，不需要最强档。

**档位的物理边界**：默认 stage-agent 每阶段一个新会话，按 job 选择模型。
宿主无模型覆盖参数时如实记录继承，不能为凑档位取消上下文隔离；skill 不猜平台模型参数。
legacy worker-agent 的 ack 与生产同模型，明确记录降级。
独立 plan job 始终使用新会话；新阶段的 phase 可在总编辑已签脊柱与预算范围内受托起草。

### 档位表（按 job）

| 档位 | 适用角色 / 场景 | 理由 |
|---|---|---|
| **最强档** | 独立 ack / 策划 / 创意 job、按需卷级或全书复核、疑难裁决 | 需要设计判断或跨卷全局观 |
| **标准档** | 独立执笔、润色、组装阶段 worker | 有明确工作包与自检契约，但涉及多场戏的连贯判断与文字质感 |
| **经济档** | 快速编辑（`chapter patch` 段落替换）· 单条 `must` 漏带的定点补写 · 章拍条数/字段格式的机械核对 | 单文件、单点、规格明确，判断量低 |

### 四条纪律

1. **轮次比单价贵**：墙钟与上下文成本随子代理的**轮数**放大，便宜的模型在多步任务上常多花 2~3 倍轮次。
   只有"工作包已给全、剩下就是誊写＋自检"的活才降到经济档。
2. **返工收敛**：阶段机检允许原位修；正文级整链返工达到 `rewrite_limit` 即 blocked，
   由总编辑按错误证据裁决。局部文字问题用 `chapter patch`，不要为组装字段错误重跑正文。
3. **终审与复核**：ack 在独立终审会话完成；卷级、书级剧情复核若触发，则独立派最强档。
4. **同形小活批量派**：同一章内若干处同类型的小修（同一类禁词、同一处术语归一化）合并成**一次**派发，
   在同一个 prompt 里列出全部位置；逐处单独派发会重复建上下文、重复读工作包。

### 与流水线的关系

单一 compact 泳线（四位执行角色），模型档位决定每个独立 job 使用的模型。
预算集中在执笔与文风两个产出正文的工位上；情节自检随组装走、文风硬红线由机检兜底。
---

<!-- 二、总编辑派发全角色 Prompt 要点 -->

各角色的派发要点。派发配方、贯穿判据、单线/瘦身铁律、worker 重派 runbook、
返工断路器与模型档位见本文第一部分。

## 1. 执笔编辑（DRAFTING-EDITOR）

**默认不手写整段派发 prompt**：stage-agent 模式下每阶段新会话 worker 按 action 文件指到的
角色卡 `agents/roles/drafting-editor.md` 执行；worker-agent/inline 兼容模式按同一卡执行但无阶段上下文隔离。角色卡就是
本角色的派发契约（读取路径、冻结边界、输出、自检、状态卡），本节只留手工重派单阶段时的要点：

- 读取路径：仅 `draft_pack_path`（`writing_brief` 完整句式任务书；如配有内容层指南
  `voice_content_text` 一并只读它）；
- 内容冻结边界：阶段一声明、单调向前、每 beat 当场发生有来往有未完、直接入戏；
  文风硬红线不进本阶段 prompt——真源是 polish 包内 `voice_writing_text`，写一句指向即可；
- 输出：纯文本草稿到 `draft_output_path`，无 JSON、标题或元数据；字数以包内 `word_band`
  与 `aim_chars` 为准，缺口一次性补整场戏；
- 自检与证据：角色卡自检清单逐项核对；`chapter draft-submit` 作正式机检；
- 回复：≤100 字状态卡（路径、字数、beats/must 覆盖、退出码）。

---

## 2. 文风编辑（VOICE-EDITOR）

同上：运行时契约在 `agents/roles/voice-editor.md`。手工派发要点：

- 读取路径：草稿 `draft_output_path` + `polish_pack_path`（`voice_writing_text` 是唯一文风
  真源）；如响应附带 `style_metrics_path`（机检 ✗ 清单）或 `polish_anchor_path`（上轮锚点
  失败清单）则一并只读、只定向修清单项；
- 重构边界：内容冻结（事件因果与 must 锚点保留），场景级重构（Scene-level Rewrite）；
  must 词以包内 beats 为唯一来源，不硬编码进 prompt；
- 输出：纯文本终稿到 `polished_output_path`；`chapter polish-submit` 作正式机检；
- 回复：≤100 字状态卡（路径、字数、自检证据文件路径、退出码）。

---

## 3. 事实/组装编辑（LEDGER-EDITOR）

同上：运行时契约在 `agents/roles/ledger-editor.md`。手工派发要点：

- 读取路径：`assemble_brief_path`（一次读全的线性任务书：拍点、expected_delta、连续性、
  账本投影、名字白名单、提交模板与字段形状）+ 终稿 `polished_output_path`（正文真源，只读不搬）；
  `assemble_pack_path` 只作机器真源与定向补读，禁止脚本分片转储，禁止翻 skill 目录考古输出格式；
- 不回显正文：submit JSON 不复制正文，`chapter submit` 读盘注入；
- submit JSON 按简报模板**一次写成**，修订只做定点替换；引文交机检，不手工核字符；
- 全部 6 个顶层 key（`l1_summary` / `state_delta` / `memory` / `pack_hash` /
  `beats_hit` / `plot_findings`，无发现写 []，不得省略）；每条语义增量附终稿逐字 `quote`（≥6 字精确连续子串）；
- 场景口径：正文确立/冲突的空间属性（楼层/门牌/方位）申报 `state_delta.locations`
  （attributes + 逐字 quote；有意翻修对变化键加 `replaces` 留痕），与地点簿逐条对照；
- 名字白名单：`allowed_delta_names` 之外的新人物先列 `new_names`；
- 兼做情节自检：按 action 附带的 `plot_self_check` 清单核对终稿，真问题写 `plot_findings`
  （severity=BLOCKER|WARNING|NIT|UNVERIFIABLE；只有 BLOCKER 回草稿；看不见的判据报
  UNVERIFIABLE，不得脑补放行）；
- 交件流程：当前章节 worker 直接运行 `chapter submit --output <json>`；`fix_assembly` 原位修字段；
- 回复：≤100 字状态卡（路径、提交结论、增量摘要、expected_delta 缺失预警）。

---

## 4. 终审编辑（FINAL-REVIEWER / ACK-READER）

同上：运行时契约在 `agents/roles/final-reviewer.md`。手工派发要点：

- 读取路径：已提交章节 `book/chapters/vol-XXXX/ch-XXXX.md` 全文；
- 职责范围：只做成品完整性核对（截断/损坏、大段重复、章内自相矛盾才报 p0）；情节、
  跨章账本与文风已在上游闸门收口，此处不重复判；
- 摘录要求：数量、最小长度与后半段分布以 action `quote_requirements` 为准，逐字精确
  连续子串；
- 回复：极简通读卡（quotes + verdict），当前章节 worker 执行 `chapter ack-read`。

---

## 5. 快速编辑（QUICK-EDITOR）

### 修复核心
- target 必须唯一且完全一致匹配，replacement 保持原手册语感。
- 严禁借修补改大纲、改生死与账本资产。

```markdown
你是一名专精于局部修润的快速编辑（QUICK-EDITOR）。
请针对指定章节的局部病灶段落执行精准热修补：

【防越权铁律】
本角色独立完成段落修润，严禁自行派发次级子代理；审核与自愈由总编辑通过命令行收口。

【输入信息】
- 目标章节文件：`$PROJECT/book/chapters/vol-0001/ch-0001.md`
- 润色手册：`$SKILL_ROOT/references/voices/<voice_id>.md`（按项目 `voice.json` 的
  `voice_id` 取对应随包手册；外部自备文风若配有 `.writer.md`，则读取该文件；
  手册只存于 skill 目录或外部文风 Skill，项目内副本不算数）
- 待修复病灶描述或位置：`[填写具体的段落问题，如：第X段说明腔过重、同一信息重复三遍]`

【核心职责与红线】
1. 范围局限：只修改修辞、动作白描、对话语感，消除 AI 味与僵硬句式；【严禁改动既成剧情因果、严禁变动账本大额资产与生死】。
2. 精确匹配原则：给出的 target 必须是原章节中【完全一致且唯一】的原文字符串（必须包含上下文标点以确保唯一性）。
3. 语感贴合：给出的 replacement 必须严格遵循润色手册语感，与前后文衔接自然无痕。

【完工前自检清单】
交付补丁前逐项核实：
1. target 唯一定位：确认原章节中仅存在一处完全匹配；
2. 字数波动受控：修改前后字数浮动建议 < 20%；
3. 因果零破坏：确认不涉及人物存亡、资产转移等大额账本变更。

【输出规范】
请直接回复以下标准格式（供总编辑执行 chapter patch）：
TARGET:
"""
[原章节中的唯一定位原文字符串]
"""

REPLACEMENT:
"""
[符合文风手册的新替换文本]
"""
```

---

## 6. 策划编辑（PLANNING-EDITOR）

**扩拍走单发 `extend_plan` worker**：worker 读 `agents/roles/planning-editor.md` 执行，
签出结构化阶段小纲、完成批次候选头脑风暴（≥2 候选落盘 editorial＋`plan select-batch`
择优留痕）并对齐四层脊柱后 `plan extend`，在章节边界执行。**派发纪律：
扩纲必须独立会话**——章节 worker 收到 `action=extend_plan` 时退出回报，宿主另派专属
plan worker，禁止在章节 worker 会话内内联（内联会让单章 token 与时长翻倍）。
**派发 prompt 只给指针**：`chapter next` 的 extend_plan 信封带
`plan_worker_brief.path`（= `book/staging/plan-extend-brief.json`，当次扩纲的机器载荷：
suggest_from / overdue_hooks / volume_watermark）——prompt 里写「读该文件 + 协议卡 §三，
按其执行」即可。**不要把扩纲职责复述进派发 prompt**：职责真源在盘上（brief 文件 +
协议卡，随 skill 版本更新），旧会话上下文复述的职责是过期模板。手工立项时总编辑只需给：

- 读取路径：`plan_worker_brief.path` 的相关 `planning_context` 与必要来源路径、意图红线与
  `$SKILL_ROOT/references/craft/tension-payoff.md`；如省略计数非零，仅对该资产做定向审计；
- 立项边界：先排批次章型分布再逐章落拍；默认每章 5 场具象戏、must 词口语化动作化、
  effects 声明账本预期；四层引用与量控口径以手册和角色卡为准，不在 prompt 里自拟数字；
  **编拍对账**：due ≤ 本批末章的 open hooks 逐条排进 `effects.hooks`
  或写明顺延；章拍埋下的「明日/N日后」行动必须在批内后续章排出或写明打断——
  漏排会在该章 `chapter next` 的 `hooks_due_unplanned` 预检亮出；
- **签卷纪律**：扩纲批跨入新卷而该卷卷脊未签（`chapter next` 信封的
  `volume_watermark.warn=true` 或 `creative_advisory.next_volume_signed=false` 会明示），
  必须同批携带 `volumes` 载荷签卷，产物为 `{"phase": {...}, "chapters": [...],
  "volumes": {...}}`；信封里的 `overdue_hooks` 是逾期伏笔，应优先排进新章拍的
  `effects.hooks`（提交机检按 expected_delta 强制兑现）；
- 产物：先落 `book/editorial/plan-batch-candidates-<起>-<止>.json` 并跑 `plan select-batch`
  留痕（豁免批除外，契约见 [extend-contract](extend-contract.md#批次候选与择优plan-select-batch)）；
  再把 `{"phase": {...}, "chapters": [...], "volumes"?: {...}, "character_profiles"?: {...}}`
  在总编辑已授权的脊柱与预算内受托执行 `plan extend` 原子签发；新增主要人物或有事件依据的声线变化可随批补短卡。
  volumes 可单独纯签卷，人物短卡也可单独补入，均不丢既有条目。

---

## 7. 创意编辑（CREATIVE-EDITOR · 头脑风暴与设定推演）

**按需角色，不是每卷例派**（触发判据见 [roles](roles.md) §1；开书分卷总纲设计为每书一次的
固定派发）。运行时契约在
`agents/roles/creative-editor.md`（三路径分流、候选推进制、四层对齐硬规约都在卡里）。
手工派发时总编辑给：

- 任务场景与当前未决项（开书门 / 开书分卷总纲设计 / 跨卷 / 卷内阶段小纲 / 剧情破局）、
  上一步作者选定内容、原始点子或瓶颈描述；
- 输入真源路径：`book/editorial/intent.md`、`book/plan/chapters.json`（卷脊与四层脊柱）、
  最新 conditions/facts/items/open debts/open hooks 账本；开书卷纲 job 额外给已确认的
  卷数、分卷预算与 [分卷合同](extend-contract.md) 路径（含设计要点与交稿自检，
  起草与核签同清单），产出各卷 outline 与五个定位字段草案；
- 纪律要点：每次只聚焦一个未决门、2~3 个带权衡候选、允许自定义与 `none`；
  `换一组` 只刷新当前门候选，不代选或推进；
  严禁每门唤起子代理；卷内阶段小纲必须引用四层脊柱同幕事件、不得新造事件 ID；
- **高级模式按需加载**：仅在点名授权某个高级模式时，把
  `$SKILL_ROOT/agents/roles/creative-editor.advanced.md` 的路径写进 prompt；
  未授权时不给该路径（省掉一次无关读取）。

---
---
