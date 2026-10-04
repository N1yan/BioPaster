# TREC 2019 混合检索评估

中文 | [English](trec2019_chroma_retrieval.md) · [建库与 MCP 教程](../examples/chroma_mcp/README.zh-CN.md)

在同一批 **8,567 篇临床试验**上比较纯向量、BM25 和 RRF 混合检索。固定随机种子 **42**，从 38 个有标注的问题中抽取 10 题：**2, 7, 8, 9, 15, 16, 18, 22, 24, 29**。所有方法使用这同一组查询。[查看查询文本](assets/trec2019/hybrid/topics.csv)

## 测试方法

| 项目 | 设置 |
| --- | --- |
| 查询 | `topics2019.xml` 中的疾病、基因/变异、年龄和性别，用空格拼接 |
| 标注 | `qrels-treceval-trials.38.txt`；32、33 题没有标注，不参与抽样 |
| 向量 | BGE-small-en-v1.5，归一化向量，cosine 距离；43,459 个带背景的正文片段 |
| BM25 | 每篇完整 Markdown 为一条文档；小写后按 `[a-z0-9]+` 分词 |
| 候选 | 向量侧逐步增加片段数，按 NCT 去重后取 100 篇；BM25 取 100 篇 |
| 融合 | `EnsembleRetriever`，两路权重各 0.5，RRF `c=60`，按 NCT ID 合并 |

P@10 和 nDCG@10 评价前 10 篇试验；Recall@100 评价前 100 篇。等级 1、2 都算相关，未标注按 0 计；nDCG 使用 `2**grade - 1` 作为增益。Recall 的分母为该题全部已标注相关试验数。每项指标先逐题计算，再对这 10 题平均。

## 结果

| 方法 | P@10 | nDCG@10 | Recall@100 |
| --- | ---: | ---: | ---: |
| 向量 | 48.00% | 0.4356 | 38.69% |
| BM25 | **63.00%** | **0.5726** | 42.37% |
| 混合 RRF | 59.00% | 0.5394 | **44.20%** |

![三种检索方法的指标](assets/trec2019/hybrid/metrics_comparison.png)

BM25 的前 10 名质量最好，混合检索的前 100 篇覆盖率最高。因此，这次等权融合并没有全面优于 BM25。

![混合检索相对纯向量的逐题P@10变化](assets/trec2019/hybrid/per_topic_change.png)

逐题图以纯向量为参照：混合检索有 **6 题提高、1 题下降、3 题不变**。它不表示混合检索优于 BM25。

## 复现

打开 [05_evaluate_retrieval.ipynb](../examples/chroma_mcp/05_evaluate_retrieval.ipynb)，在 **Configuration** 中设置原文、两个索引和输出路径，依次运行单元。已有执行输出和图表保存在 Notebook 中。抽样设置：

```python
sample_size = 10
sample_seed = 42
candidate_count = 100
```

运行时加载现有索引，不重新建库。结果文件包括 [逐题指标](assets/trec2019/hybrid/per_topic.csv)、[3,000 条排名](assets/trec2019/hybrid/ranking.csv)、[候选数量](assets/trec2019/hybrid/candidates.csv)和[完整配置与输入文件哈希](assets/trec2019/hybrid/summary.json)。

本轮评估每路取 100 篇不同试验；MCP 服务仍在向量侧取 100 个片段后去重，因此本报告对应的是 Notebook 的候选设置。语料是 qrels 涉及的试验子集，结果不能直接与官方全量 TREC 成绩比较；未标注也不等于不相关。[TREC 数据来源](https://pages.nist.gov/trec-browser/trec28/pm/data/)
