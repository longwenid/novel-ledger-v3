# 无人值守运行器

## 边界

无人值守＝确定性 supervisor 持续运行，模型 worker 默认每次只完成一个 draft/polish/assemble/ack 阶段，扩纲为独立 plan job。supervisor 不写正文、
不做艺术判断、不保存聊天历史；它只读 HEAD、派发新会话、核验进度、记账和熔断。同一部书始终串行。
卷界与完本另派独立 `story_review` job，所有执行模式均要求 fresh 会话和真实 session 回执。
**形态定位（作者定）：本形态是备选**——无人值守连写的默认形态是[会话子 agent
循环连写](unattended.md)（宿主主线程一直执行、循环唤起子 agent，无定时器）。
本形态用在**没有会话宿主、或需要跨断电独立存活**的场景：`run start` 起一个确定性
supervisor 常驻进程，循环派发子进程（每个工作单元一个 fresh 新会话跑一个阶段）。
定时器接力是最后的兜底（见文末）。

## CLI

```bash
python3 "$SKILL_ROOT/scripts/novel_ledger.py" run validate-config --driver-config "$CONFIG"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" run once --project "$PROJECT" --driver-config "$CONFIG"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" run start --project "$PROJECT" --driver-config "$CONFIG"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" run start --project "$PROJECT" --driver-config "$CONFIG" --max-chapters 5
python3 "$SKILL_ROOT/scripts/novel_ledger.py" run status --project "$PROJECT"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" run handoff --project "$PROJECT"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" run handoff --project "$PROJECT" --host-session-start
python3 "$SKILL_ROOT/scripts/novel_ledger.py" run checkpoint --project "$PROJECT"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" run pause --project "$PROJECT" --reason "人工检查"
python3 "$SKILL_ROOT/scripts/novel_ledger.py" run resume --project "$PROJECT"
```

`run handoff` 只读故事状态，把 HEAD/下一动作/逾期与临期钩子/开放 findings/进度日志尾部
收进 `book/run/handoff.md`——任何形态（supervisor 或会话宿主）的中止点都可以先跑它，
恢复会话读交接卡续跑，不做聊天考古。`--host-session-start` 标记这是**新宿主会话**的
第一个动作并重置宿主批界基线；批中收口用不带旗标的普通 handoff（不重置、不解除
boundary 闩锁）。

运行状态写 `book/run/autopilot.json`，追加审计写 `book/run/autopilot-events.jsonl`。独立
`AUTOPILOT_LOCK.d` lease 每 10 秒心跳；90 秒无心跳且持有进程已死时才自动回收。`run pause` 使用独立请求文件，
不与章节写锁争抢。派发期间另写 `AUTOPILOT_ACTIVE_JOB.json`：所有会改故事状态的 CLI 都必须携带匹配的
`NOVEL_LEDGER_JOB_ID`，旧 Sidecar 会话即使延迟返回，也不能越过新 job 的写入隔离。
**只读诊断不受 fence 限**（不取写锁也就不进 fence）：`status` / `run status` / `ledger verify` /
`book audit` / `plan validate` / `chapter precheck` 在长跑期间照常可用，供现场核查扩纲与账本。

### 批级与卷界机器检查点

每完成 10 章、或下一章进入新卷时，supervisor 在派发下一章前写
`book/run/checkpoints/ch-NNNN-*.json`。会话子 agent 宿主在章界直接调用
`run checkpoint`：与环内同一条确定性检查路和 last_checkpoint_ch 游标（幂等，非到期章
返回 skipped），机器部分零模型调用——CLI 回执只带结论与路径（报告全文留盘上，不回显
进宿主上下文），仅 review_required 才派 triage
子代理。批级检查点另核对**批次候选留痕**：待写批次
（首章>3）没有 `plan.batch_select` 治理事件即以 `plan_batch_selection_missing` 暂停
（该批扩纲没做候选择优，报告 `batch_plan_review` 节汇总本窗口择优）。检查按当前批窗口读取
正文、meta、摘要与 ack，定位该批完整质量记录与提交事件并核对哈希链；
卷界报告另外引用当前卷的完整滚层摘要、卷纲和下卷合同。
读取不设单文件字节上限，也不按日志尾部字节数漏掉窗口内事件。诊断报告可按章或记录分页，
选中的摘要、资产与证据保留全文和来源路径。`run status`
的 `last_checkpoint` 只保存报告路径与结论；`autopilot-events.jsonl` 记录
`quality_checkpoint`，卷界额外记录 `volume_review`。

缺少已确认章节的文件或质量回执、哈希/字数/引文不一致、快照章号不符、伏笔已逾期，
以及已签卷体系中下卷仍未签脊柱，会使 supervisor 以 `review_required` 暂停。
处理后执行 `run resume`，同一检查点会重新检查；有问题的报告不会推进检查游标。
返工率、人物选择与代价、主支线兑现和世界规则属于卷界复核议题：机器报告提供相关证据与
复核信号。独立剧情审稿会话必须按 [剧情复核](story-review.md) 提交与当前正文及合同绑定的证据回执；
通过才跨卷，fix 自动停线。原有项目首次接入时机器检查只覆盖最近 10 章，并明确标明历史范围；
历史卷的剧情复核仍须补齐，完本须再通过全书终局复核与机器审计。

### plan job 限制（validate-config 会亮告警）

plan job 要在**一个新会话**里产出完整 phase brief + 最多 20 章章拍并跑通四层
脊柱与规模合同校验；落拍前先完成批次候选头脑风暴并 `plan select-batch` 留痕
（步骤固定在派发 prompt 与协议卡 §三里）。两组下限：

- `limits.plan_timeout_seconds` ≥ 3600；`limits.no_progress_seconds` ≥ 1800——no_progress 只按
  「可观察工件变化」（HEAD/plan/quality/staging）重置：plan worker 落盘前 HEAD 不变，
  静默思考超窗会被先杀；章节 worker 的草稿在写作末尾才落盘，单章耗时随书变厚可涨到
  ~15 分钟。缺省值即上述下限；
  显式配得更紧时 `run validate-config` 返回
  `plan_job_timeout_tight` / `plan_job_no_progress_tight`（不阻断）。
  **校准原则：单章/单 job 实测耗时涨了，就同步放宽对应窗口**——staging 工件已计入
  进度，剩下的窗口只兜真挂死。

### 低水位提示的生命周期

`chapter next` 在剩余章拍 ≤ `plan_low_water` 时返回一次 `action=extend_plan`（一次性标记
`plan_low_water_nudged_at` 记录当时的 max_planned）。**扩纲跨度由 `config plan_extend_span`
统一下达**（默认 20；`"volume"`=按卷合同 chapters_budget 一次签满当前卷，当前卷已签满则
整签下一卷，无卷合同回退 20）：回执与 plan 简报以 `extend_through_ch` 给出覆盖契约，
worker 必须从 suggest_from 一路编拍到该章，**不贴水位线补章**——实测贴线补章 63 章烧 13 轮
扩纲 ≈109 万 token（每轮的钱≈写一章）。跨度 >20 章时协议卡规定同一 plan worker 会话内
拆 ≤20 章候选批连续 `select-batch → plan extend`（spawn 一次，候选批机检不变）。
plan job 失败 → pause → `run start`
续跑时，supervisor 会在写锁内**重置未兑现的标记**（标记仍等于当前 max_planned 才清，
事件 `plan_nudge_marker_reset`），让下一次 `chapter next` 重新提示扩纲——「扩纲优先」
不会被一次性标记静默吞掉；计划已增长过的旧标记保持原语义。

## Driver v1

通用阻塞命令：

```json
{
  "schema": "novel-ledger.driver.v1",
  "driver": {
    "kind": "command",
    "session_policy": "fresh",
    "argv": ["model-cli", "{prompt_file}"],
    "cwd": "{project}"
  },
  "limits": {
    "chapter_timeout_seconds": 2700,
    "plan_timeout_seconds": 3600,
    "no_progress_seconds": 1800,
    "max_infra_retries": 3,
    "retry_backoff_seconds": [30, 120, 600]
  }
}
```

`argv` 是参数数组，绝不经 shell。支持 `{prompt_file}`、`{prompt}`、`{project}`、`{job_id}`、
`{result_file}`；同时向进程注入同名 `NOVEL_LEDGER_*` 环境变量。command worker 超时或无进度时先终止，
10 秒仍未退出才强杀；章节达标后另留最多 10 秒供宿主落结果文件，确认结束后才允许新会话重试。

### 接线前先确认 argv 在本机起得来（Windows）

`run validate-config` 会顺带报告 `executable_warnings`：`argv[0]` 在本机解析不到、或只解析到
`.cmd`/`.bat` 垫片时点名给出原因（不阻断——校验器不替宿主决定装什么）。看到这两条就先别接线：

- **裸命令名只匹配 `.exe`**。`subprocess` 不走 shell，Windows 上的 `dsh` 是 `.CMD` 垫片，
  写成 `["dsh", ...]` 会在派发瞬间 `FileNotFoundError`，被记成 `dispatch_failed`，排查方向被
  带到 PATH 上。**改用解释器直连**（如 `["node", "<...>/bin.js", ...]`）或给出 `.exe` 完整路径；
  运行期真出这种错时，`dispatch_failed` 里也会带 `argv_head` 与 `hint`。
- **多行 / 中文 prompt 不要经 `cmd.exe`**：会被 shell 重新分词并搅碎。这正是 `{prompt_file}`
  存在的理由——给文件路径，让 worker 自己去读。
- **外部 CLI 的模型由其自身账户默认决定**：宿主无头入口通常没有模型参数位，skill 也不再投喂模型参数——评估成本时以外部平台账单为准。

### 阶段派发与恢复

默认 stage-agent。supervisor 写 autopilot-current.action.json，worker 只读当前 action 与角色卡，
不运行 chapter next。active-job fence 绑定 job_id、chapter、initial_action：错阶段、越章或
提交后继续修改都被 CLI 拒绝。stage result 必须带 job_id/action/session_id/context_origin=empty。
Driver session_policy=fresh 表示每次调用都创建不继承历史的新模型 conversation，不能复用 CLI 会话。
stage-sessions.json 拒绝跨 job 复用 session_id。阶段已迁移但缺少回执时保留 current_job 和 fence，
恢复先补验回执，不跳到下一阶段。worker-agent 是显式整章兼容模式，不提供阶段上下文隔离。

### 模型与用量（外部 CLI 的模型投喂已移除）

**skill 不再向外部 CLI 投喂任何模型参数**（`tiers` / `{model}` 占位符 /
`NOVEL_LEDGER_MODEL` 均已移除；旧配置里的 `{model}` 会被 `run validate-config` 按
未知占位符拒收）。外部无头 CLI 的模型与计费由**它自己的账户默认**决定，
宿主无模型参数位时无从控制，残留配置会静默烧钱。因此：

- 连写默认形态是[会话子 agent 循环连写](unattended.md)（用量进宿主会话计量）；
  本形态（command driver）用在无会话宿主/需跨断电存活的场景。
- 无头子进程的模型与计费由**它自己的账户默认**决定，skill 不投喂任何模型参数；校验期恒亮
  `external_driver_model_unmetered` 告警提醒这层风险，接线前由作者确认通道与额度。
- [会话子 agent 形态](unattended.md)是替代而非默认：每阶段从空历史创建新会话，
  用量进宿主会话计量；适合已有会话宿主在跑、不愿再起常驻进程的场景。

Antigravity Sidecar：

```json
{
  "schema": "novel-ledger.driver.v1",
  "driver": {
    "kind": "antigravity-agentapi",
    "executable": "agentapi",
    "session_policy": "fresh"
  },
  "limits": {
    "chapter_timeout_seconds": 2700,
    "plan_timeout_seconds": 3600,
    "no_progress_seconds": 1800,
    "max_infra_retries": 3,
    "retry_backoff_seconds": [30, 120, 600]
  }
}
```

此模式必须从已配置 `projectId` 的 Sidecar 中运行。`agentapi new-conversation` 成功即视为已派发；接口没有可靠
取消/轮询能力，派发后的超时或无进度会暂停 supervisor，不会自动派第二个会话。只有“尚未成功派发”的
基础设施失败才允许重试。

Worker 结果契约、协议版本、恢复与停止、分批接力见 [unattended.md](unattended.md) 第二部分。

## 常驻形态接线示例（无头 CLI 直连）

以本机已装的无头编码 CLI 为例（`pi -p` 非交互模式，每次调用即一个 fresh 新会话）。
Windows 上 npm 装的 CLI 是 `.cmd` 垫片，subprocess 不经 shell 起不来——**用 node 直连
垫片里的 cli.js**（打开 `pi.cmd`，`%dp0%
ode_modules\...` 那行就是入口；下例把全局
目录记作 `<npm-global>`，接线前换成 `npm root -g` 的实际值）：

```json
{
  "schema": "novel-ledger.driver.v1",
  "driver": {
    "kind": "command",
    "session_policy": "fresh",
    "argv": [
      "node",
      "<npm-global>/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
      "-p",
      "这是一次无历史的新会话。逐字执行任务文件 {prompt_file} 里的 novel-ledger 指令；"
      "结束时把结果 JSON 写到 {result_file}：session_id 用你 bash 环境里 PI_SESSION_ID 的原值"
      "（pi 每次调用都生成新会话 id），context_origin 填 empty。禁止 resume/continue 旧会话。"
    ],
    "cwd": "{project}"
  },
  "limits": {
    "chapter_timeout_seconds": 2700,
    "plan_timeout_seconds": 3600,
    "no_progress_seconds": 1800,
    "max_infra_retries": 3,
    "retry_backoff_seconds": [30, 120, 600]
  }
}
```

接线顺序：`run validate-config`（本机起得来才会放行，executable_warnings 必须清零）→
手工跑一次 `run once` 看单 job 闭环 → `run start` 常驻。

**通道核对是接线前置**：无头 CLI 的默认通道可能指向外部提供商（实测 pi 默认即走了
非受控的 deepseek 通道）——接线前先用 pi 自己的 auth/check 命令核对本机默认通道，
必要时在 pi 侧改默认通道配置，别让连写整夜烧外部账单。

## 生命周期（无定时器）

- **`run start` 即主循环**：主线程一直执行——派 job → 拉子进程 → 等回执 → 记账 →
  下一 job；整书写完、blocked、契约违规或 `--max-chapters` 到量才退出。
- **`run pause`**：写暂停请求，主循环在当前 job 收口后退出；**`run resume`**：清暂停并续跑。
- **崩溃恢复**：重新 `run start`，supervisor 先补验上一个未闭环 job 的回执再继续
  （不重写已完成阶段）；`run status` / `run handoff` 随时只读查看台面。
- 批界 checkpoint 到点会停线出报告（零模型调用），处置后 `run resume` 续跑——
  这些都是**进程内循环的常规节奏**，与定时器无关。
