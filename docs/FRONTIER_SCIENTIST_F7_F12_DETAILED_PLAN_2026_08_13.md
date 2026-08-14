# Aletheia F7–F12：通向自主前沿科学家的总体规划与详细执行计划

**日期：** 2026-08-13<br>
**状态：** 提议中的主计划；后续实现应以本文件为第 7–12 项的范围与验收依据<br>
**当前基线：** Real Campaign Gate v1 已通过工程/协议验收；真实科学结果因外部数据不支持而
诚实归档为 results_rejected<br>
**适用范围：** 从“可审计的自主计算研究系统”推进到“领域级自主前沿科学家候选”

---

## 0. 执行摘要

Aletheia 已经证明它可以：

- 在硬隔离、无网络的容器中执行 AI 编写的代码；
- 让固定 harness 而不是模型自己决定实验是否成立；
- 将探索、逐轮确认、最终留出和外部验证分开；
- 在负结果出现后更新信念并继续或停止；
- 保存代码、数据身份、统计预注册、评论和结论的可审计轨迹；
- 在内部结果成立、外部结果不成立时拒绝最终科学结论。

这使它跨过了“自动写实验报告”的阶段，但尚未证明它能够自主选择重要的新问题、形成并
区分机制解释、在开放环境中长期保持研究方向，或用独立现实实验产生新知识。

第 7–12 项不是六个平行功能，而是一条有依赖关系的能力链：

~~~text
F7  Frontier Discovery Gate
     └── 给所有后续能力建立不可自评、不可挑结果的统一尺子
          │
F8  Knowledge Boundary Engine
     └── 确定“已知什么、未知什么、候选主张与最近前人工作的精确差异”
          │
F9  K3 / Competitive Causal World Model
     └── 同时维护竞争解释，并选择最能区分它们的实验
          │
F10 Open Experiment Engine + Materials Deep Domain
     └── 把可执行行动从表格回归扩展到结构、模拟、干预和机制实验
          │
F11 Long-Horizon Research Portfolio
     └── 在多日、多分支、多预算约束下保持科学与运行一致性
          │
F12 Reality Bridge + Independent Replication
     └── 将锁定协议接到真实实验和第二独立站点，获得现实世界裁决
~~~

程序的核心判断是：

> 下一步首先应建立 F7，而不是接入更多模型。没有独立验收门，后续能力只会让系统更快地产生
> 候选结果，却无法证明它更可靠地产生了新知识。

---

## 1. 命名、范围与历史文档关系

### 1.1 规范编号

本计划使用 F7–F12，其中 F 表示 Frontier program，避免与历史路线图中的 K1–K5 混淆。

- 历史 K1：探索到确认的 demonstration seal，已完成。
- 历史 K2：campaign learning loop，已完成并真实验证一次。
- 历史 K3：知识 grounding 的早期设想，现在由 F8 完整接管。
- 本文 F9 中的 “K3” 指 epistemic world model 的第三代能力，即从 K2 的单命题 Beta 信念
  升级为竞争性因果世界模型；其规范编号始终是 F9。

后续 issue、分支、事件和验收脚本应使用 F7、F8……F12，不再单独使用含混的 K3。

### 1.2 本计划不重新打开的已完成工作

除非 F7 发现回归，否则本阶段不重新设计：

- authored-code hard Docker sandbox；
- Epistemic Seal v2 和逐轮独立 confirmation batch；
- one-time final holdout；
- locked-code external evaluation；
- K2 reason → prediction → harness verdict → belief update 基本闭环；
- cross-vendor author-excluded critic 方向；
- claim/evidence ledger 基础结构。

### 1.3 本阶段的非目标

本计划不以以下结果作为成功标准：

- 生成更多或更漂亮的论文；
- 单次击中一个正结果；
- 在公开 benchmark 上 best-of-N 取最好成绩；
- 让多个模型互相投票后称为“独立证据”；
- 仅增加新数据集适配器便宣称增加了新科学领域；
- 让 AI 自主采购、发布、进行危险实验或绕过伦理与安全审批；
- 以机器人替代人作为“自主科学家”的必要条件。

---

## 2. 最终能力定义与两级毕业标准

### 2.1 领域级自主前沿科学家候选

人类只提供一个宽泛 scientific quest、资源、数据接入和安全边界。系统需要自主完成：

1. 建立带可测覆盖率的知识边界；
2. 提出多个相互竞争、可被反驳的解释；
3. 选择高信息增益而非只提高指标的实验；
4. 锁定预测、协议、代码和分析计划；
5. 获取新数据并接受固定 harness 的裁决；
6. 根据负结果放弃、修订或缩小理论；
7. 在独立数据或独立实验站点复现；
8. 生成逐句可追踪的研究 bundle；
9. 披露所有尝试、失败、偏差和资源消耗。

### 2.2 通用自主前沿科学家候选

在没有新增任务特定硬编码科研路径的前提下，在第二个明显不同的领域重复上述过程。允许增加
新的领域工具和安全适配器，但不允许把目标答案或特定候选主张写进 harness。

### 2.3 “可靠”一词的额外要求

一次成功只能称为 capability demonstration。只有满足以下条件才可称为 reliable：

- 至少三个前瞻性 campaign，而不是从历史运行中挑一个；
- 至少一个结论在冻结知识快照下通过新颖性审计；
- 至少一个机制结论通过竞争解释的判别实验；
- 至少一个结论由独立数据源或第二实验站点复现；
- 对完整 campaign 组合报告成功率、错误发现率和校准，而不是只报告成功项目；
- 第三方能从 bundle 复算主要结论；
- 关键 fail-closed 不变量在全部运行中零违规。

---

## 3. 全阶段共同不变量

下面的不变量优先级高于任何单项能力和性能提升。

### I1. 提议者不能成为裁判

LLM 可以提出问题、假设、代码、实验和解释；独立 evaluator、固定统计 harness、现实仪器或
隐藏环境决定结果。模型评论只能成为 critique evidence，不能成为 empirical evidence。

### I2. 预测必须先于观察

任何会影响主张的预测、阈值、分析计划、排除规则和停止规则，必须在对应 observation
可访问前完成内容寻址提交。提交后只能产生新版本，不能原地修改。

### I3. 所有尝试进入 family ledger

失败代码、无效实验、样本不足、被 critic 拒绝和主动放弃的分支都计入尝试族。不得通过重命名
问题、重启 run 或删除分支逃避多重比较与失败披露。

### I4. 未知状态不能升级为否定或肯定

retrieval unavailable、coverage insufficient、not evaluated、invalid measurement 和
sample starved 必须保留为独立状态，不能折算成 “未发现前人工作”“不成立”或 “控制通过”。

### I5. 新颖性是搜索协议的结果，不是检索器的自信

“没有搜到”永远不等于 novel。强新颖性主张必须给出搜索版本、覆盖证据、最近前人工作、
精确差异和时间边界。

### I6. 机制主张必须面对竞争解释

相关性、性能提升、特征重要性或单个消融不能单独支持机制。机制至少需要 null、主解释和一个
可信替代解释，并有预注册的判别预测。

### I7. 负结果是一等产物

负结果可以增加知识，只要它排除了某个预测、缩小了假设空间或暴露了测量失败。系统不得以
重新调参直到转正作为默认响应。

### I8. 评价平面与研究平面隔离

研究代理不能读取隐藏答案、scorer、测试资产、人工 rubric 或后续时间段文献。evaluator
只接收提交 artifact，不接收模型的隐式思维过程。

### I9. 原始证据不可变

原始数据、仪器文件、代码、容器、模型版本、prompt、tool schema、split 和评审输入均以
hash/version 标识。转换生成新 lineage，不覆盖旧对象。

### I10. 现实世界动作保持人类治理

实验设计可以自主；采购、危险化学品、人体/动物研究、外部发布、云实验室提交和不可逆动作
继续要求授权。科学自主不扩大伦理与运营权限。

---

## 4. 共同工程底座

F7–F12 会新增大量状态对象。不能继续依赖 create_all 和零散 ALTER TABLE。以下工作在 F7
开工时前置，后续各项共同复用。

### PF-1. 版本化 schema 与 migration

交付：

- 引入 Alembic，给当前数据库建立已审计 baseline revision；
- 所有新表、索引、约束和枚举变化只能通过 migration；
- CI 覆盖空库 upgrade、生产快照副本 upgrade、downgrade 可行性或明确不可逆说明；
- migration 与应用代码带 schema compatibility version；
- 备份/恢复演练留下机器可读 receipt。

完成条件：

- 从空 Postgres 可一次升级到最新；
- 从当前 schema 的脱敏副本升级后，既有 run 和 Real Campaign Gate 证据 hash 不变；
- 应用遇到过旧或过新 schema 时 fail closed，不自行改表。

### PF-2. Run Manifest v1

每次正式运行在首个科学动作前冻结：

- git tree/patch identity；
- Python/Node 依赖锁和 SBOM；
- sandbox image digest；
- orchestrator、critic、embedding 和专用科学模型的精确版本；
- prompts、tool schemas、domain capability manifests 的 hash；
- 原始/标准化数据、row identity 和 split ledger；
- evaluator/harness 版本；
- 预算、安全策略和批准记录。

任何变化产生新 manifest lineage。resume 只能在兼容策略允许时继续；否则 fork 新 run。

### PF-3. Typed artifact envelope

所有新增 artifact 使用统一 envelope：

~~~text
artifact_id
artifact_type
schema_version
content_sha256
producer
producer_version
run_id / quest_id / experiment_id
parent_artifact_ids
created_at
data_classification
license_or_usage_policy
payload_uri
validation_status
validation_receipt
~~~

这不是替换现有 Artifact 表，而是将其从 uri 指针升级为可验证对象。

### PF-4. 两类“完成”必须分开

每项都有：

- **Engineering complete：** 功能、测试、文档和验收工具已经正确实现；
- **Scientific exit：** Aletheia 在冻结的真实/隐藏任务上达到该能力标准。

工程完成不能因科学结果为负而失败；科学退出不能因软件测试通过而自动成立。

---

## 5. 总体依赖与建议排期

### 5.1 依赖图

~~~text
PF-1..PF-3
   │
   └── F7 ───────────────┬─────────────────────────────────────┐
                         │                                     │
                         └── F8 ─── F9 ─── F10 ─── F11 ─── F12
                              │       │       │       │
                              └───────┴───────┴───────┘
                              所有阶段持续回归 F7
~~~

F11 的完整 portfolio 能力排在 F10 后，但 durable job、lease、migration 等可靠性基础可以
提前施工；不得因此提前开放长期科学自治。

### 5.2 粗略工作量

以下是 2–3 名工程/研究人员、已有 Aletheia 基础之上的量级估算，不是日期承诺。单人推进通常
需要 1.5–2.5 倍日历时间；F12 由外部实验条件主导。

| 阶段 | 主要依赖 | 工程量级 | Scientific exit 的额外条件 |
|---|---|---:|---|
| PF + F7 | 当前基线 | 4–6 周 | 冻结私有任务并完成多次基线运行 |
| F8 | F7 schema | 6–10 周 | temporal holdout 上低 false-novelty |
| F9 | F8 claim graph | 6–10 周 | 隐藏世界中优于 K2/单假设消融 |
| F10 | F8 + F9 核心接口 | 10–16 周 | 一个结构/模拟/机制材料 campaign |
| F11 | F9 + F10；可靠性底座可提前 | 8–12 周 | 72 小时故障注入与多分支研究运行 |
| F12 | F10 + F11 | 3–9 个月 | 独立站点或等价强度的盲法复现 |

### 5.3 发布节奏

每项遵循同一节奏：

1. RFC/ADR 和 threat model；
2. 纯数据结构与 deterministic primitive；
3. ledger/migration；
4. 隔离执行与 driver wiring；
5. offline fixtures；
6. adversarial tests；
7. frozen live acceptance；
8. 证据 bundle 和复盘；
9. 达标后才作为下一阶段强依赖。

---

# F7 — Frontier Discovery Gate v1

## F7.1 目的

建立独立于 Aletheia 研究循环的统一能力评估平面，回答：

- 它能否找到并正确引用关键文献？
- 能否复现已有研究，而不只是写出可运行代码？
- 能否在隐藏规律环境中设计真正有辨识力的实验？
- 能否提出并实现优于强基线的新方法？
- 能否在负结果和不确定性下保持校准？
- 完整系统是否优于相同基础模型的直接 agent 和去掉 K2/K3 的消融版本？

F7 是后续每项的回归门，不是一次性 benchmark。

## F7.2 非目标

- 不用单一 LLM judge 总分代表科学能力；
- 不把公开 benchmark 的训练污染结果当成最终证据；
- 不以 pass@k 或 best run 代替单次可靠性；
- 不允许 Aletheia 根据隐藏测试反复改 prompt 后继续使用同一 test；
- 不要求 F7 v1 覆盖湿实验。

## F7.3 评估层级

### L0：Epistemic invariants

继续运行现有 K2、Seal v2、sandbox、claim/evidence、critic independence 测试，并新增：

- evaluator asset 不可见；
- attempt family 无遗漏；
- ungrounded novelty 不能升级；
- mechanism claim 无竞争解释不能升级；
- invalid observation 不触发 belief update；
- one-time holdout 在 resume/duplicate delivery 下仍只打开一次。

此层为二元 hard gate，正式发布要求 100%。

### L1：Literature and knowledge boundary

接入或适配：

- AstaBench 的 PaperFindingBench、LitQA2-FullText、ScholarQA 类任务；
- 自建带 source-span gold 的检索、矛盾识别、方法/数据/指标抽取任务；
- 时间截断任务：只允许访问截止时间 T 的 corpus，评估对 T 之后发现的预测和最近前人识别。

### L2：Scientific coding and reproduction

分层接入：

- ScienceAgentBench 的许可任务子集；
- CORE-Bench / Core-Bench-Hard；
- PaperBench 的可承受子集；
- AstaBench 的 coding/execution 和 DiscoveryBench。

scorer 必须验证产出和数值，不以“代码存在”作为成功。

### L3：Hidden-rule discovery

接入 DiscoveryWorld 或等价隐藏世界。研究代理只看可执行动作和观察，不看规则、答案或 scorer。
评价：

- 是否完成任务；
- 是否选择了有信息的动作；
- 是否明确发现了解释规律；
- 是否在错误假设后修订；
- 每单位成本减少了多少假设熵。

### L4：Open-ended method innovation

接入 MLRC-Bench、ResearchGym 或自建固定计算约束任务。使用隐藏 test 与客观 metric；论文评分
仅作补充。系统必须提交完整 repository、运行 receipt、全部实验记录和资源消耗。

### L5：Private prospective quests

建立最小私有套件：

- 10–20 个未公开任务；
- 至少两个领域；
- 包含真效应、零效应、混杂、标签错误、分布漂移和样本不足；
- 每个任务由领域人员写 gold evidence 与可接受结论范围；
- 开发者只看到 validation analog，不看到 test；
- 每批 test 使用一次后退役或进入公开回归集。

## F7.4 评估对象与消融矩阵

每个任务至少比较：

1. 相同基础模型直接回答/直接编码；
2. 通用 coding/research agent；
3. Aletheia，关闭 campaign learning；
4. Aletheia，启用 K2；
5. 后续 Aletheia，启用 F8/F9；
6. 可承受任务上的领域专家或作者 baseline。

模型、预算、工具权限和 wall time 应尽量匹配。若无法匹配，报告差异，不能做无条件优越结论。

## F7.5 建议数据模型

新增模块：

~~~text
aletheia/evals/
  schemas.py
  registry.py
  runner.py
  sandbox.py
  scorers.py
  contamination.py
  statistics.py
  report.py
  adapters/
    astabench.py
    scienceagentbench.py
    corebench.py
    paperbench.py
    discoveryworld.py
    mlrc.py
scripts/run_frontier_gate.py
configs/evals/frontier_gate_v1.yaml
~~~

核心对象：

~~~text
EvaluationSuite
  suite_id, version, task_manifest_hashes, scoring_policy_hash

EvaluationTask
  task_id, layer, public_prompt, hidden_asset_ref, resource_budget,
  allowed_tools, expected_artifacts, scorer_ref, contamination_policy

EvaluationAttempt
  system_manifest, task_id, repeat_index, seed, start/end,
  submission_artifacts, cost, intervention_count, terminal_status

EvaluationScore
  objective_scores, rubric_scores, confidence,
  invalid_reasons, scorer_receipt, adjudication_status
~~~

评价数据库与研究 ledger 使用不同权限。研究运行只能写 submission inbox；独立 evaluator 读取
inbox 并写 score，研究进程不能查询 hidden task 和 score internals。

## F7.6 指标与统计

每个 suite 必须至少报告：

- pass@1 和全部重复运行分布；
- objective task score；
- invalid/timeout/infra failure 分解；
- false discovery rate；
- false novelty rate；
- Brier score、ECE 或适合该任务的校准指标；
- evidence provenance completeness；
- reproduction fidelity；
- human intervention count；
- token、USD、GPU、wall time；
- 每单位成本和每个实验的信息增益；
- 不同模型/系统差异的置信区间。

随机任务开发阶段至少 3 次，release gate 至少 5 次。不得只报告最好一次。比较前冻结主要终点、
统计检验和缺失运行处理方式。

## F7.7 工作包

### F7-S1：Threat model 与 eval contract

- 列出污染、答案泄漏、scorer hacking、prompt injection、best-of-N、重复查看 test 等威胁；
- 定义 task/submission/scorer 的内容寻址 contract；
- 定义 invalid 与 scientific false 的区别；
- 写 ADR：评价平面必须在研究 sandbox 外、只暴露最小提交接口。

### F7-S2：独立 runner

- runner 生成每次 attempt 的独立 workspace 和 manifest；
- 对研究 agent 隐藏 evaluator code、gold 和 test labels；
- 强制 wall time、CPU/GPU、网络和工具权限；
- 捕获完整事件、费用、artifact hash 和退出原因；
- retry 只用于明确 infra failure，scientific failure 不自动重跑；
- 每次 retry 保留记录。

### F7-S3：三个最小公开适配器

第一版先实现：

1. ScienceAgentBench 小型许可子集；
2. CORE-Bench 或 Asta Core-Bench-Hard 小型子集；
3. DiscoveryWorld 小型子集。

每个适配器先用参考实现和故意错误实现验证 scorer 能区分：

- 正确；
- 只运行但数值错误；
- 偷看答案；
- 缺 artifact；
- 输出不可复现；
- 超预算。

### F7-S4：私有任务生成与保管

- 由 evaluator owner 而非 Aletheia 开发循环管理；
- task metadata 与 hidden assets 分开加密/权限控制；
- 每题记录来源、许可、领域审阅者、污染风险和退役日期；
- validation/test 使用结构相似但内容不同的问题；
- 任何开发期间泄露的 test 立即退役。

### F7-S5：基线与消融

- 冻结模型和预算；
- 在 validation 上调通系统；
- 一次性登记 test 配置；
- 运行完整消融矩阵；
- 统计 Aletheia 的增益来自哪里，尤其区分基础模型升级与系统设计增益。

### F7-S6：Frontier Gate report

输出：

- frontier_gate_report.json；
- frontier_gate_report.md；
- per-attempt JSONL；
- system/evaluator manifests；
- score receipts；
- contamination declaration；
- cost/reliability plots；
- 未通过项及下阶段 blocker。

报告本身是评估产物的视图，不能编辑分数。

## F7.8 测试计划

建议新增：

~~~text
tests/evals/test_eval_schemas.py
tests/evals/test_hidden_asset_boundary.py
tests/evals/test_runner_limits.py
tests/evals/test_retry_semantics.py
tests/evals/test_scoring_receipts.py
tests/evals/test_contamination_policy.py
tests/evals/test_frontier_report.py
tests/evals/test_adapters_*.py
~~~

关键 adversarial cases：

- agent 猜测 hidden path；
- symlink/path traversal；
- submission 内嵌 evaluator 指令；
- 伪造 score JSON；
- 通过异常退出请求重跑；
- 只提交最好 seed；
- 修改时钟或随机种子；
- evaluator 崩溃后错误地将任务记为 fail/pass。

## F7.9 完成与退出标准

**Engineering complete：**

- 至少三个公开 benchmark 适配器和一个私有 suite 可由统一命令运行；
- evaluator/研究权限隔离通过攻击测试；
- 五次重复、成本、校准和 invalid 分解可自动汇总；
- 完整消融矩阵可运行；
- L0 hard invariants 100%；
- 任意分数都能追溯到 submission hash 与 scorer receipt。

**Scientific exit：**

- 在 test 解封前冻结数值门槛；
- full Aletheia 对直接模型和 no-campaign 消融具有预注册的显著/实质增益；
- 不以牺牲 false discovery、校准或成本异常增长换取任务分数；
- private suite 无 critical leakage、无未披露尝试；
- 具体阈值由 validation 和专家 baseline 校准后写入版本化 gate 配置，test 后不得修改。

建议正式命令：

~~~bash
conda run -n aletheia python scripts/run_frontier_gate.py \
  --suite configs/evals/frontier_gate_v1.yaml \
  --repeats 5 \
  --frozen
~~~

---

# F8 — Knowledge Boundary Engine

## F8.1 目的

把当前“搜索若干论文 + prose briefing + critic 判断”升级为可测覆盖、逐主张追踪、能处理
矛盾/修订/不可比结果的知识边界系统。

它需要回答四个不同问题：

1. **Grounding：** 候选主张依赖的事实是否有原文支持？
2. **Coverage：** 搜索是否达到足以讨论新颖性的质量门槛？
3. **Novelty：** 候选主张与最近前人工作的差异是什么？
4. **SOTA comparability：** 数据、split、metric 和资源条件是否真的可比？

## F8.2 核心产物

每个候选问题生成：

- knowledge_snapshot.json；
- search_protocol.json；
- coverage_report.json；
- atomic_claim_graph.json；
- nearest_prior_art.json；
- novelty_assessment.json；
- sota_comparability.json；
- contradiction_and_correction_report.json。

这些对象必须在 ideation gate 前冻结；后续新增文献创建新 snapshot，不静默改变旧判断。

## F8.3 数据模型

### 文献层

~~~text
PaperSnapshot
  canonical_id, title, authors, venue, publication_type,
  publication/version dates, DOI, source URLs, full_text_hash,
  license, peer_review_status, retraction/correction status

SourceSpan
  paper_snapshot_id, section/page, start/end or normalized span hash,
  exact_text_hash, extraction_method, OCR confidence

SearchSession
  objective, cutoff_time, corpus/source versions, query plan,
  seed papers, expansion rounds, stopping rule, failures

SearchQuery
  query, source, filters, result IDs, rank, timestamp, error state
~~~

### 科学语义层

~~~text
AtomicClaim
  subject, relation, object, qualifiers, population, conditions,
  direction, effect_size, uncertainty, claim_type

ClaimEvidenceEdge
  claim_id, source_span_id, relation=supports|refutes|qualifies|mentions,
  extraction_confidence, reviewer_status

MethodEntity / DatasetVersion / MetricDefinition / ProtocolSignature
  canonical identity and aliases

PriorArtRelation
  candidate_claim_id, prior_claim_id,
  relation=equivalent|subsumes|special_case|extension|combination|contradiction,
  exact_differences, evidence
~~~

第一版优先用 Postgres 关系表和显式 edge 表，不先引入独立 graph database。只有在真实查询
性能或图算法需求证明必要后再做 ADR。

## F8.4 搜索协议

一次强 novelty search 至少包含：

1. 从 quest、机制、对象、方法、数据和结果分别生成查询；
2. 术语、缩写、旧命名、相邻学科和否定表述扩展；
3. 多来源检索；
4. seed papers 的 backward/forward citation traversal；
5. 作者、方法名、数据集和关键引用的二次检索；
6. 去重、版本合并、preprint 与正式版关联；
7. retraction/correction 检查；
8. full text 或可验证原文 span 抽取；
9. 新文献增益趋于饱和的 stopping rule；
10. 记录未覆盖来源、付费墙、API 故障和时间边界。

检索文本全部视作不可信数据：结构化分隔、禁止其提升工具权限，并用 adversarial paper fixtures
测试 prompt injection。

## F8.5 覆盖健康分

CoverageHealth 不是一个由 LLM 自报的 0–1 分。它由以下可审计信号组成：

- curated known-answer set 的关键论文 recall；
- seed paper/reference recovery；
- query family 覆盖；
- source/venue/date diversity；
- citation frontier 新增率；
- full-text availability；
- source-span verification rate；
- correction/retraction 检查完成度；
- 同义术语 perturbation 后结果稳定性；
- API/权限造成的未覆盖比例。

任何 hard component 缺失时输出 coverage_insufficient，而不是把其余信号平均成及格。

## F8.6 Novelty gate

候选主张必须归类为：

- known_equivalent；
- known_special_case；
- incremental_extension；
- novel_combination；
- novel_method；
- novel_phenomenon；
- contradictory_to_prior；
- indeterminate_due_to_coverage。

允许强 novelty 的最低条件：

- coverage health 过冻结门槛；
- 最接近的至少若干 prior claims 被明确列出；
- exact difference 可由结构化字段表达；
- author-excluded reviewers 针对冻结 evidence package 审核；
- 无未解决的等价/包含关系 blocker；
- 时间截断、数据污染和模型先验限制被披露。

critic 不能把 indeterminate 改成 novel；它只能提出补检索或确认已给证据。

## F8.7 SOTA 可比性

建立 ProtocolSignature：

~~~text
task_definition
dataset_id + content/version hash
train/validation/test split policy + split hash
grouping/leakage policy
preprocessing and exclusions
metric name + exact formula + aggregation
uncertainty/statistical test
compute/data budget
external resources/pretraining
evaluation date
~~~

只有 compatible signature 才能计算 SOTA delta。否则状态为 non_comparable，并在报告中解释
哪个维度不同。禁止把 non_comparable 的更好数字写成 “beats SOTA”。

## F8.8 工作包

### F8-S1：Corpus snapshot 与 source span

- 扩展 Paper，不覆盖旧对象；
- 保存 source/version/hash/license；
- 实现 PDF/HTML/abstract 的统一 span identity；
- span 指向的文本变化时产生新 paper version；
- 对付费墙只记录 metadata/abstract 能力边界，不伪装 full-text coverage。

### F8-S2：Query planner 与 citation traversal

- deterministic query families；
- LLM 仅负责提出补充同义词和跨领域连接；
- query 去重、预算和停止规则由 harness 管理；
- 搜索错误进入 health report；
- 缓存外部 API 响应，支持完整 replay。

### F8-S3：Atomic claim extraction

- schema-first extraction；
- 数值、单位、population、条件和不确定性必须分字段；
- 每个抽取必须关联 source span；
- 低 OCR/抽取置信度进入人工或第二模型复核队列；
- contradictory edges 不被摘要阶段抹平。

### F8-S4：Nearest-prior-art matcher

- lexical、embedding、citation 和结构化实体多路召回；
- cross-encoder/模型 rerank 只改变候选顺序，不删除审计轨迹；
- 输出 equivalent/subsumption/special-case/extension 关系；
- 对候选 claim 做 component-wise difference。

### F8-S5：Coverage 与 novelty acceptance

- 在已知答案 review set 上标定 recall；
- 用历史时间切分测试 false novelty；
- 冻结强/弱 novelty 的 health 门槛；
- wiring 到 direction gate、scorecard、claim strength 和 write-up；
- coverage unknown 时 novelty claim 最高为 speculative/unverified。

### F8-S6：SOTA comparator

- 对 DatasetVersion、MetricDefinition、ProtocolSignature 做 canonicalization；
- curated fixtures 覆盖“数字相同但协议不同”；
- 自动生成 comparability matrix；
- 只有 comparable row 能进入 headline delta。

## F8.9 建议代码边界

~~~text
aletheia/knowledge/
  schemas.py
  corpus.py
  snapshots.py
  query_plan.py
  traversal.py
  claims.py
  spans.py
  prior_art.py
  coverage.py
  novelty.py
  sota.py
  acceptance.py
aletheia/research/literature.py      # 保留 provider adapter，逐步变薄
scripts/build_knowledge_snapshot.py
scripts/audit_novelty.py
~~~

## F8.10 测试与验收

建议测试：

~~~text
tests/knowledge/test_snapshots.py
tests/knowledge/test_source_spans.py
tests/knowledge/test_query_protocol.py
tests/knowledge/test_citation_traversal.py
tests/knowledge/test_claim_extraction.py
tests/knowledge/test_prior_art_relations.py
tests/knowledge/test_coverage_health.py
tests/knowledge/test_novelty_gate.py
tests/knowledge/test_sota_comparability.py
tests/knowledge/test_retractions.py
tests/knowledge/test_prompt_injection.py
~~~

**Engineering complete：**

- 每个 literature-backed claim 具有可复核 source span；
- retrieval outage/低覆盖无法通过 novelty gate；
- 至少一个 known-answer corpus、一个 temporal holdout 和一个 protocol mismatch suite；
- 搜索可从缓存完整 replay；
- nearest prior art 与 exact difference 进入 ledger 和 bundle；
- 不可比 SOTA 不生成胜出主张。

**Scientific exit：**

- known-answer 关键文献 recall 和 citation-support precision 达到预注册门槛；
- temporal holdout 的 false-novelty rate 在预注册上限以内；
- 同义词/查询扰动下 novelty 分类稳定；
- 领域专家盲审认为最近 prior art 和 exact difference 达到可用于选题的水平；
- 完整结果进入 F7 L1，并优于当前 retrieval + panel 基线。

建议真实验证：

~~~bash
conda run -n aletheia python scripts/audit_novelty.py \
  --suite configs/evals/knowledge_boundary_v1.yaml \
  --frozen-corpus \
  --temporal-holdout
~~~

---

# F9 — K3：竞争性因果世界模型

## F9.1 目的

K2 当前回答的是：“这一条 formulation 在 held-out 数据上成立的概率如何变化？”<br>
F9 要回答的是：“哪些相互竞争的解释仍可能为真，什么实验最能区分它们？”

从单一 Beta credence 升级为：

~~~text
Research Question
  ├── H0: null / measurement artifact
  ├── H1: proposed mechanism
  ├── H2: plausible confound
  └── H3: alternative mechanism
          │
          ├── each predicts outcomes under intervention E1
          ├── each predicts outcomes under intervention E2
          └── harness chooses high-discrimination experiment
~~~

## F9.2 非目标

- 不让 LLM 自由生成一个 DAG 后自动获得“因果”标签；
- 不将内部 posterior 当作 empirical verdict；
- 不要求所有领域立即提供完整结构因果模型；
- 不用一个标量 EIG 掩盖安全、成本、可行性和测量质量；
- 不把 observational feature importance 当成 intervention。

## F9.3 核心对象

~~~text
ResearchQuestion
  scope, target phenomenon, decision relevance, knowledge snapshot

HypothesisVersion
  immutable statement, parent version, status, assumptions,
  predicted mechanism, falsifiers, author provenance

CausalVariable / CausalEdge
  definition, units, observable/intervenable/latent,
  edge direction, assumptions, identification status

ExperimentCandidate
  intervention/observation, feasible actions, cost, safety,
  expected outcomes by hypothesis, measurement model

PredictionCommitment
  hypothesis_version, experiment_id, outcome bins/distribution,
  likelihood model hash, committed-before-observation receipt

WorldBeliefState
  normalized posterior over active hypotheses, calibration class

WorldBeliefUpdate
  prior, validated observation, likelihoods, posterior,
  entropy change, sensitivity, update receipt

Contradiction
  conflicting claims/observations, unresolved/resolved state
~~~

HypothesisVersion 一经用于 prediction commitment 就不可改。缩小、修订或增加机制均创建子版本，
旧版本和其失败证据保留。

## F9.4 因果与证据等级

每个实验标记：

- descriptive；
- observational association；
- natural experiment / quasi-experimental；
- controlled intervention；
- simulation intervention；
- measurement validation；
- independent replication。

不同等级允许不同 claim：

- descriptive 只能支持现象范围；
- observational association 不能单独支持 mechanism；
- simulation intervention 只能支持“在该模型中”；
- controlled intervention 还需要识别假设、测量有效性和替代解释审计；
- independent replication 决定是否有资格升级为 strong。

## F9.5 预测与 likelihood contract

在 observation 开放前，每个 active hypothesis 必须声明该实验下的预测。优先形式：

1. 离散 outcome bins + 概率；
2. 参数化分布；
3. 可执行、锁定的 likelihood function；
4. 无法校准时使用 ordinal prediction，但此时不能把输出称为概率或计算强 EIG。

所有概率需通过：

- 非负、归一化；
- 非退化检查；
- sensitivity analysis；
- 与 measurement uncertainty 兼容；
- 预测不能在 observation 后补写。

## F9.6 实验选择

对于候选实验 e：

~~~text
EIG(e) = H[p(H)] - sum_y p(y | e) H[p(H | y,e)]
~~~

实际选择使用受约束多目标规则，而不是只取最大 EIG：

~~~text
maximize discrimination / uncertainty reduction
subject to:
  safety approval
  feasible capability
  measurement validity
  remaining data-role budget
  total cost and time budget
  replication debt
  no repeated use of sealed evidence
~~~

若预测不可校准，则使用 pairwise discrimination matrix：某实验能区分哪些假设、在哪些 outcome
下仍无法区分。LLM 可以提出候选和预测依据，harness 负责格式、顺序、数据隔离和选择规则。

## F9.7 Belief update

只有 ObservationValidator 标记为 valid 的 observation 可以更新 posterior。以下情况不更新：

- infra failure；
- protocol deviation 超出预注册容忍；
- unblinded/identity mismatch；
- sample starved 且无预注册小样本更新规则；
- evaluator/audit 仍 unresolved；
- observation 来自 exploration 但被当作 confirmation；
- LLM/critic 的语言判断。

更新同时记录：

- posterior；
- entropy reduction；
- prior predictive surprise；
- likelihood sensitivity；
- 若换用合理 alternative likelihood，结论是否翻转；
- 哪些假设被淘汰、保留、合并或需要新版本。

## F9.8 工作包

### F9-S1：World-model schema 与版本语义

- 建立 ResearchQuestion、HypothesisVersion、Assumption、Prediction、BeliefState 表；
- 所有 lineage 使用 stable ID；
- version mutation 测试；
- K2 BeliefState 保留，提供迁移/兼容 view，不重写历史 K2 事件。

### F9-S2：Competing-hypothesis generator

- 每个 mechanism 题至少生成 H0、主解释和可信替代解释；
- F8 knowledge graph 用于找文献中的常见 confound 和替代机制；
- 去除语义重复假设；
- 要求每个假设给出不同的可观测预测；
- 若无法形成有区分度的 alternatives，mechanism 研究阻塞或降为 descriptive。

### F9-S3：Causal contract 与 identification audit

- 明确 variable、edge、latent confound、selection、measurement process；
- 静态检测 cycle、未定义变量、不可观测 endpoint；
- reviewer 审核 identification assumptions；
- assumption unresolved 时限制 claim strength；
- 将 causal graph 作为 evidence artifact，而不是 prompt 内 prose。

### F9-S4：Prediction commitment 与 likelihood

- 实现 immutable prediction receipt；
- observation staging 物理依赖 receipt；
- continuous outcome 的预注册 binning/likelihood；
- probability calibration 与 degeneracy probe；
- post-observation mutation 必须被拒绝并记录 security/science violation。

### F9-S5：Experiment selector

- 计算 EIG/discrimination；
- 纳入成本、时间、风险、fresh confirmation availability 和 replication debt；
- 输出候选排名及未选原因；
- selector 本身不能看隐藏 observation；
- 对 proxy gaming 建立 fixture：高 EIG 但无测量效度的实验必须被拒绝。

### F9-S6：Update、revision 与 negative result policy

- observation validator → update 的单向接口；
- 负结果后允许 retire、narrow、fork，不允许覆写；
- contradiction queue；
- 若所有假设预测相同，强制寻找新测量或停止；
- 若 posterior 对 likelihood 极敏感，状态标记 fragile。

### F9-S7：K3 acceptance scorer

在现有 k2_acceptance.py 旁新增独立 scorer，检查：

- active set 中存在非重复 competing hypotheses；
- prediction 均先于 observation；
- 更新数量与 valid observations 一一对应；
- selected experiment 确实区分至少两个高概率假设；
- mechanism claim 只在替代解释被证据排除后升级；
- negative result 导致 belief/model 范围真实变化；
- 所有版本、尝试和停止理由持久化。

## F9.9 建议代码边界

~~~text
aletheia/epistemics/
  schemas.py
  hypotheses.py
  causal.py
  likelihood.py
  prediction.py
  selector.py
  update.py
  revision.py
  acceptance.py
aletheia/scheduler/k3_acceptance.py
scripts/real_k3_hidden_world_e2e.py
~~~

driver.py 目前职责过多。F9 不应继续把全部逻辑塞进 Driver；先抽出纯函数和 service，再让 driver
编排。科学数学、持久化和 orchestration 三层应能单独测试。

## F9.10 测试

~~~text
tests/epistemics/test_hypothesis_versions.py
tests/epistemics/test_causal_contract.py
tests/epistemics/test_prediction_commitment.py
tests/epistemics/test_likelihoods.py
tests/epistemics/test_eig_selector.py
tests/epistemics/test_belief_update.py
tests/epistemics/test_negative_results.py
tests/epistemics/test_mechanism_claim_gate.py
tests/test_k3_acceptance.py
~~~

必须包含：

- posterior 数学 gold cases；
- 零概率、未归一化、过度自信预测；
- observation 先于 prediction；
- critic 语言结论试图触发更新；
- invalid measurement；
- 两个语义相同假设伪装成竞争解释；
- 所有候选实验对各假设预测相同；
- EIG 高但不可执行/不安全；
- 混杂造成表面机制；
- likelihood sensitivity 翻转结论；
- negative result 后只换措辞、不换预测。

## F9.11 验收

**Engineering complete：**

- 一个 research question 可维护至少三个版本化竞争假设；
- 每个实验有先验预测 commitment；
- valid observation 是唯一更新入口；
- selector 可计算 EIG 或显式 discrimination；
- mechanism claim gate 与 alternative exclusion 绑定；
- K2 历史 run 可读且不被重解释；
- K3 scorer 可从事件流独立重建 verdict。

**Scientific exit：**

- 在冻结的隐藏规律/因果模拟任务中，F9 比 K2 单假设版本更快排除错误解释；
- 相比只优化 headline metric 的 agent，选择更多真正有判别力的实验；
- posterior calibration 和 false-mechanism rate 达到 F7 中冻结门槛；
- 至少一个真实材料问题完成 alternatives → discriminating experiment → update；
- 正结果不是必要条件，但必须观察到假设空间实质收缩。

建议真实验证：

~~~bash
conda run -n aletheia python scripts/real_k3_hidden_world_e2e.py \
  --suite configs/evals/k3_hidden_world_v1.yaml \
  --repeats 5 \
  --frozen
~~~

---

# F10 — Open Experiment Engine + Materials Deep Domain

## F10.1 目的

把 Aletheia 的科学行动空间从“加载表格 → 特征化 → 回归/分类 → 指标”扩展为显式、可发现、
可组合、带验证器的科学实验能力，并先在一个领域形成足够深度。

第一深领域建议选择材料科学，原因不是它最容易，而是当前已有：

- materials domain plugin；
- Matbench/UCI/SuperCon 数据经验；
- composition/chemical-system leakage protocol；
- Magpie 与模型失败诊断；
- 现成的外部 SuperCon2 负复现案例；
- 可逐步连接晶体结构、材料数据库、DFT 和实验合作方的自然路径。

目标不是“再换一个更强 regressor”，而是研究结构、机制、可合成性和测量。

## F10.2 领域能力目标

F10 完成后，材料 domain 至少能表达并执行以下实验族：

1. **数据/测量审计：** 重复样品、测量不确定性、标签冲突、分布漂移；
2. **结构表示对照：** composition-only 与 structure-aware 表示；
3. **模型与特征消融：** 成对、固定预算、相同 split 的因果式对照；
4. **反事实/干预：** 成分或结构扰动下的预注册预测；
5. **机制诊断：** 不同机制假设产生不同可观测结果；
6. **模拟：** 至少一种可复现的领域模拟/第一性原理接口；
7. **主动学习：** 在预算下选择下一批计算或实验；
8. **外部候选验证：** 生成可交给独立计算或实验站点的 protocol。

## F10.3 Experiment Capability Manifest v1

当前 DemonstrationCapability 只有 id、description、compute 和 reproduction_factor，无法承载
开放科学实验。新增独立 contract，不破坏旧接口：

~~~text
ExperimentCapabilityManifest
  capability_id
  version
  domain
  scientific_questions[]
  claim_types_supported[]
  evidence_level

  input_schema
  output_schema
  accepted_data_modalities
  required_metadata
  units_and_ontologies

  action_type
  executor_ref
  validator_ref
  observation_parser_ref
  sandbox_or_external_boundary

  preregistration_schema
  controls_required[]
  assumptions[]
  known_failure_modes[]
  minimum_sample_or_power_rule

  estimated_cost/time/resources
  nondeterminism_policy
  reproduction_policy
  safety_class
  approval_class
  license/data_egress_policy
~~~

Capability 的四个角色必须分开：

- **Planner:** 决定何时适合使用；
- **Executor:** 执行动作；
- **Observation parser:** 把原始输出转为候选 observation；
- **Validator:** 判断 protocol 是否合规、observation 是否有效。

同一个 AI 可以帮助生成 executor，但不能同时成为唯一 validator。

## F10.4 建议的材料数据模型

~~~text
MaterialIdentity
  normalized_formula, composition, structure_id,
  polymorph, sample_id, synthesis_batch, provenance

CrystalStructureArtifact
  CIF/structure hash, parser version, symmetry,
  periodicity, quality flags

MaterialMeasurement
  property, value, unit, uncertainty, method,
  temperature/pressure/field, sample/batch, raw artifact

SimulationRun
  method, code/version, pseudopotential/basis,
  parameters, convergence, input/output hashes, quality flags

SynthesisProtocol
  precursors, quantities, sequence, atmosphere,
  temperature/time profile, equipment, safety, tolerances

CandidateMaterial
  identity, predicted properties, uncertainty,
  novelty/provenance, feasibility, selection rationale
~~~

formula 相同不代表样品或结构相同。所有 split/overlap 判断需要分别处理 formula、structure、
sample 和 synthesis batch 层级。

## F10.5 第一批 capability

### C1：Measurement and duplicate audit

功能：

- 找同 formula/structure/sample 的重复记录；
- 分解 within-sample、between-batch 和 between-source 变异；
- 识别不兼容测量条件；
- 给后续模型建立噪声下限或明确“无法估计”；
- 禁止把标签冲突简单平均后当作干净 truth。

这项应先于复杂模型，因为错误 measurement model 会污染所有后续机制。

### C2：Composition vs structure discrimination

固定任务、预算和 split，对比：

- composition-only baseline；
- structure-aware baseline；
- 结构打乱/边移除/坐标扰动 negative controls；
- matched chemical families；
- out-of-distribution chemical systems；
- calibration 和 failure strata。

结果支持的是“结构信息对某一预注册任务/范围具有增量价值”，不能直接推广为物理机制。

### C3：Structural intervention / counterfactual

对固定 composition 的多晶型、可控结构扰动或可信模拟结构：

- 预注册每个 competing hypothesis 的方向/量级预测；
- 计算 property response；
- 检查扰动是否仍处于物理可行域；
- 对 symmetry breaking、volume、coordination 等变量做单因素或设计化干预；
- 将 simulation-level claim 与 experimental claim 分开。

### C4：DFT/simulation adapter

第一版只选一个成熟且可控制的计算栈，由实际资源决定。候选需 ADR 决策，例如：

- ASE 作为 workflow interface；
- GPAW/Quantum ESPRESSO 等一个 executor；
- Materials Project 等只能作为数据/API，不代替新计算；
- 小规模、已知基准体系先验证。

必备：

- container/environment pin；
- input structure 和全部计算参数；
- convergence validator；
- partial/failure 状态；
- raw output retention；
- cost estimate 与 timeout；
- reference-system calibration。

未收敛不是物理负结果。

### C5：Active acquisition

在固定候选池或可行生成器上：

- uncertainty/disagreement；
- hypothesis discrimination；
- diversity；
- synthesis/simulation cost；
- safety/availability；
- expected value of information；
- replication debt。

选择策略必须与 F9 predictor 和 budget ledger 对接，且使用 batch-level confirmation seal。

## F10.6 “AI 可编写新实验”接口

为了保持开放性，允许 agent 创建新 capability proposal，但分两级：

### Provisional capability

- AI 编写 executor、parser 和测试；
- 只能访问 exploration data；
- 静态 gate + hard sandbox；
- 必须有显式 input/output schema、controls、assumptions；
- validator 由 harness primitive 或独立实现生成；
- 只能形成 exploratory evidence。

### Registered capability

升级需满足：

- reference fixtures；
- adversarial fixtures；
- null/positive controls；
- independent recomputation；
- domain reviewer；
- reproduction policy；
- safety/资源 policy；
- capability manifest 内容 hash；
- 在 F7 regression suite 中通过。

只有 registered capability 可产生 confirmatory evidence 或升级 mechanism claim。

## F10.7 工作包

### F10-S1：General capability registry

- 建立 manifest schema、版本、发现和兼容性检查；
- domain profile 只列 capability ID，不塞长 prompt；
- planner 根据 question/evidence level/inputs 查询 capability；
- capability 不存在时为 unsupported，不做 fuzzy fallback；
- 注册变更进入 run manifest。

### F10-S2：Typed observation pipeline

- raw output → parser → candidate observation → validator → validated observation；
- 原始文件和解析后值都保留；
- units、uncertainty 和 condition 必填；
- invalid 与 negative 分开；
- validated observation 才能进入 F9 update。

### F10-S3：Materials identity and measurement

- 规范 formula、structure、sample、batch；
- content hash 与来源许可；
- 重复/冲突/测量条件检查；
- split ledger 支持多层 identity；
- 建立小型 gold fixtures。

### F10-S4：Structure-aware experiment

- 支持结构数据加载和质量 gate；
- 至少一个 structure-aware reference model；
- composition/structure matched protocol；
- ablation/control；
- 锁定 internal/external evaluation；
- 避免因模型容量/训练预算不同产生伪因果结论。

### F10-S5：Simulation capability

- 完成 ADR；
- digest-pinned simulation image；
- reference systems 的能量/结构/收敛 gold；
- job receipt、checkpoint、timeout 和 quota；
- parser/validator 独立于 agent；
- 失败原因 taxonomy。

### F10-S6：Mechanistic campaign template

- 从 F8 选择一个知识边界清楚的问题；
- F9 创建竞争解释；
- 使用 C1–C4 中至少两类实验；
- 预注册机制判别；
- fresh confirmation；
- external dataset 或 independent implementation；
- 输出完整 evidence bundle。

### F10-S7：Capability authoring pipeline

- provisional sandbox authoring；
- test generation 不等于 validator；
- independent audit；
- promotion receipt；
- manifest registry 签名/权限；
- 恶意 capability tests。

## F10.8 建议代码边界

~~~text
aletheia/capabilities/
  schemas.py
  registry.py
  planner.py
  executor.py
  observations.py
  validators.py
  promotion.py

aletheia/domains/materials/
  identity.py
  measurements.py
  structures.py
  interventions.py
  simulations/
    base.py
    <chosen_engine>.py
  capabilities/
    measurement_audit.py
    structure_discrimination.py
    structural_intervention.py
    simulation.py
~~~

## F10.9 测试

~~~text
tests/capabilities/test_manifests.py
tests/capabilities/test_registry.py
tests/capabilities/test_observation_validation.py
tests/capabilities/test_promotion.py
tests/materials/test_identity.py
tests/materials/test_measurement_audit.py
tests/materials/test_structure_protocol.py
tests/materials/test_interventions.py
tests/materials/test_simulation_adapter.py
tests/materials/test_mechanism_campaign.py
~~~

关键 cases：

- formula 相同但 polymorph 不同；
- sample 重复被错误跨 split；
- 单位错误；
- 测量条件不可比；
- simulation 未收敛却返回有限数；
- parser 偷偷丢弃失败运行；
- structure model 预算远高于 baseline；
- intervention 生成物理不可行结构；
- provisional capability 试图产生 strong claim；
- agent 编写 validator 让自己通过。

## F10.10 验收

**Engineering complete：**

- capability manifest/registry 可发现、版本化、fail closed；
- observation pipeline 保留 raw、parse、validate 三层；
- 至少 C1、C2、C3 和一个模拟 adapter；
- 至少一个 provisional → registered 的完整升级案例；
- materials identity 支持 formula/structure/sample/batch；
- validated observation 可无特殊路径进入 F9；
- 全部 AI code 仍走统一 hard sandbox。

**Scientific exit：**

- 预注册一个不由最终答案反推的材料 quest；
- 至少三个竞争解释和一个真正有判别力的 structure/simulation experiment；
- 使用 fresh confirmation，随后 independent dataset 或 independent implementation；
- 结果可以为负，但必须改变 hypothesis set；
- 领域专家审计认为协议、计算收敛、测量与主张范围一致；
- F7 中 materials private quest 相比 composition-only/K2 基线有实质提升。

---

# F11 — Long-Horizon Research Portfolio

## F11.1 目的

让 Aletheia 从一个最长数轮、单一问题 lineage 的 campaign，升级为可跨多日运行、并行维护多个
研究问题、在故障和上下文切换后仍保持科学一致性的研究组合。

“运行更久”不是能力。F11 必须证明：

- 战略状态不依赖单个模型上下文；
- 每个任务有明确依赖和验收；
- 失败恢复不重复不可逆动作；
- 负结果和旧 contradiction 不会在摘要压缩后消失；
- 预算在复现、机制、探索和新问题间合理分配；
- 不因并行度增加而破坏统计 family 和证据隔离。

## F11.2 Quest、Program、Campaign 与 Experiment

统一层级：

~~~text
Quest
  人类给出的长期方向、价值、安全和资源边界

ResearchProgram
  对 quest 的一个可管理问题域，包含知识边界与多个 ResearchQuestion

Campaign
  围绕一个或一组竞争假设的适应性实验序列

Experiment
  一次有 prediction、protocol、execution、observation、validation 的原子尝试

Task
  为完成上述科学对象而执行的工程/检索/评审/计算工作
~~~

不得通过新建 Campaign 来清零同一 scientific family 的尝试计数。

## F11.3 Durable orchestration

新增 durable task state：

~~~text
Task
  task_id, type, inputs_hash, dependency_ids, owner,
  status, attempt, lease_owner, lease_expiry,
  idempotency_key, retry_policy, result_artifact_id

TaskAttempt
  start/end, worker manifest, heartbeat, logs,
  terminal category, partial artifacts

Approval
  action, exact payload hash, approver, scope, expiry

Checkpoint
  program state hash, open hypotheses, unresolved contradictions,
  budget state, artifact frontier, resume compatibility
~~~

语义：

- delivery 可 at-least-once；
- scientific transition 和 artifact commit 必须幂等；
- 外部动作以 idempotency key 或人工确认避免重复；
- lease 超时只让 task 可重领，不自动判定前次科学失败；
- partial output 只有经过 validator 才能成为 evidence。

## F11.4 Cognitive accumulation

不把所有历史塞回 prompt。维护四类状态：

1. **Evidence memory：** 不可变 observation/claims/source spans；
2. **Hypothesis memory：** 版本、预测、posterior、被淘汰原因；
3. **Procedural memory：** capability 使用经验、失败模式和修复；
4. **Strategic state：** quest、优先级、open decisions、replication debt。

摘要只是 cache，不是 source of truth。每个摘要带被包含 artifact ID 列表和 coverage marker；
若摘要遗漏 blocker，恢复时可由 ledger 重建。

## F11.5 Portfolio selection

Portfolio planner 每个 epoch 从以下动作选择：

- 深挖当前高价值 hypothesis；
- 做反例/机制判别；
- 复制关键结果；
- 修复测量/能力；
- 获取新数据；
- 暂停低价值 program；
- 启动新 question；
- 停止并归档。

选择依据为受约束、多目标组合：

- expected scientific value；
- expected information gain；
- novelty/importance；
- replication debt；
- success probability 与校准质量；
- cost/time/resource；
- dependency readiness；
- safety/approval；
- diversity 和 correlated-failure risk。

禁止让一个由 LLM 自报的总分直接决定资源。各维度、约束和选择理由必须分别记录。

## F11.6 Stopping、pivot 与 anti-loop

硬停止：

- confirmation/family alpha 或 fresh data 用尽；
- 预算用尽；
- 安全/伦理/许可阻塞；
- 没有 registered capability；
- measurement 无法验证；
- knowledge coverage 不足以支持所需 claim；
- 所有剩余实验 discrimination 低于门槛；
- repeated infra failure 达到策略上限。

soft stop/pivot：

- posterior 已足够集中；
- 价值低于其他 program；
- replication debt 过高；
- 连续实验只改变指标、不改变 hypothesis set；
- 相同 failure reason 重复且 proposal 没有结构变化。

anti-loop fingerprint 应综合：

- hypothesis semantics；
- prediction pattern；
- capability/inputs；
- analysis plan；
- expected discriminated pairs。

只改文字或随机种子不算新策略。

## F11.7 工作包

### F11-S1：Schema migration 与 durable queue

- 完成 PF-1；
- 用 Postgres-backed queue 或经过 ADR 的等价方案；
- lease、heartbeat、idempotency、retry；
- API 与 worker 分离；
- multi-process SSE 使用 durable event source；
- kill/restart 测试。

### F11-S2：Transactional scientific transitions

- prediction commit、observation validation、belief update 分事务边界；
- 使用 outbox 或等价机制保证 DB/event 一致；
- duplicate event 不造成 duplicate update；
- one-time holdout/external action 在 worker 重试中仍保持一次；
- 外部 action receipt。

### F11-S3：Quest/program graph

- 建立层级与 dependency graph；
- 跨 campaign family identity；
- budget 与 data-role allocation 绑定 quest/program；
- program 状态可从 ledger 重建；
- UI 只作为 view/controller。

### F11-S4：Memory compaction with receipts

- artifact-backed summary；
- contradiction、limitation、failed hypothesis 为不可丢字段；
- 随机重建测试；
- provider/model 切换后恢复；
- prompt context 只拉取当前任务必要状态。

### F11-S5：Portfolio planner

- deterministic hard filters；
- LLM 提案与解释；
- harness 计算成本、EIG、replication debt；
- 预算分配和 diversity policy；
- shadow mode 与人工计划对比后才启用 autonomous allocation。

### F11-S6：Fault-injection harness

在关键边界随机：

- kill API；
- kill worker；
- DB reconnect；
- evaluator timeout；
- provider unavailable；
- duplicate message；
- stale lease；
- disk quota；
- model/version mismatch on resume。

验证科学状态、费用和 outward action 均无重复/丢失。

### F11-S7：72-hour research endurance gate

一个冻结 quest：

- 至少两个 research questions；
- 至少三个 campaign branches；
- 至少一个 negative result；
- 至少一个 reproduction；
- 至少一次 process kill；
- 至少一次 provider/model transport interruption；
- 结束时生成完整 portfolio report。

不是要求全部结论为正，而是验证方向、证据和预算的一致性。

## F11.8 建议代码边界

~~~text
aletheia/programs/
  schemas.py
  graph.py
  state.py
  portfolio.py
  stopping.py
  memory.py

aletheia/jobs/
  queue.py
  leases.py
  worker.py
  idempotency.py
  recovery.py
  outbox.py

scripts/run_endurance_gate.py
scripts/replay_program.py
~~~

## F11.9 测试与验收

建议新增：

~~~text
tests/jobs/test_leases.py
tests/jobs/test_idempotency.py
tests/jobs/test_outbox.py
tests/jobs/test_recovery.py
tests/programs/test_graph.py
tests/programs/test_family_identity.py
tests/programs/test_memory_compaction.py
tests/programs/test_portfolio.py
tests/programs/test_stopping.py
tests/programs/test_anti_loop.py
tests/test_endurance_gate.py
~~~

**Engineering complete：**

- API/worker 任一进程中断可恢复；
- task/event duplicate 不重复 belief update 或 outward action；
- program 状态能从数据库和 artifact 重建；
- memory compaction 不丢 contradiction/negative result；
- portfolio planner 遵守预算、数据角色和 approval；
- replay 同一 event stream 得到相同 scientific state。

**Scientific exit：**

- 完成 72 小时冻结 endurance gate；
- 期间故障注入零证据丢失、零重复 one-time action；
- 至少一次由负结果引发真正结构性 pivot；
- 相比单 campaign baseline，单位成本的信息增益或问题覆盖有实质改善；
- 结束时所有活跃/停止/失败 program 均有明确、可审计原因；
- F7 long-horizon/private suite 达到冻结门槛。

---

# F12 — Reality Bridge + Independent Replication

## F12.1 目的

把 Aletheia 的锁定研究协议接入现实世界执行器，并让关键结果由独立数据、独立实现或第二实验
站点裁决。

第一版不要求自主机器人。最可靠的起点是 Robin 式 lab-in-the-loop：

1. Aletheia 自主选择问题与实验；
2. 冻结 protocol、随机化、分析和停止规则；
3. 人类/合作实验室按 protocol 执行；
4. 原始仪器数据和 protocol deviations 进入不可变 ledger；
5. Aletheia 分析并更新世界模型；
6. 第二独立站点盲法复现关键结果。

人类可以负责操作、安全与伦理，不应在看到结果后替系统挑选分析或丢弃失败批次。

## F12.2 Independence ladder

所有复现必须标记级别，不能统一称为 independent：

| 等级 | 定义 | 能支持的结论 |
|---|---|---|
| R0 | 同代码、同数据重算 | 确定性/计算一致性 |
| R1 | 同数据、独立实现 | 实现鲁棒性 |
| R2 | 独立数据、相同站点/仪器 | 数据迁移 |
| R3 | 新批次/新样品、相同站点 | 批次鲁棒性 |
| R4 | 第二站点盲法执行锁定 protocol | 独立实验复现 |
| R5 | 第二团队独立 protocol/conceptual replication | 最高的概念稳健性 |

strong empirical/mechanism claim 的最低等级由 domain policy 冻结。F12 的领域级毕业目标至少
要求一个 R4；若实际条件暂时不允许，最多称为 Reality Bridge pilot，不能称毕业。

## F12.3 Lab/External Executor Contract

~~~text
ExternalExecutorManifest
  executor/site identity
  capabilities and calibrated ranges
  equipment/software versions
  accepted protocol schema
  safety/ethics/approval classes
  sample custody support
  raw data formats
  idempotency/cancellation
  SLA and cost

ExperimentOrder
  order_id, protocol_hash, samples, randomization/blinding,
  acceptance tolerances, controls, requested raw outputs,
  approval receipt, no-result-access-before-lock flag

ExecutionReceipt
  site/order/batch/operator/equipment
  timestamps, protocol version, deviations,
  sample custody events, raw artifact IDs, completeness

ObservationPackage
  raw files, calibration/QC, parser outputs,
  exclusions, missingness, validator result
~~~

外部 executor 返回的是 observation package，不返回科学 verdict。Aletheia 的 validator 和
锁定分析 harness 决定 claim outcome。

## F12.4 Sample identity、randomization 与 blinding

最低要求：

- sample/barcode 与 scientific identity 分离；
- 随机化表在实验站点执行，但分析方在必要阶段保持盲法；
- positive、negative、vehicle、blank 等 controls 预注册；
- 批次和板位进入设计；
- 排除样品/孔位的规则预注册；
- 原始仪器文件不可由模型改写；
- unblinding 有时间戳与 receipt；
- chain-of-custody 缺失时 observation invalid 或降级。

## F12.5 Protocol deviations

每次 deviation 分类：

- within tolerance；
- scientifically material but analyzable under prereg sensitivity；
- invalidating；
- safety incident；
- missing/unknown。

站点或 Aletheia 都不能在看到效果方向后决定分类。规则随 protocol 冻结；边界案例交给不知结果
方向的独立 reviewer。

## F12.6 伦理、安全和治理

F12 开工前建立 domain-specific risk gate：

- 禁止/限制实验类型；
- 危险材料、生物安全、双重用途；
- 人体/动物、隐私、知情同意；
- 采购和供应商；
- 外部数据跨境/模型 provider egress；
- 废物和环境要求；
- incident response；
- publication/press release approval。

Aletheia 可以提出 risk assessment，但不能批准自己的高风险实验。

## F12.7 第一现实项目选择标准

第一项目应满足：

- 对人类/动物/病原体无直接高风险；
- 实验 protocol 成熟、endpoint 客观；
- 周期短、成本可控；
- 有 positive/negative controls；
- 样本身份和批次可追踪；
- raw data 格式可自动解析；
- 至少两个潜在执行站点；
- 正负结果都有科学价值；
- 不依赖高度隐性的手工技艺。

对当前材料方向，优先考虑小规模、低风险、标准表征明确的合作实验，或先做 R1/R2 的独立计算
复现，再升级到 R4。具体选题由 F8/F9/F10 的结果决定，不在本计划中预写答案。

## F12.8 工作包

### F12-S1：External action policy

- capability/approval matrix；
- exact payload preview；
- idempotency、cancel、timeout；
- least-privilege credential broker；
- outward action audit；
- 模拟 executor 先行。

### F12-S2：Protocol compiler

- 将 F10 registered capability + F9 prediction 转为站点 schema；
- units、tolerance、randomization、controls、raw outputs；
- protocol lint 和可执行性预检；
- 站点确认 protocol hash；
- 修改触发新 prereg lineage。

### F12-S3：Mock lab/digital twin

- 实现 deterministic/mock executor；
- 注入批次效应、缺失孔、条码交换、设备漂移、延迟和 deviation；
- 验证 order/receipt/parser/validator；
- 不能以 mock 科学结果作为现实证据。

### F12-S4：Pilot site integration

- 选一个合作方或云实验室；
- capability calibration；
- 小型 known-control run；
- 数据、许可、安全和保留协议；
- raw files 直达证据存储；
- reconciliation 和 billing receipt。

### F12-S5：Prospective discovery run

- 人只给 quest 和约束；
- F8 冻结知识边界；
- F9 竞争假设和预测；
- F10 registered experimental capability；
- 第一站点完成 discovery/confirmation；
- 所有结果和失败进入 family ledger。

### F12-S6：Second-site blinded replication

- 在第一站点结果锁定后提交；
- 第二站点仅看到 protocol 所需内容；
- 样品/条件盲法；
- 不以第一站点 effect size 调整 primary endpoint；
- 同一锁定分析；
- 预先规定成功、失败、equivalence 和 inconclusive。

### F12-S7：Independent replication report

报告：

- independence level；
- site/sample/equipment differences；
- protocol deviations；
- effect and uncertainty；
- heterogeneity；
- failed/invalid batches；
- combined conclusion；
- 哪些 claim 被升级、缩小或 refuted；
- 人类干预清单。

## F12.9 建议代码边界

~~~text
aletheia/reality/
  schemas.py
  policy.py
  protocol.py
  orders.py
  custody.py
  receipts.py
  observations.py
  replication.py
  providers/
    mock.py
    <pilot_site>.py

scripts/run_reality_pilot.py
scripts/audit_external_replication.py
~~~

## F12.10 测试与验收

建议新增：

~~~text
tests/reality/test_policy.py
tests/reality/test_protocol_compiler.py
tests/reality/test_idempotent_orders.py
tests/reality/test_custody.py
tests/reality/test_blinding.py
tests/reality/test_deviations.py
tests/reality/test_raw_artifacts.py
tests/reality/test_replication_levels.py
tests/reality/test_mock_lab_faults.py
tests/test_reality_pilot.py
~~~

关键 cases：

- duplicate order；
- protocol hash mismatch；
- barcode swap；
- missing raw file；
- site 只返回汇总 CSV；
- unblinding 早于 analysis lock；
- protocol deviation 在看到方向后重分类；
- 第一站点结果泄漏给第二站点；
- 第二站点样本不独立；
- invalid batch 被当作 negative；
- 成本/审批过期仍提交。

**Engineering complete：**

- mock lab 全链路和至少一个 pilot executor；
- protocol/order/receipt/custody/raw artifact 可追溯；
- duplicate delivery 不重复下单；
- deviation/blinding/invalid observation fail closed；
- R0–R5 independence 明确计算和展示；
- 外部动作均有 exact-payload approval 和 receipt；
- 真实数据能进入 F9 update，而实验站点不能设置科学 verdict。

**Scientific exit：**

- 完成一个前瞻性、预注册、站点 1 的现实实验 campaign；
- 完成第二站点 R4 盲法复现；
- 使用相同 primary endpoint 和锁定分析；
- 无未披露失败批次或 post-hoc exclusion；
- 结果可以不复现，但系统必须据此正确降级/refute；
- 若复现，claim 仍需 F8 novelty、F9 mechanism 和 bundle gate 才能升级；
- 第三方审计完整 evidence bundle 后可确认 lineage 和结论。

---

## 6. 跨阶段统一 acceptance matrix

| 能力 | F7 | F8 | F9 | F10 | F11 | F12 |
|---|---|---|---|---|---|---|
| 独立评价 | 主交付 | 回归 | 回归 | 回归 | 回归 | 回归 |
| measured literature coverage | 测试 | 主交付 | 输入 | 输入 | 持久化 | 冻结 |
| competing hypotheses | 测试 | 提供 prior art | 主交付 | 执行 | 组合管理 | 现实裁决 |
| mechanism experiments | 测试 | grounding | claim gate | 主交付 | 调度 | 外部执行 |
| long-horizon durability | 基础指标 | snapshot replay | state replay | job replay | 主交付 | order replay |
| independent replication | 评价定义 | prior art 区分 | belief update | protocol | replication debt | 主交付 |
| evidence bundle | 每次评估 | knowledge artifacts | world state | raw/validated obs | program report | site receipts |

任何阶段的 full pass 必须同时满足：

1. 本阶段主交付验收；
2. F7 对应层无显著回退；
3. 既有 sandbox/Seal v2/K2 hard invariants 无违规；
4. 负结果、invalid、infra failure 和成本完整披露；
5. 真实验收而不只是 mocked tests。

---

## 7. 统一 Frontier Research Bundle v1

到 F12 时，一项完整研究应输出以下目录。F7 起逐步填充，不等 F12 才实现。

~~~text
bundle/
  manifest.json
  quest.json
  governance/
    approvals.jsonl
    safety_review.json
  knowledge/
    corpus_snapshot.json
    search_protocol.json
    coverage_report.json
    claims.jsonl
    source_spans.jsonl
    prior_art.json
    novelty.json
    sota_comparability.json
  world_model/
    questions.jsonl
    hypotheses.jsonl
    causal_models.jsonl
    prediction_commitments.jsonl
    belief_updates.jsonl
    contradictions.jsonl
  experiments/
    family_ledger.jsonl
    split_ledger.json
    preregistrations/
    capability_manifests/
    code/
    environments/
    raw_observations/
    validated_observations/
    metrics/
  replication/
    internal.json
    external_site_1.json
    external_site_2.json
  reviews/
    panels.jsonl
    unresolved_objections.jsonl
  reports/
    campaign.md
    portfolio.md
    paper.md
  reproduce/
    README.md
    verify_bundle.py
    expected_receipts.json
~~~

原则：

- paper.md 只能引用 bundle 内 evidence object；
- 每个数字、表、图和 factual sentence 有来源；
- source span、raw observation、code 和 environment hash 可验证；
- 缺失项在 manifest 标记 absent + reason，不能静默省略；
- publication 是人类批准的 view，不是科学 source of truth。

---

## 8. 建议首批 12 个 implementation issues

这些 issue 是开工顺序，不代表完成整个 F7：

1. **PF-1 / Alembic baseline：** 冻结当前 schema，建立 upgrade/backup 测试。
2. **PF-2 / Run Manifest v1：** 将模型、prompt、tool、image、data 和 harness 统一 hash。
3. **F7 / Eval schemas：** suite/task/attempt/submission/score/receipt 纯 schema。
4. **F7 / Hidden evaluator boundary：** 独立 workspace、权限与攻击测试。
5. **F7 / Runner + repeated statistics：** 资源、成本、retry、五次运行汇总。
6. **F7 / ScienceAgentBench adapter：** 小型许可子集与 objective scorer。
7. **F7 / CORE/Asta reproduction adapter：** 数值与 artifact 复现。
8. **F7 / DiscoveryWorld adapter：** hidden-rule 和 action trace。
9. **F7 / Baseline matrix：** direct model、generic agent、no-K2、full K2。
10. **F7 / Private suite policy：** test 保管、退役和污染申报。
11. **F7 / Report + acceptance config：** 冻结门槛、receipt 和失败分解。
12. **F8 / Knowledge schema spike：** 只做 ADR/fixture，不在 F7 前提前接入 driver。

issue 1–5 完成后应先做一次内部 dry acceptance，确认评价架构本身可信，再花预算接公开 benchmark。

进度（2026-08-14）：issue 6、7、8 已完成工程实现与真实 Docker 隔离验收，F7-S3 的三个
公开适配器至此全部工程完成。ScienceAgentBench
官方 verified archive 仍需 evaluator operator 依上游条款提供，未使用旧版资产替代；
CORE/Asta adapter 已用许可审计的两个官方公开 validation capsule 完成实际 suite 准备。
DiscoveryWorld adapter 已冻结官方源码/许可证，使用候选与隐藏世界两个不同的离线容器，
并以真实系统化实验策略通过显式规律、终态、信息增益、信念修正和双次 exact trace 验收；
最终四规则 suite 已内容寻址冻结，全项目 622 个非 Docker 测试（另 1 skip）与 29 个 Docker
测试通过。
下一项为 issue 9（direct model、generic agent、no-K2、full K2 的预注册 baseline matrix）。

---

## 9. 决策门与尚不应提前决定的事项

### D1. F7 benchmark 许可证和成本

接入前逐项确认下载、再分发、容器、模型输入和 leaderboard 条款。若 PaperBench 全量成本过高，
使用预注册分层子集，但不能临时挑容易题。

### D2. 文献全文来源

不能假定任何 API 提供合法全文。F8 需按开放获取、机构权限和用户提供资料分别建 policy。

### D3. 图数据库

默认 Postgres edge tables。只有查询/规模基准证明必要才引入 Neo4j 等额外系统。

### D4. 模拟引擎

F10-S5 用 ADR 选择一个栈，依据是许可、硬件、可容器化、参考基准和团队知识；计划不预设答案。

### D5. durable queue

F11 可用 Postgres queue、Temporal/Celery 等，但必须先明确 exact delivery、lease、workflow
versioning 和运维成本。科学状态始终留在 Aletheia ledger，不由 queue history 独占。

### D6. 第一现实实验

由 F8–F10 的证据和合作条件共同决定。不能为了展示“湿实验”预先选择一个容易得到正结果、
但科学价值低的问题。

---

## 10. 主要风险与缓解

| 风险 | 表现 | 缓解 |
|---|---|---|
| Benchmark overfitting | public score 上升、private 无提升 | private prospective suite、test 退役、validation/test 分离 |
| Model upgrade masquerades as system gain | 换模型后声称架构进步 | 同模型消融矩阵、冻结 manifests |
| False novelty | 未检索到即 novel | F8 measured coverage、prior-art relations、temporal holdout |
| Causal storytelling | 相关性被写成机制 | F9 alternatives、prediction commitment、identification gate |
| Capability self-validation | AI 编写 evaluator 使自己通过 | executor/validator 分权、promotion fixtures |
| Long-run drift | 忘记 blocker、重复失败 | artifact-backed state、anti-loop、replay |
| Portfolio proxy gaming | 高 EIG/高分但无科学价值 | 多目标约束、replication debt、private outcome metrics |
| Physical invalid mistaken as negative | 设备/协议失败变成 refutation | observation validator、deviation taxonomy |
| Independence inflation | 同源数据称独立复现 | R0–R5 ladder、sample/site lineage |
| Literature/benchmark prompt injection | 文本诱导工具/结论 | untrusted-data boundary、least privilege、adversarial fixtures |
| Publication slop | 大量低质论文挤占评审 | bundle gate、人工发布批准、组合级错误率 |
| Scope explosion | 六项同时半成品 | F7 first、单项 engineering/scientific exit、依赖门 |

---

## 11. 相关工作与设计依据

以下工作用于定义能力边界，不表示照搬其架构：

1. **Co-Scientist** — 多 agent 假设生成、辩论、演化和专家/实验验证。启示：扩大 hypothesis
   search，但内部 Elo 不能代替真实证据。<br>
   <https://www.nature.com/articles/s41586-026-10644-y>
2. **Empirical Research Assistance (ERA)** — LLM + tree search 在有客观质量函数的科学软件
   问题上进行规模化搜索。启示：F10 可采用分支搜索，但 evaluator 必须外置。<br>
   <https://www.nature.com/articles/s41586-026-10658-6>
3. **AlphaEvolve** — 演化式程序搜索与自动 evaluator。启示：开放搜索最适合可验证行动空间。<br>
   <https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/>
4. **Kosmos** — 长时程文献/数据分析和结构化 world model。启示：F11 必须把长期状态移出
   prompt；其公开结果仍需独立验证。<br>
   <https://arxiv.org/abs/2511.02824>
5. **HEP** — 显式、可审计的 hypothesis–test–evidence–belief protocol。启示：可作为 F9/K3
   的直接消融基线。<br>
   <https://arxiv.org/abs/2607.09195>
6. **Robin** — 文献、实验建议、实验数据分析和后续假设的 lab-in-the-loop 生物闭环。启示：
   F12 不需要从机器人开始，人类可以执行而不承担结果挑选。<br>
   <https://www.nature.com/articles/s41586-026-10652-y>
7. **A-Lab** — 材料合成、表征与 active learning 的自动闭环。启示：真实 observation
   contract 必须包含仪器、样品和失败状态。<br>
   <https://www.nature.com/articles/s41586-023-06734-w>
8. **Coscientist** — LLM 规划器连接文献、代码和化学设备。启示：工具接入必须伴随安全和
   外部动作 policy。<br>
   <https://www.nature.com/articles/s41586-023-06792-0>
9. **AI Scientist-v2** — template-free 研究与 agentic tree search。启示：论文被接收只能是
   output-quality evidence，不能成为 discovery ground truth。<br>
   <https://arxiv.org/abs/2504.08066>
10. **ScienceAgentBench** — 真实数据驱动科学编码任务。启示：端到端主张前必须测单项能力。<br>
    <https://arxiv.org/abs/2410.05080>
11. **PaperBench** — 从论文到复现实验的长程评测。启示：复现是进入新颖发现前的必要基线。<br>
    <https://openai.com/index/paperbench/>
12. **CORE-Bench** — 跨学科计算复现。<br>
    <https://arxiv.org/abs/2409.11363>
13. **DiscoveryWorld** — 隐藏规律环境中的完整科学发现循环。启示：评价“发现了规律”而非只
    看终局任务。<br>
    <https://arxiv.org/abs/2406.06769>
14. **AstaBench** — 文献、编码、数据分析和端到端科学 agent 评估套件。<br>
    <https://allenai.org/asta/bench>
15. **MLRC-Bench** — 用客观任务性能评估方法创新，并揭示 LLM 自评创新与真实性能错位。<br>
    <https://arxiv.org/abs/2504.09702>

外部系统的结果不直接构成 Aletheia 的 acceptance threshold。阈值由 F7 validation、
专家 baseline、预算和私有 test 共同冻结。

---

## 12. 最终验收：Frontier Scientist Gate

当 F7–F12 都声称完成时，执行一次事先注册的总验收。输入只包含：

- 一个宽泛 quest；
- 可用数据/工具/实验站点；
- 预算与时间；
- 安全、伦理和发布限制。

不得由人提供：

- 最终假设；
- headline statistic；
- 想要的结果方向；
- 候选机制；
- 应排除的特定替代解释；
- 哪个分支应获胜。

必须观察到：

1. 冻结知识快照和 measured coverage；
2. 至少三个可区分竞争假设；
3. 多轮实验，至少一个有效负结果；
4. 由 negative result 引发的实质 revision/pivot；
5. 至少一个结构/模拟/现实实验；
6. one-time locked confirmation；
7. R4 第二站点盲法复现；
8. 完整 family ledger；
9. claim-to-source/code/raw-observation lineage；
10. 第三方 bundle replay；
11. 组合级 false discovery、calibration、成本和人工干预报告；
12. 人工发布审批前无自动对外宣称。

验收输出只有以下状态：

- frontier_candidate_passed；
- capability_demonstrated_but_not_reliable；
- scientifically_inconclusive；
- results_refuted；
- invalid_or_protocol_breached；
- blocked_by_governance_or_resources。

results_refuted 不是工程失败；invalid_or_protocol_breached 才是硬失败。一个正结果若违反协议，
不得通过。

---

## 13. 下一步

本计划获批后的第一项实施工作应是：

> **PF-1 + PF-2 + F7-S1：建立 migration baseline、Run Manifest v1 和 Frontier Gate threat
> model/evaluation contract。**

在独立 evaluator boundary 和 frozen acceptance config 建成前，不开始 F8–F12 的大规模功能
开发；允许只做不接 driver 的 schema spike 和 benchmark/license 调研。

F7 第一次真实结果出来后，依据失败画像确定 F8 的先后次序：

- 若检索/引用错误占主导，先做 corpus snapshot、source span 和 coverage；
- 若科学代码/复现错误占主导，先修 executor/tool capability，再继续 novelty；
- 若隐藏世界主要失败在单假设固着，再加速 F9；
- 若主要失败是运行一致性，提前 F11 durable foundation，但仍不开放长期科学自治。

这样后续投入由独立证据决定，而不是由路线图本身的叙事惯性决定。
