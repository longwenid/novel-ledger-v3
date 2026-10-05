# SQLite 持久化与按需记忆

每本书只有一个持久化来源：`book/novel.sqlite3`。它保存配置、HEAD、章拍、正典、
章节正文、摘要、分层记忆、文风锚、账本、工作包、终审、用量、编辑裁决与运行记录。
文档中沿用的 `book/...json/md/jsonl` 是数据库内的文档地址，同时也是可再生成的文件导出位置。
外部改写或删除这些导出文件不会修改事实。写作 worker 尚未提交的 staging 稿件、外部命令输入、
进程锁与 driver 临时输出仍是文件；提交验收后才成为数据库记录。

运行时只使用 Python 标准库 `sqlite3`。SQLite schema 与项目 config schema 分别版本化；
不支持的数据库版本直接拒绝。命令级写事务先提交数据库，再输出当前改动的文件副本；
导出中断后用 `database export` 再生成。supervisor 在短写锁区内使用事务，等待模型时不占事务。
直接调用多步 Python 领域函数时，调用方需用 `with store.transaction()` 包住整个操作。

## 新书和旧书

新书照常 `book hatch`，自动使用 SQLite。已有文件项目先停止旧运行器，再迁移：

```bash
python3 "$SKILL_ROOT/scripts/novel_ledger.py" database migrate --project "$PROJECT"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" database verify --project "$PROJECT"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" ledger verify --project "$PROJECT"
```

迁移检查 UTF-8/JSON、事件哈希链与快照重放；通过后原子安装数据库。失败不安装半库，
原文件保留。重复迁移返回已完成，不会重新从导出文件覆盖数据。已验收但未入账的草稿/终稿按
HEAD 阶段一并迁移；尚未提交的输入继续留在 staging。配置 schema 仍必须符合现有版本。

导出用于阅读和阶段交接，数据库备份保留全部正文修订历史：

```bash
python3 "$SKILL_ROOT/scripts/novel_ledger.py" database export --project "$PROJECT"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" database export --project "$PROJECT" --destination "$EXPORT_DIR"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" database backup --project "$PROJECT" --destination "$BACKUP_DB"
```

备份使用 SQLite 在线备份接口，要求目标文件不存在。文件导出只包含当前文档，不包含旧修订；
不能把它当成等价的数据库备份。`database verify` 检查结构、外键与文档哈希，
`ledger verify` 检查事件和业务状态，两者互补。

编辑意图、卷纲、正典或人写文风参考时，用明确导入入口更新数据库：

```bash
python3 "$SKILL_ROOT/scripts/novel_ledger.py" database import-source --project "$PROJECT" --kind intent --source "$INTENT_FILE"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" database import-source --project "$PROJECT" --kind outline --source "$OUTLINE_FILE"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" database import-source --project "$PROJECT" --kind canon --source "$CANON_DIR"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" database import-source --project "$PROJECT" --kind anchor --source "$ANCHOR_FILE"
```

正典按相对地址合并并在同一事务中重新编译 cards；不删除输入目录之外的已有正典。
输入为普通文件，可来自项目导出目录；导入前应核对内容。`kb sync` 只重编数据库中的正典。
章拍仍通过 `plan extend` / `plan select-batch`，正文通过 submit / patch 修改。

## 完整来源补读

根据当前角色、事件和疑点读取完整来源或标题节：

```bash
python3 "$SKILL_ROOT/scripts/novel_ledger.py" context read --project "$PROJECT" --source intent
python3 "$SKILL_ROOT/scripts/novel_ledger.py" context read --project "$PROJECT" --source outline --section "$HEADING"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" context read --project "$PROJECT" --source canon --name "$CANON_RELATIVE_FILE" --section "$HEADING"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" plan volume-outline --project "$PROJECT" --volume 2
```

`--section` 匹配完整标题；未指定时返回完整来源。正典文件名是正典目录内的相对地址。
`plan volume-outline` 给全卷索引和所选卷的完整总纲及字段。材料不按字数截断，
来源内容和版本仍参与输入指纹与审稿回执；读取更多材料不授权角色改写冻结事实或跨阶段工作。

## 分层与召回

近章摘要、阶段摘要、卷摘要和全书脊柱负责方向。历史数据库保存完整细节；
新会话从当前工作包和来源路径读取任务相关材料。每章默认自动按主角、在场者与
`knowledge_refs` 召回历史。需要更早的确定性证据时，策划在章拍声明：

- `memory_refs`：旧事件 ID 列表（事件 hash）；显式引用优先，未知或未来引用拒绝装包，
  不能静默忽略。字段形状仍遵循章拍契约。
- `memory_query`：词法查询，补查尚未登记为结构化事实的旧正文细节。
- 历史记录按人物、话题、资产、事件 ID 与关键词选择。选中记录保留完整文本、引文和来源路径，
  不使用固定字符预算削短证据；诊断查询可用 `--limit` 按记录分页。

历史切片加入 canonical `historical_recall` 和输入指纹；draft 只读渲染后的简报，
assemble 读结构化证据，polish 按文风职责读取输入。角色视野不足时按事件或来源定向补读；
需要修改章拍、正典或书级方向时交总编辑裁决。

```bash
python3 "$SKILL_ROOT/scripts/novel_ledger.py" memory recall --project "$PROJECT" --person "$PERSON" --topic "$TOPIC" --before-chapter 100
python3 "$SKILL_ROOT/scripts/novel_ledger.py" memory recall --project "$PROJECT" --asset "$ASSET_ID" --query "$KEYWORD" --limit 8
```

人物、话题、资产、事件 ID 和关键词以“任一命中”查候选；显式事件优先，其次共同在场者、
最近章节。截止严格小于目标章，并且不超过 HEAD 已提交范围。数据库保存全量数据，
进入上下文的是任务相关结果与按需补读材料，检索仍可能因词法和关联条件漏查。分页信息说明
本次尚未返回的候选数量与继续读取方式，不能据此判断整库没有相关事实。
人物选择器使用规范名，别名归一仍遵循现有账本纪律。

`evidence_status=verified` 表示引文仍是当前数据库正文的逐字子串；`record_only` 表示
仅有账本记录，当前原文无法核验。正文 patch 会更新搜索索引，旧引用失效即降级；
回滚撤销事件及当前搜索记录，历史正文修订留在 revisions，不参与正常召回。
人物认知附 `who/topic/stance/source/latest_knowledge`；被后续认知覆盖的记录只作历史，
人物误信、怀疑和客观事实必须分别理解。检索命中本身不能证明因果、真伪或全书无矛盾。

## 表与关联

| 表 | 数据与用途 |
|---|---|
| `documents` | 全部当前持久化文档、内容、修订号与哈希；既有领域序列化契约继续使用 |
| `revisions` | 已入账章节正文的历次写入版本，删稿后保留 |
| `chapters` | 章号、正文地址、当前哈希与修订号 |
| `events` | 事件 ID、顺序、生效章与完整事件；由数据库内事件文档投影 |
| `event_entities` | 事件与规范名关联，支持共同参与者查询 |
| `memories` | 事实、认知、关系、物品、伏笔等事件条目与摘要，关联事件 ID |
| `passages` / `passage_fts` | 当前正文段落及可选 FTS5 trigram 索引 |

投影与来源在同一事务更新。事件和正文已提供关系查询；其余文档先保留稳定 JSON 契约，
不把每个配置字段都拆成列。中文三字以上使用可用的 FTS5 trigram，短词或缺少该模块时回退
子串查询；这是词法与关联检索，不是向量语义记忆，同义改写需要显式引用或更合适的关键词。
