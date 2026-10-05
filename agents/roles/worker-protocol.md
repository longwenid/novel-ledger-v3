# 一次性 Worker 执行协议（Worker Protocol）

> **本卡是 `chapter next` 返回的 `worker` 派发简报的固定文案真源**：简报只带路径与指针
> （`protocol_card` 指向本卡、`protocol` 指明适用小节），循环、卫生铁律与停止纪律全部住在
> 本卡。信封每个阶段都会重发，固定文案不跟着重发（消除每章 ×4 重复）。
> 简报 `worker_protocol` 是协议版本行：与本卡版本不一致说明宿主装了新旧两个 skill 实例
> 混跑，立即停止并如实报告，不要继续写作。
> 开工第一步：本卡、当前 action 文件与唯一角色卡一次性批量读取；阶段结束即退出。

## 一、卫生铁律（hygiene，硬约束）

- paths only: pack JSON and prose live on disk — never expect them inside your spawn prompt
- machine gates decide: run stage submits via the CLI; use `precheck` only for diagnostics
- reply with a concise status card containing paths and exit codes only; never echo prose or pack content back
- batch CLI calls: chain file-write → stage-submit in ONE shell command (`&&`);
  stage workers NEVER call `chapter next`; only the dispatcher advances stage boundaries
- submits are gates, self-checks are evidence: worker-side self-check text (paths/hashes/锚点核对)
  is advisory only — the machine-checked submit command is the sole authority. For the assemble
  stage the worker writes the submit JSON and the HOST runs `chapter submit`（worker 沙箱读抖动
  与自检失实两课的定型协议：确定性闸门永远在宿主侧执行）
- pass `--terse` on every CLI call, placed before the subcommand
  (`novel_ledger.py --terse chapter next`); trailing placement is also accepted
  (strips orchestration envelopes from stdout)
- thinking discipline: these rounds are mechanical execution backed by machine gates —
  think briefly per round; do not re-derive the chapter plan or re-analyze the manual
  you already read
- semantics live in cards: if an envelope/pack signal's disposition is unclear after
  checking the role card and the pack's own guidance, stop that step and say so on
  your status card — NEVER read the skill's python source or tests to
  reverse-engineer semantics; that burns your round budget on a guess the machine
  gates may reject anyway
- task-related reading: use source paths and `context read --source intent|outline|canon`
  (optional exact `--section` and relative canon `--name`), `plan volume-outline --volume N`,
  or `memory recall` by people/topics/assets/events. Read selected material completely;
  additional reading stays within the current role and never authorizes a new direction.
- usage telemetry: 每个模型请求结束立即用 chapter usage-record 记录四分量 delta，
  request-id 使用 job-id:request序号；不同请求不得复用 job-id。session-id 使用宿主真实会话ID。
  stage-action 信封自带 `usage_request.request_id` 与 `record_hint`，宿主回声该句柄即天然幂等。
  宿主只见整单总量时（stage-agent 子代理形态）改报 `--total-tokens`（total-only 同为合法
  形状，计入章节总量并驱动 stop_total_per_chapter 熔断）；分量与总量不得混报。
  无 telemetry 保持 unknown，禁止补零。结果文件只可带 usage_records 请求明细；已经入账的
  请求重发相同 ID 和元数据以幂等去重，禁止再把会话总量记一遍。

## 二、隔离阶段协议（简报 `protocol=stage`，默认）

宿主必须以空上下文创建本 job 的新模型会话，禁止 fork、resume 或传入前阶段的聊天摘要。
开工只读 action_path、当前 role_card 与本协议，再按 action 中的路径读取本阶段输入。
不要运行 chapter next：它属于确定性调度器，且阶段 fence 会拒绝该命令。
只完成 draft、polish、assemble 或 ack 中的一个阶段；本阶段原位字段修正允许继续，
但提交通过、phase 改变（包括正文返工）、blocked 或 usage_guard 后立即退出。
返工的失败依据来自 review_findings_path 或机检报告；不能继承上一角色的推理。
同书只运行一个 worker；worker 不派子 agent。

命令驱动结果须匹配当前 job_id/action，并带宿主提供的真实 session_id、context_origin=empty。
控制面拒绝跨 job 复用同一个 session_id。缺少会话证明则暂停，不将职责切换冒充隔离。
声明是宿主的接线承诺，不是对模型完整阅读的数学证明，也不是文件系统沙箱。
只有宿主允许的文件能力/进程沙箱才能阻止其它文件读取；共享工作目录中的提示词禁令仅是纪律。

完成回复只给状态、路径和退出码，逐条自检与引用写在阶段产物中。
终审用引文运行 ack-read，成功后退出；它不读取其它角色的视图或汇报。

## 三、扩纲执行循环（简报 `protocol=plan`）

single-shot job in a DEDICATED session separate from any chapter worker — if you are
a chapter worker that received this action, do NOT inline it: exit and report back
to your host so it dispatches a dedicated plan worker.
FIRST STEP: read `plan_worker_brief.path` — its task-related `planning_context` carries the
nearby plan, phase, adjacent spine events, scale, memory, relevant assets, omitted
counts, and the `batch_design` brainstorm base (recent chapter-type patterns, current
tension stage, design directive). Do not load full `chapters.json` or `snapshot.json`
without a task-related reason. Read inputs ONCE, end-to-end: consume the brief file in
a single pass; do NOT pretty-print it to staging and re-read it in pages (paginated
re-reads push the same bytes into context twice), and whole-book volume outlines already
live in `planning_context.volume_outlines` — supplement per volume only via
`plan volume-outline --volume N`, never by reading outline source files wholesale; the
role card and this card are likewise read exactly once. Follow source paths, event ids,
people and topics
to read additional complete material when needed; never trim selected evidence to a
fixed character budget. Duties come from THAT file plus this card (both on disk, always
current) — never from your dispatch prompt's memory.
BATCH BRAINSTORM (mandatory before writing beats, all inside THIS session): draft at
least 2 structurally different batch-direction candidates — per candidate: a one-line
theme, a per-chapter "goal→conflict→outcome" skeleton, chapter-type tags, a hook
settlement plan, a declared differentiator axis, and its worst failure mode (required).
Write them to book/editorial/plan-batch-candidates-<from>-<to>.json (schema
novel-ledger.plan-candidates.v1), then run `plan select-batch --file <path>`: it
machine-checks the file (too few candidates / same-skin candidates / missing failure
modes / missing loser reasons are rejected), writes fact checks (hook coverage vs
due≤batch-end, chapter-type overlap vs the recent 20 chapters) and seals your
selection as a ledger governance event. You pick the winner yourself: cite the
machine facts in verdict.rationale and give every loser an explicit why; on a tie,
settle overdue hooks first, then prefer monotonic tension, then lower repetition.
Each candidate must cover the exact continuous batch range (at most 20 chapters).
The brief's `extend_through_ch` is the COVERAGE CONTRACT of this job: your extension
must carry chapter beats from `suggest_from` THROUGH `extend_through_ch` (span comes
from config `plan_extend_span`: a chapter count, or "volume" = the full current volume
per its chapters_budget). Do not top up to just above the waterline — one extension
round is one worker spawn, and a half-hearted top-up burns a whole round every few
chapters. When `extend_through_ch - suggest_from + 1` exceeds 20, split the coverage
into consecutive candidate batches of ≤20 chapters and run the full
`select-batch → plan extend` cycle per batch, all inside THIS session before exiting
(spawn once, amortize the reading; the per-batch machine checks stay intact).
Expand ONLY the winning candidate into per-scene beats; copy its per-chapter
goal/conflict/outcome/tags unchanged and place its settles hook ids as paid in
that same chapter's effects.hooks. The CLI binds this skeleton and the expanded
batch; missing or different selections are rejected. If the signed total chapter
capacity is exhausted while actual words are short, first run `plan rebudget
--actor planning-editor --reason "actual word deficit"` as instructed by the brief,
then select the exact candidate batch and extend once. Rebudget retains book words,
pace and event spine; it never authorizes a new direction. The hatch seed batch
(chapters 1–3) is exempt — the hatch wizard already ran author-facing choices.
Within the managing editor's delegated, signed-spine/volume/intent boundary, draft and sign a structured phase brief aligned to the four-layer event spine (the
creative/planning role cards carry the phase-brief contract), write it to
book/staging/ as JSON, run `plan extend` once; on ok:true exit IMMEDIATELY —
do NOT run `chapter next`, do NOT start or verify any chapter.
If the extension batch crosses into a volume whose spine is not yet in
plan.volumes (the envelope's volume_watermark.warn or creative_advisory says so),
the SAME `plan extend` call must carry the volumes payload (title/spine/goal/
word_budget/chapters_budget, chapters_budget == ceil(word_budget/chapter_words_target),
using the value signed in the brief's `planning_context.book_scale`).
If an important new character enters or an existing voice has changed through
on-page events, the same payload may include `character_profiles` for those
canonical names; keep each short card grounded in the current story.
overdue_hooks in the envelope are hooks past their due chapter: schedule their
payoff into the new chapter beats' effects.hooks so the submit gates enforce them.
Run this job at a chapter boundary, after the previous chapter is committed and
acknowledged. Unattended batch checkpoints verify the selection trail: a written-
forward batch without a plan.batch_select event pauses the run for review.

## 四、显式兼容整章协议（简报 `protocol=chapter`）

仅 execution_mode=worker-agent 时使用；没有真正阶段上下文隔离，必须报告降级。
run chapter next in YOUR OWN session，按当前 action 角色卡串行执行到 ack-read 后退出。
ROUND BUDGET: aim for <=12 model rounds per chapter；同书串行、不要再创建子 agent。
extend_plan 仍是独立 plan job；章节 worker 遇到它即退出回报，do NOT run `chapter next` 开下一章。
恢复只依据 HEAD；未 blocked 不用 retry-authorize。
hooks_due_unplanned 是 advisory：能自然回收则演出并在 assemble 入账，不能则留给 planner，不自行改计划。

## 五、停止条件（stop_on）

- 章节任务（`stop_on: blocked, usage_guard`）：立即停止并如实报告，不得自行越权解锁。
- 扩纲任务（`stop_on: plan extend failure`）：停止并如实报告失败原因。

## 六、跨卷与终局剧情复核（action=story_review）

宿主创建新的空上下文，即使章节使用兼容执行模式也不得继承它的聊天。先读 action.review_pack_path、story-reviewer.md 及选定完整正文；按问题从来源路径与 review story-evidence 补证据，每次可按章读取。合同、摘要、正文和引文不设固定字数上限。逐项提交有正文依据的 checks 到 action.submit_output_path，执行 review story-submit 后立即退出。不得改正文、账本、合同、运行 next 或创建子 agent。BLOCKER/UNVERIFIABLE 保存为 fix，停线交总编辑；修订后新会话重审。
