# Pillow Assistant 第二层项目记忆详细需求：Hybrid RAG

- 文档版本：1.0
- 状态：Reviewed
- 依赖契约：第一层定义的项目状态、任务状态机、校验门和来源追踪语义

## 1. 目标与定位

第二层为可选 Hybrid RAG 项目记忆后端，面向更大规模的项目历史、技术资料和文本文件，组合关键词检索与向量检索。它支持两种部署模式：

1. **增强模式**：第一层仍是权威控制面，第二层只替换或增强检索面。
2. **替换模式**：第二层提供完整 `ProjectMemoryBackend` 能力，独立承接权威状态、任务校验、恢复、来源和 Hybrid RAG；不再双写第一层。

“第二层可选”只表示用户可以不启用；项目记忆契约本身始终存在。默认部署使用第一层。

## 2. 非目标

- 不在第二层建立知识图谱；图谱属于第三层。
- 不在第二层解析音频、视频或图片语义；非纯文本多模态属于第三层。
- 不把任意向量库等同于完整项目记忆后端。
- 不因启用 RAG 放宽任务完成校验。
- 不复制用户输入的原始外部文件到项目目录或 RAG 数据目录。

## 3. 能力与替换规则

### PM2-CAP-001 必需能力声明

每个后端必须声明版本化 capabilities，至少包含：`authoritative_state`、`task_validation`、`resume`、`source_references`、`keyword_search`、`vector_search`、`metadata_filter`、`delete_project`。

### PM2-CAP-002 增强模式

增强模式只要求检索相关能力；控制面操作必须继续由已选权威后端完成。检索写入失败不得回滚已提交的权威任务状态，但必须形成待重建标记。

### PM2-CAP-003 替换模式

替换模式必须通过完整能力和行为一致性检查。缺少任何权威能力时必须拒绝启用，不得静默退化成“只有向量检索的项目记忆”。

### PM2-CAP-004 单一权威写入

替换模式禁止默认双写第一层和第二层。迁移或镜像必须是显式、可观测、可停止的独立流程。

### PM2-CAP-005 状态语义不变

无论后端类型如何，任务只有在当前 revision 的全部必需校验和有效证据通过后才能为 `done`。

## 4. 文档与分块

### PM2-ING-001 内部记忆索引

需求、决策、事实、约束、失败尝试、阻塞项、产物和检查点可被索引。索引记录必须保留 project/task/turn/source/kind/revision 元数据。

### PM2-ING-002 外部文件路径

外部文件只登记规范化原路径、fingerprint、解析器版本和分块偏移，不复制原文件。默认本地索引不得持久化外部文件完整正文副本；允许保存派生 token 统计、向量、摘要和字符偏移。

### PM2-ING-003 支持格式

参考实现至少支持 UTF-8/可检测文本格式：`.txt`、`.md`、`.rst`、`.py`、`.js`、`.ts`、`.json`、`.yaml`、`.yml`、`.toml`、`.csv`。不支持格式必须标记 `unsupported`，不得伪装已索引。

### PM2-ING-004 确定性分块

分块必须由 parser version、chunk size、overlap 和源 fingerprint 确定；相同输入与配置产生相同 chunk identity。

### PM2-ING-005 边界优先

分块优先按标题、段落、函数或换行边界切分；达到硬上限才按字符切分。chunk 必须记录 start/end offset。

### PM2-ING-006 幂等 upsert

同一 document identity + fingerprint + index profile 重复摄取不得产生重复 active chunks。

### PM2-ING-007 原子可见性

新文档版本的全部 chunks 成功后才能切换 active generation。失败 generation 不参与查询，旧 generation 在切换前继续可用。

### PM2-ING-008 变更与删除

源 fingerprint 变化时重建该源；源移除、项目删除或记忆 supersede 时必须删除或失活关联索引，不得返回悬挂结果。

### PM2-ING-009 索引配置版本

embedding provider/model/dimension、tokenizer、parser 或 chunk profile 变化必须生成新 `index_profile_id`，旧向量不得与新向量直接计算相似度。

## 5. Hybrid 检索

### PM2-RET-001 关键词检索

必须支持中英文 token 化、term frequency、document frequency 和长度归一化；参考实现使用 BM25。

### PM2-RET-002 向量检索

必须支持 cosine 或提供者声明的等价相似度，并验证向量维度与 index profile 一致。

### PM2-RET-003 融合

关键词与向量候选必须分别召回后融合。默认使用归一化加权分数；提供者分数不可比时使用 Reciprocal Rank Fusion。

### PM2-RET-004 过滤

查询必须先限制 project_id，并支持 task_id、kind、source_id、revision/status 和时间范围过滤。任何情况下不得跨项目泄漏。

### PM2-RET-005 多样性

默认限制单一 source/document 占据全部 top-k；允许配置每源上限，避免长文件淹没其他证据。

### PM2-RET-006 返回来源

每个结果必须包含稳定 result/chunk id、source/document id、路径或内部来源、offset、fingerprint、score 分解和 index profile。

### PM2-RET-007 读取时验证

外部文件结果返回正文前必须验证当前 fingerprint。文件缺失或变化时不得读取旧 offset 内容，结果标记 stale/unavailable 并触发重建。

### PM2-RET-008 上下文安全

RAG 结果始终是不可信资料，必须使用项目证据 marker 包裹，并受上下文预算与语义压缩来源校验约束。

### PM2-RET-009 精确补取

项目 Agent 的补取工具必须支持 query、filters、top_k 和 reason；单轮补取上限沿用第一层，避免循环检索。

### PM2-RET-010 空结果

空结果必须可区分“确实无命中”“索引未就绪”“服务不可用”“源已变化”。required 请求仍需持久化。

## 6. Embedding 与提供者

### PM2-PRO-001 提供者协议

Embedding 提供者必须声明 `provider_id`、`model_id`、`dimension`、最大批大小、是否远程和健康状态，并实现批量 embed。

### PM2-PRO-002 本地基线

必须提供无额外依赖、可离线测试的确定性本地 embedding 基线。该基线用于可运行参考与降级，不得宣称达到专业语义模型质量。

### PM2-PRO-003 SaaS

允许接入 SaaS embedding/vector/RAG。密钥必须从 Vault 获取，不进入项目状态、prompt、JSONL、SQLite 明文字段或日志。

### PM2-PRO-004 数据上传同意

SaaS 默认 `allow_content_upload=false`。外部本地文件在路径模式下不得上传正文；只有用户对具体 provider/项目显式允许后，才可上传派生 chunk 内容，并记录策略版本和时间。

### PM2-PRO-005 批处理与限流

摄取必须按 provider 限制分批，支持超时、429/5xx 有界重试和断点续建；查询路径不得无限重试。

### PM2-PRO-006 费用可观测

远程调用记录请求批次数、输入字符/估算 token、向量数量、provider/model 和可用的费用信息，不记录正文和密钥。

### PM2-PRO-007 迁移

切换 embedding 模型必须后台重建新 profile，完成前继续查询旧 profile；不得在同一检索中混算不同维度。

## 7. 一致性、故障与性能

### PM2-NFR-001 非阻塞写回

索引构建不得阻塞普通回答。权威状态先提交，索引工作进入持久队列；当前轮可检索已完成 generation。

### PM2-NFR-002 降级

增强模式下 RAG 不可用时回退权威后端基础检索并标记 degraded。替换模式若权威能力不可用，必须停止状态写入并明确报错，禁止假成功。

### PM2-NFR-003 队列幂等

索引任务以 project/document/fingerprint/profile 形成幂等键；崩溃重启后可重试，重复执行不重复发布 generation。

### PM2-NFR-004 查询预算

参考实现 10,000 active chunks 条件下，本地 top-10 查询目标 P95 小于 500ms；候选扫描必须有硬上限。

### PM2-NFR-005 资源边界

chunk 数、文件大小、单次读取字符数、候选数、top_k 和并发 embedding 批次必须配置上限。

### PM2-NFR-006 可观测

记录 ingest queued/running/published/failed、查询耗时、候选数、profile、降级原因；日志不得包含正文。

### PM2-NFR-007 可删除

项目删除必须删除本地派生索引并向 SaaS 发删除请求。远程删除失败必须进入可重试 tombstone，不得声称完全删除。

### PM2-NFR-008 兼容

未启用第二层时不增加网络依赖、不改变第一层数据库行为、不影响现有用户部署。

## 8. 配置需求

配置至少包含：mode (`disabled|augment|replace`)、provider、embedding profile、hybrid weights、chunk size/overlap、candidate limits、per-source limit、content upload policy、fallback policy。

配置启用前必须运行静态 capability validation 和一次 health check。配置错误保留上一个可用后端。

## 9. 验收标准

1. 禁用第二层时第一层全部测试不变。
2. 同一文本重复摄取不产生重复 active chunks。
3. 源变更只有新 generation 发布后才替换旧 generation。
4. 关键词独有和向量独有结果都能进入融合 top-k。
5. project/task/kind/source 过滤正确且无跨项目数据。
6. 外部文本索引不在 RAG 数据库保存完整正文，返回时按路径与 offset 读取。
7. 文件变化后旧结果不返回正文并进入重建。
8. embedding profile 不兼容时拒绝相似度计算。
9. SaaS 未授权上传时拒绝发送外部正文。
10. 增强模式 RAG 故障可降级；替换模式权威故障不假成功。
11. 删除项目后本地索引清空；远程失败形成 tombstone。
12. 替换模式后端缺少校验门或 resume 能力时启用失败。
