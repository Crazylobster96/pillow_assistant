# Pillow Assistant 第三层 Federated GraphRAG 函数级详细设计

- 文档版本：1.1
- 依据：`LEVEL3-REQUIREMENTS.md` 1.1（已审查）
- 状态：Implemented / Verified（本地联邦 GraphRAG/metadata-only 参考实现）

## 1. 模块

```text
pillow_assistant/core/rag/graph_federation.py
    分类树、全局路由索引、分类图、跨分类 link、联邦查询

pillow_assistant/core/rag/graph_admin.py
    人工分类 revision/CAS、分类解析与可恢复迁移 job

pillow_assistant/core/rag/graph_provider.py
    category-scoped SaaS/local provider 协议与能力校验


pillow_assistant/core/rag/multimodal.py
    资产引用、extractor 协议、metadata-only 基线、上传策略

pillow_assistant/core/rag/level3_backend.py
    Level3ProjectMemoryBackend 和可选 runtime factory
```

## 2. 数据库

### `project_graph_categories`

字段：id、project_id、name、description、parent_id、routing_examples_json、backend_id、modalities_json、status、revision、is_inbox、created_at、updated_at；`(project_id,name)` 唯一。

### `project_graph_nodes`

字段：id、project_id、category_id、node_key、node_type、label、content、source_id、document_id、chunk_id、task_id、turn_id、fingerprint、revision、validity、provenance_json、created_at、updated_at；`(category_id,node_key)` 唯一。

### `project_graph_edges`

字段：id、project_id、category_id、from_node_id、to_node_id、relation_type、directed、weight、confidence、evidence_json、validity、revision、created_at、updated_at；唯一键由分类/端点/类型/方向组成。

### `project_graph_cross_links`

字段：id、project_id、from_category_id/from_node_id、to_category_id/to_node_id、relation_type、weight、evidence_json、validity、created_at、updated_at。

### `project_graph_assignments`

字段：project_id、subject_type、subject_id、category_id、revision、reason、created_at、updated_at；subject 唯一，category 外键级联。

### `project_graph_jobs`

字段：id、project_id、job_type、source/target category、status、attempt_count、payload_json、last_error、created_at、updated_at；参考实现用于分类迁移，远程 adapter 扩展 ingest/build/delete job。

### `project_multimodal_assets`

字段：id、project_id、category_id、source_id、original_path、normalized_path、modality、size、mtime_ns、fingerprint、extractor_profile、status、description、locator_json、metadata_json、created_at、updated_at。description 只能是派生说明或 metadata-only，不保存原二进制。

## 3. 分类函数

### `register_category(project_id, name, *, description='', parent_id=None, routing_examples=None, backend_id='local', modalities=None, is_inbox=False, category_id=None) -> dict`

验证同项目 parent；同名幂等；inbox 每项目最多一个；创建后把 name/description/examples 写入路由 Hybrid RAG namespace `project::routes`。

### `update_category(category_id, *, base_revision, ...) -> dict`

CAS；parent 变更调用 `_assert_no_cycle`；routing 相关字段变更 revision+1 并重建 route 文档。

### `_assert_no_cycle(project_id, category_id, candidate_parent_id) -> None`

从 parent 向上最多 100 层；遇 category_id 抛 `CategoryCycleError`。

### `classify(project_id, text, *, top_k=3, threshold=0.15, fanout_margin=0.08) -> list[CategoryRoute]`

查询路由索引；顶层先排名；子分类仅在父分类入选后竞争；低于阈值返回 inbox；在 margin 内 fan-out，硬上限 5。

### `assign_category(project_id, subject_type, subject_id, category_id, *, reason='', base_revision=None) -> dict`

验证分类同项目；已有分配按 revision/CAS 更新，记录 reason；人工分配优先于自动分类。

### `get_assignment(...)` / `resolve_category(...)`

有人工分配时返回 score=1、reason=manual-assignment；否则调用 classify。

### `migrate_category(source_category_id, target_category_id) -> dict`

同项目、不同分类且 source 无子分类；创建/重试确定性 migration job，先幂等复制节点、类内边与 cross-link 到目标，再 retarget assignments，最后删除源。cross-link 按稳定 id 分页，不受普通查询 limit=100 影响。失败标记 job=failed 并保留源，目标副本可由重试去重。

### `CategoryGraphProvider`

显式声明 provider_id、is_remote、基础 capabilities 和 `graph_capabilities`（community_summaries、graph_traversal、node_delete），实现 category-scoped upsert/search/delete/health。未知或能力不足 provider 绑定直接拒绝，不静默降级。


## 4. 图函数

### `upsert_node(project_id, category_id, node_key, *, node_type, label, content='', ...fingerprint/provenance) -> dict`

category 必须属于 project；相同 fingerprint 幂等；变化 revision+1；将 node 文本写入分类专属 Hybrid RAG namespace `project::category::<id>`。

### `add_edge(project_id, category_id, from_node_id, to_node_id, relation_type, *, directed=True, weight=1, confidence=0.5, evidence=None, allow_self=False) -> dict`

两端存在、同 project/category；跨分类抛 `CrossCategoryEdgeError`；无 evidence 时 confidence<=0.5；幂等 upsert。

### `add_cross_link(...) -> dict`

两端必须属于不同分类且同项目；只写 global cross-link table，不调用分类 graph provider。

### `traverse(category_id, seed_node_ids, *, depth=2, relation_types=None, direction='both', max_nodes=50, max_edges=100) -> dict`

BFS，有界、去重、只读同 category；返回 nodes、edges、paths 和 truncated。

### `search_category(project_id, category_id, query, *, top_k=8, graph_depth=1, filters=None) -> list[GraphHit]`

从该分类 Hybrid RAG 取种子 node_key，加载节点并遍历；每项保留 seed score 和 path。

### `delete_node(node_id) -> bool`

本地先删除分类向量 document（保留 tombstone），再删 node，SQLite 级联类内边和 cross-link；远程 provider 必须声明并实现 node_delete，否则拒绝资产删除，禁止只删本地影子数据。

## 5. 联邦函数

### `plan_query(project_id, query, *, top_categories=3, per_category_top_k=8, graph_depth=1) -> FederatedPlan`

调用 classify，生成有界 CategoryQuery；包含 category revision、backend_id 和预算。

### `search(project_id, query, *, top_categories=3, per_category_top_k=8, total_limit=20, graph_depth=1, per_category_limit=6) -> FederatedResult`

参考实现顺序执行，本地无网络；provider adapter 可并行。每分类异常加入 failures；使用 RRF `(1/(60+rank)) * (0.5+route_score)` 融合；强制 per-category/total limit；返回 partial、routes、hits、failures。

## 6. 多模态

### `MultimodalExtractor` 协议

字段：provider_id、profile_id、modalities、is_remote；函数 `extract(path, modality, locator=None) -> ExtractionResult`、`health()`。

### `MetadataOnlyExtractor`

不读取/理解文件内容，只返回文件名、扩展名、size、mtime；status=`metadata_only`。

### `register_asset(project_id, category_id, path, *, source_id=None, modality=None, extractor=None, upload_policy=None, locator=None) -> dict`

只保存路径和 fingerprint；remote extractor 前调用 `guard_remote_upload`；保存 description/locator/profile/status；把派生 description 建 node 和向量索引，但 provenance 明确 metadata_only/extracted。

### `refresh_asset(asset_id) -> dict`

比较 size/mtime/hash；变化时 status=stale，并使关联 source node validity=stale；不返回旧 description 作为新证据。

### `delete_asset(asset_id) -> bool`

幂等；先调用 federation.delete_node 清理派生向量/图关系，成功后删除资产引用。原外部文件永不删除。

## 7. 第三层后端

### `Level3ProjectMemoryBackend(structured_backend, federation, *, mode='augment', fallback_backend=None)`

委托完整控制面；`search_memory` 使用联邦结果，失败/无路由时回退 lower backend；`add_memory_item` 可按 kind/task route 或 inbox 索引；`delete_project_memory` 先删 federation/asset，再删控制面。

### `build_level3_backend(lower_backend, db_path, config) -> backend`

- disabled 返回 lower_backend。
- local 创建 `FederatedGraphRAG` 并 health check。
- category 配置幂等注册。
- replace 执行完整 capability validation。
- 非 local provider 未安装时拒绝并由 Orchestrator 保留 lower backend。

## 8. 测试矩阵

| 测试 | 需求 |
|---|---|
| 分类 parent/环/inbox | CAT-001/004 |
| 分类 revision 路由重建 | CAT-007 |
| 路由 top/fanout/inbox | CAT-003～004 |
| node 幂等/revision | GRA-001/004 |
| 同类边/跨类拒绝 | CAT-002/GRA-002 |
| cross-link 全局隔离 | FED-001/005 |
| 分类 namespace 隔离 | GRA-005 |
| BFS 深度/预算/path | GRA-006/008 |
| 联邦分类上限与融合 | FED-002/004 |
| 单分类失败 partial | FED-003 |
| metadata-only 资产路径 | MM-001/007 |
| 资产变化 stale | MM-004 |
| remote upload guard | MM-005 |
| replace capability | SAF-003 |
| 项目删除 | SAF-005 |
| 第一、二层全回归 | NFR-005 |
| 人工分类覆盖/revision CAS | CAT-005 |
| 分类迁移 job/assignment/cross-link | CAT-006/NFR-003 |
| provider 能力声明与分类隔离 | GRA-007/PRO-002 |
| asset/node/edge/vector 安全删除 | SAF-005 |
| tombstone 后旧 worker 不发布（复用 L2） | SAF-005/NFR-003 |

## 9. 一致性门槛

第三层完成后运行全部新增测试、第一/二层测试、上下文/语义/对话/R1 回归、compileall 和 `git diff --check`。逐项核对需求与函数签名，记录未实现的 provider 专属可选能力；不得把协议扩展点报告为已接入具体 SaaS。
