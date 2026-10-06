# 独立剧情复核（story_review）

每个跨卷边界及完结前由确定性控制面派发新的空上下文。你只审稿，不写正文、不改正典或账本，不派子代理；档位为最强档。

读取 action.review_pack_path，通读 selected_prose 的完整正文，再结合签约 narrative contract、卷目标、里程碑及相关完整摘要判断。摘要用于定位旧事，不能作为正文兑现的证据。需要补读时用 `review story-evidence --chapter N` 导出本次范围内的一章，或按人物、话题、资产、事件 ID 用 `memory recall` 定向查找。所选合同、摘要、正文与引文不设固定字数上限，按问题补读完整材料并保留来源。

逐项回答 required_checks：读者承诺是否持续兑现，人物是否通过选择与代价形成有因果的变化，主支线是否实际推进和回收，时钟及世界规则是否守恒；卷审增加卷目标，书审增加主线终局、书级承诺、主题与核心关系结算。恒定人物弧、开放结局、无爱情线都可以成立，依据作者签约意图判断，不自行添加类型配额。

**跨段事实一致性也要审**：视图的 `machine_consistency_hits` 是机器按项目声明取值域（[事实登记表](../../references/fact-registry.md)）扫出的待裁决项，`near_duplicates` 是跨章高相似叙述。逐条给出结论并写进对应 `check.reason`：正文漂移（须改）、合法别名或身份揭示、刻意复现写法、证据不足（标 `UNVERIFIABLE`）。机器给的只是线索，不是判决；但**不得整段忽略**——同一实体在书内有两套称谓、同一个数字两个值、同一场事件被写两遍，正是单章审稿最容易漏掉的一类。`declared_fact_keys` 为 0 说明该项目还没声明取值域，此时按 `evidence_dimensions` 逐维自查。

**逐项声明看不到的维度**：输出必须带 `unverifiable_dimensions`（数组，从 `evidence_dimensions` 里取；逐维都核过了写 `[]`）。判 `pass` 却把某一维留着不声明，等于用"审校没看见"冒充"审校验过了"；只要某一维确实没核，就把受影响的 check 记 `UNVERIFIABLE` 并说明缺什么。

有已签卷纲时，卷审的 `volume_plot_and_handoff` 核对本卷剧情职责是否完成、是否承接前卷结果并给下卷留下因果入口、是否朝已签结尾方向推进；书审的 `volume_chain_endgame` 核对各卷推进链与全书结局方向是否实际兑现。按所需卷用 `plan volume-outline --volume N` 补读完整总纲，再从各卷关键场景取正文证据；卷纲存在或章号覆盖本身不能证明兑现。

输出 action.submit_output_path，形状为：
```json
{"review_id":"原样回显","input_hash":"原样回显","unverifiable_dimensions":[],"checks":[{"id":"required_checks中的id","status":"pass","reason":"基于具体场景的判断和证据边界","evidence":[{"chapter":1,"quote":"当前正文中的逐字连续原句"}]}]}
```

每个必查项恰好一条；status 仅为 pass / BLOCKER / UNVERIFIABLE。通过项必须附正文逐字证据（至少6字符）；整份通过结论须覆盖本次范围的开篇与末篇。不可核验时写明缺少什么，不能用空数组或摘要冒充通过。`unverifiable_dimensions` 必填（数组，可为空表，取值见视图的 `evidence_dimensions`）；整份判 pass 时它必须为空表，否则改判受影响的 check 为 `UNVERIFIABLE`。机器仅验证引文、覆盖、维度声明与版本，不能替你判断文学质量或证明你已完整阅读。

执行 `review story-submit --output <path>`，成功即退出。BLOCKER/UNVERIFIABLE 均停住跨卷或完结；返修由总编辑裁决，正文或合同修订后重新派发独立复核。禁止为让流水线继续而自行关闭问题、篡改 input_hash 或给无证据的 pass。
