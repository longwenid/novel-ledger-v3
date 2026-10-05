# 巡检与治理（含量化口径）

两部分：① book audit、正典同步、glossary 防线、伏笔与关系治理、内容漂移四闸；② 量化口径清单与四步收口 SOP。

<!-- 一、巡检、基线自愈与伏笔治理 -->

book audit、正典同步、glossary 三层防线、伏笔与关系治理、反 AI 矩阵、内容漂移四闸。口径对账见本文第二部分「量化口径」。
## 巡检、基线自愈与伏笔治理

- **全书审计（`book audit`）**：全量排查字数、标点闭合、专有名词归一化（glossary）、哈希基线失配、失效 quotes、章首尾复读及账本一致性；另返回 `quality_summary`——每章情节自检/机器硬红线/submit 结果留痕（`book/run/quality.jsonl`）汇总。另出**内容漂移**信号，见下。
- **正典源同步（`canon_source_drift`）**：`canon_fingerprint()` 读的是**编译产物** `cards.json`，所以直接改 `book/kb/canon/**` 而不跑 `kb sync` 时，指纹一字不变、`canon_drift` 不响——运行时会一直按**旧正典**工作，全程静默。为此 `kb sync` 会把正典**源目录**的指纹记进 cards.json（`source_fingerprint`），`book audit` 报 `canon_source_drift` 比对二者；源目录整体丢失也按漂移告警。同步采用 fail-closed：不可读/损坏的 JSON/YAML、没有可扫描文件或零张硬卡时直接失败，并保留上一份有效 `cards.json`，不会用空库覆盖。旧编译产物没有源指纹时返回 `available: false`，不误报。改完正典记得 `kb sync`。
- **基线自愈（`book resync-baseline`）**：外部手改正文或批量格式化后，一键重算全书 SHA256，自动对齐 `meta.json` 与 `acks/**/*.json`，并从当前正文中智能自愈失效 quotes，恢复可信基线。
- **专有名词归一化（`config.glossary`）**：在 `config.json` 中配置术语映射（如 `{"旧写法": "规范写法"}`），提交机检自动拦截被禁用的旧词，确保全书用词严格统一。**它不是释义词典**：值必须是可直接替换的规范词串——把「词条→释义」填进去会让键（往往是正典词）被当违禁词全书误拦。**三层防线**：① hatch 在开书时拒绝「词条→释义」形状与撞主线/卷脊的条目；② 装配期 `glossary_self_conflict` 硬拒——禁用串命中同包拍点 must/文本、卷脊或世界脊柱即不装包（写手不可能同时满足"必须产出"与"不得使用"）；③ 机检对疑似误用条目跳过拦截、任务书渲染同规跳过（不教唆写手避开正典词），`book audit` 的 `glossary_misuse` 逐条点名指路修正。**数字口径也走这里**——把"与口径冲突的旧写法"作为被禁词、规范写法作为值，跨章数字漂移就能在提交时被拦，见下。
  **产出方是总编**：`book calibrate` 从正典「硬禁则」里提取被点名成具体词串的条目，给出 `glossary_candidates` 与 `glossary_ban_bullets` 供总编辑裁决（只建议、不自动写入——够长能限定语境的才钉，通用短词会误伤真值）。正典里有硬禁条目而 `config.glossary` 为空时，`book audit` 的 `glossary_coverage.advisory` 会明说"三处闸门全部空转"并指路 `book calibrate`，把"建了表没人填"变成可见项。
- **伏笔生命周期治理（`hooks audit / close / defer / merge`）**：
  - `hooks audit`：输出活跃伏笔数、逾期分布（`overdue_count`）与健康度；
  - `hooks close --id <id> --reason <why>`：主动结案随大地图转移或自然消散的僵尸伏笔；
  - `hooks defer --id <id> --due <new_due>`：延长长线重要伏笔的到期章节。**防线**：未写章的
    已签章拍若仍钉着旧 due（`plan_hook_due_pinned`），只改账本必然在组装时打
    `expected_delta_missing`、回正又触发 `stale_pack`——先改计划重封批次、让正文按钉住
    的 due 兑现，或 `--force` 显式豁免（治理事件记 `forced_over_pinned`）；
  - `hooks merge --from <id> --into <id>`：合并同主体重复登记的伏笔（治 `duplicate_id_stem`），
    追加治理事件，快照保留 `into` 的 id 和两条记录中最近更新的状态，旧章节申报仍可追溯；
  - **status 词表只有 open / paid / closed（abandoned 弃用）**：改期只改 `due`，不发明
    新状态值——audit 防御性把未知值（如 `deferred`）计入活跃，但词表纪律以本条为准；
  - **关系治理（`relations rename / close`）**：`relation_name_variant_suspected`
    （同一人两种叫法）用 `relations rename --from <变体> --to <规范名>` 归一（who/target
    当前快照归一、同 pair+kind 收敛，原事件、正文与 quote 锚不动）；
    `relation_pair_conflict`（同 pair 多条 open）用 `relations close --who --target
    [--kind 子串] --reason <裁决>` 收敛，裁决理由进 close_reason。两者都追加裁决事件，
    批次闸门对账时随 hygiene 当场处理；
  - 动态降权：逾期超 30 章的旧伏笔在 pack 装配中自动大幅降权，防止污染局部工作集。
    本章章拍 `effects.hooks` 已点名的活跃 hook 先进入最多 5 条的 pack 切片；书级
    `long_term_commitments` 按阶段相关性进入扩纲简报，不把数百章后的答案逐章注入写者。
  - **回流闭环**：逾期伏笔不再只靠写者顺手回收——低水位 `extend_plan`
    提示附 `overdue_hooks` 清单（策划编辑排进新章拍 `effects.hooks`，提交机检按
    expected_delta 强制兑现）；草稿任务书对逾期伏笔逐条标注「已逾期 N 章，本章应优先
    安排回收」。新建 hook 无 status 时默认 `open`（audit 与 pack 注入口径统一）。
    正文里带时点的行动承诺（明日/N日后/月内）同属此闭环：事实编辑在提交时把新承诺申报为
    hook（due=承诺到期章）；突发事由打断或取消的，按同 id 申报改期（新 due）或关闭，
    quote 逐字锚定重新约期/取消句——正文承诺从此有到期跟踪，不依赖单章自检的记忆。
    章纲层另有一道前置防线：`chapter next` 的 draft action 带 `hooks_due_unplanned`
    预检——due ≤ 本章、仍 open、而本章章拍 `effects.hooks` 未引用的伏笔在**开写前**
    亮出（advisory 不拦），宿主可回炉章拍或让正文明确改期；策划编辑编拍纪律要求
    起批时对账全部到期伏笔，漏排即失职。
    扩纲职责与载荷同样落盘：低水位/计划耗尽触发 `extend_plan` 时，
    机器载荷（suggest_from / overdue_hooks / volume_watermark）写入
    `book/staging/plan-extend-brief.json`，信封带 `plan_worker_brief.path` 指针——
    派发 prompt 只指路，职责真源=brief 文件+协议卡（随 skill 版本更新，旧会话上下文
    不再能腐蚀职责）。`plan extend` 的结果与 `plan validate` 另带 due 对排期对账
    （`hooks_schedule_gaps` / 警告码 `hook_due_precedes_schedule`）与**逾期未声明检查**
    （`overdue_hooks_undeclared` / 警告码 `overdue_hook_not_declared`）：已过 due
    仍 open 且无任何未写章 effects.hooks 引用的钩当场点名——处置只有两条正路（声明兑现 /
    `hooks defer` 显式改期），不允许静默。
  - **批次闸门必做钩子对账**：每 10 章 verify+audit 时，
    对 `hooks audit` 的每条 overdue 逐条核对正文——已演出的 `hooks close`（reason 注明章号
    依据）、未演出的 defer 到真实节点或排进下批回收；对 hygiene 的关系冲突（同 pair 多条
    open / 名字变体）同样当场收敛（supersede/归一，原事件保持不变）。**「已兑现未核销」
    是常态缺口**：写者按拍演出但不结钩、章内自检只偶尔点名，闸门不对账就会积压。
  - close/defer/merge 与关系治理会在 `events.jsonl` 链尾追加 `type=governance` 事件，
    携带 `actor`、`reason`、`effective_chapter`；命令可显式传 `--actor`，defer/merge/rename
    可传 `--reason`（未传时记录为操作说明）；未传 `--actor` 时如实记 `unspecified`。
    旧章事件及哈希保持原样。治理事件不占章节号；`ledger verify` / `repair`
    重放裁决得到当前快照，回滚生效章时同时撤销该章之后的裁决。历史旧账本（仅章节事件，
    包括此前已重封的历史）继续按原记录重放；已被旧工具改写的原始值无法自动恢复。
  - `book audit` 的 `unresolved_long_term_commitments` 按书级 id 对照 `paid` hook；手动
    `book complete` 对未兑现条目硬拒绝，无人值守完结审计同样暂停。
    机器只证明账本已结；独立卷审与终局复核须核对原场景证据、人物选择和文学回收，见 [剧情复核](story-review.md)。
- **宏观世界状态总线（`world_spine`）**：在 `book/plan/chapters.json` 中定义全局宏观状态（如“旧秩序崩塌后的重组期”），与 `volume_spine` 一起作为顶层硬约束注入 Pack，杜绝写手局部视野导致的世界线断裂。
- **文本读感与场景因果**：
  - 逐章机检只拦明显出戏的元叙述、平台话术和明确章尾套话；失败汇总为
    `style_metrics_failed`，明细落在 `book/staging/style-metrics-<ch>.json`。
  - 句长、段落、对白排版、标点、常见词类和对照句计数只作诊断，不以单本语料的
    分布要求另一部小说逐章达标。阅读审稿应指出场景中具体的理解或节奏问题。
  - 新登场陌生人假性熟络仍由连续性规则检查（`continuity_unintroduced_familiarity`：
    无引介/自报家门却在对白中直接直呼大名）。
  - 人物反应、生活细节和幽默要由当场处境支撑，不为制造风格标签硬添动作、数字或笑点。

### 内容漂移四闸（结构一致 ≠ 值正确）

`ledger verify` 只证明"事件重放 == 快照"，`book audit` 的哈希/标点检查只证明"正文自洽"。
两者都**看不见**一类最隐蔽的长篇缺陷：账本里的值（金额、状态）与正文对不上——正文被整体
改尺（正典改价、回改）后账本没跟着改，错值就会顺着 NOW 卡灌进后面每一章。补四条信号：

| 信号 | 在哪 | 判什么 | 性质 |
|---|---|---|---|
| `quote_invalid` / `ledger_quote_invalid` | `ledger verify` + `book audit` + `book reconcile` | **每一类**语义条目（`debts`/`hooks`/`relations`/`items`/**`facts`/`deaths`**）的 `quote` 必须能在某章已提交正文里逐字找到；找不到 = 断锚。`facts`/`deaths` 的 quote 不在快照里（只存 text/who），回 `events.jsonl` 取锚 | advisory（进结果，不阻断 `consistent`）；`chapter patch` 改断它则 fail-closed |
| `hygiene_issues` | `ledger verify` | 同一主体被不同 id 重复登记（`duplicate_id_stem`）、同一笔债务被多次销案（`terminal_state_repeated`） | advisory |
| `canon_drift` | `book audit` | commit 时把正典指纹（`canon_sha`）记进事件；当前指纹与最近一次 commit 不同 = 正典被改过，`since_chapter` 指明自哪章起可能按旧正典 | advisory |
| `numeric_issues` / `derived_drift` | `book audit` + `book reconcile` | ①`enum_sum_mismatch`：句内「A、B、C……拢共 D」加了不等于 D；②`algebraic_flow_mismatch`：句内「原有 A、花去 B、还剩 C」且 A - B != C（资产流水穿帮）；③`same_key_conflict`：项目 `config.quant_keys` 声明的"每章单一取值"键，同章出现两个互斥金额；④`derived_numeric_drift`：`meta.l1_summary` / `summaries` 里的银钱数词在正文中查无（正文改尺后派生件停在旧值） | advisory |

**挑键纪律**：`quant_keys` 只列"每章应当单一取值"的口径名词（年租、折价…）；一章内本就可能有多笔的（如本利/利钱）会产生真假混杂。

**为什么不直接挡流水线**：存量项目可能早有断锚，硬挡会让 `next` 卡死。总编辑看到
`quote_invalid` / `canon_drift` 后，按 `references/dispatch.md` 派一次性复核角色按问题定向
复核，确认后由总编辑改账本/正典并留 `decisions.jsonl`。**`chapter patch` 是唯一 fail-closed 的
地方**：改断账本引文（`patch_orphaned_ledger_quote`）当场拒绝——就地修润这一动作最常改数字，
不拦就前功尽弃。
---

<!-- 二、量化口径（数字不漂的根） -->

跨章数字口径的项目层清单与四步收口 SOP。漂移信号见本文第一部分。
### 量化口径（数字不漂的根）

正典卡钉的是世界观硬设定，**不必然覆盖"跨章数字"**（某活的具体工钱、某笔账的利率、
某块田的方位里程）。长篇里这类数最易漂，且单章读不出、机检看不见。对策是把口径写成
项目层清单（结构见 `templates/intent.example.md` §八 量化口径表），并做四件事：

1. **禁用写法进 `glossary`**：口径表最后一列抄进 `config.glossary`，旧写法一出现，提交与
   patch 当场被 `glossary_term_banned` 拦。只用**更长、限定语境**的违禁串（如 `押运抽两成`），
   别用会误伤真值的短词（如通用 `抽两成`——行栈过货、牙行说合本来就各有抽成）。
2. **口径键进 `config.quant_keys`**：把"每章应当单一取值"的口径名词（年租、折价、验阵…）
   列进去；`book audit` / `book reconcile` 会查同键双值，`plan validate` 会查它是否进了
   `world_spine`（每章 pack 必注入的唯一通道——口径只写在正典卡里，写者看不到）。候选清单
   由 `book calibrate` 从「量化口径」卡派生。spine 用受支持的写入口落盘：
   `plan set-spine --file <text> --reason <why>`（save_plan 真源 + `plan.spine_set` 治理
   事件留痕 + 落盘即回验 quant 覆盖；≤4000 字，长正典归 kb 卡）。
3. **正典每个口径块补一行机器可读锚**：`口径键｜定值｜禁用旧写法`。这是 `quant_keys` 与
   `glossary` 的共同来源，开书时一次定死。
4. **改口径＝改正典 → 一次收口**：
   ```bash
   # 1) 改正典卡 + 口径锚 → kb sync
   python3 "$SKILL_ROOT/scripts/novel_ledger.py" kb sync --project "$PROJECT"
   # 2) 改账本（events.jsonl，先账本后正文）→ repair
   python3 "$SKILL_ROOT/scripts/novel_ledger.py" ledger repair --from-events --project "$PROJECT"
   # 3) 按 reconcile 清单回改正文（chapter patch 就地修润，自愈哈希）
   python3 "$SKILL_ROOT/scripts/novel_ledger.py" book reconcile --project "$PROJECT"
   # 4) 收口：派生件与引用自愈（默认不动正典指纹），再确认 reconcile clean=true
   python3 "$SKILL_ROOT/scripts/novel_ledger.py" book resync-baseline --project "$PROJECT"
   python3 "$SKILL_ROOT/scripts/novel_ledger.py" book reconcile --project "$PROJECT"
   # 仅当正典改动已复盘确认"无需回改任何正文"时，才显式清 canon_drift：
   python3 "$SKILL_ROOT/scripts/novel_ledger.py" book resync-baseline --restamp-canon --project "$PROJECT"
   ```
   若正典有改动但还没回改正文，`book audit` 的 `canon_drift.changed=true` 就是漏改信号；
   `book reconcile` 会把账本断锚、算术错、派生件旧值聚成一张 `chapters_to_fix` 待办表。

利率这类行话必须给算式（定义 + 一个算例），否则"月二"会被读成 2%/20%/每月两块三种意思。

delta 以暂记入账，不升格正典。正典/换走向由总编辑在作者红线内改计划与必要卡片，
越红线先升级作者；写者无权改物理法则、死亡、核心伤口。
---
