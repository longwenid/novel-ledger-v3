# 强制跨卷与终局复核

章内剧情自检仍由 assemble 承担，ack 仍核对本章成品完整性。跨卷和计划耗尽接近字数目标时，`chapter next` 在 idle 返回 `story_review`，默认驱动自动派发新的空上下文；即使章节使用 inline 或 worker-agent，剧情复核也必须独立会话。

复核输入包括带版本的叙事合同、卷目标与相关里程碑、相关章摘要和完整正文来源，以及完结时的既有卷审结论。审稿按必查问题选择场景，通读已选正文；摘要用于定位，实际兑现须回到原文取证。所有已审范围内正文哈希、计划、相关治理历史、KB与合同共同绑定 input_hash；旧书全部来源的变更也参与哈希。原始正典与已编译 KB 不同步时，先 `kb sync`，再复核。后续新章不会使旧卷审自动失效；回改已审正文或合同会使其失效。`review story-evidence` 可按章导出范围内原文补证据；需要更多章节时继续定向读取。无法核验时须标 UNVERIFIABLE。

复核相关意图、约定与正典可用 `context read --source intent|outline|canon`，按需要选择完整标题节；
卷级定位用 `plan volume-outline --volume N` 读取完整已签卷纲。历史场景按章号、人物、话题或事件定向补读。

合同、卷纲、摘要、正文、引文与回执均不设固定字数上限。已选材料完整保留，作者红线、读者承诺和世界规则按来源读取；相关材料较多时按问题及章节分次读取，不能通过缩短证据制造通过结论。

签约卷纲同时进入审稿判据：卷审增加 `volume_plot_and_handoff`，核对本卷剧情职责、前后卷因果交接与结尾方向；有全卷合同的书审增加 `volume_chain_endgame`，核对全卷推进链与全书结局是否整体兑现。审稿读取相关卷的完整总纲，并以已写场景取证，不能只凭总纲字数或章节范围通过。

审稿只评价作者意图所要求的承诺、人物选择及代价、人物弧、主支线、时间和规则；主题/关系结算允许作者已签的开放形态。没有统一文学分数、爽点频率或人物成长节点数量。

提交 `review story-submit --output <JSON>`：恰好覆盖 required_checks，pass 必须有逐字正文证据，整份通过须引用本范围首末章。任何 BLOCKER/UNVERIFIABLE 保存 fix 回执，下一步返回 `story_review_blocked`，停线等待总编辑选择局部补丁、章重写或合同修订。修订后回执失效，新会话重审。回执保存在 SQLite 的 `book/editorial/story-reviews.json`；staging 只作交接。

宿主回传真实 session_id/context_origin=empty，驱动防止复用会话，阶段写权限只允许 story-submit、story-evidence 和 telemetry。回执、会话证明和正文引文分别证明不同边界，均不能自动证明小说好看。

正常 `book complete` 要求计划已写完、已提交全部通读、阶段收口、书级承诺已兑现或已明确改约、卷审与终局审为当前版本的 pass，并通过全书机检。`--override-target` 只批准缩短字数，不豁免这些要求；此时可用 `review story-next --complete` 准备终局复核。

自动连写验收通过后调用同一严格完本入口，更新全书状态。重新检查正常完本仍验证当前来源和回执；失效则先 `book reopen` 后复核。已记录的作者字数改约只对原签字数目标有效，重新打开会清除该完本记录。

作者明确决定提前封笔时，用 `book close-early --author-confirmed --actor <作者> --reason <理由>`。它记录 completion_kind=early_close 和裁决，报告 normal_completion=false，不作为正常完本验收。运行代理不得自行添加 author-confirmed。`book reopen` 恢复原待办阶段，保留通读及中间产物。
