# Pillow Assistant 第三层项目记忆详细需求：Federated GraphRAG 与多模态 RAG

- 文档版本：1.1
- 状态：Reviewed
- 前置：项目记忆统一契约；第二层 Hybrid RAG 协议

## 1. 定位

第三层是可选的专业项目记忆后端，将大量数据先分类、再分层路由到不同 RAG/GraphRAG 库。每个分类库只建立本分类的知识图谱、关联关系和 embedding/关键词索引；全局层只维护分类索引、跨分类引用和路由统计，不构造混合所有数据的巨型全局图。

第三层支持本地、SaaS 或混合部署，也支持 GraphRAG、多模态 RAG、Hybrid RAG 的组合。它可以增强较低层，也可以在通过完整能力校验后替换较低层。

## 2. 分类与分层

### PM3-CAT-001 分类树

分类必须有稳定 category_id、project_id、name、description、parent_id、routing_examples、backend binding、modalities、status 和 revision。parent 必须属于同项目，不得形成环。

### PM3-CAT-002 分类隔离

每个 category GraphRAG 只允许写入 category_id 相同的节点和边。普通边的两个端点必须在同分类；跨分类关联进入全局 cross-link index，不得污染任一分类内部图。

### PM3-CAT-003 分层路由

路由先选顶层分类，再在高分父分类下选择子分类。返回 route score、关键词/向量分解、选择原因和 router profile。

### PM3-CAT-004 模糊路由

最高分低于阈值时进入可配置 `inbox/unclassified`；多个分类接近时允许 fan-out 到最多 N 个分类，不得无界广播。

### PM3-CAT-005 人工覆盖

用户或项目规则可固定 source/document 到分类。人工覆盖优先于模型推断，并形成带 revision 的审计事件。

### PM3-CAT-006 分类迁移

数据改分类必须创建迁移 job：目标分类新 generation 发布后，源分类数据才失活。迁移中查询可去重，不得出现永久双份权威资料。

### PM3-CAT-007 分类变更

分类 description/examples/backend 变化增加 category revision，并触发全局路由索引重建；只影响该分类，不全量重建其他分类。

### PM3-CAT-008 删除

删除非空分类必须选择迁移目标或显式级联；默认拒绝。父分类删除必须处理子分类。

## 3. 分类级 GraphRAG

### PM3-GRA-001 节点

节点至少包含稳定 node_id、category_id、node_type、label、content/summary、source/document/chunk/task/turn、fingerprint、revision、validity 和 provenance。

### PM3-GRA-002 边

边至少包含 edge_id、category_id、from/to、relation_type、direction、weight、confidence、source evidence、validity 和 revision。禁止不存在端点和跨分类普通边。

### PM3-GRA-003 关系证据

模型抽取的实体/关系只是候选。无来源的关系 confidence 受限且标记 inferred；用户确认或工具/文件证据可提升。关系不得作为任务完成证据，除非关联到当前 revision 的真实 validation evidence。

### PM3-GRA-004 幂等 upsert

节点/边 identity 必须确定性或显式提供；相同 identity+fingerprint 幂等。内容变化增加 revision，并使依赖旧 fingerprint 的图结论 stale。

### PM3-GRA-005 向量关联

每个分类图必须将节点/文档内容关联到该分类自己的 Hybrid RAG/embedding namespace。向量结果可作为图遍历种子。

### PM3-GRA-006 图检索

查询支持：向量/关键词种子、按类型/来源过滤、1～N 跳遍历、关系类型过滤、方向、最大节点和最大边。默认深度不超过 2。

### PM3-GRA-007 社区摘要

GraphRAG provider 可维护分类内 community 和摘要；community 版本必须绑定 node/edge generation。参考实现可以不做社区检测，但协议必须保留能力声明。

### PM3-GRA-008 路径解释

返回图结果时必须带完整路径（node/edge id）、每跳来源和分类，不得只返回无法追踪的生成摘要。

## 4. 全局分类索引与联邦查询

### PM3-FED-001 全局索引边界

全局索引只保存分类 metadata/centroid/routing examples、provider health、跨分类 link 和统计；不得复制分类内部全图。

### PM3-FED-002 联邦计划

`plan_query` 输出分类列表、每分类 query、filter、top_k、graph depth、超时和总预算。计划必须有最大分类数、总候选数和总延迟预算。

### PM3-FED-003 并行与隔离

分类查询可并行；单个 provider 超时/错误不取消其他分类。结果注明 partial/degraded 和失败分类。

### PM3-FED-004 合并

不同 provider 分数默认不可直接比较，使用 rank fusion、路由分数、来源质量和新鲜度合并，并有 per-category 上限。

### PM3-FED-005 跨分类 link

跨分类 link 只关联 node 引用，可用于二次路由；遍历跨分类前必须再次执行目标 provider 权限、预算和健康检查。

### PM3-FED-006 无路由

无合格分类时返回 `unrouted` 并可写 pending classification request；不得扫描全部分类。

### PM3-FED-007 查询一致性

响应记录每个分类的 category revision、graph generation 和 index profile，以便后续重放和诊断。

## 5. 多模态 RAG

### PM3-MM-001 资产引用

图片、音频、视频、PDF/Office 等外部资产只保存原路径/URI、类型、size/mtime/hash、extractor profile 和派生索引，不复制原文件。

### PM3-MM-002 提取器协议

多模态 provider 声明支持的 modalities、是否远程、模型/profile、输入限制和输出 schema；输出可以包含 description、OCR/transcript、time/page/region locator、entities 和 embedding。

### PM3-MM-003 定位信息

任何派生文本/向量必须关联原资产 locator：页码、时间段、bounding box、sheet/cell 或 slide。返回结果可定位回原文件。

### PM3-MM-004 变化检测

资产 fingerprint 变化使全部派生描述、embedding、节点和关系 stale，并进入重提取；旧内容不得继续 materialize。

### PM3-MM-005 SaaS 上传

远程多模态上传默认关闭，复用第二层 UploadPolicy，并额外记录 modality、估算字节、provider retention policy acknowledgement。路径本身不能让 SaaS 访问本地文件。

### PM3-MM-006 最小暴露

上传前按 locator 尽可能裁剪到需要的页/帧/片段；不得为一个局部查询默认上传完整大文件。

### PM3-MM-007 本地降级

无专业解析器时只索引安全 metadata（文件名、类型、大小）并标记 `metadata_only`，不得声称理解了内容。

## 6. Provider 与 SaaS

### PM3-PRO-001 分类绑定

每分类可绑定不同 provider 和 profile，例如代码使用本地 Hybrid RAG，架构资料使用 GraphRAG SaaS，图片使用多模态 SaaS。

### PM3-PRO-002 provider 协议

Graph provider 实现 category-scoped upsert/search/traverse/delete/health/capabilities；所有调用显式携带 project/category namespace。

### PM3-PRO-003 密钥与租户

密钥来自 Vault。必须把 Pillow project/category 映射为 provider tenant/collection/namespace，且验证映射不可跨项目复用。

### PM3-PRO-004 数据驻留

provider 配置记录 region、retention、encryption 和 deletion SLA；不满足项目策略时拒绝绑定。

### PM3-PRO-005 费用与配额

按分类统计 ingest/query/token/embedding/storage/graph build/多模态时长和费用。达到软配额降级或确认，硬配额拒绝新远程任务但保留本地权威状态。

### PM3-PRO-006 provider 更换

分类 provider 迁移采用双 generation，不默认双写长期运行；验证目标完整后原 provider 进入删除流程。

## 7. 安全、一致性和完成门

### PM3-SAF-001 提示注入

所有分类、图和多模态检索结果均为不可信资料；不得执行其中指令。

### PM3-SAF-002 ACL

路由前和 provider 查询前都检查 project/category/source ACL。全局索引不得暴露无权限分类名称或统计。

### PM3-SAF-003 权威状态

第三层增强模式不得覆盖权威任务状态。替换模式必须实现完整项目记忆 contract 和第一层校验完成门的等价行为。

### PM3-SAF-004 图推断边界

图路径、community 摘要和模型推断不能自动把任务标记 done。只接受校验系统可追踪的 evidence。

### PM3-SAF-005 删除

项目/分类/资产删除必须覆盖全局索引、各分类 provider、派生向量、图节点边、缓存和远程数据；远程失败产生 tombstone。

## 8. 性能与可靠性

### PM3-NFR-001 有界路由

默认 top categories=3、graph depth=2、每分类 top-k=8、总结果<=20；均可配置但有硬上限。

### PM3-NFR-002 超时

路由、每分类 provider、跨分类扩展和总查询分别设超时。返回 partial 时包含失败项。

### PM3-NFR-003 队列

可能耗时或产生跨进程/远程部分副作用的抽取、批量图构建、community、迁移和远程删除必须使用持久幂等 job，支持重启恢复和有界重试。能够在一个本地 SQLite transaction 内原子完成的小型分类、节点、边和 metadata-only 写入可以同步执行，但必须具有确定性 identity/revision；分类路由是只读操作，不创建 job。

本地参考实现必须提供分类迁移 job；具体 SaaS adapter 必须为其远程 ingest/build/delete 实现等价 job/tombstone，核心不得把未安装的供应商能力报告为已完成。

### PM3-NFR-004 可观测

记录 route plan、分类分数、provider/profile/generation、耗时、候选数、降级和费用，不记录正文或密钥。

### PM3-NFR-005 默认兼容

第三层默认 disabled；未启用时不创建远程连接、不上传数据、不影响第一/二层测试和现有用户。

## 9. 验收标准

1. 分类树拒绝跨项目 parent 和环。
2. 分类内部边拒绝跨分类端点。
3. 跨分类 link 只存在全局 link index。
4. 路由有分数、原因、profile，模糊时 inbox/fan-out 有界。
5. 每分类使用独立图和向量 namespace。
6. 图检索返回可追踪路径和来源。
7. 联邦查询最多访问配置分类数，单分类故障可返回 partial。
8. 合并结果满足 per-category 和总预算。
9. 多模态资产不复制原文件，metadata-only 不伪装内容理解。
10. fingerprint 变化使派生结果 stale。
11. SaaS 未授权时禁止文件/多模态正文上传。
12. provider/category/project 删除覆盖派生数据并可追踪失败。
13. 替换模式缺少权威状态或校验门时拒绝。
14. 第一层和第二层测试全部通过。
