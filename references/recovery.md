# 恢复与 blocked 处置

写锁、崩溃恢复语义与人三处扩展 action。文件契约见 [runtime-contract.md](runtime-contract.md)。

恢复会话从 `status`、当前 action 和问题来源路径重建任务。按需读取完整相关合同、账本记录和正文，
不继承旧聊天，也不按字数裁短已选证据；诊断明细可按窗口分页。
## 并发与恢复语义

- **写锁**：`chapter *`、`plan extend`、`kb sync`、`voice apply`、`retry-authorize`、`hooks close/defer/merge`、`relations rename/close`、`book hatch/complete/reopen/resync-baseline` 与 `ledger repair` 先取 `book/run/LOCK` 排他锁；拿不到立即返回 `locked`，不排队。
- **CLI commit 是数据库事务**：events、snapshot、正文、meta、summary、hierarchy、voice 与 HEAD
  在同一 SQLite 事务提交；异常回滚，进程崩溃由 SQLite 日志恢复。文件导出在事务成功后生成，
  中断可 `database export` 再生成。supervisor 的短写锁区也使用事务；直接调用 Python 函数
  需显式 `store.transaction()`。旧文件项目或未包事务的程序调用仍按下述幂等前滚恢复。
- **重跑不会重复入账**：`commit_event` 幂等保护——账本里已有本章及其后的事件就拒绝再 append（`ledger_replay_conflict`）。事实按人物与文本去重，但重复事件仍会污染哈希链与其他状态迁移，必须整体拒绝。
- **崩溃重跑默认前滚**：`ledger_replay_conflict` 时若账本里的本章事件与落盘 `output.json` 的 `state_delta` 逐字节一致、且已写盘的 meta（若有）与本次正文哈希一致，幂等尾部（md/meta/summary/hierarchy/voice/HEAD 全是原子覆盖）直接续跑进 `ack`——已写完的章不再要求人工 `retry-authorize` 整章回滚重写。delta 分歧或正文哈希不符 → 仍保守 `blocked` 等人。逐崩溃点收敛由 `test_commit_failpoints.py` 参数化证明。
- **事件哈希链**：事件携带 `prev_hash` + `hash`（sha256(prev_hash + canonical(事件体))）成链；无哈希事件即断链。`ledger verify` 逐条重算（`event_chain_ok` / `event_chain_issues`）；链断时 `ledger repair` 拒绝从真源重建（否则等于把篡改洗白进 snapshot）。伏笔与关系裁决追加独立治理事件，不改旧章哈希；`book resync-baseline --restamp-canon` 追加正典基线裁决事件。
- **崩溃后的恢复动作是 verify + repair，不是盲目重跑**：先 `ledger verify` 看清 `diffs`；快照被篡改/半写坏 → `ledger repair --from-events`；本章已入账但要重写 → `retry-authorize`（`blocked`，或 `idle` 且 `last_committed_ch == last_acked_ch == chapter`；会先撤销本章事件、删掉旧稿、重置 `last_acked_ch` 再重写）。
- **已写项目更新运行时后**：先 `ledger verify`。若事件链完整，而旧快照与新版本的事实去重、角色状态配额或道具转交归一结果不同，这是派生视图差异；核对 `diffs` 后运行 `ledger repair --from-events` 重建快照，再继续写章。事件链损坏仍拒绝修复。
- **配置 schema**：只接受当前精确版本；缺失、较旧或较新都直接拒绝。提供旧文件存储到 SQLite 的显式迁移，不自动升级 config schema；见 [SQLite 契约](sqlite-memory.md)。
- **计划层缺陷（死锁签名与修补通道）**：`expected_delta_missing` 与提交端唯一性闸门
  互斥时（签名：expected 里同章同 `(who, topic_id)` 两条 knowledge + 回执带
  `knowledge_duplicate_topic`），任何提交都无法通过，正文返工无效——根因在计划层。
  未写章用 `plan patch-chapter` 定点修（合并重复条目；契约与边界见
  [extend-contract.md 未写章的拍级定点修补](extend-contract.md#未写章的拍级定点修补plan-patch-chapter)），
  禁止手改计划真源。已提交章的计划缺陷不做拍级回改，按作者授权走 retry-authorize。
- **失败梯度**：submit 机检失败 → `rewrite` 一次（同 pack，仍自动）→ 再失败 `blocked`；文风硬红线
  机检失败 → 清单落 `style-metrics-<ch>.json` 并回 polish 定向返工，连续满 `style_metrics_limit`
  次 → `blocked`；节奏分布只在累计层出 advisory `style_fingerprint`；ack `--verdict p0` → 同章重写
  （仍自动，先回滚账本）；配额用尽或入账冲突 → `blocked` 不跳章。
  写锁冲突是 `locked`，不排队。
- **同章重写必先回滚账本**：`ack --verdict p0` 触发的同章重写，先 `rollback_ledger_to(本章)`（滤掉 `chapter >= N` 的章节事件和生效章 `>= N` 的治理事件，再按剩余事件重建快照），再删旧稿的 md/meta/summary/ack，**然后才装配新 pack**。否则废稿申报的 facts/debts/hooks/relations 永久留账，重写稿入账后同一实体下出现两稿并存的事实，而废稿正文已删、无从排查；pack 也会带上废稿状态，且清空 `session_notes` 后指纹失配触发 `stale_pack`。`deaths` 入账即置 `dead=true`，此后涉及他的 `named`/`moves` 都会 `dead_speaking` 停线（要写遗体/鬼魂/闪回用 `nonliving` 声明，要写亡者归来用 `revivals` 撤销，见 [write-output.md Write output](write-output.md#write-outputchapter-submit---output)）；废稿里误杀的角色若不回滚仍会一直挡路，所以同章重写必须先回滚账本。回滚后若 `kept_events=0`（整本从第 1 章重来），同时清空 `voice.json` 的 `session_notes`，避免废稿回灌的笔记再进重写 pack。
- reopen 配额用尽而 `blocked` 时**不**自动回滚：废稿保留给总编辑排查，总编辑
  `retry-authorize` 时才回滚重写；越过作者红线才升级作者。
- **ack 门禁与 reopen 配额（`config.require_ack` / `config.reopen_limit`）**：`require_ack`
  （默认 `true`）控制"无 ack 不能下一章"这道硬门禁——置 `false` 可让 commit 后直接开下一章，
  用于一次性校验之外的批量场景；`reopen_limit`（默认 1）是 ack `p0` 同章重写的配额上限，
  同一章用尽后 `blocked`（见上一条）。两者都在新项目 `DEFAULT_CONFIG` 中显式生成。


## chapter next 扩展 action

当流水线遭遇阻断（blocked）、低水位（extend_plan）或全书完结（complete）时，由作者或总编辑介入处置（即“人三处”）：

```bash
# 1. 换走向或已 ack 章整章重写（撤销本章事件、重置状态以重跑流水线）
python3 "$SKILL_ROOT/scripts/novel_ledger.py" retry-authorize \
  --project "$PROJECT" --actor "$WHO" --reason "$WHY"

# 2. 全书完结（经总编辑书级审计建议后由作者正式收口完结）
python3 "$SKILL_ROOT/scripts/novel_ledger.py" book complete \
  --project "$PROJECT" --actor "$WHO" --reason "$WHY"

# 3. 计划低水位扩充（结构化阶段小纲 + 本阶段章拍，原子追加）
python3 "$SKILL_ROOT/scripts/novel_ledger.py" plan extend \
  --project "$PROJECT" --chapters "$MORE_CHAPTERS_JSON"
```

- **`retry-authorize`**：仅在 `blocked` 或最后一章已 ack 时可用。由总编辑或作者在调整必要账本/正典后执行，禁止直接手改 `book/chapters/` 已入账文件。**授权即回滚当前稿**：末章正文、ack 与账本事件从当前状态整体撤销，SQLite 保留正文旧修订但不参与正常召回（`last_acked_ch` 回退、hooks 回到上一章状态），不是"只解锁不改数据"。
- **`book complete`**：整书写完且通过全书审计后由作者收口；不足目标字数 90% 默认拒绝。作者明确改约缩短全书时才加 `--override-target`。
- **`plan extend`**：用于解除低水位或“计划耗尽但目标未完成”的 `action=extend_plan`。输入文件必须是
  `{"phase": {...}, "chapters": [...]}`；`phase` 的章节范围必须与本批连续章号完全一致，并同时引用
  四层全书事件脊柱中同一幕的主线事件、1–3 个支线事件、1–3 个时间线事件及起伏曲线阶段。
  四层缺项、错层、跨幕或章节越界都会整批拒绝，不会只写入一半。章拍默认 5 场，非空为硬门；书级规模合同仍需满足。扩纲的全部硬校验清单见 [extend-contract.md 扩纲硬契约清单](extend-contract.md#扩纲硬契约清单plan-extend-的全部硬校验)。
