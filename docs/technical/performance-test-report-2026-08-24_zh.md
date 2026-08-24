# 2026-08-24 性能测试记录

> 范围：`D:\LeAgent` 当前工作区，StAgent `1.2.12`。
>
> 类型：本机离线微基准、前端构建检查、Electron 回归、本地环回 HTTP 测量，以及隔离公开检索评测。
>
> 结论边界：以下数值只描述本机、当前依赖环境和当前工作区。它们不是生产容量承诺，也不代表真实 LLM、真实语料或外部基础设施性能。

## 已测指标

| 子系统 | 工作负载 | 结果 | 判定 |
| --- | --- | --- | --- |
| 缓存 | `bench_cache_hot_paths.py --iterations 10000` | p50 `0.0047 ms`，p95 `0.0057 ms`，`factory_calls=1` | 热路径单飞与命中行为正常 |
| BM25 recall | 400 个内存 lexical candidates，500 次循环 | `2.938 ms/call`；相关记忆排名由第 8 位升至第 1 位；Precision@3=`1.00` | 通过脚本的 `<5 ms/call` 守卫 |
| sqlite-vec | 100,000 条、512 维、batch=256、top-10 | 写入 `160.106 s`，`624.6 rows/s`；单次 ACL 查询 `0.807 s`；数据库 `224,677,888 B`，约 `214.3 MiB` | 扩展可用，返回 10 个命中 |
| 后端启动 | 独立 `StAgent_HOME`、单 worker、本地 SQLite | 冷启动至 `GET /health` 可用 `15.930 s` | 包含首次本地数据库初始化/迁移 |
| 前端生产构建 | `npm run build` | 总墙钟 `48.080 s`；Vite 阶段 `24.700 s` | 构建成功 |
| 前端包预算 | `npm run check:bundle` | JS 总量 `9.64 MiB / 16 MiB`；最大 chunk `3.30 MiB / 3.5 MiB`；140 chunks；Chat GenUI 为 deferred | 通过硬门禁 |
| Electron | `npm run build` | `1.967 s` | 编译成功 |
| 知识检索指标 | 确定性 qrels 夹具、`k=5/10` | Recall@5、Recall@10、Precision@5、Precision@10、NDCG、MRR、p95 计算，以及有界候选池重排 | 指标计算、API 输出与 top-K 候选提升已回归 |
| 公开中文知识检索 | NanoMIRACL-zh 固定 revision、项目内 qrels 评测器、1,700 passages / 200 queries、隔离 SQLite | R@5 `0.720`、R@10 `0.850`、P@10 `0.085`、nDCG@10 `0.599`、MRR@10 `0.520`、p95 `19.748 ms` | 单机词法基线已实测；非业务库或语义检索结论 |

### 检索指标用什么测

Recall@5、Recall@10、Precision@K、nDCG 和 MRR 分成“评估器正确性回归”和“公开集真实运行”两层，不能混为一项测试：

| 层次 | 数据与工具 | 实际作用 |
| --- | --- | --- |
| 评估器回归 | `pytest` + `tests/test_knowledge_evaluation.py`、`tests/test_knowledge_evaluation_api.py` | 用确定性手工 qrels 夹具验证公式、K 截断、API 参数透传和结果字段；不产生业务或公开集质量结论 |
| 公开集实测 | `hotchpotch/NanoMIRACL` `zh` 固定 revision；`backend/scripts/prepare_retrieval_eval.py`；`backend/scripts/run_nanomiracl_zh_eval.py` | 对 `1,700` passage / `200` query / `200` qrel 的隔离 SQLite 运行进行真实 top-K 检索评估 |
| 评分实现 | `StAgent.knowledge.evaluation.evaluate_retriever(...)` | qrels 的 passage 正例经一对一映射为 `DocumentChunk.chunk_id` 后，自动计算 Recall、Precision、nDCG、MRR；执行器另行逐查询计时并汇总 top-10 百分位 |
| 被测系统 | `KnowledgeRetrievalPipeline` 本地 SQLite 中文词法检索：CJK bigram 优先，FTS5 / trigram 只在条件满足时回退；本次未分别统计各路径命中次数 | 对照项是同一数据和查询的 `LexicalRerankProvider`；rerank 候选池最多 `4K`、上限 `400`；向量 embedding、Milvus、OpenSearch、外部 reranker、query rewrite 与缓存均关闭 |

这不是 ChatGPT/LLM 判分，也没有使用上游 `bm25` 预计算候选；后者含为保证覆盖而插入的正例，会污染独立检索评估。真实运行环境为 `uv run --no-sync`、Python `3.12.3`、Windows 11、SQLite `3.45.1`。`results.json` 保存完整 source manifest、映射 SHA、SQLite 运行时和指标；`run_metadata.json` 保存数据 manifest SHA、脚本、Python/平台及源码/锁文件哈希。两者均位于 `downloads/retrieval_eval/runs/nanomiracl-zh-20260824-real-v6-final/`；详见 [知识检索评测说明](knowledge-retrieval-evaluation.md)。

### sqlite-vec 解释

`scripts/bench_sqlite_vec.py` 当前只执行一次 ACL 过滤查询。因此 `0.807 s` 是单次样本，不是 p50、p95 或 p99，不能与其他环境的百分位结果直接比较，更不能据此宣称 100k 规模已满足生产 SLO。

## 本地 HTTP 健康接口

测试服务使用 `python -m StAgent.server --host 127.0.0.1 --port 7861 --workers 1` 启动，数据目录与现有开发数据隔离。只访问 `GET /health`，没有发送聊天请求或调用任何外部模型。

首轮在服务首次达到 healthy 后完成 20 次预热，再以并发 20 发出 200 次请求：

| 并发 | 请求数 | 成功率 | p50 | p95 | p99 | 吞吐 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 20 | 200 | 100% | `123.142 ms` | `351.494 ms` | `351.993 ms` | `133.028 RPS` |

第二轮复用已初始化的数据目录，重新启动服务、预热 20 次后，分别执行 100 次请求：

| 并发 | 请求数 | 成功率 | p50 | p95 | p99 | 最大值 | 吞吐 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 100 | 100% | `4.662 ms` | `5.910 ms` | `6.482 ms` | `12.001 ms` | `199.740 RPS` |
| 4 | 100 | 100% | `14.325 ms` | `35.176 ms` | `36.535 ms` | `36.664 ms` | `226.225 RPS` |
| 20 | 100 | 100% | `109.986 ms` | `160.070 ms` | `160.421 ms` | `160.575 ms` | `162.185 RPS` |

首轮和第二轮差异说明初始化后的本地健康接口仍受启动后后台活动、SQLite 状态和本机调度影响。这里的结果只能用于后续同机、同配置的回归比较。

## 回归与构建验证

| 命令或范围 | 结果 |
| --- | --- |
| `pytest tests/test_perf_benchmark.py tests/bench_recall_bm25.py -q` | `9 passed` |
| `pytest tests/test_knowledge_evaluation.py tests/test_knowledge_evaluation_api.py tests/test_knowledge_quality.py tests/test_knowledge_answer_grounding.py -q` | `30 passed`；覆盖 Recall@5/10、Precision@5/10、NDCG、MRR、p95、端点 K 透传、缓存策略版本、引用 ID 校验和 OpenSearch 测试替身契约 |
| `pytest tests/test_knowledge_retrieval_rerank.py -q` | `6 passed`；覆盖 K+1 词法候选、vector-only 候选与过滤、重排失败降级、top-10 指标提升和重复 chunk 去重 |
| `pytest tests/test_query_engine.py -k 'grounding or knowledge_search' -q` | `4 passed, 44 deselected`；未引用或无证据答案不能绕过严格引用门 |
| `npm.cmd run test -- src/components/canvas/LazyGenUiInline.test.tsx src/components/canvas/LazyGenUiTreeView.test.tsx src/features/workflow/components/WorkflowOperationPanel.test.tsx` | 3 个文件、6 个测试通过，耗时 `2.25 s` |
| `desktop/electron: npm test` | 9 个文件、28 个测试通过，耗时 `433 ms` |
| 知识检索/回答质量回归 | `64 passed` | 包含候选池、重排失败降级、vector-only 筛选、缓存策略版本、ACL/UUID、缓存装配与原有 OpenSearch `_Client` 替身契约 |
| 本轮检索与公开评测回归 | `47 passed` | 覆盖 SQLite CJK 索引同步、空间/文件夹/类型过滤下推、ACL、nDCG 口径、公开数据校验、隔离导入、结果映射/源码哈希、CJK rerank 与候选池 |

### Recall@5、Recall@10 与检索准确率覆盖

`tests/test_knowledge_evaluation.py` 使用手工标注的确定性结果验证评估器本身：

- 两个 case 的预期平均值为 Recall@5=`0.75`、Precision@5=`0.30`、NDCG@5=`0.6532867982`、MRR=`0.75`。
- 相关 chunk 排在第 6 名时，Recall@5、Precision@5、NDCG@5 和 MRR 都必须为 `0`。
- Recall@10 夹具中，第一个 case 在前 10 名命中 3 个相关 chunk，第二个 case 的唯一相关 chunk 位于第 11 名；平均 Recall@10=`0.50`、Precision@10=`0.15`。第 11 名不会计入任何 top-10 指标。
- 只返回 1 个相关结果时，Precision@10=`0.10`，不会按实际返回条数被虚高为 `1.00`。重复相关 chunk 只占用名次，不会重复增加 NDCG；回归值为 `0.9197207891`，避免出现 NDCG 大于 1。
- `tests/test_knowledge_evaluation_api.py` 以独立 FastAPI 路由调用 `POST /api/v1/knowledge/evaluate`：`k=10` 透传为 `SearchOptions.limit=10`，同时保留 `user_id`、`query_rewrite` 与 `rerank`，返回体包含 `precision_at_k`。

检索准确率在这里定义为 Precision@K：

`Precision@K = 前 K 名中唯一相关 chunk 数 / K`

每个 case 独立计算后取算术平均。它评价检索结果，不等同于模型答案的事实正确率。`precision_at_k` 当前是可观察指标，尚无配置化通过阈值，因此不会改变既有 `retrieval_gate_passed`。

### 隔离公开中文检索实测

本轮使用 `hotchpotch/NanoMIRACL` 的 `zh` split，固定 revision `6e98c995a5be54ef41d3a016a2f30314dd625619`。评测在新建的 SQLite、blob 根目录和知识空间中执行，不接触业务知识库；导入了 1,700 passages，建立一对一的 1,700 File/1,700 chunk 映射，200 个 qrels 均通过映射校验。审计工件在 `D:\LeAgent\downloads\retrieval_eval\runs\nanomiracl-zh-20260824-real-v6-final\`：`results.json` 记录固定输入、mapping SHA `f3e68b86e58f76657e895a41da758f2093ef7e8966c30de3d19a634e8da9ae85`、`rag-v1` 和指标，`run_metadata.json` 记录实际源码与 `uv.lock` 的 SHA-256。

| 策略 | R@5 | R@10 | P@5 | P@10 | nDCG@10 | MRR@10 | Hit@1 | p50 / p95 / p99（top-10） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| CJK bigram lexical baseline | 0.720 | 0.850 | 0.144 | 0.085 | 0.599 | 0.520 | 0.375 | `15.706 / 19.748 / 22.913 ms` |
| 同数据/查询 + local lexical rerank（最多 4K 候选） | 0.720 | 0.850 | 0.144 | 0.085 | 0.599 | 0.520 | 0.375 | `15.854 / 19.953 / 22.773 ms` |

两个配置均为 200 个成功查询、0 空结果、0 异常；最终配对比较为 200 个并列。早期完整中文短语 FTS 会得到 200 个空结果，已改为受限 CJK bigram 覆盖排序。随后发现本地 rerank 对文档前 48 个 bigram 截断会使长 chunk 回退，已改为“受限查询词 × 完整文档”检查并用 v6 审计复跑确认无回退。`space_id`、文件夹和类型筛选也已下推到底层候选 `LIMIT` 前，避免其它可见空间候选挤掉目标。`P@10=0.085` 接近该数据单正例口径下的上限 `0.10`，绝不是回答准确率；回答事实准确率未测量。

评测关闭 query rewrite、向量 embedding、OpenSearch、外部 reranker 和缓存。它只反映 1,700 passage 规模的本地 SQLite 词法检索；`retrieval_gate_passed` 使用的通用 `R=0.90` / `nDCG=0.80` 阈值未按此公开集校准，不能作为发布通过或失败结论。

### top-K 候选池优化

此前的 Recall@10=`0.50`、Precision@10=`0.15` 是故意构造的截断反例，不是当前知识库实测。该夹具的两个 case 只标注了 3 个和 1 个相关 chunk，即使全部命中，平均 Precision@10 的上限也只有 `0.20`。因此不能把 `0.15` 当成线上质量基线或发布目标。

实际检索链路存在可证明的候选截断缺陷：底层已取 `4K` 词法候选，但在 ACL 校验、融合和重排前就截断到 K；当词法结果填满 K 时，vector-only 候选也不会参与竞争。修复后：

- 重排前保留最多 `4K` 个候选，硬上限为 400；最终响应和缓存仍只保留 K 个结果。
- query-rewrite 的各变体先各自保留候选池，融合后再有界截断。
- vector-only 候选在最终排序前加入候选池，且继续受 ACL、文件夹和类型筛选约束；重复 lexical chunk 只占一个位置。
- 可选 reranker 异常时安全降级到融合后的 top-K，不会泄露内部候选池。
- 缓存键加入 `candidate-pool-v5` 检索策略版本，旧候选截断、CJK rerank 和 scoped-candidate 逻辑生成的缓存不会遮蔽本次优化。

`tests/test_knowledge_retrieval_rerank.py` 使用隔离 SQLite 元数据和确定性候选验证该行为：

- 初始 K=2 外的相关词法候选可被默认 `LexicalRerankProvider` 提升到最终 top-2。
- 词法 top-K 已满时，vector-only 候选仍可进入重排并被提升。
- 对 10 条低相关候选加 3 条初始排名 11--13 的相关候选，受控 qrels 的结果为 Recall@10=`1.00`、Precision@10=`0.30`、NDCG@10=`1.00`、MRR=`1.00`。
- 同一 lexical chunk 从多个来源重复返回时，不会挤占另一个 top-K 位置。
- vector-only 候选若不匹配文件夹或类型过滤，不会送入 reranker；reranker 故障时仍只输出请求的 K 条。

这些是回归夹具的机制验证，不是现有业务知识库的效果数据。公开 NanoMIRACL-zh 已补充一次真实词法语料测量，但候选池会使重排输入最多增加到原来的 4 倍；业务语料上线前仍必须用固定 qrels 同时比较 Recall@10、Precision@10、NDCG@10、MRR 和 p95。

### 引用结构准确率与答案正确率边界

`tests/test_knowledge_answer_grounding.py` 的 11 项测试已通过。其中刻意构造的“一条有效、一条无效”引用结果得到 `citation_reference_accuracy=0.50`、`strict_acceptance_rate=0.50`，且 `hallucination_rate=None`。这证明 K# 引用 ID 的存在性和严格引用门行为；它不证明引用文本支持答案，也不证明答案事实正确。

`POST /api/v1/knowledge/evaluate` 当前把 `citation_accuracy` 与 `hallucination_rate` 作为请求输入接收，而非从回答与证据自动计算。因此在没有外部人工判定结果时，严格回答门应保持未证实，不能把客户端提交的数值当作真实引用准确率或幻觉率证据。

评估器和 API 夹具覆盖 `evaluate_retriever(...)`、K 透传和 top-K 截断，不查询业务数据库、不调用 embedding。候选池回归使用一次性 SQLite 元数据数据库和确定性候选；两者都不评价当前知识库的实际检索效果。

## 已发现的性能门禁问题

1. `tests/load/ci-perf.sh` 使用 `LEAGENT_TOKEN`，而 `tests/load/locustfile.py` 读取 `StAgent_TOKEN`；其预算变量也未与 Locust 使用的 `StAgent_PERF_P95_BUDGET_MS`、`StAgent_PERF_ERROR_BUDGET` 对齐。因此当前 CI 压测脚本不能作为可信发布门禁。
2. `scripts/BENCHMARK_GUIDE.md` 在 `cd backend` 后多处仍使用 `../scripts/perf_benchmark.py`。正确路径应为 `scripts/perf_benchmark.py`。
3. 前端构建虽然通过硬包预算，Vite 仍报告部分压缩后 chunk 超过 500 kB。该提示不等于门禁失败，但应在下一轮包体积优化中处理。
4. sqlite-vec 基准应增加多查询采样和 p50/p95/p99 输出，才能作为检索延迟回归门禁。
5. `POST /api/v1/knowledge/evaluate` 接受 `k=5`，但其 `retrieval_gate_passed` 仍使用名为 `recall_at_10_target` 与 `ndcg_at_10_target` 的配置阈值。修复为按 K 配置阈值前，不能把该布尔门禁解释为校准过的 Recall@5/NDCG@5 发布结论。
6. `precision_at_k` 已输出但没有单独的配置阈值；当前只可观察，不能作为发布通过/失败结论。
7. `citation_accuracy` 和 `hallucination_rate` 由调用方提交。应改为由版本化回答评测集和独立计算器产出，避免把输入值当成严格回答门的可信证据。
8. `SearchOptions.query_rewrite=False` 在部署配置 `query_rewrite_enabled=True` 时表示“使用默认改写”，不是强制关闭。做纯 BM25 基线时需同时关闭该部署开关，或将该选项改为三态语义。
9. 候选池优化将重排输入从 K 扩大到最多 4K；已在 NanoMIRACL-zh 的单机 SQLite 词法运行中记录 p95，但没有业务语料、向量或外部 reranker 的延迟对比，不能宣称生产延迟已经达标。
10. CJK bigram 路径最多检查 48 个查询词项，并以 SQLite `instr` 覆盖计分；本次只在 1,700 passages 验证，较大业务库仍需单独做 p50/p95/p99 与资源占用测试。

## 未验证范围

- 真实 `POST /api/v1/chat/stream`：需要已启动服务、认证 token、固定 provider/model、提示词和成本上限。可信基线应至少包含 100 次请求、终态 `[DONE]` 校验和并发分档。
- 业务知识库检索：已用公开 NanoMIRACL-zh 测得隔离 SQLite 词法检索，但仍缺少版本化、人工标注且按业务评测用户 ACL 可见的 `relevant_chunk_id` 集。业务库的 Recall@10、Precision@10、NDCG@10、MRR 和 p95 尚未测量。默认环境未启用 embedding、Milvus、外部 reranker 或 OpenSearch；不能自动下载模型或把聊天模型当 embedding 模型。标注不能只从当前 top-10 结果反推。
- 真实答案正确率、引用蕴含性和引用完整性：需要固定问题、原子事实、允许拒答条件、每个事实对应的证据片段及人工判定。随后才能计算事实准确率、引用 precision/recall、幻觉率；LLM 判审只能用于初筛。
- 真实 embedding、Milvus、Redis、PostgreSQL、对象存储和外部模型服务。
- 浏览器 Core Web Vitals、前端/桌面 CPU、内存、帧率、Electron 启动耗时和打包后运行表现。

## 复现命令

```powershell
Set-Location D:\LeAgent\backend
$env:UV_CACHE_DIR = 'D:\LeAgent\.uv-cache'
uv run --no-sync python tests/bench_cache_hot_paths.py --iterations 10000
uv run --no-sync pytest tests/bench_recall_bm25.py -s -q
uv run --no-sync pytest tests/test_knowledge_evaluation.py tests/test_knowledge_evaluation_api.py tests/test_knowledge_quality.py tests/test_knowledge_answer_grounding.py -q
uv run --no-sync pytest tests/test_knowledge_retrieval_rerank.py -q
uv run --no-sync pytest tests/test_document_chunk_filter_pushdown.py tests/test_document_chunk_trigram_sync.py tests/test_document_chunk_search_isolation.py tests/test_file_document_uuid_compat.py::test_knowledge_search_applies_space_membership_acl tests/test_knowledge_evaluation.py tests/test_knowledge_evaluation_api.py tests/test_knowledge_quality.py tests/test_knowledge_retrieval_rerank.py tests/test_public_retrieval_evaluation_import.py tests/test_public_retrieval_evaluation_runner.py tests/test_retrieval_eval_dataset.py tests/test_prepare_retrieval_eval.py tests/test_run_nanomiracl_zh_eval.py -q
uv run --no-sync python scripts/prepare_retrieval_eval.py --verify
uv run --no-sync python scripts/run_nanomiracl_zh_eval.py --run-dir D:\LeAgent\downloads\retrieval_eval\runs\nanomiracl-zh-<new-run-name>
uv run --no-sync pytest tests/test_query_engine.py -k 'grounding or knowledge_search' -q
uv run --no-sync python scripts/bench_sqlite_vec.py --rows 100000 --dimension 512 --batch-size 256 --limit 10

Set-Location D:\LeAgent\frontend
npm.cmd run build
npm.cmd run check:bundle

Set-Location D:\LeAgent\desktop\electron
npm.cmd run build
npm.cmd test
```

本次环境中，普通 `uv run` 的自动同步阶段曾尝试替换 `.venv` 内的 `official_registry.json` 并出现访问拒绝，且没有产生 pytest 汇总。后续命令使用 `--no-sync`，并已确认解释器从当前工作区导入 `StAgent` 源码。这保证了本次指标针对当前源码，但不构成依赖同步成功的证明。

## 工作区处理

本次补充 `precision_at_k`、Recall@5/10、nDCG、MRR 与 p95 的评测口径和回归，修复不足 K 条时 nDCG 的理想排序分母；修复 SQLite trigram 同步、全句中文 FTS 空结果、CJK rerank 长文本截断，以及 space/folder/type 在候选 LIMIT 后过滤造成的候选饥饿。公开评测使用隔离 SQLite 与受管 blob，不写业务库；最终 v6 运行保留一对一 chunk 映射、数据/映射哈希、相关源码/锁文件 SHA-256 和结果。变更尚未暂存；Git 仍会提示无法读取用户全局 ignore 文件。
