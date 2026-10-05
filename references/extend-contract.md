# 扩纲与卷合同（含分卷总纲契约）

两部分：① plan extend 硬校验、批次候选择优、编拍纪律与卷合同；② 全书分卷总纲合同（volume_outline_contract）的形状、签署链与设计要点。

<!-- 一、扩纲硬契约与编拍纪律 -->

plan extend 的全部硬校验、卷合同（plan.volumes）与编拍纪律。章拍 schema 见 [plan-contract.md](plan-contract.md)。
**编拍纪律（默认 5 场，条数软警告）**：每章默认 **5 个事件**。每个事件是**一场戏**（当场发生、有来往、有未完），不是拍点标签或 must 词。`must` 选口语能自然带出的词，避免机械重复。偏短先补完整事件，不注水；特殊短章可少于 5 场，但仍要支撑字数合同和因果推进。**执行点**：`plan extend` 对非空但不等于 5 场只报 `beats_count_off_band`；空 beats 仍硬拒 `missing_beats`。hatch 种子章按五场起步；submit 不因条数 rejected；`plan validate` 对未写章报警（已写章不追）。

**跨章节奏与兑现量控（编拍纪律的延伸）**：上面是**单章**纪律；跨章先看目标、阻力、选择、结果与代价如何改变阶段状态，再安排铺垫、推进、局部兑现与卷终结算。旧的“每 3–5 章一次小兑现”和近期显性承诺“约 15 章内处理”只是审稿提示，不能按章距硬塞爽点，也不限制跨卷低调伏笔。方法和三种尺度见 [张力与兑现](craft/tension-payoff.md)，由策划编辑起草、总编辑签发。近期承诺与长线伏笔的正文变化都复用 `beats[].effects.hooks` 同 id 记录；长线的书级目标另记在 `book_outline.long_term_commitments`，`due` 若填表示最终兑现章。

**新人物与成长后的声线**：`plan extend` 输入可带可选的 `character_profiles` 对象，按规范名替换
对应人物的完整短档案、保留其余人物；也可单独提交 `{ "character_profiles": {"人物名": {...}} }`
而不追加章节。每人只用 `background_register` / `speech_habits` / `under_pressure` /
`desire_and_mask` / `sample_quote` 五个短字符串字段。仅在已有事件足以支持变化时更新，
不要让同一人物因换卷突然改口。旧知识和误信仍由 `state_delta.knowledge` 事件账记录，
不是人物声音卡的一部分；详见 [角色声线](craft/character-voice.md)。

### 扩纲硬契约清单（plan extend 的全部硬校验）

plan worker / 总编辑在签阶段小纲前对照此清单，不必靠撞错来学（每条都有对应错误码，命中即**整批原子拒绝**）：

| 约束 | 错误码 |
|---|---|
| 批内章号只能向后追加、不重复、为正整数 | `chapter_not_append` / `duplicate_chapter` |
| 每章 beats 非空（5 场戏是软警告 `beats_count_off_band`） | `missing_beats` |
| v2/hatch 项目必须附一个 phase brief | `event_spine_phase_required` |
| 一批 = 一个 phase = 连续章号区间，brief 起止精确等于批首尾 | `event_spine_phase_gap` / `event_spine_phase_range_mismatch` |
| 批内每章 `phase_id` == brief.id，phase id 全局唯一（批次被卷/幕界裁短时禁止算术式编号） | `event_spine_phase_id_mismatch` |
| 单个 phase 最多 20 章 | `event_spine_phase_too_long` |
| phase 不得跨卷（批内 chapter.volume 只许一个取值） | `event_spine_phase_crosses_volume` |
| 章的 `story_stage` == phase 的 stage；phase stage 只能随章号前进 | `event_spine_chapter_stage_mismatch` |
| 章的 `thread_refs` ⊆ 本 phase 选中事件的 owner 线，且线须处于 open..payoff 幕范围内，≤3 条 | `story_map_inactive_thread_ref` 等 |
| 章的 `clock_refs` ⊆ 本 phase 选中的时间线时钟，1–2 条 | `story_map_clock_refs_over_cap` 等 |
| phase `event_changes` 必须恰好覆盖本 phase 选中的全部事件 id | `event_spine_phase_event_changes` |
| 书级规模合同（含终局措辞三条，见 `infra/scale.py`） | `book_scale_*` |
| 新版全卷总纲齐全、每纲≥500汉字、正文与方向未改约；不得追加未知卷 | `volume_outline_contract_violation` |

### 批次候选与择优（plan select-batch）

扩纲批落拍前的**批次候选头脑风暴**：plan worker 在同一会话内先产出多个批次走势候选、
自行评审择优、留痕后再展开。候选全档是永久审计工件，裁决进账本治理事件。

- 工件：`book/editorial/plan-batch-candidates-<起>-<止>.json`，schema
  `novel-ledger.plan-candidates.v1`：

```json
{
  "schema": "novel-ledger.plan-candidates.v1",
  "batch_from": 21,
  "batch_to": 21,
  "candidates": [
    {
      "id": "A",
      "theme": "一句话批次主题",
      "differentiator": "差异轴声明：压力来源/章型排序/兑现时机/主推线路之一",
      "failure_mode": "本候选的最坏失效模式（负项必填）",
      "chapters": [
        {"chapter": 21, "goal": "…", "conflict": "…", "outcome": "…",
         "tags": ["章型"], "settles": [{"id": "h1"}]}
      ],
      "spine_adjustment_proposal": "（可选）advisory 上报总编辑，不落拍"
    },
    {
      "id": "B", "theme": "绕行查证", "differentiator": "先查证再对峙",
      "failure_mode": "错失期限",
      "chapters": [{"chapter": 21, "goal": "另寻凭证", "conflict": "线人失约",
                    "outcome": "找到替代入口", "tags": ["暗访"], "settles": []}]
    }
  ],
  "verdict": {
    "selected_id": "A",
    "rationale": "胜选依据（引用机器事实断言）",
    "losers": [{"id": "B", "why": "败因（必填）"}]
  }
}
```

- `plan select-batch --file <路径>` 的机检拒绝码（命中即拒绝记录，不追加事件）：
  `plan_candidates_too_few`（候选<2）、`plan_candidates_same_skin`
  （章型序列+兑现安排完全相同的同皮候选）、`plan_candidates_missing_failure_mode`
  （缺负项）、`plan_candidates_missing_loser_reason`（缺败因）、
  `plan_candidates_selected_unknown`（胜选 id 不在候选内）、
  `plan_candidates_batch_too_long`（批次超过 20 章）、`plan_candidates_chapter_range`
  （每候选的章号须按顺序准确覆盖批次连续区间，不得漏章、错号或重复）。
- 展开时将胜选候选每章的 `goal/conflict/outcome/tags` 原样保留到章拍，同章 `settles`
  对应的 hook 必须在 `beats[].effects.hooks` 置 `paid`；新增兑现也先修正候选并重选。
  v2 审稿合同项目的 `plan extend` 在入库前校验精确批次、选中候选及上述稳定字段；
  缺选择报 `plan_batch_selection_missing`，不一致报 `plan_batch_selection_mismatch`。
  候选骨架与哈希封进治理事件，实际扩章哈希存于 `plan.expansion_batches`；检查点逐实际
  phase/扩章批核验，不凭一条宽泛的历史章号范围宣称后续批次都已择优。
- 机器事实断言（写回工件 `machine_checks`，只陈述事实不打综合分）：每候选的
  `hooks_due_in_batch` / `hook_settlement_missing`（due≤批末 open hooks 的兑现缺口）与
  `recent_tag_overlap`（候选章型与近 20 已写章 tags 的重合率）。
- 裁决以 `plan.batch_select` 治理事件封进账本（无状态：校验后不改快照，重放不依赖
  工件文件；`effective_chapter`=当前已提交章，该章回滚时裁决随区间作废）。
  平票确定性顺序：先清逾期 hook ＞ 张力单调递进 ＞ 重复距离更大。
- **强制面**：无人值守批级检查点（每 10 章）核对留痕——待写批次（首章>3）无
  `plan.batch_select` 事件 → `plan_batch_selection_missing` → `review_required` 停线等人；
  `plan validate` 同条件出 advisory `plan_batch_no_selection`。**豁免**：开书起手批
  （第 1～3 章，开书向导已有人机候选交互）与纯签卷批（不带 chapters）。
- attended 场景总编辑可在候选工件落盘后暂停给作者过目（可选，非默认）；无人值守不暂停。
- 扩纲简报的 `batch_design` 节（近 20 章章型直方图、当前幕张力档、指令摘要）是评审底座。

### 实写不足时续签章数（`plan rebudget`）

已签计划到达当前总章数上限，而 `已通读章数 + ceil((目标字数 - 已写字数)/签约章幅)`
超过原上限时，独立 plan worker 先执行 `plan rebudget --actor ... --reason ...`，再做
候选择优与 `plan extend`。命令只在已提交且已通读的章边界生效；全书目标字数、签约章幅、
事件脊柱和正文均保留。新总章数精确按上述公式计算，并把增量章数加入当前续写卷的章预算；
卷字预算保留，禁止直接增大总章数绕过合同。

### 未写章的拍级定点修补（`plan patch-chapter`）

extend 只能向后追加，rework-patch 只动正文；当缺陷长在**已签章拍**上（如同章同
(who, topic_id) 的重复 knowledge——提交端 `knowledge_duplicate_topic` 禁第二条、
`expected_delta_missing` 要求逐条回填，构成任何提交都无法通过的计划层死锁），
用 `plan patch-chapter --chapter N --patch-file <json> --actor ... --reason ...` 定点修：

- **只允许未提交章**（chapter > last_committed）；已提交章是历史事实，回改被
  `chapter_already_committed` 硬拒。
- **只允许 `beats`/`location`/`present` 三个字段**（beats 整组替换）；goal/conflict/
  outcome/tags 被批次候选封存绑定，改它们等于改约，走候选重选。
- 修补后的章过 `plan_shape_reject` 全量形状闸：effects 稳定键、must∈拍文本、
  **同章 knowledge 唯一**——死锁类缺陷在落盘前被点名（extend/init 同闸，新计划
  根本进不来这类声明）。
- 覆盖批次封存范围的章同步刷新该批 `expansion_batches.chapters_hash`（封存语义=
  候选绑定字段不变，拍级修订经治理事件审计后反映进证明）。
- 每次修补封 `plan.chapter_patch` 治理事件（目标章、变更字段、拍数前后、是否刷新
  封存哈希）；修补后 `chapter next` 重建本章 pack/简报再继续。
- 遗留带毒计划（守卫生效前已签）在提交端已解锁：expected 的重复 knowledge 只对
  第一条收口，合并后的 delta 即可通过，重复以 `plan_effects_duplicate_topic` 告警
  随回执提示合并。

依据存于 `book_outline.chapter_rebudget` 并追加 `plan.rebudget` 治理事件，包含原总章数、
已写章数/字数、续写卷与新额度。未写里程碑按剩余区间同比向后映射，逐项记录前后章号；
已写里程碑保持历史位置，未写 `book_climax` 仍须落在新预算末 15%。机器核验该依据及全书/卷章额度公式，随意改大仍硬拒。
同一 stage plan job 可先续签再择优再扩章；实际扩章成功后 fence 仍要求立即退出。

### 卷合同（`plan.volumes`）

新版 hatch 在开书前签齐全书所有卷，每卷总纲至少 500 汉字，预算精确覆盖全书；字段与完整样例见本文第二部分「全书分卷总纲合同」。后续逐卷细化只能补充已有卷的 `detail_outline`/`recap` 等细纲字段；提供已签字段时须与原文一致，不得删除、更换总纲或新增未知卷。可只补卷细纲而不追加章节：

```json
{
  "volumes": {
    "vol-0002": {"detail_outline": "在原卷取证责任内，先核对出入库记录，再安排证人到场和第三方见证。",
                  "recap": "前卷留下的实证及双方已付代价。"}
  }
}
```

- 每卷声明 `word_budget` + `chapters_budget` 时必须满足 `chapters_budget == ceil(word_budget/chapter_words_target)`；新书默认目标 3200，旧书按已签值
  （`book_scale_volume_chapters_mismatch`），该校验不依赖 `book_outline` 存在。
  只有 `plan rebudget` 已签出的续写卷可增加其机器校验过的 `additional_chapters`；
  其余卷仍严格使用原公式。
- 卷键与章拍 `volume` 取值按**卷号归一**解析（`store.volume_entry_for`）：`1` / `"1"` /
  `"vol-01"` / `"vol-0001"` / `"第一卷"` 互相等价，正文目录统一用 `vol-000N` 形态。
- 新版保存的卷数、卷表顺序与总纲 hash 均须一致；每卷章预算按独立向上取整，全卷章数总和可能比全书取整多不到卷数，`plan rebudget` 保留该舍入差。`plan volume-outline --volume N` 可按需读取某卷完整总纲；不按字符截断。
- **旧书兼容**：未声明新版全卷总纲合同的旧书沿用同键替换式卷签发，允许补签后续卷；同时报告 `volume_outline_legacy_unsigned`。其余覆盖率左移告警（`plan validate` / `book audit` 的 warnings，不阻断）：
  `volume_contract_coverage_gap`（Σ卷字预算 < book_words）、
  `volume_contract_chapters_gap`（Σ卷章预算 ≠ total_chapters）、
  `book_outline_missing_volume`（acts[].volumes 引用的卷无 spine/goal，list 与 `"2-4"`
  字符串形态都解析）。终局措辞一旦出现在任何章拍文本，scale 合同会要求两个总和都
  对齐——缺额必须在那时之前补齐，这些告警就是提前量。
---

<!-- 二、全书分卷总纲合同 -->

开书先明确全书卷数并签齐每一卷的总纲，之后才细化当前卷和每批最多20章的阶段小纲。不能先签首卷，把后续卷的剧情职责和终局路径留给连载时临时猜测。卷纲固定各卷任务和因果承接，不提前穷举全书章拍。

新书沿用 novel-ledger.hatch.v3，但必须增加顶层 volume_outline_contract；book hatch --check-only 与真正建库使用同一校验器。字段形状：

    {"volume_outline_contract": {
      "schema": "novel-ledger.volume-outlines.v1",
      "volume_count": 3,
      "volumes": [{
        "volume": 1, "title": "首卷卷名", "spine": "卷脊", "goal": "局部结果",
        "word_budget": 180000, "chapters_budget": 57,
        "outline": "每卷定制的完整总纲，至少500个汉字，不设上限",
        "plot_role": "本卷在全书承担的剧情职责",
        "inherits": "承接前卷的事实、代价、关系；首卷写开篇依据",
        "advances": "矛盾如何升级，人物线如何通过选择与代价推进",
        "ending_direction": "本卷结果如何构成全书终局的因果条件",
        "next_handoff": "下一卷的明确状态与问题；末卷写终局收束去向"
      }]
    }}

上面只展示形状，不是可直接校验的三卷输入。[完整三卷样例](../templates/hatch.volume-outlines.example.json) 含每卷不同的长卷纲；使用时依据已选故事改写，不能复制样例故事或把同一段卷纲批量换卷名。

每个卷纲要叙述入口状态、主要对抗、证据或资源的因果推进、人物关键选择与代价、局部兑现和出口状态，写清与前卷、下一卷和全书终局的关系。plot_role、inherits、advances、ending_direction、next_handoff 是对应方向的明确定位字段，不能用“继续推进”等空话替代定制设计。程序校验结构、长度和版本，故事是否成立仍须编辑判断。

强制约束：

- 卷号连续覆盖1..volume_count，每卷恰好一条，不设卷数上限。
- 每卷outline至少500个汉字；标点、数字和英文不计，不设字符或字节上限。完全相同的卷纲不能复用于多卷。
- 所有纲领字段须为明确原文，卷字数／章数预算须为正整数。所有卷字数预算精确合计book_words，各卷初签章数按ceil(word_budget/chapter_words_target)计算。
- 分卷独立向上取整可能比全书向上取整多0至卷数-1章；合同记录这项真实舍入差，不把它当作漏签，也不允许借它随意增加故事规模。
- 幕的卷区间须完整覆盖签约卷，起手章不得引用未知卷。step5_volume1的卷名、卷脊、预算及下一卷接口须与预签第一卷一致，不能另签冲突首卷。

起草与签发（谁设计卷纲）：

- 卷纲由**创意编辑**设计。开书向导门 8 确定卷数与分卷预算后，总编辑派发一次创意编辑 job
  （空上下文、最强档），输入为已确认的意图、书脊、四层事件脊柱与分卷预算，产出各卷
  outline 与五个定位字段的完整草案；不开多套并行，不按卷数拆多 job。
- 总编辑校验草案与合同的结构、预算和首卷一致性；作者在启动确认页按卷终审，改某卷即回
  创意编辑重拟该卷。签署链路不变：仍写入 manifest 的 volume_outline_contract 由
  `book hatch` 统一落地。
- 宿主无法派发创意会话时，显式降级为总编辑内联执行创意编辑职责并在向导决策记录注明，
  不得静默回退。

设计要点（创意编辑起草用；总编辑核签按同清单验收）：

- **plot_role 写因果句**：终局为什么必须经过这卷——只有这卷能建立的关系、能暴露的真相、
  会欠下的债；不写题材概括。
- **势能看状态差**：先写 inherits 入口态与 next_handoff 出口态两句话，落差即本卷势能；
  出口态用「赢了但…／输了但…」句式——局部结果加意外代价或意外所得，并构成下卷入口。
- **跌宕三装置**：中点换性质（前半目标到中点被发现是错的或不够的：取→还、藏→暴露）；
  压力换维度（威胁沿生存→身份→关系→信念升级，不是同一对手反复加码）；
  假胜利／假失败（结果与预期错位，翻转所需伏笔先以同 id hook 埋种）。
- **人物锚**：每卷 2~3 个不可逆选择点，每次赢都拿走点什么（关系、资源、底线、健康）；
  事件起伏与主角选择无关即是噪音，卷末选择定义下卷入口态。
- **承诺配比**：每卷一次大兑现（结算本卷 goal）＋至少一个大钩开进下卷＋1~2 条长线伏笔
  定向显形（改变人物当下判断，不是提醒谜底存在）；本卷回报类型在卷内早期预告。
- **预算换算波段**：按 word_budget／chapter_words_target 粗分 3~5 个大波段，每波段走
  铺垫（须改变局势）→施压→小结算→余波；以性质变化分界，不按固定章距硬切；
  卷末悬念类型轮换（危机／选择／真相）。

交稿自检（任一命中即返工，程序不查这部分，靠编辑判断）：

- **均匀起伏**：每隔固定章数一个同量级小高潮，无性质变化。
- **目标漂移**：卷中目标更换，无因果链衔接。
- **代价豁免**：主角赢而无损，账本不记真实代价。
- **对手降智**：转折靠对手犯错而非人物选择推动。
- **支线有埋无收**：本卷支线既不结算也不显形。
- **闷卷或赖账卷**：卷末全清账无钩，或全钩无兑现。
- **空话定位**：plot_role、advances 等字段用「继续推进」「矛盾升级」等无定制内容的短语。

建库后完整条目保存在plan.volumes.vol-0001等规范卷键，原文完整进入book/editorial/outline.md。plan.volume_outline_contract保存版本、卷数、有序卷ID、书目标、章幅、书总章数、分卷章数总和及signed_outlines_hash。book_outline.volume_outline_schema与配置版本防止删除合同后降级。

signed_outlines_hash绑定所有卷的卷序、卷名、卷脊、目标、字预算、完整卷纲、职责、承接、推进、终局方向和交接。plan extend只能在已有卷内补detail_outline、recap等细化；不允许新增未知卷、删除或替换签约纲领，也不能减掉原卷纲。容量续签仍由plan rebudget处理，保持故事方向和卷字预算。

只读补读：

    python3 "$SKILL_ROOT/scripts/novel_ledger.py" plan volume-outline --volume 2 --project "$PROJECT"

策划简报的volume_outline_view列出全卷索引、职责、交接、预算和预计章范围，附每个字段的真源定位；索引不复制全部长卷纲。本卷和下一卷完整纲随简报交付，其他卷按需补读。旧项目未声明新版本仍可使用，输出volume_outline_legacy_unsigned诊断，不由程序补造卷纲或擅自重签。
---
