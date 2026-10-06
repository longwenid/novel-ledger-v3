# 事实登记表（跨章硬事实不漂）

一章的正文可以完全自洽，全书却前后打架：同一个人在两处两套称谓、同一个数字两个值、
同一批数目对不上、同一年在三处三个说法、同一场事件前后各写一遍、上一稿的格式标记
留在成稿里。这类缺陷**单章读不出来**，字数/标点/术语/连续性机检也看不见，只能靠人通读整本。

对策与数字口径同一路数：把**取值域**声明出来，让机器按结构判据扫全书。

- **观测**由 skill 提供（`scripts/novel_ledger_core/content/consistency.py`），与题材无关：
  键邻近的数词、称谓后缀族、年式、集合成员、章头/残留标记/引号体例、跨章近重复、高频片段。
- **取值**由项目声明（`config.fact_keys`）。留空 → 全部事实探针恒空输出，默认零误报。
- 制度层**零作品内容**：skill 里只有键名与结构判据，没有一个具体人名、地名、年份、
  门牌或年代物件。

## 一、什么时候要声明

开书时按正典与意图把「**全书只允许一个取值**的硬事实」钉下来。至少覆盖这五类：

| 类别 | 典型对象 | kind |
|---|---|---|
| 主体称谓 | 主要人物/家族的规范写法与合法别名 | `entity` |
| 数值 | 年龄、里程、规格、深度这类带单位的量 | `number` |
| 年份与时点 | 案发/成书内事件的年份口径 | `date` |
| 二选一取值 | 方位、内外、左右、甲乙这类互斥集合 | `set` |
| 清单总数 | 一批事物的总数（人数、件数、口数） | `count` |

候选不用手抄：`book calibrate` 会从正典卡里带数值/年份的行派生 `fact_keys_candidates`
（只建议、不自动写入——哪些事实必须单值是编辑判断）。

## 二、落盘

`config set` 只能改**已存在**的键，所以先看默认表里有没有对应键（`fact_keys` / `quote_style`
/ `consistency_scan` 都在 `DEFAULT_CONFIG` 里）。事实登记表整体是一个 JSON 对象，用
`config set` 写入：

```bash
python3 "$SKILL_ROOT/scripts/novel_ledger.py" config set \
  --key fact_keys \
  --value '{"<键名>":{"kind":"number","observe":"岁","canonical":11,"hint":"<为什么钉它>"}}' \
  --project "$PROJECT"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" config get --key fact_keys --project "$PROJECT"
```

命令行传长 JSON 不方便时，可先用 `config set` 写一条最小合法形状，再按同一命令整体覆盖。
真源在 SQLite；直接编辑 `config.json` 不生效（`status` 的 `config_shadow` 会点名）。

## 三、键的形状（五种 kind）

公共字段：

- `kind`（必填）——`entity` / `number` / `date` / `set` / `count`。
- `canonical`（必填）——规范取值。数字/年份写数值，其余写词串。
- `hint` / `evidence`（建议）——为什么钉它、依据在哪张正典卡；命中回执会带出来。

按 kind 追加：

| kind | 观测字段（二选一） | 其它 |
|---|---|---|
| `entity` | `suffixes:["家"]` 按**后缀族**扫；或 `observe:"<称谓>"` 按名字扫 | `aliases` 列合法别名/身份揭示；`allow` 同义 |
| `number` | `observe:"<键>"`（如「岁」「文」） | `tolerance` 容差；同值异写（十一/11）天然不报 |
| `date` | `observe:"<键>"`（如「年」） | `era_map` 给纪年基准（`{"base":1900}`）或特例（`{"八八":1988}`） |
| `set` | `observe:"<键>"` | `canonical` 是首选取值，`allow` 列同处允许的其他成员 |
| `count` | `observe:"<那批事物的名词>"` | `canonical` 是总数 |

示例（占位键名，与任何具体作品无关）：

```json
{
  "k_household": {"kind":"entity","canonical":"张甲","suffixes":["家"],"allow":["老张"],"hint":"家族规范称谓"},
  "k_age":       {"kind":"number","observe":"岁","canonical":11,"hint":"档案定的年龄口径"},
  "k_stage":     {"kind":"number","observe":"层","canonical":2,"hint":"场景楼层"},
  "k_toll":      {"kind":"number","observe":"文","canonical":120,"tolerance":0,"hint":"过路钱口径"},
  "k_year":      {"kind":"date","observe":"年","canonical":1989,"era_map":{"base":1900}},
  "k_footprint": {"kind":"set","observe":"脚印","canonical":"左脚重","allow":["右脚重"]},
  "k_batch":     {"kind":"count","observe":"驮子","canonical":5}
}
```

`kind=entity` 配 `observe` 时只报**近形名**（与规范名同起首字、不在 `aliases` 里），
避开同句里的任意词；`suffixes` 则按后缀族扫，适合家族/宅院这类集体称谓。

## 四、什么时候扫、在哪拦

| 时机 | 命令 | 结果性质 |
|---|---|---|
| 章内提交 | `chapter draft-submit` / `chapter polish-submit` / `chapter submit` | **格式类**（重复章头、错章号、分场标记、引号混用/不闭合）当场判 `fix_draft`／`polish_rejected` 回正文；事实取值域在批级扫 |
| 批级检查点 | `run checkpoint`（每 10 章） | 硬事实冲突进 `blockers` → `review_required` 停线；近重复/格式/高频片段进 `advisories` |
| 全书巡检 | `book audit` | 出 `fact_issues` / `chapter_format` / `near_duplicate_passages` / `repeated_phrases` |
| 一次收口 | `book reconcile` | 把上面几类并进 `chapters_to_fix` 待办表 |
| 定向对账 | `book facts` | 单独摆出全部事实命中与待裁决项（只读） |
| 完本门槛 | `book complete` | `fact_issues` / `chapter_format` 为 blocker，收口前不能完本 |

`book facts` 与 `book reconcile` 同规：**单章阶段禁止直调**——章内写者不需要整书视野，
也不该为它付扫描成本。

## 五、常见失误

1. **拿 `glossary` 当事实登记表用。** `glossary` 是「旧写法→规范写法」的替换表，
   只能拦你**猜到**的那一个错词；它拦不住「另一个从没见过的称谓」「另一个数字」。
   事实登记表声明的是取值域，扫的是整类取值。
2. **只钉金额不钉年份/人数/称谓。** 金额有 `quant_keys` 管，最容易漏的恰恰是年份口径、
   批次数目、家族称谓这三类结构性事实。
3. **把同值异写当冲突。** 同值异写（十一/11、二百/两百）解析后相等，不会报；
   但 `kind=number` 的 `canonical` 必须写数值，不能写「十一」这种词串。
4. **`observe` 写得太泛。** `observe` 是「正文里按什么找取值」——写成「的」「说」会在整章扫，
   产生大量假阳。取那个真正的口径名（「岁」「文」「年」「脚印」）。
5. **单位不同的两个数当成冲突。** 探针按单位分组（「文」与「成」是两个量纲），
   所以「一段」与「二段」不会互相定罪；但同一单位下的两个值一定报，交人工裁决。
6. **declaration 空转。** `fact_keys` 为空时 `book audit` 出 `fact_declarations_missing`
   advisory 点名「闸门全部空转」，并指路 `book calibrate`。

## 六、修复 SOP（发现冲突之后）

```bash
# 1) 取事实清单（只读）
python3 "$SKILL_ROOT/scripts/novel_ledger.py" book facts --project "$PROJECT"
# 2) 定权威值：以正典/账本/最早证据为准；改错的一方
#    单句级用 chapter patch 定点替换（自愈哈希，零模型轮次）
python3 "$SKILL_ROOT/scripts/novel_ledger.py" chapter patch --chapter <N> \
  --from "<旧写法>" --to "<规范写法>" --actor <谁> --reason "<依据>" --project "$PROJECT"
# 3) 收口：确认 clean=true
python3 "$SKILL_ROOT/scripts/novel_ledger.py" book reconcile --project "$PROJECT"
```

`chapter patch` 是唯一 fail-closed 的地方：把账本引文改断会被当场拒绝，所以先改账本
（`ledger repair --from-events`）再改正文的顺序与量化口径收口一致。

大面积改尺/改正典之后，按 [巡检与治理](governance.md) 的量化口径四步走完整流程。
