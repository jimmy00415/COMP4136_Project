# Core source publication note

Supplement: the independent [5 October challenge evaluation](CHALLENGE_RESULTS.md) adds harder tasks and a paired BM25 baseline. Its cases, safe answer projections and AI-reviewed summary are published separately. The original 30-case plan and results below are preserved; its no-baseline statement refers only to that original study.

The plan below describes the actual completed metadata study. Its data and raw result files remain in the author's local course workspace and are not included in this core-code publication. Install the application and run offline source/unit tests as documented in START_HERE.md. Do not infer that referenced study data or receipts are present in this repository.

---

# 最小测试与报告方案

目标是回答：当前港片系统能否根据电影资料完成事实问答、满足明确推荐条件，并在证据不足或问题超出范围时拒答？系统源码和工程均属于用户为 COMP4136 原生开发的贡献。

| 组别 | 数量 | 检查方法 |
| --- | ---: | --- |
| 事实问答 | 10 | 导演、上映年份或类型是否包含资料中的正确值，是否引用对应电影的 metadata |
| 条件推荐 | 10 | 每题推荐 3 部；按本地 catalog 核验导演/演员/类型/年份，检查去重与引用身份 |
| 拒答或证据不足 | 10 | 6 个非电影问题、4 个资料不支持的电影问题；拒答文字与引用范围做自动初检，再逐条阅读 |

数据：4,659 条结构化电影记录。问题与 gold 已写入 `data/cases.jsonl`，哈希记录在 `preparation/input-pins.json`。推荐 gold 包含全部符合条件的 ID，不要求唯一排序。事实题含同一电影的不同字段；30 个案例是作者挑选的诊断样本，不能作为总体随机样本或新独立 benchmark。

原始 PDF、PDF 文本、向量导出均不需要。本地评估 catalog 不包含 PDF。现有服务器仍有 5 份文档的 21 条 PDF passage；本轮提问只针对 metadata，若返回 PDF 引用，将作为超出本轮证据范围记录，不能宣称服务已被改为只含 metadata。

准备时仅访问 `/health` 与 `/api/config`。正式运行前重新验证 release、manifest、revision、镜像和两项 policy 哈希，以及 embedding/generation 模型。正式运行按 F01–F10、R01–R10、A01–A10 顺序，history 为空，最多 30 次串行逻辑请求，每次超时 45 秒，不自动重试、不新增云资源。三次连续请求失败停止；不把未执行案例算成科学实验结果。运行结束再次检查配置。

保存原始 JSON、HTTP 状态、错误类别与单次请求耗时。HTTP 失败与答案不合格分开；完整执行时按各组 10 题报告机械检查通过数，再人工阅读所有回答并记录修正与案例分析。这里的“人工阅读”若由 Root AI 完成必须明确写成 AI qualitative review；真正 human audit 保持 pending。未实际测量 token 或费用时填“未测量”。只报告观察到的响应耗时中位数与最大值，不作稳定性能或吞吐量结论。

自动规则仅检查答案中的 gold 文字、电影条件与引用身份，不证明语义完全正确、引用段落完全支持答案或不存在额外幻觉。报告应展示自动指标与逐条审查的差异。部分 metadata 回答由确定性路径产生，不能把正确答案统称为 LLM 推理效果。没有对照或消融实验，因此不声称比 dense RAG 更好或证明某一模块带来因果改进。

报告结构：摘要、背景与相关工作、系统架构与原生工程贡献、数据与测试设置、真实结果、成功与失败案例、局限、结论和 IEEE 风格参考文献。无需沿用旧 memory 报告模板。先产出报告，再按课程 10 分钟要求准备演示；成员信息暂留空，不能填写虚构组号、姓名、学号或标记已提交。

准备阶段状态曾为正式问答请求 0。用户随后授权启动；本轮现已完成 30 次请求及 Root AI 逐条审查，详见 `results/study-01/TEST_RESULTS.md`。报告 PDF 与演示按最新要求暂缓。 既有仓库里的历史 evals 是工程历史数据，不属于本轮 30 题结果。
