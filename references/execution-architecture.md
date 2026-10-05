# 执行与上下文架构 v6

## 会话边界

默认 stage-agent：确定性调度器持有 HEAD、闸门、账本和恢复；每个模型 job 只完成一个阶段。

```text
调度器 next → draft 新会话 → draft-submit → 退出
调度器 next → polish 新会话 → polish-submit → 退出
调度器 next → assemble 新会话 → submit → 退出
调度器 next / 确定性 commit → ack 新会话 → ack-read → 退出
跨卷/终局 next → story_review 新会话 → review story-submit → 退出
```

同书串行。每个会话从空 messages/history 启动；禁止 fork、resume、复制前阶段聊天或注入摘要。
文件是唯一交接物。阶段视图保留必要事实与任务材料，不能携带前一角色的推理或自评。
剧情复核契约见 [story-review.md](story-review.md)，其当前版本 pass 回执是跨卷和正常完本的必经条件。

`execution.context_isolation=fresh_session` 是宿主必须实现的契约，不是脚本能自行建立的模型边界。
调度会话只看路径、状态与错误码。宿主不支持新空会话时应暂停报告能力缺口；用户明确选择
worker-agent（整章 worker）或 inline（角色切换）时可降级，但它们的 context_isolation=stage_pack
只描述职责视图，不是上下文隔离。人工按阶段创建新任务也可实现隔离；人工按章创建新任务仅是兼容降级。

## 受控交接

- next 附单阶段 worker 简报：action_path、唯一角色卡、协议卡、输出路径、停止条件。
- worker 只提交当前阶段，不能运行 chapter next、进入下一阶段或创建子 agent。
- 返工使用新的空会话，读取机检清单或 review_findings_path，并按问题补读相关完整证据。
- 命令 driver 必须声明 driver.session_policy=fresh，并实际创建新的模型 conversation。
- 每次成功 result 含 job_id、action、宿主真实 session_id、context_origin=empty。
  控制面保存 stage-sessions.json 并拒绝跨 job 复用 session_id；恢复不能跳过该证明。
  这是宿主对接线的审计承诺，不能阻止恶意宿主伪造 ID，也不能证明模型逐字读完。
- stage fence 绑定 job_id、目标章及 action，只允许对应阶段提交和 telemetry；阶段移交后不再允许修改。
  凭据匹配时 execution 返回 spawn_allowed=false，表示该会话就是被派发的 worker。

## 数据与权限

正文、提交 JSON、引文和自检证据落盘；状态卡只传路径、计数和退出码。
文件视图按当前角色组织任务相关材料，并保留来源路径供定向补读。上下文材料不设固定字数或字节上限，
选中规则、摘要、合同和原文证据完整保留。真正的文件访问隔离需要宿主进程沙箱或能力白名单。
本 CLI fence 只约束经 CLI 的写操作，无法拦截共享目录中的任意 Python/ shell 读写。
因此不宣称 skill 自身提供 OS 安全隔离。需要该等级时宿主只挂载本阶段输入与 staging 输出。

事实编辑 v2 必填 plot_findings（无问题 []）；除 UNVERIFIABLE 外发现必须有逐字 quote。
所有合法 findings 在变更 phase 前写入 editorial/findings.jsonl。review list 可按窗口分页；
review resolve 显式记录 accepted/deferred/closed、actor 和理由。检查点读未决项，书级审计不得漏掉它们。
已入账 patch 复验 word_band、内容锚点、账本引用和文风；哈希同步后标记 needs_review，
独立终审通读再 review ack-patch。同步哈希不等于审核新稿。

## 用量与档位

每请求结束由宿主逐请求回报四分量 telemetry，用 chapter usage-record 记录 delta；
request-id=job-id:request序号，uncached 必须明确给出，缺 telemetry 保持 unknown。
stage-agent 宿主只见整单总量时改记 `--total-tokens`（total-only 合法形状，计入章节总量并按
`stop_total_per_chapter` 熔断；stage-action 信封自带 `usage_request.request_id` 供回声）。
结果若回传 usage_records，只重放相同请求 ID/元数据，禁止再次计入会话总量。
usage_guard 在下一模型 action 前拦截，确定性 commit 可以完成。不能靠最后汇总实现实时预算保护。

draft/polish/assemble 标准档；ack、plan、疑难卷级/全书复核最强档；机械补丁经济档。
不支持模型覆盖时如实记录继承，不破坏会话隔离以凑档位。阶段间独立调用允许按 job 选择模型。

## 部署验收

Skill 指令本身不是运行器。run start 按 Driver 协议创建新会话，不能将长对话包装为新 session。
上线核对 --version、status.runtime.control_fingerprint，并用一章闭环验证：
四个阶段不同 session_id、上下文起点为空、错阶段写命令被拒、返工与中断恢复仍由新会话继续。
按阶段读取角色卡、当前视图和任务所需来源；材料不足时定向补读。成本靠相关性筛选、文件交接及减少重复请求控制；
不能为降低调用数撤销独立终审。总编辑只派发、收状态卡；不转运正文。
