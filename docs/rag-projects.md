# 五个高星 RAG 项目与标准版的取舍

本项目既要回答“文档里怎么规定”，也要回答“当次接口返回了什么”。建议行业版本优先补齐原文定位、接口口径和验收题。复杂版式、工作流或图检索可用真实失败样本分别验证，再决定是否引入。

## 上游能力与本项目对照

星数取自 **2026 年 11 月 23 日（北京时间）** GitHub 官方 REST API 的 `stargazers_count`。下表选取五种不同用途。星数反映社区关注度，检索效果需要在目标行业的材料上实测。功能链接固定到观测当日的主仓提交。

| 项目 | 观测星数 | 主仓文档确认的能力 | 标准版现状与可借鉴之处 |
| --- | ---: | --- | --- |
| RAGFlow | [94,573](https://api.github.com/repos/infiniflow/ragflow) | 按文档类型选用分块模板，可视化检查分块，并从回答查看引用。[分块与引用](https://github.com/infiniflow/ragflow/blob/db49df8c1f3dc3a4ac996ab5a04df00f462ebf5d/README.md#L104-L124) | 已保留页码、工作表或段落位置。针对跨页表格、表头重复等失败样本，可先做导入预览，让实施人员核对原表与提取片段。 |
| Dify | [161,714](https://api.github.com/repos/langgenius/dify) | 支持在可视画布上构建、测试工作流，覆盖文档摄取与检索，并能分析应用运行日志。[工作流、检索与监控](https://github.com/langgenius/dify/blob/c971b4c17d7c034b9b871359cecce49a7c86d1ae/README.md#L105-L123) | 当前文档检索、只读接口和综合查询各有固定路径。可先记录各阶段耗时与失败位置，借此区分解析失败、检索无结果和业务接口超时。 |
| LlamaIndex | [52,991](https://api.github.com/repos/run-llama/llama_index) | 提供数据连接器，支持扩展索引、检索器、查询引擎与重排序模块。[开源框架的接口](https://github.com/run-llama/llama_index/blob/99d7433f6f71888951379ccca9294e272442c832/README.md#L89-L97) | 当前用全文索引筛选候选，可选向量相似度参与排序。可独立比较候选召回与重排，特别检查“报账/报销”等近义问题是否在第一步就丢了正确段落。 |
| Microsoft GraphRAG | [36,725](https://api.github.com/repos/microsoft/graphrag) | 局部检索结合实体关系图和原始文本，全局检索汇总多个图社区的报告；上游注明全局查询的资源开销较高。[查询方法](https://github.com/microsoft/graphrag/blob/34b2b67ae4601ad0b47bcd179fbe3f2a23aa0857/docs/query/overview.md#L12-L22) | 当前给出文档片段和业务记录的证据。多份设备规程经常涉及同一部件时，可在小语料中比较图检索与现有检索的引用正确率、建索引耗时和查询成本。 |
| Ragas | [16,404](https://api.github.com/repos/vibrantlabsai/ragas) | 上下文召回检查检索结果覆盖了多少参考答案要点；忠实度检查生成回答中的陈述能否由检索材料支持。[上下文召回](https://github.com/vibrantlabsai/ragas/blob/8c63f10d500c44efbf24fdb9e2920de2104c321c/docs/concepts/metrics/available_metrics/context_recall.md) · [忠实度](https://github.com/vibrantlabsai/ragas/blob/8c63f10d500c44efbf24fdb9e2920de2104c321c/docs/concepts/metrics/available_metrics/faithfulness.md) | 现有离线用例已经核对意图、引文、原件摘要和接口快照。行业评测可再分别统计“是否找全关键条款”和“回答是否有据”，对金额、计量单位和版本用确定值核对。 |

本地对照依据包括[文件解析](../ragcore/documents.py)、[检索与问答流程](../ragcore/engine.py)、[候选检索与原文摘录](../ragcore/retrieval.py)和[离线验收](../ragcore/evaluate.py)。

## 行业版本如何验收

| 场景 | 建议交付 | 可复核的验收问题 |
| --- | --- | --- |
| 科研财务 | 由财务确认预算执行率定义、金额单位、统计年度和零分母规则。分别准备制度原件、接口样例和答案。 | 问同一项目同一年度的执行率及计算依据，同时核对制度条款、筛选条件和接口快照；把预算改为零，验证约定的展示口径。 |
| 制造设备 | 从同型号设备的两个规程版本和一份交接记录开始，标注设备编号、适用版本、页码及对应字段。真实业务接口就绪后再加入记录查询。 | 询问交接必填项并打开对应原文；再用另一型号或过期版本提问，检查引用会否串用。现有[制造业示例](../profiles/manufacturing.json)可用于演示文档路径。 |

每次改分块或排序，使用同一组行业题分别记录正确证据是否入选、答案是否引用正确版本、数值是否与接口一致。可先复用[现有验收题格式](../eval/seed_cases.jsonl)，新增题由业务人员标注。这样能在决定是否引入上游组件前，看到具体改善了哪些题，还有哪些问题需要修订原始资料或接口口径。
