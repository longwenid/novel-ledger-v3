# 章拍契约（plan.v2）
plan/chapters.json 的数据契约：章拍 schema、字段档位、book_outline 与阶段签发。
扩纲硬校验、卷合同与编拍纪律见 [extend-contract.md](extend-contract.md)；流水线行为见 [pipeline-gates.md](pipeline-gates.md)。
## 章拍真源（`book/plan/chapters.json`）
整条流水线的章拍真源。`book hatch` 先签齐全书分卷总纲再生成起手章拍，`plan extend` 按已签卷纲追加细化。以下只展示章级字段；新书全卷合同见 [分卷总纲契约](extend-contract.md) 与 [完整开书卷纲示例](../templates/hatch.volume-outlines.example.json)。

```json
{
  "schema": "novel-ledger.plan.v2",
  "title": "书名（可空）",
  "protagonist": "主角",
  "character_profiles": {"主角": {"background_register": "熟悉行当和称呼", "speech_habits": "句式与节奏倾向", "under_pressure": "受压时的变化", "desire_and_mask": "想得到什么、对外如何遮掩", "sample_quote": ""}},
  "volume_spine": "第一卷：主角要拿回被扣的凭据。",
  "volumes": {
    "1": {"spine": "第一卷：主角要拿回被扣的凭据。", "goal": "第一卷：拿回凭据。", "recap": ""},
    "第二卷": {"spine": "第二卷：……", "goal": "第二卷：……", "recap": ""}
  },
  "chapters": [
    {
      "chapter": 1,
      "volume": 1,
      "location": "市集",
      "present": ["主角", "掌柜"],
      "tags": ["市集", "凭据"],
      "phase_id": "phase-01",
      "story_stage": "第一幕",
      "thread_refs": ["thread-main", "thread-trust"],
      "clock_refs": ["clock-protagonist"],
      "goal": "本章主角要的东西（缺省用 volume_spine）",
      "wound": "主角的伤口（缺省留空）",
      "speech": ["口头禅一", "口头禅二"],
      "recap": "（可选，卷首/长间隔章的前情提要，保留提供的完整原文）",
      "beats": [
        {
          "id": "b1",
          "required": true,
          "text": "主角在柜台当面拒收改期，掌柜不让他把凭据拿走",
          "must": "拒收",
          "effects": {
            "moves": [{"who": "主角", "to": "市集"}],
            "facts": [{"who": "主角", "text": "主角当面拒绝了改期", "pin": false}],
            "debts": [{"id": "d1", "who": "掌柜", "text": "欠主角一个明早的答复", "status": "open", "due": "第 2 章"}],
            "hooks": [{"id": "h1", "text": "掌柜明早的答复是否兑现", "due": 2, "status": "open"}],
            "relations": [{"who": "主角", "target": "掌柜", "kind": "旧交", "status": "open"}],
            "knowledge": [{"who": "主角", "topic_id": "topic-receipt-origin", "claim": "凭据出自旧账", "stance": "suspects", "source": "亲眼看到落款"}]
          }
        },
        {"id": "b2", "required": true, "text": "凭据还扣在掌柜手里，两人为此来回争执，当场拿不走", "must": "凭据"},
        {"id": "b3", "required": true, "text": "有人来催下一趟期限，主角被夹在中间走不掉", "must": "期限"},
        {"id": "b4", "required": true, "text": "散场时掌柜丢下一句未了的话，明天还得来", "must": "明天"}
      ]
    }
  ]
}
```

阶段签发形状：

```json
"phases": [{
  "id": "phase-01",
  "name": "第一阶段",
  "story_stage": "第一幕",
  "chapter_start": 1,
  "chapter_end": 12,
  "objective": "阶段目标",
  "climax": "阶段高潮",
  "mainline_event_ref": "main-event-01",
  "subplot_event_refs": ["subplot-event-01"],
  "timeline_event_refs": ["timeline-event-01"],
  "tension_stage": "第一幕",
  "entry_state": "阶段开始时的可验证状态",
  "exit_state": "阶段结束时必须形成的可验证状态",
  "event_changes": {
    "main-event-01": "主线在本阶段形成的变化",
    "subplot-event-01": "支线在本阶段形成的变化",
    "timeline-event-01": "时间线在本阶段形成的变化"
  },
  "tension_change": "本阶段如何沿本幕曲线完成施压、转折与兑现"
}]
```

字段分三档：

| 字段 | 档位 | 缺失后果 |
|---|---|---|
| `protagonist`（顶层） | **必填** | hatch 缺失时以 `missing_protagonist` 拒绝开书；手写盘缺主角名会在 `chapter next` 装 NOW 卡时以 `missing_now_card` 拒绝开章 |
| `chapters[].chapter` | **必填** | 正整数，编号只能向后追加（`plan extend` 校验 `chapter_not_append`） |
| `chapters[].volume` | 建议 | 卷标识（`1` / `"第一卷"` / 任意文本）；决定输出目录 `book/chapters/vol-0001/…`。缺省按第一卷 |
| `chapters[].beats` | **必填** | 非空列表；空则 `missing_beats` 拒绝装配 pack。**建议 5 条**，每条一场戏（见下）；条数不为 5 不拒装、submit 也不因条数 rejected |
| `beats[].effects` | 可选 | 本场戏应留下的账本结果（moves/facts/debts/hooks/relations/items/conditions/knowledge/new_names/deaths/revivals/nonliving，形状同 `state_delta`，`knowledge` 的计划结果不带 `quote`）。装配成 `expected_delta`：执笔看完整句式，事实组装看结构化条目；未兑现回草稿，兑现无 `quote` 回组装 |
| `chapters[].location` | **强烈建议** | 不报错但静默降级：KB 切片少一路 needle（`slice_kb` 命中变差）、债务打分丢掉「本章地点」那 4 分（`select_debts`）、`occupancy` 切不到本章地点 |
| `chapters[].present` | **强烈建议** | 不报错但静默降级：关系切片退化成只有主角（`select_relations`）、`present_cards` 为空、债务打分丢掉「在场者」那 5 分、写者输出里点名其他人会撞 `unnamed_in_pack` |
| `chapters[].tags` | 建议 | 只影响 KB 切片命中质量 |
| `chapters[].knowledge_refs` | 可选 | 不超过 8 个非空、互异 `topic_id`，定向召回本章场景需要的旧认知；缺席不等于人物不知道，关键旧消息未入包时应补引用或报视野不足 |
| `chapters[].memory_refs` | 可选 | 最多 16 个唯一旧事件 ID（事件 hash），优先召回；未知、未来或超预算硬失败 |
| `chapters[].memory_query` | 可选 | 词法检索字符串，用于召回旧正文；来源与证据边界见 [SQLite 契约](sqlite-memory.md) |
| `chapters[].phase_id` | event-spine 项目**必填** | 必须命中 `plan.phases[].id`，且章号落在该阶段范围内 |
| `chapters[].story_stage` | event-spine 项目**必填** | 本章所属 `book_outline.acts[].name`，必须与阶段一致；起手三章自动取第一幕 |
| `chapters[].thread_refs` | event-spine 项目**必填** | 本章推进的主／支线 ID，1–3 项；只能引用本阶段四层对齐覆盖的线路 |
| `chapters[].clock_refs` | event-spine 项目**必填** | 本章推进的时间线 ID，1–2 项；只能引用本阶段覆盖的时钟 |
| `chapters[].kb_refs` | 可选 | 必须是 `book/kb/cards.json` 中存在且不重复的卡片 id 列表；用于把同义改写难以命中的关键规则确定性钉入本章切片。未知 id、重复 id 或数量超过 `pack_caps.kb_slice` 均拒绝开章 |
| `goal` / `wound` / `speech` | 可选 | NOW 卡对应字段留空（`goal` 回退 `volume_spine`；`wound`/`speech` 只取章拍，缺省留空） |
| `recap` | 可选 | 卷首/长间隔章不注入前情提要，跨卷易漂移 |
| `title` / `volume_spine` | 可选 | `volume_spine` 是 NOW 卡 `goal` 的兜底；**不再**作为 KB 切片 needle（卷脊里的世界观词会把说明书拖进开篇） |
| `volumes` / `volume_outline_contract` | 新 hatch **必填** | 完整全卷注册表，键从 `vol-0001` 连续到已签卷数；每卷 `outline` 至少 500 汉字并提供剧情职责、承接、推进、终局方向与交接。元数据绑定全书预算和不可变总纲 hash；只允许在原卷内补 `detail_outline`/`recap`，见 [分卷总纲契约](extend-contract.md)。旧书保留编号别名兼容并诊断缺纲 |
| `schema`（顶层） | hatch 项目**必填** | 固定 `novel-ledger.plan.v2`；它锁定四层事件脊柱合同，删除 `event_spine` 不会降级放行 |
| `book_outline`（顶层） | plan.v2 **必填** | 书级大纲骨架（创意编辑起草、总编辑签发）。结构见下；规模与四层合同矛盾均硬拒，不整张注入逐章 pack |
| `narrative_contract`（顶层） | 新 hatch 自动保存 | `novel-ledger.narrative-contract.v1`；已确认的读者承诺、人物动力、主线与终局、完整红线、必要世界规则，及已声明的关系/主题。已选合同原文完整保留，无字符上限；扩纲与书级审稿通过共同视图读取 |
| `character_profiles`（顶层） | 可选 | 按规范人名索引声线档案：`background_register`、`speech_habits`、`under_pressure`、`desire_and_mask`、`sample_quote`；字段为字符串，无字符上限。hatch 可提供，也可后来补进 plan；仅作为表达参照，不当成世界事实或固定口癖配额；详见 [角色声线](craft/character-voice.md) |
| `narrative_pov` / `content_fence`（顶层） | 可选（hatch 门 8 声明） | 视角结构与平台尺度档。hatch 校验枚举后原样随 plan 落盘；装配时渲染成一句话进 canonical pack（计入 `pack_hash`）并出现在 writing_brief 定位段。未声明则不落盘、不渲染 |

**`book_outline`（plan.v2 必填）＝全书大纲的机检骨架**：把 `book_words` 切成幕与卷，

```json
"book_outline": {
  "book_words": 3200000,
  "chapter_words_target": 3200,
  "total_chapters": 1000,
  "acts": [
    {"name": "第一幕", "volumes": "1-3", "arc": "起点态→第一次跃迁", "stakes": "局部纠纷→区域势力"}
  ],
  "milestones": [
    {"chapter": 60, "kind": "volume_payoff", "note": "第一卷大兑现"},
    {"chapter": 950, "kind": "book_climax", "note": ""}
  ],
  "event_spine": {
    "schema": "novel-ledger.event-spine.v1",
    "mainline": "唯一主线；2–8 个带唯一 id 的幕级事件",
    "subplots": "1–6 条支线；每条 2–8 个带唯一 id 的幕级事件",
    "timelines": "protagonist / antagonist / world 各一条；事件带唯一 id、期限与后果",
    "tension_curve": "按 acts 顺序每幕一项"
  }
}
```

- 新版 `acts[].volumes` 必须覆盖每个已签卷，卷表必须齐全且纲不少于 500 汉字；旧书缺 `spine`/`goal` 仍告警 `book_outline_missing_volume`。
- `milestones[].kind` 取值 `volume_payoff` / `turning_point` / `book_climax`；`chapter` 须为正且不超 `total_chapters`（声明了才校验）。
- `milestones` 空缺会报告 `book_outline_character_arc_unspecified`：人物变化或恒定弧缺少可追踪检查点，应声明关键选择、代价与终局证据；不强制固定数量，也不把章号已到等同于人物成长已成立。
- 各卷 `word_budget` 之和应等于 `book_outline.book_words`（缺则与 `config.book_words` 比；不符告警 `book_outline_budget_mismatch`）。
- 新书的 `chapter_words_target` 取写作字数带的安全目标（默认 `2500 + 700 = 3200`）；已签约旧书继续使用其原值，不会因升级重写章数。初签 `total_chapters` 必须等于 `ceil(book_words / chapter_words_target)`，每卷 `chapters_budget` 必须等于 `ceil(word_budget / chapter_words_target)`。合法短章累积导致原章数不足时，只能用 `plan rebudget` 从实写字数续签有界章额度；公式、留痕与未来里程碑映射见 [扩纲合同](extend-contract.md#实写不足时续签章数plan-rebudget)，不得手改总章数或目标字数绕闸。
- `book_climax` 和全书终局措辞只能进入规划末 15%；出现全书终局时，卷字数预算之和须在目标 10% 容差内，卷章数预算之和须在总章数 20% 容差内。
- `long_arc_question` 仅在开书 manifest 的 `long_arc_question.mode=present` 时落入 `book_outline`，形状为 `{core_mystery, surface_illusion, decryption_ladder:[{stage,truth}]}`。
  它是供策划看局部揭示阶梯的**编辑层假说**，不进逐章写者 pack，也不把 `surface_illusion` 写成世界硬规则：角色对旧事实的解释可修正，已发生的行动、物件状态、引文与客观世界规则不可改写。显眼主谜仍须逐卷给出局部答案。
- `long_term_commitments` 是可选的跨卷低调伏笔账，缺省为空。开书输入的基础必需项是非空 `promise`、正整数 `planted_volume` / `resolved_volume`（埋设卷不晚于结算卷）。
  强烈建议给 `id`、`arc_ref`、`seed_use`、`final_condition`，可选 `description`；省略 `id` 时 hatch 稳定编号。落盘后的 `id` 与首次埋种、后来显形、最终兑现的 `effects.hooks` / `state_delta.hooks` 共用；`arc_ref` 引用现有 `event_spine.mainline/subplots` 线路。
  `seed_use` 是首次线索的**当场作用**，不能写成“以后会很重要”；`final_condition` 是终局可核状态，不能只写“揭秘”。省略增强项可兼容旧 manifest，但会降低扩纲定向召回与终局审稿质量。卷次只定书级范围，不要求提前编出数百章细纲。
  公开誓言、当下主谜与限时危机仍按近期 hook 和每卷局部结算处理，不能用长线记录把显性承诺静默推迟数百章。长期 hook 的 `due` 若已填，表示最终承诺到期章，可等具体收束阶段确定后再填。

**叙事合同视图**：策划简报的 `planning_context.narrative_contract` 与独立剧情复核复用
`novel-ledger.narrative-contract-view.v1`。新书 `mode=structured`，`contract` 包含
`reader_promise/protagonist_engine/main_arc/hard_constraints/world_rules`，以及已声明的 `core_relationships/theme`；`fingerprint` 绑定原文合同。
未升级的旧书标为 `mode=legacy_unstructured`，`contract=null`，提供意图书、大纲和正典的原文 `sources[].text`，
来源原文完整送入该视图，无字符/字节裁剪；`limitations` 说明未结构化事实，不由脚本猜出作者未声明的结构。全卷索引见 `planning_context.volume_outlines`，当前/下一卷完整总纲在 `planning_context.volumes`；其他卷按 `plan volume-outline --volume N` 读取完整总纲，源材料用 `context read --source intent|outline|canon` 按问题补读。
结构化合同保存意图书、大纲、完整原始正典的 `source_baselines`；任一来源更新后，规划与审稿以 `narrative_contract_source_drift` 停线。`kb sync` 只更新编译卡片，不自动修改作者合同。
先将编辑文件用 `database import-source` 导入，正典改动再 `kb sync`；随后以 `plan amend-contract --file 完整合同.json --actor 签发者 --reason 修改理由` 显式重签。保存来源基线和前后完整合同审计，原审稿回执失效；无基线的旧结构化合同同样需要重签。
- `event_spine` 启用四层全书事件脊柱硬合同：`acts` 必须有 2–5 个唯一名称；`mainline` 恰一条，
  `subplots` 为 1–6 条，每条声明 `purpose/mainline_link/open_stage/payoff_stage/participants` 和 2–8 个
  按幕排序的 `{id,stage,event,change}`；`timelines` 必须恰有 `protagonist/antagonist/world` 三种，
  每条含 `start_state` 与 2–8 个 `{id,order,stage,event,deadline,consequence}`；所有事件 ID 全书唯一。
  `tension_curve` 与幕一一对应，`level` 为 1–5，
  `mode` 取 `build/reversal/payoff/breather/climax`，强度至少发生一次变化、达到 4 或 5，并包含一次
  `payoff` 或 `climax`。线路首末触点必须分别对齐 `open_stage` / `payoff_stage`。
- `plan.phases[]` 是强制中间层：每个阶段必须有唯一 `id`、连续且不跨卷的章节范围、目标、高潮，
  最多覆盖 20 章；还必须提供 `entry_state/exit_state/event_changes/tension_change`，并同时提供
  `mainline_event_ref`、1–3 个 `subplot_event_refs`、1–3 个 `timeline_event_refs` 和 `tension_stage`。
  `event_changes` 必须逐一覆盖所有选定事件 ID。四类引用必须存在、层级正确且属于同一 `story_stage`；
  阶段范围连续不重叠，故事幕与同一线路／时间线的事件顺序只能保持或前进，不能倒退。
- 每次 `plan extend` 的 JSON 必须是 `{phase:{...}, chapters:[...]}`；`phase.chapter_start/end` 必须与
  本批连续新增章节完全一致，所有章节 `phase_id` 必须等于该阶段 ID。本批 `thread_refs/clock_refs` 的
  并集必须覆盖阶段选中的全部主／支线和时间线；任一层只签名未落章、错层或越界都整批不写盘。
- 开章时控制面从阶段与章节引用确定性生成 `pack.story_focus`：只含本章所选线路、时钟、当前幕触点与
  起伏点，并严格限制到 Phase Brief 选中的事件 ID。执笔只看到完整句式，不看到内部 ID；full 剧情审校
  看到结构化切片，compact 事实编辑在验证简报中看到同一
  起伏切片；polish/style 不接收。整张全书事件脊柱不会逐章进入模型上下文。
- 扩纲 brief 可定向投影有界 `long_term_commitments` 与同 id 当前 `ledger_hook`，供策划依据
  本阶段线路和眼前冲突决定是否显形；**逐章 pack 不注入整张承诺账**。只有本章
  `beats[].effects.hooks` 指名的活跃同 id hook 才优先占用 `pack_caps.hooks`，其它按既有到期
  优先级切片，避免长期承诺随总章数膨胀工作包。
- 每条线另带 `beats_support`：本章拍点正文与 `present` 名单里都找不到该线的具名参与者（不算主角）
  时为 `false`，视图在展示该线的同时追加"以 beats 为准、不得为它另起场次或硬塞人物"。
  `thread_refs` 与 beats 对不上是**软**状态（不改章拍、不阻断流水线），但它决定执笔看到的指令
  是否自相矛盾。
- 上述规模矛盾是**硬错误**：`plan validate` 不通过，`plan extend` 拒绝写盘，空闲态 `chapter next` 拒绝开新章。叙事缺项仍为软告警。推演理由写进 `book/editorial/outline.md`。

`beats[]` 可以是字符串（自动补成 `{id: "bN", required: true, must: ""}`），但**建议写全 `id`/`must`**：`id` 是 `beats_hit` 机检的凭据，`must` 是硬门禁词。
**`beats[].effects`（可选，推荐）＝本场戏“该留下的账本结果”**：形状与 submit 的
`state_delta` 各列表一致（moves / facts / debts / hooks / relations / items / conditions / knowledge / new_names / deaths / revivals / nonliving）。
装配时按章展平成 `pack.expected_delta`。执笔视图不重发 JSON，而在 `writing_brief`
中渲染为“计划结果”完整句式，要求在场上真实发生；assemble 用结构化
`expected_delta` 验证兑现并收口账本。润色看不到这份账本。
事实编辑在组装时逐条核对：
- 终稿确实兑现 → 原样把该条目填进 `state_delta` 并附逐字 `quote`；
- 终稿没兑现 → 申报 `expected_delta_missing`，回草稿重写（或先改章拍，不要由组装自造）；
- 兑现了却没附 quote → `expected_delta_quote_missing`，只回组装重跑。
`effects` 只描述结果状态，不等于台词或动作；正文仍要把结果演出来，不能让角色背诵计划。
`effects.knowledge` 的 `(who, topic_id)` 是人物认知变化的计划键，`claim`/`stance`/`source` 写结果，
`quote` 由事实编辑从终稿提取；不把读者或策划已知的真相提前赋给人物。长线显形复用同 id `effects.hooks`，中途 `open` 更新 `text`，最终 `paid`；正文证据与
审稿标准见 [张力与兑现](craft/tension-payoff.md) 和 [提交输出](write-output.md)。
空种子（只有空 `chapters` 数组）见 `templates/plan.chapters.json`；**字段齐全、含铺垫→兑现两章连排的样例**见 `templates/plan.chapters.example.json`，照它复制改写即可。
