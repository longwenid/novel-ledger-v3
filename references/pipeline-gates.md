# 流水线与机检

compact 泳线的行为语义：阶段推进、返工回流与文风机检（polish-submit 收口）。数据契约见 [pack-contract.md](pack-contract.md) 与 [write-output.md](write-output.md)。
## 流水线行为（单一 compact 泳线）

默认**只写作模式**（`config.polish = "off"`，skill 缺省）：`draft → assemble`，
草稿即终稿。按书恢复润色泳线用 `config set --key polish --value on`（下一章起生效）：

- 只写作模式下 `draft-submit` 直通 `await_assembly`（详见下文「只写作模式」条）；
- 润色开启时 `draft-submit` 直接进 `await_polish`；
- `polish-submit` 直接过机检后进 `await_assembly`：
  若 `style_check` 启用，文风硬红线机检在 `polish-submit` 后原地执行，失败仍走
  `style-metrics-<ch>.json` → 回 polish，满 `style_metrics_limit` 停线 `blocked`；
  内容锚点机检（beats/must / 连续性 / glossary）也在 `polish-submit` 原地跑一次，
  但**只拦润色自己改坏的**（详见下文「润色收口的内容锚点闸门」）；
- beats / must / 连续性 / 防失忆 / 术语等 submit 机检不变，整链失败回 `await_draft`。
- **情节自检由事实编辑兼做**：`action=assemble` 的响应带
  `plot_self_check` 清单，事实编辑在组装时逐项核对终稿（拍点兑现／能力边界／时间线因果／
  量化口径／连续性／首读定位与转场／章内局部变化和人物反应／人设与行为一致性），真问题写进 submit JSON 的
  `plot_findings`。读感疑问默认记 `WARNING`，只有正文可证实的客观矛盾才记 `BLOCKER`；
  审稿方法见 [读者体验](craft/reader-experience.md)。机检对其分两类处置：
  - BLOCKER 条目形状/证据合法（`hint` 有、`quote` 逐字在终稿且 ≥6 字）→ 判**正文问题**，回 `await_draft`
    重写，`verdict=plot_fix`，消耗同一份 `rewrite_limit`；
  - 字段缺失或形状/证据不合法（`plot_finding_missing_hint` / `plot_finding_quote_not_in_prose` / …）→ 判
    **组装契约问题**，回 `await_assembly` 重跑，保留草稿与终稿。

**润色收口的内容锚点闸门**：`polish-submit` 用 canonical pack 对终稿跑
`validate_prose_anchors`（beats/must 兑现、连续性防失忆、glossary 术语归一化），
并按**问题来源**分流——草稿本就是坏的锚点不在这里抢先处理，仍由 `submit` 回 `draft`
整链重写（既有语义与 `rewrite_count` 预算不变）；只有终稿**新出现**的问题（草稿干净、
润色在场景级重构里改丢了 must 词 / 写了陌生人初见 / 引入非规范术语）才回 polish 重润。
判定逐条归属，不看整体：混合场景只弹回润色引入的那几条。

失败时把清单原子写入 `book/staging/polish-anchors-<ch>.json`，返回
`verdict=polish_anchor_failed`（phase 回 `await_polish`，下次 `action=polish` 带
`polish_anchor_path`）；**上一版终稿保留在原路径**（锚点缺失多半只是丢词，原地补锚点后
重交即可，`polish-submit` 每次重跑逐字机检，不存在绕过空间）。文风硬红线闸门同理：
`verdict=style_metrics_failed` 返回 `polished_kept=true`，终稿不删，写者按
`style-metrics-<ch>.json` 的机检清单原位修复。计数
`HEAD.polish_anchor_retry` 达到 `config.polish_anchor_limit`（默认 2）→ `blocked`
（reason=`polish_anchor_failed`），由总编辑裁决：若锚点其实源自草稿（补拍没兑现），
从 blocked 走 `retry-authorize` 回 `draft`；若只是润色手滑，调 `polish_anchor_limit` 或修手册。

**style 耗尽 blocked 的轻解锁**：文风机检重试耗尽
（reason=`style_metrics_failed`）而正文未入账时，`retry-authorize --action style`
**原位恢复重试预算**——不回滚、不重装配，phase 回 `await_polish` 并带上失败清单，
写者定点修润色稿（或 `chapter patch`）后重交，`polish-submit` 重跑全部机检、无绕过空间。
默认 `retry-authorize`（rewrite）仍是整章回滚，适用于锚点源自草稿或已入账的场景。
人因留痕进 quality 日志（`style_retry_authorized`，actor/reason 必填）。

这道闸门是**纯增量**：没有它时这些锚点要等到 `submit` 才发现，然后按正文类问题回 `draft`
整链重写（重写草稿 → 重润 → 重组装）；有它之后润色侧的滑手只需回 polish。

**submit 失败重试分级**：仅命中组装契约 issue（delta/beats_hit 结构、
`pack_hash`、`missing_field` 等）→ 回到 `await_assembly` 只重组装，草稿与润色终稿保留；命中
must/字数/连续性/术语等正文 issue → 回到 `await_draft` 整链重跑（正文重写）。

**外文残片闸（`foreign_fragment`）**：正文混入长拉丁字母串（模型输出事故，如
condensedcondensed 这类粘连重复）确定性机检直拦——曾只靠 draft prompt 自查项兜底，
自查是概率性的，残片是确定性的。与字数带同 philosophy：最便宜的 `draft-submit` 点
就地拦（`draft_rejected`，phase 留 `await_draft`，staging 草稿保留，原位删除/改写后重提，
零配额）；组装 `submit` 时仍复检，单独命中按 recovery `one_point_prose` 走零模型
rework-patch。确需拉丁文（咒语/术语）的书 `config set foreign_fragment_gate=allow` 按书豁免。

**成稿格式闸（`chapter_format`）**：与字数带、外文残片同 philosophy——最便宜的
`draft-submit` 点就地拦，`draft_rejected` / `polish_rejected`，phase 留在原相位、
staging 文件保留，原位修完重提。判据全部是**确定性**缺陷（小说正文不会自然出现）：

- `duplicated_chapter_header`（同章两遍章头：多稿缝合的典型残留）、
  `chapter_header_mismatch`（章头数字 ≠ 实际章号）、`chapter_header_style_mixed`
  （全书阿拉伯/中文数字体例分裂，跨章聚合）；
- `story_marker_residue`（「（本章完）」「待续」类平台话术、编辑提示式标记）、
  `scene_break_marker`（写作期分场标记「第 N 场」）、`bare_scene_heading`（裸标题行）；
- `quote_style_mixed` / `quote_unpaired`：四套引号体例逐一查配对与混用。

只写作模式（`polish=off`，skill 缺省）下草稿即终稿，这道闸是格式残留唯一还能被拦住的地方，
所以它同时挂在 `draft-submit`。润色阶段同理：只拦**润色自己引入**的格式缺陷
（草稿干净、润色重排标点时改坏的），回 polish 重润而不整链重写。按书锁定体例用
`config set --key quote_style --value <cn_double|cn_corner|zh_book|ascii>`（默认 `auto`：
只判「同章内只准一种体系」）。

格式之外的书级事实一致性（同一事实两套取值、跨章近重复叙述、高频片段）不在章内硬闸：
判据需要整书视野，落在 `run checkpoint`（硬事实冲突停线）、`book audit` 与 `book reconcile`
（advisory 待办）、`book complete`（收为 blocker）。写法见[事实登记表](fact-registry.md)。

## 文风机检（polish-submit 收口）

`config.style_check`（`true`，或 `auto` 且 voice=shijing）时，`polish-submit` 区分明确出戏的
话术与单本参考小说的统计特征：

- **逐章硬闸**只拦明显的元叙述、平台话术和明确的章尾套话。命中时把清单写入
  `book/staging/style-metrics-<ch>.json`，返回 `style_metrics_failed`（phase 回 `await_polish`，
  下次 `action=polish` 附 `style_metrics_path`），文风编辑定点修复。同一 pack 的失败计数
  `HEAD.style_metrics_retry` 达到 `config.style_metrics_limit`（默认 2）后进入 `blocked`，
  保留终稿与清单，由总编辑裁决。
- **只写作模式**（`config.polish = "off"`，**skill 缺省**）：`draft-submit` 直通组装，草稿即终稿——润色相位、
  润色手册与文风机检整体退出写链（硬闸不再拦线，要自查用 `chapter precheck`，只读）；
  字数带（章合同）仍在 `draft-submit` 收口。适用于「先不管文风、只要产出」的批量写作；
  后续想补润色可按批回补（quick-editor 就地修润，不重走整章流水线）。
  恢复润色走 `config set --key polish --value on`（下一章起生效，无需重启）：
  磁盘 `config.json` 只是数据库真源的投影视图，直接改文件**永远不生效**，
  `status` 的 `config_shadow` 字段会点名这种漂移。
- **统计只作诊断**：句长及其变异（句长CV）、对白段型、句式形状（短句排队/同头排比）、标点、
  连接词、对照句、明喻、独白引导词、现代词、职场黑话和套路词密度的偏离均不触发返工。
  `polish-submit` 按指标去重返回 `style_warnings_count`；
  批级与卷级指纹保留具体偏离供抽样通读。数值来自单本参考小说，不能证明另一章好坏，
  不应为了落带添加动作、延长台词或重排段落。
- **人写节奏锚（可选）**：`config.voice_anchor_file` 指向人写参考文本
  （相对路径按书项目根解析，编码 utf-8/gb18030 自动探测）。按当前场景词选择完整参考段落，
  也可用 `voice_anchor_query` 或 `voice_anchor_paragraphs` 指定需要的例子；无匹配时按章号种子稳定选段。
  `voice_anchor_slices` 是默认示例段数，不是文字长度门；选中段落不按 `voice_anchor_chars` 裁短。
  `voice_anchor_text` 随 polish 视图下发并附来源，锚文件全部内容 sha256 进入 `inputs_fingerprint`。
  契约是**只学节奏不学内容**：片段头部自带防泄漏声明，严禁借用其中人名/组织/设定/情节。
- **书级诊断旋钮**：`config.style_structure = "off"` 关闭句长、对白段型与句式形状提示；
  `config.style_structure_limits` 调整句长参考带（含句长CV 下限），`config.style_contrast_limit` 调整对照句
  提示的计数口径。这些旋钮不改变硬闸通过条件。项目编辑以实际阅读证据判断是否需要修订；
  检查方法见 [读者体验](craft/reader-experience.md)。
- **findings 分级契约**：`plot_findings`（情节自检）与总编辑收到的 findings 逐条带分级标签，
  只认这四个（见 [roles.md 审校 findings 分级](roles.md#6-审校-findings-分级与生命周期)）：

  | 标签 | 对 verdict 的作用 | 收口后的去向 |
  |---|---|---|
  | `[BLOCKER]` | **仅当存在 BLOCKER 才判 `fix`**；fix 回对应编辑域重做 | 硬断路器（第 1 轮原位修 / 每次跨阶段回流创建新空会话；满 2 轮由总编辑裁决收口或转 `chapter patch`） |
  | `[WARNING]` | 允许 `pass` | 逐条落 editorial/findings.jsonl，quality 同时记计数和证据，批级闸门 triage |
  | `[NIT]` | 允许 `pass` | 同上，卷级闸门一次性清账 |
  | `[UNVERIFIABLE]` | **不阻断本域判定**，可以照常 pass | 由总编辑逐条处置（补派复核 / 追加视图 / 明确记账） |

  `[UNVERIFIABLE]` 必须写清“需要什么才能验证”。它是显式出口而不是免检通道：
  把本次视图里看不见的判据判成 `pass` 又不声明，等于用“审校没看见”冒充“审校验过了”。
  机检校验 severity 和逐字证据；缺标签回组装修字段，不能直接触发正文重写。
  v2 输出必须显式含 plot_findings，无问题 []。UNVERIFIABLE 可不带 quote，但须说明缺失判据；其它分级必须带原句。
  所有合法发现写入 editorial/findings.jsonl，review resolve 追加处置，不随 staging 清理消失。
- **风格诊断不做逐章 pass/fail**：`polish-submit` 汇总统计偏离与重复套语的提示，
  只返回去重后的 `style_warnings_count`，不触发返工；累计指纹在每
  `config.style_fingerprint_every`（默认 10）章 ack 时与 `book audit` 中自动比对同一基线
  （需 ≥ `style_fingerprint_min` 默认 5 章才出结果），作为 advisory `style_fingerprint`，不挡下一章。
