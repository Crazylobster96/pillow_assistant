# Pillow Assistant 第二层 Hybrid RAG 函数级详细设计

- 文档版本：1.0
- 依据：`LEVEL2-REQUIREMENTS.md` 1.0（已审查）
- 状态：Implemented / Verified（本地 Hybrid RAG 参考实现）

## 1. 模块

```text
pillow_assistant/core/project_memory_backend.py
    后端 capability、替换模式校验、委托包装

pillow_assistant/core/rag/base.py
    EmbeddingProvider、RAGProvider、数据类和公共异常

pillow_assistant/core/rag/local_hybrid.py
    SQLite generation、分块、BM25、向量、融合和回源

pillow_assistant/core/rag/project_backend.py
    Layer2ProjectMemoryBackend：控制面委托 + RAG 检索面
```

## 2. 公共数据类与协议

### `BackendCapabilities`

布尔字段：authoritative_state、task_validation、resume、source_references、keyword_search、vector_search、metadata_filter、delete_project；另有 `backend_id`、`contract_version='1'`。

### `validate_backend_capabilities(capabilities, mode) -> None`

- disabled 不校验。
- augment 需要 keyword_search 或 vector_search。
- replace 需要全部八项。
- 不满足抛 `BackendCapabilityError`，错误列出缺失项。

### `EmbeddingProfile`

字段：provider_id、model_id、dimension、metric、profile_id。`profile_id` 由规范化配置 SHA-256 产生。

### `EmbeddingProvider`

```python
provider_id: str
model_id: str
dimension: int
is_remote: bool
def embed(texts: list[str]) -> list[list[float]]: ...
def health() -> dict: ...
```

返回数量或维度不匹配时由调用方抛 `EmbeddingError`。

### `LocalHashEmbeddingProvider(dimension=192)`

- 对中英文 token 与中文 bigram 做带符号 feature hashing。
- L2 normalize 后返回确定性向量。
- 仅作为离线基线和测试，不标榜专业语义质量。

## 3. 本地数据库

新增 4 表：

### `project_rag_documents`

`id`、project_id、source_id、document_kind、original_path、normalized_path、fingerprint、parser_version、profile_id、active_generation、status、error_code、created_at、updated_at；`(project_id,source_id,profile_id)` 唯一。

### `project_rag_generations`

`id`、document_id、generation_no、fingerprint、profile_id、status (`building|published|failed|superseded|deleted`)、chunk_count、created_at、published_at；generation identity 唯一。

### `project_rag_chunks`

`id`、generation_id、project_id、document_id、source_id、task_id、kind、start_offset、end_offset、internal_text、term_freq_json、token_count、embedding_json、fingerprint、created_at。

外部 source 的 `internal_text` 必须为 NULL；内部项目记忆可以保存正文。

### `project_rag_jobs`

`idempotency_key` 主键、project_id、document_id、fingerprint、profile_id、operation、status、attempt_count、last_error、created_at、updated_at。

索引：active document、generation status、chunk project/source/task/kind、job status。

## 4. 分词与分块函数

### `tokenize(text) -> list[str]`

- 英文 lower-case 单词。
- 连续中文词片段和相邻 bigram。
- 去除纯空白，单文档 token 数有上限。

### `fingerprint_text(text) -> str`

SHA-256 UTF-8。

### `chunk_text(text, *, chunk_chars=1600, overlap_chars=160) -> list[TextChunk]`

- 参数约束：chunk 256～16000，overlap 0～chunk/2。
- 优先在 `\n\n`、标题、换行、句末切分。
- 保证覆盖、start < end、下一块有界 overlap、无空块。
- chunk id 由 document/fingerprint/profile/start/end 在摄取时生成。

### `read_text_source(path, max_chars) -> tuple[text,status]`

- 扩展名白名单。
- `utf-8-sig` 优先，失败使用显式可接受 fallback。
- 超限返回 `too_large`，不部分发布。

## 5. `LocalHybridRAG`

### `__init__(db_path, embedding=None, chunk_chars=1600, overlap_chars=160, max_file_chars=5_000_000, candidate_limit=2000)`

校验配置，创建 profile，不自动迁移。

### `ensure_schema() -> None`

幂等建表索引。

### `index_internal(project_id, content_id, text, *, kind, task_id=None, fingerprint=None) -> dict`

- document_kind=internal。
- internal_text 保存 chunk 正文。
- 调用 `_publish_generation`。

### `index_source(project_id, source: dict, *, task_id=None, kind='source', expected_queue_fingerprint=None) -> dict`

- 验证 availability 和格式。
- 读取但不复制原文件。
- external chunks 的 internal_text=NULL。
- term/vector/offset 写入 building generation，全部完成后发布。
- worker 传入 queued fingerprint；document 已 tombstone 或已有更新 queued fingerprint 时抛 `StaleIndexJob`，禁止旧作业发布。

### `_publish_generation(document, chunks, fingerprint) -> dict`

单 transaction：创建 building generation/chunks，校验数量和维度，旧 published -> superseded，新 -> published，document.active_generation 切换。

### `remove_document(project_id, source_or_content_id) -> int`

保留 document tombstone（status=deleted、active_generation=NULL），删除派生 generations/chunks，并把未完成 jobs 标记 deleted；后续已取出的旧 worker 也不得重新发布。

### `search(project_id, query, *, task_id=None, kinds=None, source_ids=None, top_k=8, per_source_limit=3, lexical_weight=0.45, vector_weight=0.55) -> list[RAGHit]`

1. 校验 query/top_k/weights。
2. 只取同 project/profile 的 active published chunks，candidate_limit 硬限制。
3. 计算 BM25 和 cosine。
4. 各自归一化后加权；全零时使用 RRF/可用单路。
5. 按融合分数、稳定 chunk id 排序。
6. 执行 per-source limit。
7. 外部命中调用 `_materialize_external_hit`。

### `_bm25(query_tokens, candidates) -> dict[id,float]`

`k1=1.5,b=0.75`；DF 仅基于当前候选集合，空查询返回零。

### `_cosine(query_vector, vector) -> float`

维度不一致抛 `EmbeddingProfileMismatch`，零向量返回 0。

### `_materialize_external_hit(row) -> RAGHit`

- 重新读取路径并计算 fingerprint。
- 不一致返回 `stale=True, content=''`，并把 document 标为 stale。
- 一致时用 start/end 读取当前正文。

### `delete_project(project_id) -> int`

删除 documents/generations/chunks/jobs，幂等返回影响 document 数。

## 6. `Layer2ProjectMemoryBackend`

### `__init__(structured_backend, rag, *, mode='augment', fallback_to_structured=True)`

- structured_backend 是完整控制面实现，可以是第一层或专业/SaaS 状态后端。
- augment 校验 RAG 能力；replace 还校验 structured_backend 的完整 capabilities。
- `__getattr__` 仅委托未覆盖的控制面方法。

### `add_memory_item(...) -> dict`

先写权威后端；成功后同步参考实现索引内部 item。索引失败返回权威结果并记录 `index_status=failed`，不得撤销权威写入。

### `search_memory(...) -> list[dict]`

优先 RAG；RAG 异常且 augment+fallback 时调用 structured backend；结果统一成第一层 search schema 并额外保留 score breakdown。

### `register_source(...) -> dict`

权威后端只登记路径；本地 reference implementation 随后索引。SaaS provider 在 upload policy=false 时只登记，不上传。

### `refresh_source(...) -> dict`

先由权威后端检测变化；changed 时提交/执行 reindex。发布完成前旧 generation 保持可查，但 materialize 会因 fingerprint 不符不返回旧正文。

### `process_pending_jobs(project_id=None, limit=20) -> dict`

- 按持久队列顺序处理 pending/failed job。
- 调用 `index_source(... expected_queue_fingerprint=job.fingerprint)`。
- `StaleIndexJob` 调用 `supersede_job`，不计为失败；普通异常增加 attempt 并保留可诊断错误。
- 返回 processed/succeeded/failed，保持普通回答路径非阻塞（由 Orchestrator 在线程中调用）。

### `finish_job(...)` / `supersede_job(job_id)`

- finish 不得把 deleted tombstone 恢复为 failed/done。
- supersede 只改变非 deleted 作业。

### `delete_project_memory(project_id) -> None`

先为 RAG 建 tombstone/删除派生索引，再删除控制面。远程失败抛可重试错误，调用者不得报告完全成功。

## 7. SaaS 接口

`RAGProvider` 与本地实现使用相同 `index/search/delete/health/capabilities` 语义。远程适配器额外接收 `UploadPolicy`：provider_id、project_id、allow_content_upload、approved_at、policy_version。

`guard_remote_upload(provider, source, policy)` 在任何远程 embed/upsert 前调用；本地外部路径且未授权时抛 `ContentUploadDenied`。

## 8. 单元测试矩阵

| 测试 | 需求 |
|---|---|
| capability augment/replace 校验 | CAP-001～005 |
| hash embedding 确定性/维度/归一化 | PRO-001～002 |
| chunk 覆盖、overlap、稳定性 | ING-004～005 |
| 内部文档幂等 generation | ING-006～007 |
| 外部文件不保存正文 | ING-002 |
| BM25 独有命中 | RET-001 |
| vector 独有命中与维度拒绝 | RET-002～003 |
| project/task/kind/source 过滤 | RET-004 |
| per-source 多样性 | RET-005 |
| source offset/materialize | RET-006～007 |
| 文件变化 stale | RET-007 |
| profile 切换隔离 | ING-009/PRO-007 |
| 增强模式 fallback | NFR-002 |
| 替换模式不完整后端拒绝 | CAP-003 |
| SaaS 上传策略拒绝 | PRO-004 |
| 项目删除 | NFR-007 |
| 删除与 worker 竞态保持 tombstone | ING-008/NFR-003 |
| 新旧 fingerprint job 正确 supersede | ING-006/NFR-003 |
| 第一层全回归 | NFR-008 |

## 9. 实现一致性门槛

完成实现后必须：编译全部新增模块；运行第二层测试；运行第一层 20 项测试和既有上下文/语义/R1 回归；执行 `git diff --check`；逐函数核对本文签名。任何偏差先修订设计或代码，再进入第三层。
