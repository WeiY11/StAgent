# NanoMIRACL-zh 知识检索评测

本文件定义公开数据集在 LeAgent 知识检索中的可复现准备和评测边界。它不把下载成功、单元测试通过或 `Precision@10` 称为答案准确率。

## 固定数据身份

| 项目 | 固定值 |
| --- | --- |
| 数据集 | [`hotchpotch/NanoMIRACL`](https://huggingface.co/datasets/hotchpotch/NanoMIRACL) |
| 语言与 split | `zh` |
| 固定 revision | `6e98c995a5be54ef41d3a016a2f30314dd625619` |
| 必需配置 | `corpus`、`queries`、`qrels` |
| 排除配置 | `bm25` |
| 默认下载目录 | `D:\LeAgent\downloads\retrieval_eval\nanomiracl-zh\6e98c995a5be54ef41d3a016a2f30314dd625619\` |

不要使用浮动的 `main`，也不要混入同仓库中旧的 `NanoMIRACL-zh...` 布局。固定 revision 下，评测读取三个表：`corpus` 的 `_id`/`text`、`queries` 的 `_id`/`text`，以及 `qrels` 的 `query-id`/`corpus-id`。`bm25` 是上游预计算候选池，其中包含为保证候选覆盖而插入的正例；把它作为 LeAgent 的检索输入会污染独立评测。

`downloads/` 被仓库忽略。原始数据、导入映射和结果不应提交到 Git；版本、文件哈希和运行配置必须写进本次评测的 manifest 或结果记录。

## 本次用什么测评

本次真实检索指标由项目内评测链路计算，不使用大模型判分，也不把上游 `bm25` 预计算候选当作检索结果。

| 角色 | 实际使用 | 用途 |
| --- | --- | --- |
| 公开金标 | `hotchpotch/NanoMIRACL` 的 `zh` `corpus`、`queries`、`qrels` | `qrels` 给出每个问题对应的 passage 正例；本次为 `1,700` passages、`200` queries、`200` qrels |
| 数据准备与校验 | `backend/scripts/prepare_retrieval_eval.py` | 下载固定 revision，并以 `--verify` 校验行数、文件 SHA-256 和 qrels 引用完整性 |
| 隔离评测执行器 | `backend/scripts/run_nanomiracl_zh_eval.py` → `StAgent.knowledge.evaluation_runner.run_isolated_public_retrieval_evaluation(...)` | 建立新的 SQLite、blob 根目录和知识空间，导入受管 File / FileContent / DocumentChunk，生成一对一 passage-to-chunk 映射后执行查询 |
| 指标计算器 | `StAgent.knowledge.evaluation.evaluate_retriever(...)` | 以 qrels 映射后的 `chunk_id` 为相关性金标，分别在 `k=5`、`k=10` 计算 Recall、Precision、nDCG、MRR；执行器另行逐查询记录 top-10 延迟并汇总 p50/p95/p99 |
| 被测检索链路 | `KnowledgeRetrievalPipeline` 的本地 SQLite 中文词法路径：CJK bigram 优先，FTS5 / CJK trigram 只在条件满足时回退；本次未分别统计各路径命中次数 | 同一数据和查询另跑本地 `LexicalRerankProvider` 对照；rerank 候选池最多 `4K`，上限 `400` |
| 实际运行环境 | `uv run --no-sync`、Python `3.12.3`、Windows 11、SQLite `3.45.1` | 数据 manifest SHA、脚本、平台和 20 个相关源码/锁文件 SHA-256 在 `run_metadata.json`；完整 source manifest、映射 SHA、SQLite 版本和指标在 `results.json` |

因此，`Recall@5`、`Recall@10` 和 `Precision@K` 都是“检索结果是否找回 qrels 正例”的自动计算结果。它们不评价回答文字是否事实正确；`results.json` 中的 `answer_accuracy` 仍为 `null`。

## 下载与校验

此命令会显式访问 Hugging Face Dataset Viewer，下载并校验固定 revision 的 `corpus`、`queries` 和 `qrels`，然后生成 `corpus.jsonl`、`queries.jsonl`、`qrels.jsonl` 与 `manifest.json`。它拒绝覆盖已经存在的输出目录。

```powershell
Set-Location D:\LeAgent\backend
uv run --no-sync python scripts/prepare_retrieval_eval.py
```

默认输出目录即上表所列目录。`--output-dir` 必须仍在 `D:\LeAgent\downloads\retrieval_eval\` 下，且必须是尚不存在的目录：

```powershell
uv run --no-sync python scripts/prepare_retrieval_eval.py `
  --output-dir 'D:\LeAgent\downloads\retrieval_eval\nanomiracl-zh\6e98c995a5be54ef41d3a016a2f30314dd625619-rerun'
```

已下载副本不需要联网即可复核清单 SHA-256、行数和 qrels 引用完整性：

```powershell
uv run --no-sync python scripts/prepare_retrieval_eval.py --verify
```

该 CLI 的边界是刻意收紧的：它**不**导入知识库、不写 `document_chunks`、不下载或选择 embedding 模型、不调用 reranker，也不运行 `evaluate_retriever`。因此它的成功输出只证明固定上游数据被完整保存并通过模式校验，不是任何 Recall、NDCG、延迟或业务质量结论。

## qrels 到 LeAgent chunk 的映射

NanoMIRACL 的 `corpus-id` 是上游 passage/document 标识，qrels 也是这个粒度；而 `evaluate_retriever(...)` 只接受 LeAgent `chunk_id`。两者不能直接混用。

真实评测前，必须由受管 `File` / `FileContent` / `DocumentChunk` 导入和索引流程生成可审计映射，并满足以下一对一约束：

```text
上游 corpus-id → 一份本地 File → 一个且仅一个本地 DocumentChunk.chunk_id
```

- 每个上游 corpus passage 必须映射到一个唯一的本地 chunk；`0` 个或超过 `1` 个 chunk 均应使转换失败。
- 不同 `corpus-id` 不能映射到同一个本地 `chunk_id`。
- 不得把一个上游 passage 的所有分块都填入 `relevant_chunk_ids`。这会把文档级 qrels 伪造成多个独立相关 chunk，改变 Recall、NDCG 和 Precision 的分母与含义。
- 映射至少保存 `dataset_id`、固定 revision、`corpus-id`、`file_id`、`chunk_id`、源文本与 chunk 文本的 SHA-256、chunking/index 版本。重新提取或重新分块后必须重新生成映射。
- 评测检索必须使用导入时有权访问该数据的同一用户/ACL 视角；否则空结果反映的是权限而不是检索质量。

当前 `StAgent.knowledge.evaluation_dataset.build_evaluation_cases(...)` 会拒绝不完整映射、一个来源对应多个 chunk、或多个来源复用同一个 chunk。若业务需要允许一个 passage 分成多个 chunk，应先扩展评估器为 document/file 级相关性计算；不要放宽现有 chunk 级 qrels 转换。

## 指标解释

当前固定 revision 的 NanoMIRACL-zh 每个保留 query 只有一个正例 qrel。对于完整且正确的一对一映射：

```text
Precision@10 = top 10 中的相关 chunk 数 / 10
```

因此单个 query 即使把唯一正例排在第 1 名，`Precision@10` 最高也约为 `0.10`；返回不足 10 条时，分母仍是 10。它不是“准确率低”，也不是模型回答的事实正确率。

这里应把 `Precision@10` 称为“检索精确率”，并与以下指标一起报告：

- Recall@5 / Recall@10：唯一正例是否被召回；
- MRR、nDCG@10、Hit@1 / Hit@5：首个正例和排序位置；
- p50 / p95 检索延迟、失败率、候选池和 rerank 配置：性能代价；
- 答案事实准确率、引用蕴含性、幻觉率：需要独立的回答级金标，不能由 qrels 或 Precision@10 推导。

运行记录必须同时写入：数据集 ID、revision、manifest 哈希、映射哈希、LeAgent commit、索引/分块版本、评测用户、`k`、query rewrite、rerank 与向量后端状态。仅比较数值而不固定这些条件，不构成可复现实验。

## 已执行的隔离公开评测（2026-08-24）

最终审计记录位于 `D:\LeAgent\downloads\retrieval_eval\runs\nanomiracl-zh-20260824-real-v6-final\`；该目录被 Git 忽略。运行创建了全新的 `evaluation.sqlite3` 与 blob 根目录，不使用业务知识库。`results.json` 保存数据、映射与指标，`run_metadata.json` 保存每个相关源码、锁文件和脚本的 SHA-256。固定数据、受管导入和映射检查结果如下。

| 项目 | 实际记录 |
| --- | --- |
| 数据 | NanoMIRACL-zh，固定 revision `6e98c995a5be54ef41d3a016a2f30314dd625619` |
| 输入校验 | corpus `1,700`、queries `200`、qrels `200`；source manifest SHA-256 `b18496eaed202ff489f442524da392bd3d5eda652af1d7c12cbe99e98694b347` |
| 映射 | `1,700` 个受管 File、`1,700` 个 chunk；重复 chunk `0`；每个 passage 恰有一个 chunk；mapping SHA-256 `f3e68b86e58f76657e895a41da758f2093ef7e8966c30de3d19a634e8da9ae85` |
| 运行时 | SQLite `3.45.1`；索引版本 `rag-v1`；分块策略 `one-passage-one-chunk-v1`；CJK trigram 的 insert/update/delete 同步触发器均存在 |
| 刻意关闭 | 向量 embedding、OpenSearch、外部 reranker、query rewrite、缓存 |
| 对照 | 词法 CJK bigram 基线；同一数据和查询的本地 `LexicalRerankProvider` 对照，rerank 候选池最多 `4K`（上限 `400`） |

中文问题不再作为一个完整短语送给 SQLite `unicode61`。受限的重叠 bigram 在 SQL 的 ACL 和知识库范围过滤之后计分；这是一条零依赖的词法兜底路径，不是语义检索，也不会下载或选择模型。

| 配置 | Recall@5 | Recall@10 | Precision@5 | Precision@10 | nDCG@5 | nDCG@10 | MRR@5 | MRR@10 | Hit@1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| lexical baseline | 0.720 | 0.850 | 0.144 | 0.085 | 0.556 | 0.599 | 0.502 | 0.520 | 0.375 |
| local lexical rerank | 0.720 | 0.850 | 0.144 | 0.085 | 0.556 | 0.599 | 0.502 | 0.520 | 0.375 |

两条配置均完成 200 个查询，空结果 `0`、异常 `0`。top-10 详细测量的延迟分别为：baseline p50/p95/p99 = `15.706/19.748/22.913 ms`，local rerank = `15.854/19.953/22.773 ms`。配对比较为 200 个并列，Recall@10 与 MRR@10 的差均为 `0`；它只证明当前本地词法 rerank 不再损伤中文排序，不证明 rerank 带来增益。

`Precision@10=0.085` 不是回答准确率低：此 revision 每个 query 只有一个正例，所以 `Precision@10` 的理论上限就是 `0.10`。回答事实准确率没有测量，结果文件明确记为 `null`；需要独立的问答级金标。

### 已修复的评测和排序问题

- 首轮隔离运行曾得到 `200/200` 空结果和全零指标。根因是完整中文问题被当作必须原样出现的 FTS 短语。该结果只保留为失败诊断，不能当作质量基线。
- 本地 rerank 的第一版只从文档前 48 个 CJK bigram 取词，长 chunk 后部的相关词会被忽略。加入长文本回归后改为：最多 48 个查询 bigram 逐项检查完整文档。最终 v6 配对运行确认不再回退。
- nDCG 的理想 DCG 已按请求的 `k` 而非实际返回条数计算；返回不足 K 条时不会被错误抬高为 1。
- `space_id`、文件夹和文件类型已下推到 CJK、FTS、trigram 和词法查询的 `LIMIT` 前；上层仍重复校验 ACL 与文件元数据。真实 SQLite 回归验证错误空间/文件夹/类型的高分候选不能挤掉目标结果。

### 解释边界

这是一次固定公开集、单机、单次配对运行。代码中的 `recall=0.90`、`nDCG=0.80` 等通用 gate 尚未按这个数据集校准，`retrieval_gate_passed=false` 不应被解释为发布失败。本次仅能说明 1,700 passage 规模的 SQLite 词法检索表现；CJK bigram 路径对每个查询最多检查 48 个词项并用 `instr` 覆盖计分，较大业务库仍需单独压测。OpenSearch 与不支持 `allowed_file_ids` 的向量后端仍只能在候选返回后做二次范围过滤；它们在本次关闭，需扩展其候选过滤协议后才能声明 scoped recall。真实业务语料、embedding/Milvus、外部 reranker、回答事实准确率和引用蕴含性均未在本次证明。

## 许可与署名

NanoMIRACL 数据集卡标记为 `other`，并明确说明它是派生数据，使用者必须遵守上游数据集的许可、条款和署名要求。不要因为任一上游代码仓库使用 Apache-2.0，就把 NanoMIRACL 或下载副本重新标注为 Apache-2.0。

- 记录并保留 [NanoMIRACL 数据集卡（固定 revision）](https://huggingface.co/datasets/hotchpotch/NanoMIRACL/blob/6e98c995a5be54ef41d3a016a2f30314dd625619/README.md) 与上游 [`hotchpotch/miracl-hf-unified`](https://huggingface.co/datasets/hotchpotch/miracl-hf-unified) 的来源链接。
- 对外展示、论文、报告或分发派生评测工件前，按上游要求署名 MIRACL；可引用 [MIRACL 论文](https://doi.org/10.1162/tacl_a_00595) 和 [项目仓库的 citation 指引](https://github.com/project-miracl/miracl#-citation)。
- 公开再分发、商业使用或把下载文本加入产品前，单独复核数据卡、上游条款和组织的数据治理要求。本仓库的下载脚本及 Git ignore 规则不替代许可审查。
