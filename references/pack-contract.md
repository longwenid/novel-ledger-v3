# Pack 契约

canonical pack、阶段视图拆分、按需材料与自动分层记忆。持久化地址和历史召回见 [SQLite 契约](sqlite-memory.md)。submit 输出契约见 [write-output.md](write-output.md)。
## Pack（`book/pack/current.json`）

脚本装配的 **canonical pack**（哈希 / submit / 归档 / ack 的唯一权威）。`pack_hash` 锁死。
缺主角 NOW 卡或缺文风概念则拒绝 begin。

**阶段视图拆分**：同一个 canonical pack 派生 draft、polish、assemble 三个只读视图，流水线只按视图消费——
随包文风由两份文件构成：`<id>.md` 在开书或切换文风时解析为公式、概念与自检，
并作为润色手册；`<id>.content.md` 给执笔。两份文件按内容摘要进入
`inputs_fingerprint`；常规章节按阶段分发手册，角色只读自己阶段的视图。
外部自备文风仍可提供可选的 `.writer.md`，此时润色阶段使用它。
视图文件走**保序序列化**（`util.atomic_json_ordered`，不排序键）：把每章一字不差的手册/契约
排在文件最前，逐章变化的 `chapter`/拍点/锚点排到后面，形成尽可能长的**稳定前缀**供宿主
prompt cache 命中。canonical pack（`current.json` / 归档）**仍走排序键**
（`atomic_json`）——它是 `pack_hash` 权威，字节顺序不能动；`pack_hash` 由内存 dict 计算，
与视图落盘顺序无关。

- `book/pack/draft-<ch>.json`（内容写作视图，schema `novel-ledger.view.draft.v2`）：**不含润色手册字段**
  （`voice_writing_text` / `voice_writing_manual` / `voice_manual_text` / `voice_concepts` /
  `voice_checklist` 等一律不给；内容取向手册
  `voice_content_text` 若配置则按需注入），不含 pack_hash 回显与机检负担。脚本把 canonical 中的
  拍点 / NOW / KB / 上章尾 / 状态账本 / 分层记忆 / 历史召回**单向确定性渲染**为一个 `writing_brief`
  完整句式任务书；这些原始结构不再重复出现在 draft 视图。渲染不调用模型，canonical JSON 仍是
  唯一真源。在场 `character_profiles` 与本章选出的 `knowledge` 也只渲染进简报，
  用于分辨人物稳定说话倾向与截至本章的认知。视图另附 `draft_contract`，执笔只输出纯文本草稿。
- `book/pack/polish-<ch>.json`（润色视图）：含**润色手册**（`voice_writing_text` 是全文，
  `voice_writing_manual` 是来源路径；随包文风分别取 `<id>.md` 的全文与路径），以及在场人物
  `character_profiles` 的可观察表达字段与不许改情节的内容锚点；
  润色只看语域、句式、受压变化，隐藏动机和样句不进入此视图；润色手册管全章叙述语感；本视图不带 `knowledge` 或 `historical_recall`，不允许
  文风阶段重判谁知道什么。
  只输出纯文本终稿，**零 JSON / 零 pack_hash / 零审校配额表**。
  **阶段字段固定**：运行时 canonical pack 始终带 `voice_writing_text` 和
  `voice_writing_manual`；随包文风取 `<id>.md`，外部自备文风若有 `.writer.md` 则取该
  companion，否则取其主手册。`voice_content_text` 若出现，只给 draft 视图。
  这些键出现在阶段视图时必须与 canonical 逐字一致，不能把一层手册内文冒名放进另一键。
  仅为旧版或手写 pack 保留回退：没有 `voice_writing_text`、只有 `voice_manual_text` 时，
  polish 视图用后者填入 `voice_writing_text`；正常装配不写 `voice_manual_text`。
  手册全文始终以文件 sha256 进 `inputs_fingerprint`，上游变更仍会触发检测。
  **本视图不带 `kb_slice` 与 `near_summaries`**：该阶段的内容契约是"冻结、只改文风"，
  正典不是它的判据，递到面前反而是在邀请它推理剧情（越界）；「不得依据一般常识改写
  世界细节，拿不准一律原样保留」由 `polish_contract` 显式给出。
  must 锚点（`beats`）与 `word_band` 必须保留。

`config.style_check`（`true`，或 `auto` 且 voice=shijing）时 `polish-submit` 原地执行两层文风机检
（明确话术的硬红线、节奏与对白统计提示、人写节奏锚、findings 分级、累计节奏指纹）。它们的**行为语义**
——拦什么、失败怎么回流、blocked 怎么解锁、配额旋钮怎么调——全部见
[pipeline-gates.md 文风机检](pipeline-gates.md#文风机检polish-submit-收口)。

- `book/pack/assemble-<ch>.json`（组装视图）：零 voice_* 字段；含 canonical `pack_hash`、
  `inputs_fingerprint` 与输出契约（keys / delta_schema + 组装 gates）。正文不必回显——
  submit 从 `polished_output_path` 读盘注入；输入里的 `prose` 不参与提交。
  结构字段含 `beats`、人物与状态工作集、`items` / `conditions`、正典切片与世界约束；
  `ledger_refs` 投影未结资产，在场者的短 `character_profiles`、相关 `knowledge` 与
  `historical_recall` 与 `verification_brief` 供事实编辑核对人物行为、认知与情节；人物卡只是参照，不能替代正文证据。
  同一人物事实在本视图只保留一份；`allowed_delta_names` 明示机检认可的名字。

状态机按 `await_draft → await_polish → await_assembly → submitted` 推进：`next` 依次返回
`action=draft / polish / assemble`，当前章节 worker 分别用
`chapter draft-submit` / `chapter polish-submit` / `chapter submit` 推进。
任何内联角色都不需要打开 canonical `current.json`。

```json
{
  "schema": "novel-ledger.pack.v1",
  "chapter": 1,
  "instruction": "手册即任务的短指引（润色手册在 voice_writing_text，不重复拼接）",
  "voice_concepts": ["……"],
  "style_formula": "所选手册的风格公式",
  "voice_checklist": ["手册 ## 自检 的每一条"],
  "voice_skill_manual": "当前 skill 内 …/references/voices/<id>.md（按 voice_id 重解析，不用 voice.json 冻结路径）",
  "voice_writing_manual": "当前 skill 内 …/references/voices/<id>.md（随包文风的润色手册路径）",
  "voice_writing_text": "所选润色手册全文（随包文风为 <id>.md）",
  "voice_content_manual": "当前 skill 内 …/references/voices/<id>.content.md（若配置）",
  "voice_content_text": "内容层手册全文（若配置，执笔视图用）",
  "now_card": {
    "name": "主角名",
    "goal": "本卷目标",
    "wound": "",
    "speech": [],
    "location": "本章地点",
    "owes": "",
    "facts": [],
    "dead": false,
    "rendered": "……"
  },
  "present_cards": [{"name": "在场者", "location": "", "is_first_appearance": false, "first_seen_chapter": 1, "relations_with_protagonist": ["盟友"], "facts": [], "dead": false, "rendered": "角色【在场者】。现在某地。与主角的关系：盟友。开茶馆为生。"}],
  "character_profiles": {"主角": {"background_register": "", "speech_habits": "", "under_pressure": "", "desire_and_mask": "", "sample_quote": ""}},
  "knowledge": [{"who": "主角", "topic_id": "topic-receipt-origin", "claim": "凭据出自旧账", "stance": "suspects", "source": "亲见落款", "quote": "旧账落款果然在凭据背面", "updated_chapter": 7}],
  "character_continuity": [{"name": "在场者", "is_first_appearance": false, "first_seen_chapter": 1, "continuity_directive": "【重大连续性纪律】……"}],
  "debts": [{"id": "d1", "who": "", "text": "", "status": "open", "due": 50}],
  "hooks": [{"id": "h1", "text": "埋下的伏笔/悬念", "due": 50, "status": "open"}],
  "relations": [{"who": "A", "target": "B", "kind": "师兄弟", "status": "open"}],
  "near_summaries": [{"chapter": 1, "l1_summary": "……"}],
  "memory_layers": {
    "phase_summary": {"relation": "current", "summary": "……"},
    "volume_summary": {"relation": "current", "summary": "……"},
    "book_spine": {"chapter_count": 120, "summary": "……"}
  },
  "volume_spine": "",
  "recap": "（可选，卷首/长间隔章由章拍提供，≤1200 字）",
  "beats": [{"id": "b1", "required": true, "text": "本章必须发生的事", "must": "须出现在正文的词", "rendered": "第1场：本章必须发生的事（关键词须带出：须出现在正文的词）"}],
  "kb_slice": [{"id": "c1", "kind": "world", "title": "", "excerpt": "", "tags": []}],
  "kb_slice_meta": {"matched_cards": 1, "selected_cards": 1, "omitted_cards": 0, "explicit_refs": []},
  "state_near": {"pinned": [], "recent": [], "occupancy": {}, "open_debt_ids": []},
  "word_band": {"min": 2500, "max": 8000},
  "write_contract": {"keys": ["…"], "gates": ["…"]},
  "inputs_fingerprint": "sha256:…",
  "context_isolation_required": true,
  "read_only": true,
  "forbid_library_browse": true,
  "pack_hash": "sha256:…"
}
```

默认 `stage-agent` 的 `context_isolation_required=true` 要求宿主为各阶段创建空上下文；
阶段视图限制投喂内容，本身不能创建模型会话或沙箱。派发策略在 `chapter next` 的 action
信封中表达；普通调用返回：

```json
{
  "execution": {
    "mode": "stage-agent",
    "role": "drafting",
    "spawn_allowed": true,
    "context_isolation": "fresh_session",
    "history_inheritance_allowed": false,
    "session_scope": "stage"
  }
}
```

这个权限只允许当前阶段派一个空上下文 worker，提交后由调度器另建下一阶段会话。
本会话已是 supervisor 派出的 worker 时 `mode=inline`、`spawn_allowed=false`，仅表示
不得重复派发；`configured_mode=stage-agent` 和 `context_isolation=fresh_session` 保留会话契约。
宿主无法创建空会话时应暂停报告能力缺口；用户显式选择 `worker-agent` 或 `inline` 才可
兼容降级，返回 `context_isolation=stage_pack`、`context_isolation_required=false`，只有职责视图隔离。
协议与宿主运行器的职责边界见 [execution-architecture.md](execution-architecture.md)。

按需加载：`kb_slice` 在 **pack 组装时按需检索**（本章 `beats` / `must` / 出场人名 / 地点；tags 匹配卡片 id/title/tags/aliases）。结构化正典中的 `aliases` 原样编译并参与检索；同义改写未命中时，章拍可用 `kb_refs` 按 id 引入关键规则。显式引用必须存在且不重复，入选卡片保留完整内容和来源路径；相关总表与世界规则按场景需要读取，不按摘录字数削短。`kb_slice_meta` 说明检索条件、候选与已选材料，关键规则不足时可按来源补读并在章拍补 `kb_refs`。

`near_summaries` 负责紧邻接榫，`memory_layers` 提供阶段、卷与全书方向。债务、伏笔、关系、认知、状态和地点占用按在场者、事件、到期事项及当前问题选择；显式章拍结果涉及的资产优先。`story_focus` 保留本章四层脊柱的相关事件、阶段变化、线路触点与时钟事项。已选内容完整进入工作包，不因字符或字节预算丢失。每个角色从自身阶段视图和来源路径补读任务所需材料，`write_contract` 及职责边界继续生效。

材料补读使用 `context read --source intent|outline|canon`，可选 `--name` 定位正典相对文件、
`--section` 定位完整标题节；不指定节时读取完整来源。`plan volume-outline --volume N`
返回全卷索引和所选卷完整总纲。历史事实用 `memory recall` 按人物、话题、资产或事件查询，
需要正文兑现证据时再按来源章号补读。命令示例见 [SQLite 契约](sqlite-memory.md#完整来源补读)。

本章 `present` 只列实际参与场景的人，章拍字段的数量与形状仍按计划业务契约校验。`present_cards` 和 `character_continuity` 的主角关系列表复用同一份任务相关关系；若本章涉及更早的关系，按关系 id、人物和历史事件补读。材料未被当前查询选中不能证明关系不存在。

`character_profiles` 只选在场规范名（含主角）的可选短档案；项目未填写时不捏造个性。
`knowledge` 是事件快照按 `(who, topic_id)` 更新后的本章相关认知，优先本章可选
`chapters[].knowledge_refs` 的话题，再取在场者相关认知；选中条目的 claim、来源与原文证据完整保留。
需要较早的认知变化时按人物、话题及事件 ID 召回历史。
认知条目的默认选择数仍为 16；显式 `knowledge_refs` 对应在场者认知超过数量门时，
按业务契约缩小场景范围或拆场，不静默漏掉已登记认知。这项数量校验不裁短任何已选记录。
这份切片反映**已登记且被选中**的内容，缺席既不证明人物无知，也不许可人物无因得知。
关键旧认知没入包时，回策划补 `knowledge_refs` 或请总编辑定向核对；声线与认知使用见
[角色声线与人物鲜活度](craft/character-voice.md)。

### 自动分层内容记忆（`book/memory/hierarchy.json`）

`hierarchy.json` 是从已提交 `book/summaries/l1/**/ch-NNNN.json` 派生的可重建视图，不是第二份正文真源。
commit 不调用模型，直接把本章已有 `l1_summary` 滚入当前阶段，再滚入卷摘要和全书脊柱：

- `near_summaries`：仍保留最近 2–3 章，负责紧邻接榫；
- `phase_summary`：优先按章拍 `phase_id` 分段；都未声明时，在每卷内每 20 章自动成段，保留阶段内的完整章摘要；
- `volume_summary`：由本卷阶段摘要确定性汇总，保留来源章号与完整内容；
- `book_spine`：合并书名、`world_spine`、各卷总纲、`book_outline` 与已滚卷史，供相关任务选择读取。

分层材料按任务选取阶段与卷，并提供来源路径供进一步阅读；完整 L1 仍在分章文件中，账本事实/债务/伏笔继续由专门索引负责，
分层摘要不替代结构化账本。`memory_layers` 的内容只通过 `writing_brief` 进入 draft；polish、assemble
均不接收，避免重复投喂。目标章只读取 `< 目标章` 的记忆快照：即使 commit 在推进 HEAD 前崩溃、本章摘要已滚层，
重放时也不会把本章输出反向算成本章输入。`retry-authorize`/同章回滚删除 L1 后会从剩余摘要重建层级。

`inputs_fingerprint`＝pack 装配所依赖上游（本章与书级 plan 字段、目标章之前的账本事件与治理裁决、分层记忆、KB、voice profile、材料选择配置）的 sha256。治理按 `effective_chapter` 纳入；本章 commit 产生的事件是输出，不纳入本章输入，保证尾部崩溃重放不会使自身工作包失效。装配后上游被改 → submit 时自动重装配，**回退深度由新旧视图内容逐层比对决定**（指纹只是触发器，防误删已过闸正文）：draft 视图变（本章拍点/切片变了）→ `verdict=stale_pack` 回 `draft` 整链重写；仅润色及后续视图变 → 回 `polish` 保留草稿；本章视图全部未变（如 kb 加了不进本章切片的无关卡、别章 plan 调整）→ 视图与 canonical pack 静默换新、staging 与 phase 保持，submit 按新包继续机检（组装件补对 `pack_hash` 即可，正文零重写）。均不消耗 rewrite 配额；submit 之后、commit 之前又被改 → commit 停线 `blocked(reason=stale_inputs)` 等人核对。

`word_band` 默认是 **2500～8000 字硬门禁**（`config.word_band_enforce: true`）：偏短或超长返回
`word_count_low/high`；草稿和终稿在各自阶段原位修正。偏短优先补足实体场景、动作来往与必要细节，不注水凑数；偏长则拆章或删去重复说明。
只有作者明确要求实验性短章时才可关闭 `word_band_enforce`，关闭后字数问题降为 warning。

**字数纪律必须写进执笔真正读的那份文本**：`writing_brief` 的定位段现在给的是
**安全目标 + 场次尺度 + 越线后果**（"目标 3200 个中文字（硬闸 2500 至 8000）：共 5 场戏，每场约 700 汉字。
写不足 2500 会被 `chapter draft-submit` 当场拒收"）。只给区间会被写成下限（首轮草稿曾普遍
落在下限之下，需要补整场戏）。同一套口径由 `content/pack.py` 的
`word_targets()` / `scene_budget()` 单点提供，`chapter next` 的行动载荷与 `writing_brief` 共用，禁止各写一套。

`recap` 来自章拍（`chapters[].recap`）：卷首或长间隔章写一条前情提要，给写者卷级锚点，防止跨卷漂移；缺省不注入。`debts[].due` 若有则透传给写者（到期章提示）。

`beats[].must` 是硬门禁（子串必须出现在正文）：**选口语能自然带出的词**（人名/地名/物件/动作），不要填设定术语或抽象词；若 must 是设定词，写者用口头禅带出，不写设定说明。写者**不要把 must 词当标签贴进正文**。每条 beat 的 `text` 应是一场戏，不是标签。

### 历史关联记忆（`historical_recall`）

默认按主角、在场者、认知话题从 SQLite 召回截至本章之前的事件。
章拍 `memory_refs` 钉住旧事件，`memory_query` 补查正文；选中记录的全文、完整引文与来源进入输入指纹。
引用必须存在且在已提交历史中。诊断查询可按记录分页；显式选择的证据不能因字数被丢弃。draft 将结果渲染进 `writing_brief`，
assemble 保留证据结构，polish 排除。`verified` 只表示当前正文引文存在，`record_only`
不能当成原文已核验；人物认知附 stance/source 与是否最新，旧认知不能当成当前事实。
详见 [SQLite 与历史记忆](sqlite-memory.md)。
