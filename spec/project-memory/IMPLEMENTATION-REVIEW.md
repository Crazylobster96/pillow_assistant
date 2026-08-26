# 项目记忆三层实现与最终一致性审查

- 审查版本：1.0
- 审查范围：第一层结构化项目记忆、第二层 Hybrid RAG、第三层 Federated GraphRAG/多模态引用
- 结论：三层本地参考实现与运行时装配已完成；具体 SaaS 厂商适配器未内置，未安装的 provider 会被明确拒绝，不会静默回退或上传数据。

## 1. 交付物

每层均包含详细需求、需求审查、函数级设计、实现和单元测试：

- `LEVEL1-REQUIREMENTS.md` / `LEVEL1-REQUIREMENTS-REVIEW.md` / `LEVEL1-DESIGN.md`
- `LEVEL2-REQUIREMENTS.md` / `LEVEL2-REQUIREMENTS-REVIEW.md` / `LEVEL2-DESIGN.md`
- `LEVEL3-REQUIREMENTS.md` / `LEVEL3-REQUIREMENTS-REVIEW.md` / `LEVEL3-DESIGN.md`

## 2. 最终审查发现与处理

| 编号 | 发现 | 风险 | 处理结果 |
|---|---|---|---|
| F-01 | 第一层设计把 blockers/current step 误写为任务规格 revision 变化 | 执行态变化会无意义使校验证据失效 | 设计已修正：只有描述、步骤、验收计划和相关 source 等规格变化增加 revision；执行态仍受状态机/CAS 约束 |
| F-02 | 第一层原渲染器只裁剪历史证据，超大 current task 可能超过 MemoryPack | 大量 steps/checks 仍可造成上下文溢出 | 新增 detailed/summary/minimal 三级状态渲染与硬字符上限；保留 revision、当前 task/step、blockers、progress 和 reconcile 标志 |
| F-03 | 第二层已取出作业与 remove_document 并发时可能重新发布已删除文档 | tombstone 被旧 worker 复活 | document 保留 deleted tombstone；发布事务检查 deleted/新 fingerprint；finish 不覆盖 deleted；旧作业标记 superseded |
| F-04 | 第三层需求有人工分类覆盖，但首版实现仅自动 classify | 用户固定分类不能稳定生效 | 新增带 revision/CAS 的 assignment、resolve_category，人工分配优先级最高 |
| F-05 | 分类迁移需要目标先发布、源后删除及可恢复记录 | 分类移动可能丢节点/关系或永久双份 | 新增确定性 migration job；复制 node/edge/cross-link、迁移 assignment 后删除源；失败记录可重试 |
| F-06 | 多模态资产只有项目级删除，缺单资产派生清理 | 删除引用后可能残留图节点、边和向量 | 新增 delete_asset/delete_node；本地清理向量 generation、节点和级联关系；远程不支持 node_delete 时拒绝假成功 |
| F-07 | GraphRAG provider 协议未显式保留 community 能力 | 适配器能力不可判定 | provider 强制声明 community_summaries、graph_traversal、node_delete 布尔能力 |
| F-08 | 第三层 NFR 把只读分类和单事务小写入也要求为 job | 产生不必要队列、与原子 SQLite 实现冲突 | 需求 1.1 已明确：跨进程、远程或长耗时副作用必须 job；本地单事务幂等小写入可同步；路由为只读 |
| F-09 | 分类迁移复用普通 cross-link 查询的 limit=100 | 单节点超过 100 条跨类关系时会丢失尾部关联 | 改为按稳定 link id 分页；105 条单节点关联迁移回归通过 |

未发现会导致本地默认部署错误、跨项目召回、绕过任务完成门或自动上传外部文件的未关闭问题。

## 3. 第一层核对

### 权威状态与恢复

- SQLite 保存项目状态、任务、步骤、校验、证据、turn checkpoint、记忆请求、外部 source 和 resume transcript。
- JSONL 只作为 SQLite outbox 的不可变审计镜像；写镜像失败不阻塞回答，未镜像事件可重试。
- 项目状态使用 revision compare-and-set；同 turn 重放只有完整内容一致才幂等。
- Agent 达到步骤上限或中断时保存完整 tool-call transcript，可在同 project/session 恢复。

### 任务完成门

- 任务创建必须有至少一个 required check。
- 公共更新 API 禁止直接写 done。
- 只有当前 task revision 的有效 evidence 可通过 check。
- required checks、子任务、blockers 和冲突全部满足后，`evaluate_task_completion` 才能写 done。
- 规格、步骤、校验计划或依赖 source 变化后旧 evidence 保留审计但标记 stale。

### 上下文

- PROJECT_STATE 为权威控制面，PROJECT_EVIDENCE 为不可信历史资料。
- 确定性与语义压缩都保护 PROJECT_STATE 标记。
- 大任务状态采用有界层级表示，证据截断后的 omission suffix 也计入总预算。

## 4. 第二层核对

- 默认 disabled；启用 local 后用确定性 feature-hash embedding、BM25、cosine 和融合排序。
- 外部文件只保存原路径、fingerprint、offset、term stats 和 vector；`internal_text` 为 NULL，命中时重新从原路径 materialize。
- 新 generation 在单事务全部写入后才切换 active；旧 generation 在发布前保持可用。
- project/task/kind/source/profile 过滤与 per-source diversity 有硬边界。
- 文件 fingerprint 变化时旧 offset 不返回正文并标记 stale。
- 持久队列以 project/document/fingerprint/profile 形成幂等键，删除 tombstone 和新旧 fingerprint 竞态已有测试。
- augment 失败回退第一层；replace 必须通过完整控制面 capability 校验。

## 5. 第三层核对

- category tree 拒绝跨项目 parent 和环；每项目最多一个 inbox。
- 全局只保存路由 metadata 与 cross-link；普通 edge 强制同 project/category。
- 每个分类使用独立 Hybrid RAG namespace；可绑定不同 category provider。
- 路由支持层级、阈值 inbox、有界 fan-out 和人工 assignment 覆盖。
- 本地图检索以 Hybrid RAG 命中为 seed，再做有界 BFS，返回 node/edge path。
- 联邦合并使用 route score + rank fusion，并限制分类数、每分类和总结果；单分类异常返回 partial。
- 分类迁移保留 job、node mapping、关系与 assignment 审计。
- 多模态本地基线只生成 metadata_only；远程 extractor 必须有显式 UploadPolicy。
- 外部资产只登记路径，不复制或删除原文件；fingerprint 变化使 asset/node stale。
- disabled、unknown provider 和 replace capability 边界均在运行时 factory 测试覆盖。

## 6. SaaS/专业 Provider 边界

核心已提供 `RAGProvider`、`CategoryGraphProvider`、`MultimodalExtractor`、capability validation 和 UploadPolicy，但没有内置微软 GraphRAG、Qdrant Cloud、Pinecone、Weaviate、Azure AI Search 等厂商客户端。

接入具体远程 adapter 时仍必须由 adapter 实现并测试：

- Vault/密钥读取和 project/category 到 tenant/collection/namespace 的不可跨项目映射；
- region、retention、encryption、deletion SLA 与项目策略校验；
- provider/总查询超时、远程 job/tombstone、重试和删除回执；
- ingest/query/token/storage/graph-build/multimodal 用量与软硬配额；
- provider 自身 ACL；核心调用方的项目级权限不能替代远程服务授权；
- 若声明 community_summaries=true，community generation 必须绑定 node/edge generation。

在这些适配器安装并通过 contract test 前，配置非 local provider 会明确报错并保留较低层后端，不会声称 SaaS 已连接。

## 7. 验证结果

- 三层新增测试：66 passed。
- 上下文预算：5 passed。
- 语义压缩：11 passed。
- 一般对话记忆：19 passed。
- R1：22 passed。
- R1 complete：22 passed。
- tool permissions 脚本：exit 0。
- 合计 145 个有计数断言通过，另有 tool permissions 脚本通过。
- 全模块 `compileall`：passed；`git diff --check`：passed；任务临时补丁：0。

## 8. 结论

第一层是默认必备控制面；第二层和第三层默认关闭，可作为增强检索面。替换模式不是简单关闭第一层：只有提供 authoritative state、validation gate、resume、source reference、delete 等完整 contract 的后端才允许替换。

当前代码可以直接用于本地三层能力和后续 adapter 开发；不能把协议存在等同于某个 SaaS 已实际接入。
