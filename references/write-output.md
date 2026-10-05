# 提交输出契约（chapter submit --output）

state_delta 各列表的字段语义、quote 证据与 submit 机检契约。pack 结构见 [pack-contract.md](pack-contract.md)。
## Write output（`chapter submit --output`）

账本字段缺一不可。`pack_hash` 必须回显当前 pack。输出契约随**事实编辑**的组装简报
（action 的 `assemble_brief_path`：提交模板预填 `pack_hash` 与 required 拍 id）交付，
机器真源是 `assemble_pack_path` 里的 `write_contract`——执笔角色只产草稿文本、润色角色只产终稿文本，
两者都不产出 submit JSON。
**`prose` 不是必填键，也不再逐字回显**：正文真源是数据库内已验收的阶段二终稿（`polished_output_path`），
`chapter submit` 会从数据库读取并注入 canonical output（并据此算 `prose_hash`、落章、跑正文类机检）。
这样省掉一次整章输出，也从根上消除了双份正文不一致问题。输入若多带 `prose` 会被忽略，
canonical output 只写入数据库内已验收终稿。

```json
{
  "l1_summary": "一章一句近摘要",
  "state_delta": {
    "moves": [{"who": "人名", "to": "地点"}],
    "facts": [{"who": "人名", "text": "新事实", "pin": false, "quote": "正文里支撑这条事实的原句"}],
    "knowledge": [{"who": "人名", "topic_id": "topic-receipt-origin", "claim": "此人目前如何理解凭据来源", "stance": "suspects", "source": "亲见落款", "quote": "正文里演出获知或推断的原句"}],
    "debts": [{"id": "d1", "who": "人名", "text": "债", "status": "open", "due": 50, "quote": "正文原句"}],
    "hooks": [{"id": "h1", "text": "埋下的伏笔", "due": 50, "status": "open", "quote": "正文原句"}],
    "relations": [{"who": "A", "target": "B", "kind": "师兄弟", "status": "open", "quote": "正文原句"}],
    "items": [{"id": "item_jian", "name": "青铜残剑", "holder": "主角", "quantity": 1, "kind": "weapon", "status": "held", "quote": "正文原句"}],
    "conditions": [{"who": "主角", "kind": "伤势", "text": "右臂齐肩斩断", "irreversible": true, "quote": "正文原句"}],
    "named": ["工作集内已有名"],
    "new_names": ["本章新申报"],
    "deaths": [{"who": "阵亡人名", "quote": "正文原句"}],
    "revivals": ["复活/夺舍归来的已故角色"],
    "nonliving": ["本章以遗体/鬼魂/闪回出场的已故角色"]
  },
  "memory": {"voice_concepts": []},
  "plot_findings": [],
  "pack_hash": "sha256:…",
  "beats_hit": ["b1"]
}
```

`facts[].pin: true`＝铁律事实，**永不驱逐**（身世、誓言、物理后果等跨百章仍须在场的）；缺省 `false`。同一人物同一 `text` 的重复申报合并为一条，`pin` 只可升级；每人物保留全部 pinned 和最多填满 12 条总额度的近期普通事实。pack 每处最多展示 3 条，优先本章相关事实，再取最近 pinned / 普通事实；留在快照不等于每章都展示。

**`knowledge`＝人物认知索引**：`who` 用规范人名，`topic_id` 是同一话题的稳定键，
`claim` 写该人物当下接受或考虑的具体说法，`stance` 仅取 `knows|believes|suspects|refuted`；
`source` 简述其在本章的获知渠道或推断根据，`quote` 从终稿逐字截取演出这次认知变化的原句。
同一 `(who, topic_id)` 在快照中保留当前状态，旧版本仍可从事件链追溯。`knows` 是人物已接受的信息，
不等于世界正典已证实；`believes`、`suspects` 和 `refuted` 分别记录相信、怀疑和否定的状态。
读者/旁白知情、编辑层 `long_arc_question` 或 plan 中预定揭晓都不能代替正文中的获知过程；
没有认知条目也不证明人物不知道。本章计划结果见 `beats[].effects.knowledge`，组装时以终稿核实。

**`items`＝道具/装备/器具/资产账本**：解决物品随 facts 滚动驱逐导致遗忘、或消耗品无限使用的漏洞。条目按 `id` 合并（本条不带 `id` 时按 `name` 合并；两者都无法区分时才新增，避免同一件东西长出第二条）。记录持有者（`holder`）、数量（`quantity`）、类别（`kind`）与状态（`status`：`held|used|damaged|lost|transferred`）。转交时沿用同一物品 `id`，在增量里写新 `holder` 和 `status: "transferred"`；事件保留转交动作，快照把已确认的旧→新持有人变更归一为 `held`，下一章只注入新持有人的 Pack。没有可确认的旧持有人或新持有人时，`transferred` 保持未在持状态；确认归属后需显式申报 `holder` 与 `status: "held"`。`held` / `damaged` 会作为在持资产注入 Pack；`used`（已消耗）、未归一的 `transferred`、`lost`/`destroyed` 不再出现，`quantity` 为 0 的同理；未登记的未知状态照常注入（宁可多给，也不静默藏道具）。在场者持有的物品动态切片注入 Pack（`pack.items`），其 `quote` 享受逐字正文锚定。

**`debts`＝承诺与欠账**：同 `id` 的后续增量更新当前状态；`paid` / `cancelled` / `closed` / `resolved` / `done` / `settled` 不再进入下一章的欠账切片、NOW 卡与近况。活跃欠账按在场者、地点、到期线索取相关项；同等相关度时最近更新优先，本次未选中的条目数进入 `omitted`，相关旧事可按人物、资产或事件来源补读。

**`conditions`＝角色状态账本（伤势 / 体力 / 欠债 / 职务…）**：解决「正典写得最硬、机器通道最薄」的那一类漂移。`kind` 是**项目自定**的自由标签（制度层不携带任何一本书的世界观，词汇由项目正典注入），可按需带 `value` / `unit` 记录可量化的层级或数值。条目按 `(who, kind, text)` 合并；同键更新视作最新状态参与额度淘汰。`status` 缺省 `active`，`resolved` / `healed` / `closed` 视为结案。

关键在于 **`irreversible: true`（不可逆代价）**：断肢、毁容、资格吊销、破产清零的定义就是"它不会自己好"。这类条目

- **永不驱逐**，不占每个角色的 `CONDITIONS_PER_ENTITY` 条可逆状态额度；
- **在条目数量保护门内全量注入**，不占 pack 的 `conditions` 可逆状态展示额度（故展示总数可超过该额度），并在 pack 里附 `conditions_directive` 硬约束：
  正文不得出现与不可逆代价矛盾的动作或能力（断肢不能握剑、毁容不能以貌识人、吊销的资格不能照旧行使、清零的本钱不能凭空回来），
  除非本章账本显式申报了对应状态变更。

在场人物的活跃不可逆状态默认最多 32 条（`pack_caps.irreversible_conditions`）。这是状态条目数量保护门，独立于上下文文字长度；已选状态文字和证据完整保留。超限时 `chapter next` 报 `irreversible_conditions_overflow` 停线，不会把永久代价静默截断；快照与 `book audit` 仍保留全部记录。先用 `book audit` 核对，临时上调此配置以写一章对账剧情，在该章 `state_delta.conditions` 中把已不再生效的同键状态显式标为 `closed` / `resolved`（例如修复代价或合并重复申报），提交后把配置调回正常上限。若代价确实同时有效，保持上调后的上限，接受对应的工作包长度。

可逆状态在快照中每个角色最多保留 16 条：当前有效状态优先占额度，剩余空间留最近的 `resolved` / `healed` / `closed` 记录；更早的结案历史仍在 `events.jsonl`。pack 的 `conditions` 展示额度默认全场 16 条，按在场角色顺序轮流取各人的最近有效状态。额度足够覆盖有状态角色时，每人至少展示一条；不足时先列出的角色优先。未展示的有效可逆状态条数进 pack 的 `omitted`（见下）。

`book audit` 另出 `irreversible_conditions` 全量清单，供复核角色一眼核"断了的手怎么又握上剑了"。

**`omitted`＝材料选择范围**：自动装包按当前人物、地点、阶段及默认记录选择数取相关项；未进入初始包的条目数可汇总进 `pack.omitted`。选中条目的完整文本与引文不按字数截断，未选中也不能当作不存在。角色按当前问题从来源路径、`memory recall`、`context read` 或资产审计补读；仍缺证据时以 `UNVERIFIABLE` 说明所需材料。诊断窗口可以分页，显式选择的证据必须完整返回。

**`deaths` / `revivals` / `nonliving`＝死亡三态**：`deaths` 把角色标记为已故；此后任何把该角色写进 `named` 或 `moves` 的 delta 都会以 `dead_speaking` 停线（`blocked`，须总编辑裁决）。两个显式出口：
- **`nonliving`**：声明"本章他确实在场，但是以遗体/遗物/鬼魂/闪回的身份"——pack 死亡警告
  允许的合法情形，显式列出即不拦，且随事件留痕、可重放。
- **`revivals`**：撤销死亡标记（夺舍、假死揭秘、亡者归来），不必回滚死亡章或手改 `events.jsonl`。

两条都**只放宽不收紧**：不声明时行为与从前完全一致。
三态列表的条目只接受非空人名字符串或 `{who: 非空人名}`，同一列表不能重复；复活者须已在账本中死亡，或本章同时申报其死亡。同章死亡再复活以存活收口，合法复活者可在同一提交中进入 `named` / `moves`。章拍 `effects.revivals` / `effects.nonliving` 会进入计划结果，提交须兑现这些声明。

**`prose_hash`（按组装任务书模板回显）**：任务书预填的终稿哈希凭证，逐字回显；submit
  与现行终稿比对，不符整份拒收（`stale_plot_findings`）——plot_findings 必须是对现行正文的判断。

**`plot_findings`（可选，情节自检）**：数组，每条 `{code, severity, hint, quote}`；是组装
  worker 对其所读正文的一次性判断，正文返工后作废，须重跑组装重建，不得补 quote 重提旧件。
事实编辑兼做情节自检时填，`severity` 必填且仅限 `BLOCKER` / `WARNING` / `NIT` / `UNVERIFIABLE`，
`hint` 必填，`quote` 逐字取自终稿且 ≥6 字；全过写 `[]`
（缺省视为已核无问题）。
- 只有形状/证据合法且 `severity=BLOCKER` 的条目才判**正文问题**，回
  `await_draft` 重写（消耗同一份 `rewrite_limit`）；`WARNING` / `NIT` / `UNVERIFIABLE` 落 warning 后继续；
  形状/证据不合法（`plot_finding_severity_invalid` / `plot_finding_missing_hint` /
  `plot_finding_quote_too_short` / `plot_finding_quote_not_in_prose` 等）→ 判**组装契约问题**，
  回 `await_assembly` 重跑。

**账本增量正文证据（`quote`）**：`facts` / `debts` / `hooks` / `relations` / `deaths` / `items` / `knowledge` 的每个语义条目
应附 `quote`——从终稿逐字抄出支持该增量的原句，机器逐条验证子串；`delta_quote_not_in_prose` /
`delta_quote_too_short` 只回组装重跑（正文与润色终稿保留）。一般增量缺 `quote` 不构成机器硬错，
因为并非所有状态都能用单句完整取证；但 `knowledge` 与计划中的 `effects.knowledge` 必须有获知原句，
组装契约要求事实编辑尽量给出证据；
没有逐字证据就不要申报该条，避免账本成为一锤子抽取的幻觉源。
计划中显式声明的 `effects.conditions` 与其他计划语义结果一样必须附正文 `quote`；缺失时只回组装补证据，不消耗正文返工次数。

`hooks`＝伏笔/钩子账本（按 id 合并）：近期显性承诺通常填 `due`，到期钩子按紧迫度进入
pack；默认记录选择数为 `pack_caps.hooks`=5，其余计入 `omitted.hooks`，需要时用 `hooks audit` 和历史召回补读完整记录。
书级 `book_outline.long_term_commitments[].id` 与同 id hook 对接：初次埋种用 `open`，
`due` 若填是**最终承诺到期章**，可待收束阶段定章时再填，不是中途显形的固定间隔。
本章 `beats[].effects.hooks` 点名的活跃同 id hook 优先进入本章相关 pack，以便写者看见
当前状态；整张长线承诺账不会进入逐章 pack。

中途显形仍用同 id、`status:"open"`，`text` 更新为本章真实形成的新理解或因果，
并在 `state_delta.hooks` 附本章逐字 `quote`；仅复述线索、无新行动或代价，不能当作显形。
最终在当前冲突中兑现、与已填的 `final_condition` 相符时才同 id 置 `paid`，附本章证据。
原始埋种可从事件链追溯，扩纲 brief 为选中的承诺只投影 `opened_chapter`、`seed_text`、
最近更新与当前 hook 完整记录及来源；需要核对埋收因果时按事件和章号补读原文。`hooks close` 是留痕放弃/结案，
不能代替正文兑现；确需改期按治理契约 `hooks defer`。不能把未入本章 pack 的线索误判为已兑现。

`relations`＝关系索引（按 who+target+kind 合并）：实体之间显式的"师兄弟/欠债/仇敌…"，pack 只切在场者涉及的关系（`select_relations`，双向命中、已结束关系过滤、**本场双方都在场优先，其次按 `updated_chapter` 倒序**、默认记录选择数 `pack_caps.relations`=6）。这样场外关系不会挤掉本场纽带，关系演变（盟友→仇敌）也先给到新状态。写者用对话与行为体现关系，不用旁白宣读。同一对角色并存多条不同 kind 的 open 关系时，`ledger verify` / `book audit` 的 `hygiene_issues` 报 `relation_pair_conflict`（advisory）：可能是合法多重身份（师徒＋姻亲），也可能是旧关系忘了置 `closed`，由总编辑裁决。

**关系演变怎么收敛**：`kind` 是自由文本，"同一段关系换个说法"会积出同对角色的多条 open 关系。两条显式通道：

- 在**新的**那条增量上加 `"supersedes": true`，账本把该对角色其它 open 条目置 `closed`（并记 `closed_chapter` / `closed_by_kind`）；只想退掉指定的一条时再加 `"supersedes_kind": "<旧 kind 原文>"`。确实同时成立的多重身份（师徒＋姻亲）不要加。
- 关系真的结束、本章也不写新条目时，直接提交一条同 (who,target,kind) 的 `status: "closed"` 更新。

`status` 的结束词表两侧共用（`open` 为缺省；`closed` / `已关闭` / `已结束` / `结束` / `关闭` / `终止` 都算结束）。事实编辑的 assemble 视图里，若同一对角色已挂多条 open 关系，会直接给 `relation_conflicts` + `relation_conflict_policy` 前置提示，不必等 verify 事后报警。关系表两端出现互相包含的两个名字时报 `relation_name_variant_suspected`，交总编辑统一规范名或确认是两个人。


机检：required beat 的 `id` 与 `must` 词、`named`/`moves.who`/`facts[].who` ⊆ 工作集 ∪ `new_names`、`pack_hash`、delta 合法性（含 delta 各字段列表/对象类型及 facts 必须为 `{who, text, pin}` 对象等结构合法性校验）、账本增量的 `quote` 逐字证据（提供了就必须命中正文）、章拍 `effects` 的 `expected_delta` 收口（有计划结果必须原样回填且语义条目带 quote）、**人物连续性与防失忆门禁**（`continuity_character_amnesia`：严禁已知登场人物被写成陌生人初见或询问姓名；**方向性**——只有两个已登场角色之间出现这类写法才算违规，已知角色询问/打量一个真新面孔是合法登场写法）。字数带默认硬校验，2500～8000 之外触发 `word_count_low/high`。**beats 条数不是机检项**：1 条也能 accepted，每章 5 场戏是编拍纪律。入账冲突（死人说话）→ `blocked`，可 `retry-authorize`；不跳章。`memory.voice_concepts` 只收更短的正面概念；以「不要」「禁止」开头的条目脚本丢弃（这是"不建禁词表"这条设计在代码里的落点，不是文案偏好），**但 ack 响应会回 `voice_note_dropped` 明说丢弃并指路 intent 红线**，不再静默吞输入。它进入 `voice.json` 的 `session_notes`（见 [runtime-contract.md 文风记忆](runtime-contract.md#文风记忆bookmemoryvoicejson)），完整保留且**不会**替换蒸馏的长驻概念。

**单次提交**：`chapter submit --output <json>` 直接运行完整机检。名字越界、引文错误、
字段形状等纯组装问题返回 `fix_assembly`，保留终稿与正文返工预算，在同一 JSON 原位修正后重交。
`chapter check-submit --output <json>` 是可选只读诊断，不生成提交回执，也不改变阶段；
`blocked` 相位同样可用——返工耗尽停线后先用它定位提交稿问题，再决定 retry-authorize 还是改字段。

submit 失败后的回流分级（组装契约 issue vs 正文 issue）见
[pipeline-gates.md 流水线行为](pipeline-gates.md#流水线行为单一-compact-泳线)。

### 通用输出契约（`_WRITE_CONTRACT`）抽象化规范

canonical pack 的 `write_contract` 已彻底实现通用抽象化；流水线把它（keys/delta_schema）
放进 `assemble_pack_path`，gates 替换为组装专用不变量：
- **`keys`**：顶层 5 大输出键规范（`l1_summary`, `state_delta`, `memory`, `pack_hash`, `beats_hit`）。
  `prose` **不是必填键**——正文真源是阶段二终稿，脚本在 submit 时读盘注入（见上文）；
- **`delta_schema`**：纯结构化的账本增量定义（明确 `moves`, `facts`, `debts`, `hooks`, `relations`, `knowledge`, `named`, `new_names`, `deaths` 的对象与类型约束）；
- **`gates`**：系统不变量在组装视图注入——组装 gates（正文由脚本注入不必回显、delta/beats_hit
  按终稿精确填、allowed_delta_names 白名单、expected_delta 原样回填、plot_findings 分级、
  submit 机检、pack_hash 逐字回显）。首尾接榫/角色防失忆/人设/术语等正文问题由
  组装情节自检与 submit 机检分层把关。
  跨题材 100% 通用，不绑特定情节/人名/题材。

### 【硬】写者禁则：正文与设定库的边界

文风怎么写全在项目所选手册（润色视图的 `voice_writing_text` 全文，来源路径见 `voice_writing_manual`；随包文风的解析与润色真源为 `references/voices/<id>.md`，可按 [文风手册索引](../SKILL.md#文风手册索引) 选读）。这里只留手册**管不到**的一条边界——它是 novel-ledger 特有的，因为只有这个 skill 才有"正典库"这个东西：

- **设定术语只活在设定库，不进正文**：把设定词当口头禅用；物理法则、世界规则、总表这类说明书留在 `book/kb/canon/`，正文里一个字都不出现。
- **宏观设定靠近角色写**：写腿沉、耳鸣、手抖，不写说明书名词。
- 章拍的 `beats[].must` 万一是设定词，让人物用口头禅把它带出来，不写设定说明（同一条边界的另一面，见上文 `beats[].must`）。

前两条脚本检测不到（不像 `must` 词有硬门禁），靠组装视图的 `write_contract` 与本节传达。

## 审校契约 v2

plot_findings 必填，检查无发现为 []。每项含 code、severity、hint、逐字 quote（≥6 字）；UNVERIFIABLE 可省 quote，但须明确缺少的判据与所需材料。缺字段或证据回 assemble，不消耗正文返工预算。合法发现独立存入 editorial/findings.jsonl；review list 按记录分页提供未决项，review resolve 记录 accepted/deferred/closed、actor、reason。legacy review_contract_version=1 仅为旧五字段工件的显式兼容，不视省略为完成审稿。
