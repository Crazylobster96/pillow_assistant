# 第三层需求审查记录

- 审查对象：`LEVEL3-REQUIREMENTS.md` 1.1
- 结论：21 个问题已处理，无未关闭阻塞项

| 编号 | 问题 | 处理 |
|---|---|---|
| R3-01 | “多个 GraphRAG”缺少全局边界 | 全局只做分类路由/跨分类引用，不建总图 |
| R3-02 | 分类内部关系可能跨库 | 普通边强制同 category，跨类关系进入 global link |
| R3-03 | 分类层级可能成环 | parent 同项目 + cycle validation |
| R3-04 | 低置信分类数据无处可放 | inbox/unclassified + pending request |
| R3-05 | 多分类广播成本失控 | fan-out、总候选和总延迟硬上限 |
| R3-06 | 不同 provider 分数不可比 | route score + RRF + 质量/新鲜度融合 |
| R3-07 | 图关系由模型幻觉产生 | inferred 标记、来源、confidence 上限 |
| R3-08 | 图结论可能绕过任务校验 | 明确禁止图/摘要自动完成任务 |
| R3-09 | 分类迁移可能丢数据 | 目标 generation 先发布、源后失活 |
| R3-10 | provider 更换长期双写 | 双 generation 仅迁移期，完成后删旧 |
| R3-11 | 多模态“记录路径”与 SaaS 冲突 | 默认不上传，显式政策后按 locator 最小上传 |
| R3-12 | 多模态无解析器会误报 | metadata_only 明示能力边界 |
| R3-13 | 派生结果不能定位原始内容 | 强制 page/time/bbox/cell/slide locator |
| R3-14 | 文件变化后图和向量仍有效 | fingerprint 级联 stale/重提取 |
| R3-15 | 跨分类遍历可能绕过权限 | 每次进入目标分类重新 ACL/预算/health |
| R3-16 | 删除分类语义不清 | 非空默认拒绝，需迁移或显式级联 |
| R3-17 | SaaS tenant 映射可能跨项目 | project/category 到 namespace 强校验 |
| R3-18 | 远程费用不可控 | 分类级费用/配额、软硬门槛 |
| R3-19 | 替换模式可能只是 Graph 检索 | 复用完整 backend capability 门槛 |
| R3-20 | 默认部署风险 | disabled 默认、无强制网络/多模态依赖 |
| R3-21 | 所有分类/小写入都强制 job 会与本地单事务原子实现冲突 | 只读路由不建 job；本地幂等单事务小写入可同步；远程/长耗时/部分副作用操作必须持久 job/tombstone |

## 额外边界检查

- 分类只有 parent 无内容：可路由但不查询 provider。
- node 删除后 edge：同 transaction 失活/删除关联边。
- 多个 source 产生同 label：node identity 不只依赖 label，保留 provenance。
- 自环：默认拒绝，relation type 显式允许时才接受。
- graph depth=0：只返回种子节点。
- provider 超时但其他分类成功：partial=true，不能返回 overall empty。
- inbox 被删除：配置必须指定新的 fallback 分类后才能删除。
- asset 路径移动：视为新 source 或显式 relink，不按文件名猜测。
- 远程删除 SLA 未完成：tombstone 保留且 UI/API 可查询。

## 结论

分类隔离、图证据、全局联邦、多模态路径、SaaS、权限、迁移、删除、费用和替换模式边界已经闭合，可进入函数级设计。
