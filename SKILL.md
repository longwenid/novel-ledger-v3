---
name: novel-ledger-v3
description: >-
  Use when starting a new novel project, writing long-form serial chapters unattended,
  resolving story continuity drift, auditing ledger consistency, extending chapter beats,
  or unblocking an interrupted book pipeline.
  (novel-ledger v3 精简派生版：与 v1 行为等价、文档面与代码结构瘦身；百万字长篇连写、开新书、无人值守、连续性治理)
---

# novel-ledger-v3

宿主以总编辑身份工作；确定性控制面持有状态、派发与质量闸门。
无人值守长跑由宿主会话主线程内循环推进：每阶段唤起一个空上下文子 agent，
不把模型会话延长成整书任务。
默认 `stage-agent`：每个 draft / polish / assemble / ack 阶段派发一个无历史继承的新会话，
只通过指定视图与 staging 文件交接。阶段成功或回流即退出，下一阶段与返工由宿主另建会话；
宿主派发提示词与 `chapter next` 的 worker 简报共用同一 `WORKER_PROMPT_PROTOCOL` 版本行。
`worker-agent`（整章会话）与 `inline` 只作显式兼容降级，只有职责隔离，不能宣称上下文隔离。
宿主若无法创建空上下文，应报告能力缺口，不得静默回退。写作与裁决按需读
[总编辑操作卡](agents/roles/managing-editor.md)，只读查询直接执行对应命令。

## 入口与根目录

`SKILL_ROOT` 取第一个存在 `scripts/novel_ledger.py` 的候选：`$NOVEL_LEDGER_SKILL_ROOT` →
宿主提供的安装目录 → `$SKILL_ROOT`，不要用 cwd 猜。`PROJECT` 必须是小说项目绝对路径。
运行时为 Python 3.9+ 标准库零依赖。安装或更新后先核对版本；已有项目再看
`status.runtime.skill_root` 与 `status.runtime.control_fingerprint`，确认没有命中旧安装。

Windows / Git Bash 的解释器探测与路径规则见 [运行平台](references/runtime-platform.md)，
仅在该平台执行时读取。

## 不变量

- 默认 `stage-agent` 每阶段一个空上下文，禁止继承聊天历史、fork 或注入前阶段会话摘要。
  同书阶段串行；worker 不派子 agent、不执行 `chapter next`，提交后退出。确定性调度器推进状态。
  只有宿主能建立模型会话边界；阶段文件视图不能代替它，文件访问安全需宿主沙箱。
- 兼容 `worker-agent` 的 strict 模式每个 LLM 上下文最多覆盖一章：`ack-read` 返回
  `requires_new_session=true` 后必须结束当前会话；没有 ack 不能进入下一章。
  显式 `inline-compact` 兜底按运行态契约压缩后才可续章。
- SQLite 是项目持久化唯一来源：HEAD、账本、正文与 editorial 均存于 `book/novel.sqlite3`；
  文件是导出或待提交输入。无当轮命令 stdout 与退出码，
  不得宣称通过。
- 每个模型阶段先读 action 指定的视图路径，再按本阶段任务从来源路径、`kb_refs`、`memory_refs`
  与定向查询补读相关材料；选中材料完整加载，不按字数裁剪。只写返回的 staging 路径，
  不读取其他阶段的推理记录，不把整章正文回显到主对话。
- 数据库内事件是账本历史真源，snapshot 与关系索引是派生视图；断电或崩溃先 `ledger verify`，确认事件
  可重放才 repair；不要手改 HEAD。
- `chapter submit` 直接运行完整机检；组装字段错误原位修正，不消耗正文返工次数。
  `chapter check-submit` 和 `chapter precheck` 是可选只读诊断，不能替代正式阶段闸门。
- 字数只数汉字：单章 word_band 默认 2500～8000 并硬校验，不足或超出都不能提交；写作目标按
  aim（下限+700 缓冲 ≈ 3200 字）拍而不是贴下限，字数缺口一次性补整场戏，严禁每轮挤几十字。
- 开书按默认 aim 章幅签出全书与各卷章数预算；已有书保留原签约章幅（含旧版 2500）。
  `book_climax` 只能进末 15%；计划用尽但正文不足目标 90% 时继续 `extend_plan`。
  到达章数上限仍不足字数时，先 `plan rebudget` 按实际缺口重算剩余章数，不改作者字数目标或事件方向。
- 开书先签全书所有卷的总纲，每卷 `outline` 至少 500 个汉字；明确该卷剧情作用、承接、推进、
  结尾方向与下卷交接，预算覆盖全书目标。卷纲由创意编辑设计、总编辑签发、作者启动确认终审；
  宿主无法派发创意会话时显式降级为总编辑内联执行创意职责并留痕，不得静默回退。
  总纲完整持久化，后续按选定卷读取并在授权内细化，详见[分卷合同](references/extend-contract.md)。
- 全书必须签出 `book_outline.event_spine` 四层事件脊柱（主线、支线、时间线、张力曲线，事件
  ID 全书唯一，每线只列幕级触点）；每次 `plan extend` 附带结构化 phase，四层任一层不对齐、
  顺序倒退或范围不一致整批原子拒绝。
- 扩纲批落拍前先做批次候选头脑风暴：候选≥2 且结构性不同，`plan select-batch` 机器校验
  并把择优裁决封进账本；无留痕的待写批次在检查点停线。开书起手批（第 1～3 章）豁免。
- 每个模型请求结束立即记录 usage 四分量 delta；`uncached` 必须显式给出可为 0，缺 telemetry
  保持 unknown 禁止补零；达到章节阈值后下一个模型动作返回 `usage_guard`，确定性 commit 仍可完成。
- 返工最多两轮；单句禁词、台词和勘误优先 `chapter patch`，避免整链重跑。
- findings 分为 `BLOCKER/WARNING/NIT/UNVERIFIABLE`；只有 BLOCKER 自动返工，其他留痕裁决；
  重大裁决写入 `book/editorial/decisions.jsonl`（`Ruling: 内容 — 依据 — 若错代价`）。
- 模型档位按 job：独立终审、策划与全书复核用最强档，执笔、润色、组装用标准档，机械单点用经济档。
- 跨章记忆由脚本确定性滚层（近章摘要 → 阶段 → 卷 → 全书脊柱，不发起模型请求）；
  长线连续性按需读取 `memory_layers`、`historical_recall` 与相关历史正文，保留来源与完整证据。
  上下文不设固定字数上限，材料选择依据当前角色、人物、事件、阶段和待核问题。
- 验收正文存于 SQLite，待提交稿件通过 staging 交接：工具输出和角色汇报只给路径、字数、覆盖、自检与退出码，
  禁止 dump 全文；文风 prompt 不复述手册规则，手册文件是唯一真源。
- 无人值守只用会话宿主内循环（主线程按阶段唤起空上下文子 agent），不用单会话长期目标
  命令；blocked、usage_guard、契约损坏和重试耗尽都暂停，不自行越权解锁。
- `book complete` 默认门槛为目标字数 90%；`--override-target` 是作者显式改约，不得由运行代理
  自行添加；正常完本还须计划写完、全部通读、承诺兑现、当前卷审与终局审通过、全书机检通过。
  作者明确提前封笔用 `book close-early --author-confirmed`，记录为 early_close，不称正常完本。
  开书验证至多内联跑完第一章 compact 闭环，不得扩展成长跑。
- 跨卷与终局必须独立剧情复核：`story_review` 用新的空上下文核对人物选择与代价、主支线、读者承诺和结局；
  通过项须有当前正文证据，回执绑定合同及正文版本；BLOCKER/UNVERIFIABLE 停线，修订后另建会话重审。

## 路由

按任务只读当前需要的 reference，读完即开工；角色边界总览见
[roles](references/roles.md)：

| 任务类 | 何时读 | 文档 |
|---|---|---|
| 开书 | 零输入或一句点子开新书，沿基础门和按需补充门选择并确认启动 | [hatch wizard](references/hatch-wizard.md) |
| 章节主循环 | 写章、阶段视图、worker 派发与成本纪律 | [执行架构](references/execution-architecture.md) |
| 章拍契约 | 章拍 schema、字段档位与书级大纲合同 | [plan-contract](references/plan-contract.md) |
| 扩纲与卷 | 扩纲硬校验、卷合同、批次候选择优与编拍纪律 | [extend-contract](references/extend-contract.md) |
| Pack 契约 | pack 结构、阶段视图与分层记忆 | [pack-contract](references/pack-contract.md) |
| 材料补读 | 按当前角色、事件或疑点读取完整来源与卷纲 | [Pack 契约](references/pack-contract.md)、[SQLite 记忆](references/sqlite-memory.md) |
| 提交输出 | state_delta 字段与 submit 输出契约 | [write-output](references/write-output.md) |
| SQLite 与历史记忆 | 全量持久化、旧项目迁移、导入导出与定向历史召回 | [sqlite-memory](references/sqlite-memory.md) |
| 运行态文件 | HEAD、账本文件布局与文风记忆 | [运行态契约](references/runtime-contract.md) |
| Windows / Git Bash | 解释器探测与路径规则 | [runtime-platform](references/runtime-platform.md) |
| status 字段 | status 只读输出与用量口径 | [运行态契约·status 节](references/runtime-contract.md) |
| 机检与返工 | 流水线行为、文风机检与回流分级 | [pipeline-gates](references/pipeline-gates.md) |
| 恢复与处置 | 写锁、崩溃恢复与 blocked 人三处 | [recovery](references/recovery.md) |
| 审计治理 | 巡检、伏笔与关系治理、漂移信号 | [governance](references/governance.md) |
| 量化口径 | 数字口径对账与四步收口 | [治理·量化口径节](references/governance.md) |
| 治理与派发 | 角色边界、权限矩阵、派发模板、编辑宪法 | [roles（含编辑总宪法）](references/roles.md)、[派发手册](references/dispatch.md) |
| 文风 | 换风格、按阶段读手册 | 见下方文风手册索引；按项目 `voice_id` 选择对应文风 |
| 张力方法 | 编排章型、埋收节奏与量控 | [张力与兑现](references/craft/tension-payoff.md) |
| 人物声线 | 规划角色声线、知识边界与成长后的说话变化 | [角色声线与人物鲜活度](references/craft/character-voice.md) |
| 读者体验 | 首读可理解性、章内回报与卷级连读复核 | [读者体验](references/craft/reader-experience.md) |
| 剧情复核 | 跨卷与终局必经复核、证据回执、正常完本及提前封笔 | [剧情复核](references/story-review.md) |
| 无人值守 | 会话内循环唤起子 agent 连写（主线程一直执行）：调度循环、检查点、用量、有界批与降级 | [无人值守连写](references/unattended.md) |

路由与不变量在 `policies/*.json` 有机器可校验镜像（命令死链、锚点覆盖、强制位与证明用例），
改任何一侧先同步另一侧。

### 文风手册索引

每套只保留两份：`<id>.md` 同时承载文风公式、自检和润色写法；`<id>.content.md`
供执笔写草稿。章节按阶段只读对应内文。开书向导在独立文风门选择文风 id，
默认推荐市井烟火 `shijing`，选择后才写入项目。
市井烟火的短摘来自用户提供的小说；其余五套各有附出处的经典句式短摘，
也有原创例句与润色对照。短摘只用于观察句序与表达作用，不代表原作整体属于该文风，
也不提供本书剧情事实。若项目有人写参考文本，可用 `config.voice_anchor_file`
给润色阶段补充节奏锚。样本数量不设硬门槛；
先用试章核对风格辨识、事实保留和阅读顺畅度，再决定是否增补。

- 市井烟火 `shijing`：[总手册兼润色](references/voices/shijing.md) · [内容层](references/voices/shijing.content.md)
- 电影镜头 `cinematic`：[总手册兼润色](references/voices/cinematic.md) · [内容层](references/voices/cinematic.content.md)
- 平实白描 `baimiao`：[总手册兼润色](references/voices/baimiao.md) · [内容层](references/voices/baimiao.content.md)
- 悬疑冷峻 `mystery`：[总手册兼润色](references/voices/mystery.md) · [内容层](references/voices/mystery.content.md)
- 热血群像 `heroic`：[总手册兼润色](references/voices/heroic.md) · [内容层](references/voices/heroic.content.md)
- 细腻抒情 `lyrical`：[总手册兼润色](references/voices/lyrical.md) · [内容层](references/voices/lyrical.content.md)

## 最短操作路径

1. 旧文件项目先按 SQLite 契约执行 `database migrate` 与双重 verify。新书按路由表读开书向导，执行 `book hatch`；需要提前诊断 manifest 时用 `--check-only`。第一次
   `chapter next` 应返回第一章 `draft`。已有书先用 `status` 核对 HEAD、运行版本与控制面指纹。
2. 调度器写章只按当次 `chapter next` 的 action 和路径推进：缺省只写作 `draft → assemble → ack`；按书开润色（`config set --key polish --value on`）后为 `draft → polish → assemble → ack`。组装阶段 worker 只写 submit JSON，`chapter submit` 由宿主执行——机检 submit 是唯一闸门，worker 自检文本只是旁证。返工时旧稿自动轮转为 `.revK` 轮转档，返工 action 携 `prior_draft_path`：定点修复，不从零重写；单句级修复宿主可直接 `chapter rework-patch` 在轮转稿上锚定替换（零模型轮次），`chapter draft-submit` 阶段闸照跑。
   worker 每阶段写入 staging 后直接执行对应 submit 并退出；组装 JSON 交 `chapter submit`，
   `chapter ack-read` 成功后结束终审会话。`extend_plan` 在章间独立会话处理。
   `chapter next` 会推进 HEAD、落 pack 并追加 begin；只想查看台面用只读 `status`。
   stage-agent 宿主调度一律 `chapter next --card` / `status --card`：调度卡只含动作、
   相位、停止位与 worker 派发指针，全量信封留在盘上由 worker 自取——宿主是唯一长生命
   周期对话，信封进宿主上下文就是章章叠加；执行者会话（inline/worker-agent）自动回退全量。
3. 无人值守由宿主会话内循环连写：按 [无人值守连写](references/unattended.md) 的调度循环
   每阶段唤起空上下文子 agent，遇到 blocked、usage_guard 或契约损坏即停；每 10 章章界跑
   `run checkpoint`，检查点 review_required 暂停时处置后 `run resume` 重查。恢复时先 `status`
   与 `ledger verify`，事件链完整才可 `ledger repair --from-events`。命令参数、停止语义与
   故障处置按路由表读对应 reference。
4. 改书级配置（polish、字数带、pack_caps 等）只用 `config set --key <key> --value <value>`，
   读用 `config get [--key <key>]`：配置真源在 SQLite，磁盘 `config.json` 只是投影，
   直接编辑文件不生效——`status` 的 `config_shadow` 会点名这种漂移。

## 长篇资产闭环

- 计划层只在本章 `beats[].effects` 声明会改变的状态；正文必须演出变化，事实编辑用逐字
  `quote` 把 `state_delta` 落进事件账本。机检核对计划显式声明的状态值；只报同一个 id
  而不改变状态，不能算兑现。
- 事件是历史真源；快照保存当前人物、事实、关系、债务、伏笔、物品、状态与生死。下一章
  从当前人物、地点、阶段、到期事项与数据库历史关联选取工作包；相关规则由章拍 `kb_refs` 钉入，
  旧事由 `memory_refs` / `memory_query` 定向召回。材料保留全文与来源路径，疑点按需补读原文。
- 人物账本统一使用稳定规范名；化名、称号和身份揭示写进正文与 KB 别名，同一个人不要以
  新名字另建实体。身份合并涉及历史账本时走总编辑审计，不能只改关系名。
- 可选 `plan.character_profiles` 保存人物稳定的说话倾向，认知变化按 `(who, topic_id)` 写入
  `state_delta.knowledge`；写章只取在场者与本章相关话题。人物不知道什么不能仅凭包内缺席推断，
  声线与世界文风的分工见 [角色声线与人物鲜活度](references/craft/character-voice.md)。
- 读感审稿要遮住章拍问读者能否说清本场人物目标、阻力和实际变化；切场给足时地与视角线索，
  卷界回看开书的读者承诺。字数、场数、句长指标只能定位异常，不能证明故事好看；
  具体审稿与证据边界见 [读者体验](references/craft/reader-experience.md)。
- 人物的选择、言语、动作与情绪须符合已确立的经历、欲望、能力、认知、关系和当场处境；
  偏离既有行为模式时，正文应留下可追索的变化线索与后果。审稿以项目正典和已写正文为据，
  不凭题材、身份标签或动作词设禁令。判断方法见 [读者体验](references/craft/reader-experience.md)。
- 回收以明确状态迁移入账：伏笔与债务兑现/关闭、关系终止、道具转交/消耗、伤势恢复、死亡
  或复活。批级闸门核对逾期与遗漏，卷级闸门核对人物选择与代价、主支线兑现、时钟与世界规则。
  这些判断使用已有 phase、章拍和账本字段，不另造每章必填资产。
- 跨卷低调伏笔在 `book_outline.long_term_commitments` 记书级承诺与同 id hook；扩纲简报按需召回
  首见证据和最近显形，本章 `effects.hooks` 点名时优先切入工作包。显眼的近期承诺仍按到期章
  结算；长线埋收的因果与审稿标准见 [张力与兑现](references/craft/tension-payoff.md)。
- 更新正在写的项目后先跑 `ledger verify`：若事件链完整而旧派生快照因选择或归一语义更新
  出现字段差异，按恢复契约运行 `ledger repair --from-events`，再继续开章。
