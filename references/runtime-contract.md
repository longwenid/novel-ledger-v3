# 运行态契约（HEAD 与文件布局 · status 只读输出）

两部分：① HEAD、会话/执行模式、账本文件布局、地点簿、文风记忆与总编辑台；② status 只读输出字段、用量口径与计划水位字段。

<!-- 一、运行态文件契约 -->

HEAD、会话/执行模式、账本文件布局、文风记忆与总编辑台。submit 输出见 [write-output.md](write-output.md)；崩溃恢复与写锁见 [recovery.md](recovery.md)。

所有持久化状态存于 `book/novel.sqlite3`。下述文件路径兼作文档地址与导出副本；
脚本读数据库，外部改文件不会更新状态。未提交 staging、进程锁与 driver 临时文件例外。
迁移、备份、导入与查询见 [SQLite 与历史记忆](sqlite-memory.md)。

## HEAD（`book/run/HEAD.json`）

```json
{
  "schema": "novel-ledger.head.v1",
  "status": "active",
  "phase": "idle",
  "chapter": 0,
  "last_committed_ch": 0,
  "last_acked_ch": 0,
  "pack_hash": null,
  "prose_hash": null,
  "rewrite_count": 0,
  "reopen_count": 0,
  "blocked": null
}
```

`phase`：`idle` → `await_draft` → `await_polish` →
`await_assembly` → `submitted` → `await_ack` → `idle`。旁路：`blocked`、`complete`。

`last_committed_ch > last_acked_ch` 时不得 begin 下一章。

正常完本记 `completion_kind=normal`；作者提前封笔记 `early_close` 并保存 `early_close_resume`
以便恢复原待办阶段。`completion_target_override` 仅保存作者已明确改约的原字数目标；重新打开清除。
`last_story_review` 保存最近复核的 id、input_hash、through 与 verdict，完整证据回执见
[剧情复核](story-review.md)。卷审通过和全书完本属于不同状态。

### 会话边界（`config.session_mode`）

默认 `strict`：`ack-read` 收口返回 `requires_new_session=true`、`session_boundary=required`，
下一章必须新开会话从 `chapter next` 起跑——这是防止整书历史随请求反复重发的成本闸门。

`inline-compact`：既开不了新会话、本机又没有可无头调用的模型 CLI 时的显式降级。ack 后返回
`requires_new_session=false`、`session_boundary=advisory`，允许同一会话继续下一章，但宿主必须
先做会话内压缩（丢弃已收章节正文、只留 HEAD/plan/账本指针）。状态真源始终在盘上；切换该模式
不改变任何机检与预算语义。

### 执行模式（`config.execution_mode`）

默认 stage-agent：next 的 execution 信封附 stage 简报，action_path 是当前阶段任务快照。
每阶段创建空上下文新会话，禁止 fork/历史继承；worker 只提交当前阶段并退出，不运行 next。
supervisor 保存 stage-sessions.json，校验每 job 的真实宿主 session_id 与 context_origin=empty。
阶段文件只保证交接材料边界；访问安全需要宿主沙箱。worker-agent/inline 是显式职责隔离降级。
editorial/findings.jsonl 独立保存逐条发现与处置，ack 清 staging 不删该记录。
已入账 patch 更新 hash 后在 ack 标记 needs_review，review ack-patch 通过才算审核新版本。

## 账本

- 事件：`book/ledger/events.jsonl`（数据库内的**历史真源**，每章一条 `state_delta`；伏笔、关系及显式正典基线裁决另追加 `type=governance` 事件，含 `effective_chapter`，不占章节号；在 SQLite 事务内追加）
- 快照：`book/ledger/snapshot.json`（**派生视图**，`entities` / `debts` / `hooks` / `relations` / `occupancy` / `items` / `conditions` / `knowledge` / `locations`）。
  `knowledge` 以 `(who, topic_id)` 保存每个人当前已知、误信、怀疑或推翻的说法；历史变化保留在事件里，
  与 `entities[].facts` 所记的客观人物事实分开。旧快照缺此字段时按空列表读取，表示尚未追踪，
  不表示人物无知；其余字段仍须符合当前 `EMPTY_SNAPSHOT` 维度

### 地点簿（场景地图，`snapshot.locations`）

空间属性（楼层/门牌/方位/地址…）的注册表——worker 每章都是空上下文会话，没有它，
首章定下的「局长办公室在四楼北边101」到后章只能靠运气。三道防线：

1. **登记**：组装在 `state_delta.locations` 申报 `{id?, name?, aliases?, attributes{键:值},
   replaces?:[键], status?:open|closed, quote}`；带 attributes 必附终稿逐字 quote（机检）。
   `apply_event` 按 id/name/别名合并同地异名（旧名降为别名），属性键 last-writer-wins，
   每键保留 `{value, chapter, quote}` 锚。旧快照缺 `locations` 按空注册表读取（老书免迁移，
   首次申报自然建立）。
2. **注入**：pack 装配按章地点/拍点文本/在场者所在地/占用表切地点卡（`pack_caps.locations`，
   默认 8，不足按最近使用补满），draft 简报与 assemble 视图/简报各渲染一份——写者在动笔前
   就知道口径。章拍 `effects.locations` 可预排计划口径（expected_delta 强制回填）。
3. **对账**：申报与注册表同键不同值（按 `norm_spatial_value` 归一：去空白/小写/中文数字
   归一，「四楼」≡「4楼」）→ `location_attribute_conflict` 拦回组装；出路二选一：正文写漂了
   回草稿改，有意翻修/搬迁在申报里加 `"replaces": ["<键>"]` 显式留痕后放行。`book reconcile`
   另做注册表无关的兜底扫描（`spatial` 类：同一地点跨章/章内互斥楼层或门牌，地点名取自
   注册表与 `config.spatial_keys`），点名存量漂移。

- 自检：`ledger verify`（只读）三件事——① 重放 events 与快照逐字段比对；② 每章至多一条 commit 事件（`duplicate_chapter_events`）；③ 每条已入账章必须存在对应 `ch-NNNN.md`（`missing_chapter_files`）。任一不满足即 `consistent:false` + `diffs`。后两条是字段比对的盲区：重复入账时重放与快照都含重复项，必然"一致"
- 修复：`ledger repair --from-events`（写）按 events.jsonl 重放结果重建快照。verify 只读不修，repair 是它缺失的另一半
- 章文件：`book/chapters/vol-0001/ch-NNNN.md` + `.meta.json`（章按 `volume` 分组）
- 近摘要：`book/summaries/l1/vol-0001/ch-NNNN.json`
- 内容分层记忆：`book/memory/hierarchy.json`（由 L1 确定性派生，可重建）
- ack：`book/acks/vol-0001/ch-NNNN.json`（`quotes` 必须是正文子串；具体数量/长度/分布以 action `quote_requirements` 为准，默认 ≥3 句、每句 ≥6 字，见 `config.ack_quotes_min`）。**写后通读是强制闸门**：`chapter ack-read --quote "…" --verdict pass|p0` 只收逐字引文；`p0` 只用于本章全文单独可证的截断/损坏、大段重复或章内自相矛盾，不核验未提供的章拍/账本；未 ack 不得 begin 下一章

输出目录规范：正文/meta/近摘要/ack 全部按章 `volume` 归入 `vol-0001`、`vol-0002`…目录。
- KB 卡片：`book/kb/cards.json`（运行时 `{cards:[…]}`；`always` / `priority: core` 只作命中后的排序加分，不再默认进 pack）
- 文风记忆：`book/memory/voice.json`（两个**独立窗口**，见下）
- 人物表达基线：`book/plan/chapters.json` 顶层可选 `character_profiles`；仅本章在场者随包，
  与全书文风 `voice.json` 分开。持续变化可在 `plan extend` 的 `character_profiles` 载荷中替换单人卡。
- 章拍真源：`book/plan/chapters.json`（schema 见 [plan-contract.md 章拍真源](plan-contract.md#章拍真源bookplanchaptersjson)一节；不要靠扫目录「第N章」正则）
- KB 人读正典（经 `database import-source --kind canon` 导入）：`book/kb/canon/*.md`（按类别分文件；完整表写在 md 里）

### 文风记忆（`book/memory/voice.json`）

**文风单轨**：项目里文字质感的唯一来源是所选手册 `references/voices/<voice_id>.md`（hatch 自动蒸馏写入，默认 `shijing`；`voice apply --voice` 切换或刷新）。自备手册只能加在本 skill 的该路径；`voice.json` 的真源字段是 `voice_id`。

两条数据流分别存放，**互不覆盖**：

| 字段 | 谁写 | 窗口 | 进 pack 的方式 |
|---|---|---|---|
| `concepts` | `book hatch` 与 `voice apply`（都从所选手册蒸馏） | 完整保存已选手册概念 | 随当前阶段手册完整提供，不因投喂额度删掉硬禁 |
| `session_notes` | 只有 ack `--voice-note` 与写者 `memory.voice_concepts` | 完整保留已存笔记 | 与手册概念分别提供，按当前任务理解适用范围 |

其余字段同由 `book hatch` / `voice apply` 写：`voice_id` / `skill_manual` / `style_formula` / `checklist` / `concept_cap` / `applied_at`。

- **pack 的文风字段来源**：`voice_pack` 装配文风 profile，`pack.py` 按 `voice_id` 读取阶段手册。随包文风的 `<id>.content.md` 经 `voice_content_text` 进入 draft 视图；同一文风的 `<id>.md` 既供解析，也经 `voice_writing_text` 进入 polish 视图，路径记在 `voice_writing_manual`。装配时从当前 skill_root 重读，避免安装路径漂移。手册正文不重复拼进 instruction。外部自备文风若提供 `.writer.md`，润色视图改用该文件。
- **手册与笔记各守来源**：临时笔记不替换蒸馏概念。手册 `### 硬禁` 的原文随所选手册保留；风格统计仅作诊断，手册概念和机检各自按当前契约工作。
- **完整概念与来源补读**：历史 `--cap` 与 pack 长度配置不用于裁掉已选手册概念或已存笔记；需要更多上下文时沿手册或人写节奏锚来源读取完整相关材料。重跑 apply 保留 `session_notes`（`session_notes_kept`）。
- **手册解析空则当场失败**：非法 id 报 `unknown_voice`。hatch 与 `voice apply` 抽不到公式、概念或自检清单时都报 `invalid_voice_manual`；本 skill 不迁移旧项目。
- **缺文风概念则拒绝 begin**：`voice.json` 被清空或改坏时，pack 装配以 `missing_voice` 拒绝开章——单轨之后没有可退的兜底，不靠"写者自己记得"。
- **文风质感由润色与阅读审稿把关**：`chapter submit` 只判可机器验证的结构；明显出戏的话术在 `polish-submit` 原地拦截，统计分布只作诊断，fix 只回 polish。

## 总编辑台与决策留痕（`book/editorial/`）

总编辑是全局控制层，但不开全书。它的全局上下文落盘在 `$PROJECT/book/editorial/`，
由总编辑经只读命令与一次性复核角色维护：

| 文件 | 内容 | 谁写 | 谁读 |
|---|---|---|---|
| `intent.md` | 作者意图 / 红线 / 卖点 | 作者书写并确认；**创意编辑（creative）**可协助脑暴起草草案 | 总编辑 / 策划编辑 |
| `desk.json`（可选） | 状态摘要：当前章 / ack / 计划水位 / 最近 L1 摘要 / 待决事项 | 总编辑自写（运行时**不生成也不读**，真源是 `status` 输出与 `HEAD.json`） | 总编辑 |
| `decisions.jsonl` | 追加式决策日志：actor / time / decision / 理由（意图·宪法·机器事实） | 总编辑 | 总编辑跨会话恢复 |
| `hatch-manifest.json` | 已确认的 v3 开书决策、候选证据与最终启动确认；`book hatch` 成功后原样持久化 | 开书控制面 | 总编辑复现与审计 |

开书入口只接受 `novel-ledger.hatch.v3`。`book hatch --check-only` 只读校验选择闭合、voice 和起手章拍，
不创建项目；正式 `book hatch` 校验通过后统一生成 `intent.md`、大纲、正典种子、计划与上述 manifest。
`intent.md` 结构与填写口径见 `templates/intent.example.md`。
闸门时机：批级每 `style_fingerprint_every` 章、卷级在卷末、书级在完结前，均由总编辑主持。
总编辑台面保存来源路径和裁决；需要全文证据时，由一次性通读/复核角色按问题读取相关原文，
报告保留完整选中证据与来源。正文不回显到调度会话。
---

<!-- 二、status（只读）字段与口径 -->

status 输出字段、用量口径与计划水位字段。
## status（只读）

`status.storage` 给出 engine=sqlite、database_path、authority=database；项目文件是导出或待提交输入。

`status.usage` / `book audit.usage_summary` 汇总 `book/usage.jsonl`。宿主在每个模型请求后调用
`chapter usage-record` 写一条 v3 分量 delta，或 `usage.total.v1` 总量 delta（stage-agent 宿主只见
整单总量时用 `--total-tokens`）；旧 schema 与坏行不参与汇总并计入 `bad_lines`。

`status.runtime` 另给出当前 Skill 的 `version`、实际解析的 `skill_root` 与控制面
`control_fingerprint`，供安装或更新后排除旧安装、旧软链或环境变量覆盖；它不是小说项目状态。

| 字段 | 含义 |
|---|---|
| `effective_input_tokens` | 有效输入：`uncached + cache_read + cache_write` |
| `uncached_input_tokens` | 未命中缓存的输入 |
| `cache_read_input_tokens` / `cache_write_input_tokens` | 独立缓存读/写输入；二者都计入 effective input |
| `output_tokens` / `total_tokens` | 输出 / `effective input + output` |
| `cache_hit_ratio` | `(cache_read + cache_write) / effective input`；仅用于诊断，不代表缓存读免费 |
| `records` / `chapters` / `bad_lines` | 有效记录数 / 涉及章号 / 坏行数 |
| `voided` | 被 `chapter usage-void` 作废的样本数（聚合口径已剔除） |
| `max_input_per_chapter` / `latest_chapter_input` / `by_chapter` | 单章尖峰 / 最新章 / 逐章用量 |
| `telemetry` | `available` 或 `missing`；缺数据时不伪造零样本 |

**输出窗口**：`status` / `book audit` 的 `by_chapter` / `chapters` 明细只回最近 12 章
（`chapters_total` 给全量章数、`detail_window` 标记窗口宽）；汇总量（totals、
`input_per_chapter`、`max_input_per_chapter`）始终保持全书口径。逐章全量真源在
`book/usage.jsonl`，进程内熔断查询单章不受窗口影响。这个窗口只用于诊断明细分页，
不限制角色读取上下文材料。需要某章细节时按章查询真源，返回所选记录的完整内容。

`request-id` 全书幂等：完全相同的重放不新增记录，同 id 的 counters 或 chapter/stage/session 元数据不同
则报 `usage_request_conflict`。四分量必须显式给 `uncached_input_tokens`（允许 0）。`chapter` 必须原样
回传 action 章号。未知 telemetry 不落 0。预算按当前活跃章 effective input 熔断；ack 后下一章重新计数。
幂等拦不住「换 ID 重记同一笔」：记错用 `chapter usage-void --request-id <rid> --reason <why>` 作废
（append-only 追加 void 事件，聚合口径剔除被作废样本；禁止再记一笔负数或无视误差）。
超额章只剩终审时，`chapter usage-authorize` 可由人放行一次 ack，action 发出即消费。完整协议见
[execution-architecture.md](execution-architecture.md)。

计划水位相关字段有两个，**类型不同、别混用**：

| 字段 | 类型 | 用途 |
|---|---|---|
| `plan_remaining_count` | 整数 | 剩余计划章数。要跟 `plan_low_water`（整数）比大小的是**这个** |
| `plan_remaining_chapters` | 整数列表 | 剩余章号明细，给人看的 |
| `plan_low_water` | 整数 | 低水位阈值（`config.plan_low_water`，默认 5） |
| `book_words` | 整数 | 全书目标字数（`config.book_words`，开书设定，默认 200 万）；`0` 表示未设 |
| `book_words_written` | 整数 | 已提交正文总字数（累加各章 `meta.json` 的 `word_count`，不重读正文） |
| `book_words_remaining` | 整数 | 距目标剩余字数（不足则 0） |
| `book_words_progress` | 小数 | 完成比例（`written / book_words`，0–1；未设目标时为 0） |

水位比较只用 `plan_remaining_count`（整数比整数），勿拿明细列表与阈值直接比较。
`chapter next` 的 `extend_plan` 里同义的那个字段叫 `remaining_in_plan`，也是整数。
---
