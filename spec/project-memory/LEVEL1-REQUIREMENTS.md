# Pillow Assistant 第一层项目记忆需求规格

- 文档版本：1.0
- 状态：Reviewed
- 适用范围：基础项目记忆（SQLite + JSONL）
- 后续能力：第二层 RAG、第三层分类 GraphRAG/多模态 RAG 不在本规格实现范围内

## 1. 目标

第一层项目记忆必须作为 Pillow Assistant 随产品交付、无需外部服务即可工作的基础实现。它需要在长任务、多会话和应用重启后可靠恢复项目状态，并通过确定性校验门防止模型未经验证地宣称任务完成。

本层必须提供：

1. 项目、任务、子任务、步骤和校验的结构化状态。
2. 每轮对话后的项目检查点。
3. 当前请求相关的历史记忆召回。
4. 模型信息不足时的结构化记忆请求。
5. 外部文件的路径引用、指纹和可用性状态；不复制原始文件。
6. SQLite 权威状态、原始会话 JSONL 和不可变事件 JSONL。
7. 可审计、可恢复、幂等且支持 revision 冲突检测的写入流程。

## 2. 非目标

第一层不负责：

- 向量 Embedding、向量数据库或语义向量召回。
- GraphRAG、跨分类图谱、多模态向量检索。
- 具体 SaaS RAG Provider 接入。
- 自动复制、托管或上传外部原始文件。
- 未经工具证据或人工确认的客观完成判定。

## 3. 术语

- **项目状态快照**：某个 revision 下项目目标、当前任务、当前步骤、阻塞项、下一步动作和状态摘要的结构化视图。
- **任务 revision**：任务内容、步骤或验收条件发生有效变化时递增的版本号。
- **校验计划**：任务创建时必须存在的一组必需或可选校验项。
- **校验证据**：工具结果、产物指纹、状态查询或用户确认等可追溯结果。
- **检查点**：一轮项目对话结束后保存的状态摘要和状态增量。
- **记忆请求**：模型因信息不足而发出的结构化历史查询请求。
- **权威状态**：可决定任务状态的唯一数据来源；第一层中为 SQLite。
- **事件镜像**：由 SQLite outbox 导出的 append-only JSONL 审计记录，不独立决定当前状态。

## 4. 存储原则

### PM1-STO-001 权威来源

SQLite 必须是结构化项目状态、任务、校验和记忆请求的唯一权威来源。

### PM1-STO-002 原始会话

现有 `sessions/<session_id>.jsonl` 必须继续保存原始用户与助手消息。结构化摘要不得覆盖原始会话。

### PM1-STO-003 事件镜像

每个项目必须拥有 `memory/events.jsonl`，用于保存带 `event_id` 的不可变审计事件。事件必须由 SQLite outbox 写出；崩溃后允许重复物理行，但消费者必须按 `event_id` 去重。

### PM1-STO-004 Schema 版本

SQLite 表、事件和模型结构化输出必须包含或隐含可查询的 schema 版本。未知主版本必须拒绝写入。

### PM1-STO-005 项目隔离

所有结构化记录必须包含 `project_id`。任何查询、更新和删除必须限定项目范围。

## 5. 项目状态需求

### PM1-STATE-001 初始化

项目第一次进入 Agent 前必须幂等创建项目记忆状态，初始 revision 为 1。

### PM1-STATE-002 快照字段

项目状态至少包含：

- `project_id`
- `revision`
- `project_goal`
- `project_status`
- `current_task_id`
- `current_step_id`
- `state_summary`
- `blockers`
- `open_questions`
- `next_actions`
- `last_turn_id`
- `updated_at`

### PM1-STATE-003 Revision 更新

状态更新必须使用 compare-and-set。提交的 `base_revision` 与当前 revision 不一致时必须拒绝更新并返回冲突，不得静默覆盖。

### PM1-STATE-004 当前任务唯一性

一个项目可以有多个 `in_progress` 任务，但只能有一个 primary current task。并行任务必须显式记录，不得通过多个 current task 隐式表达。

### PM1-STATE-005 最新请求优先

项目状态和历史记忆只能作为资料。用户当前请求与系统规则的优先级始终更高。

## 6. 任务和步骤需求

### PM1-TASK-001 任务字段

任务至少包含：

- `task_id`
- `project_id`
- `parent_task_id`
- `title`
- `description`
- `status`
- `priority`
- `revision`
- `current_step_id`
- `blockers`
- `created_from_turn_id`
- `created_at`
- `updated_at`
- `completed_at`

### PM1-TASK-002 状态集合

任务状态必须限制为：

```text
planned
in_progress
blocked
implementation_complete
validating
validation_failed
awaiting_user_validation
done
needs_review
cancelled
superseded
```

### PM1-TASK-003 状态转换

状态转换必须由确定性状态机校验。模型不得绕过状态机直接更新数据库。

### PM1-TASK-004 禁止直接完成

任何公共任务更新 API 均不得直接把任务写为 `done`。只有完成门函数可以产生 `done`。

### PM1-TASK-005 子任务约束

父任务进入 `done` 前，所有未取消、未废弃的子任务必须为 `done`。

### PM1-TASK-006 进度表达

系统不得把模型任意给出的百分比作为权威进度。进度优先由完成步骤数、校验数和任务状态派生。

### PM1-TASK-007 任务变更失效

任务描述、步骤、验收条件或相关产物发生变化时，任务 revision 必须递增，旧 revision 的通过证据必须变为 `stale`。已完成任务必须转为 `needs_review`。

## 7. 强制校验需求

### PM1-VAL-001 非空校验计划

每个任务创建时必须至少包含一个 `required=true` 的校验项。没有校验计划的任务创建必须失败。

### PM1-VAL-002 校验类型

支持以下校验类型：

```text
command
artifact_exists
artifact_content
requirement_match
state_check
integration
regression
manual
model_review
```

### PM1-VAL-003 校验状态

校验项状态限制为：

```text
pending
running
passed
failed
blocked
awaiting_user
stale
```

### PM1-VAL-004 证据要求

必需校验只有在存在有效证据、证据属于当前任务 revision、证据来源可追踪时才能为 `passed`。

### PM1-VAL-005 客观校验

`model_review` 不得单独证明文件生成、命令成功、外部状态变化等客观事实。客观校验必须关联工具结果、产物指纹、状态查询或用户确认。

### PM1-VAL-006 完成门

任务只有同时满足以下条件才能进入 `done`：

1. 至少一个必需校验项存在。
2. 所有必需校验项为 `passed`。
3. 每个必需校验都有当前 revision 的有效证据。
4. 所有有效子任务为 `done`。
5. 没有活动阻塞项。
6. 没有未解决冲突。

### PM1-VAL-007 校验失败

任一必需校验失败时，任务必须为 `validation_failed`；等待用户确认时必须为 `awaiting_user_validation`；其余未完成校验期间为 `validating`。

### PM1-VAL-008 校验条件保护

Agent 不得为了通过任务而静默删除或弱化失败的必需校验。校验计划变更必须形成事件并增加任务 revision。

## 8. 每轮对话与检查点

### PM1-TURN-001 每轮记录

每轮项目对话必须记录：

- 原始 user/assistant 消息位置。
- 用户请求摘要。
- 助手结果摘要。
- 本轮前后项目 revision。
- 状态增量。
- 当前任务和步骤。
- 工具证据引用。
- 新增记忆项。
- 新增或未解决的记忆请求。
- 检查点摘要。

### PM1-TURN-002 上一轮关联

上一轮检查点必须作为下一轮项目上下文的高优先级资料；不得只依赖最近原始问答。

### PM1-TURN-003 模型输出权限

写回模型只能提出 `ProjectTurnDelta`。确定性 reducer 必须验证 revision、任务 ID、状态转换、证据 ID 和校验条件后才能应用。

### PM1-TURN-004 写回失败

模型摘要或结构化提取失败时，必须保存原始会话和最小确定性检查点，并将该轮标为 `needs_reconcile`。写回失败不得丢失用户回答。

### PM1-TURN-005 重建

系统必须能够根据结构化事件、任务和原始对话重新生成项目状态摘要，避免无限摘要旧摘要。

## 9. 记忆召回与信息补取

### PM1-RET-001 基础召回

第一层必须支持项目内的精确查询、关键词匹配、时间和任务过滤，不要求向量检索。

### PM1-RET-002 MemoryPack

进入模型的项目记忆必须形成有界 `MemoryPack`，至少包含：

- 当前项目状态。
- 当前任务、步骤和校验状态。
- 上一轮检查点。
- 未解决记忆请求。
- 与当前请求相关的历史记忆。
- 来源 ID 和时间。

### PM1-RET-003 上下文优先级

优先级从高到低为：当前请求和系统规则、当前任务状态、上一轮检查点、相关历史、旧证据。旧历史可压缩，当前项目状态不得被普通语义压缩覆盖。

### PM1-RET-004 记忆请求工具

Agent 必须能够调用只读 `request_project_memory` 工具，参数至少包括 query、kinds、task_id、required、top_k 和 reason。

### PM1-RET-005 补取上限

单个用户请求最多允许两次项目记忆补取。达到上限仍缺少必需信息时必须询问用户或明确报告阻塞，禁止无限循环。

### PM1-RET-006 未解决请求

无法满足的必需记忆请求必须持久化，后续轮次继续显示，直到 resolved、cancelled 或 failed。

## 10. 外部文件引用

### PM1-SRC-001 仅路径引用

外部文件不得复制到项目目录。第一层只保存原始路径、规范化路径、类型、大小、修改时间、内容指纹、可用性和来源 turn。

### PM1-SRC-002 路径状态

路径状态至少支持：`available`、`missing`、`changed`、`unreadable`。

### PM1-SRC-003 变化检测

优先使用 size + mtime 快速检测，变化时按需计算内容哈希。内容变化必须使依赖该文件旧 revision 的校验证据失效。

### PM1-SRC-004 路径安全

规范化路径时必须处理 Windows 大小写、符号链接、junction、OneDrive 占位文件和可移动盘不可用场景。不可访问不得等同于删除。

## 11. 恢复、删除与并发

### PM1-REC-001 Resume 持久化

达到 Agent 步数上限时，活动工具调用 transcript 必须按项目和 session 持久化。应用重启后“继续”必须能够恢复。

### PM1-REC-002 Resume 生命周期

恢复状态成功使用后必须清除；新的不兼容用户请求不得静默复用旧工具链。

### PM1-REC-003 删除

删除项目必须删除 SQLite 中该项目的结构化记忆、待处理 outbox 和恢复状态。项目目录删除继续由 ProjectStore 负责。

### PM1-REC-004 并发冲突

跨会话并发更新必须通过 revision 检测冲突。失败方必须重新读取和合并，不得最后写入者静默获胜。

## 12. 安全与隐私

### PM1-SEC-001 数据边界

项目记忆内容必须作为不可信资料注入模型，不能执行记忆或外部文档中的指令。

### PM1-SEC-002 密钥

第一层不得在项目记忆数据库或事件 JSONL 中保存模型 API Key。

### PM1-SEC-003 敏感信息

工具参数和证据写入前必须复用现有敏感字段脱敏规则。

### PM1-SEC-004 用户修正

用户必须能够修正、废弃或标记错误记忆；历史记录保留 supersede 关系，不物理改写旧事件。

## 13. 性能与可靠性

### PM1-NFR-001 响应阻塞

最小状态检查点必须可靠写入。模型增强摘要允许在回答后执行；其失败不得影响本轮回答。

### PM1-NFR-002 检索预算

第一层默认最多返回 8 个相关记忆项；`top_k` 必须限制在 1～20。

### PM1-NFR-003 上下文预算

项目状态和召回结果必须有独立字符或 Token 上限，不得把全部项目历史重新注入模型。

### PM1-NFR-004 幂等

同一 `event_id`、`turn_id`、校验证据 ID 重试不得产生重复记录。

### PM1-NFR-005 降级

SQLite 可用但模型写回失败时，项目必须继续工作；SQLite 不可用时不得伪造旧状态或任务完成。

## 14. 验收场景

1. 创建任务时未提供校验项，创建失败。
2. Agent 请求把任务直接改为 `done`，更新被拒绝。
3. 所有必需校验通过且证据有效，完成门把任务改为 `done`。
4. 一个必需校验失败，任务保持 `validation_failed`。
5. 已完成任务的相关文件变化，证据变为 `stale`，任务变为 `needs_review`。
6. 父任务校验通过但子任务未完成，父任务不能完成。
7. 两个 session 基于同一 revision 更新，后提交者收到 revision 冲突。
8. 应用在达到步骤上限后退出，重启后能够恢复完整工具调用 transcript。
9. 模型提取返回无效 JSON，原始对话和最小检查点仍被保存。
10. 当前请求触发相关历史召回，但 MemoryPack 不包含全部历史。
11. `request_project_memory` 达到两次上限后不会继续循环。
12. 外部文件只登记路径；项目目录中不存在其副本。
13. 文件暂时离线时标记 `unreadable/missing`，不删除来源记录。
14. 重复 flush outbox 不产生不同 `event_id` 的重复逻辑事件。
15. 删除项目后 SQLite 中不再存在其状态、任务、校验、记忆请求和恢复状态。

## 15. 第一层完成定义

只有以下条件全部满足，第一层才算完成：

- 本规格所有 Must 需求已有代码或明确的测试覆盖。
- 函数级详细设计与实际实现一致。
- 单元测试覆盖任务状态机、完成门、证据失效、revision、恢复、检索、文件路径和失败降级。
- 现有项目、一般对话记忆、上下文压缩和工具调用测试无新增回归。
- 文档中不存在未解决的高优先级需求冲突。
