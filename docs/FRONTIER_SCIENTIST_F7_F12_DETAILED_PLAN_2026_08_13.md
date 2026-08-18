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

实现进度（2026-08-14）：F7-S6 / issue 11 已工程完成。`SuiteCalibrationPlan` 在 validation
执行前冻结参考基线、统计规则和 validation/test 身份；`SuiteAcceptanceConfig` 只从完整
validation ledger 与签名回执推导阈值；program config 在 held-out test 前冻结四轨声明。最终
report 不接受手工 aggregate，而是重跑 raw result/ledger/HMAC 对账，输出 JSON、Markdown、SVG
及逐 attempt receipt index。缺轨为 `BLOCKED`，完整实测未达标为 `FAIL`，仅全部公有轨与私有
custody 闭环通过才允许 `PASS`。这是工程完成状态；当前仓库没有真实四轨运行，因此没有科学
Frontier Gate pass。

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

**工程状态（2026-08-14）：隔离 harness 已完成。** 已实现 deterministic core term contract、
frozen adapter/parser manifest、metadata-only content-addressed response/ledger archive、逐页成功/
失败记录、同 parser replay、全新命中机械派生的 forward/backward citation rounds、whole-round
预算与 saturation/source-exhaustion stopping，以及四项不可由调用方伪造的 fail-closed search
coverage signals。36 个 F8-S2 新测试通过，完整 knowledge suite 为 66 passed。仍无 live provider、
真实 known-answer/temporal calibration 或 driver/novelty wiring；这些非能力不能由“工程完成”推断。
最终全项目非 Docker 验收为 733 passed、1 skipped、29 deselected（290.81 s），真实 Docker 隔离组
为 29 passed、734 deselected（26.62 s）。

### F8-S3：Atomic claim extraction

- schema-first extraction；
- 数值、单位、population、条件和不确定性必须分字段；
- 每个抽取必须关联 source span；
- 低 OCR/抽取置信度进入人工或第二模型复核队列；
- contradictory edges 不被摘要阶段抹平。

**工程状态（2026-08-15）：隔离 harness 已完成。** 已实现 frozen extractor/output-schema
manifest、逐 span 的显式 `span_extraction`/`model_input` 授权与过期检查、临时非持久化原文、
document/exact-span/normalized/locator 四重 identity 校验、严格数值/单位/population/条件/不确定性
结构化输出、完整 attempt/failure ledger、OCR/六类低置信原因机械派生的 review queue、独立人类/
second-model accept/revise/reject、supports/refutes/qualifies/mentions 保留、每个 prior-art claim 到
exact span 的 graph closure，以及 execution/resolution/graph 三层 write-once ledger 与不重采样模型的
derivation replay。37 个 F8-S3 新测试使用 synthetic licensed fixtures；仍无 production content
resolver/model extractor、真实 extraction calibration、driver/novelty wiring 或科学结论。下一工程切片
为 F8-S4 nearest-prior-art matcher。权威回归结果为完整 knowledge 103 passed；全项目非 Docker
770 passed、1 skipped、29 deselected（296.81 s）；真实 Docker 隔离组 29 passed、771 deselected
（37.82 s）。

### F8-S4：Nearest-prior-art matcher

- lexical、embedding、citation 和结构化实体多路召回；
- cross-encoder/模型 rerank 只改变候选顺序，不删除审计轨迹；
- 输出 equivalent/subsumes/special-case/extension/combination/contradiction 关系；
- 对候选 claim 做 component-wise difference。

**工程状态（2026-08-15）：隔离 harness 已完成。** 已实现 exact reviewed graph/pool 绑定、四路
frozen recall/index/scorer manifest 与零工具权限、每 candidate/channel 完整 attempt/failure ledger、
single-channel hit 保留但不得形成正式 relation、从 result receipts 机械重算完整 union、reranker 对
union 每项同序打分且不得删除/换对、harness 控制最终排序和 relation budget、六类严格 relation 与十类
component difference/source-span closure、blocking/低 channel/低 relation/低 difference confidence
机械 review queue、独立 human/second-model accept/revise/reject、拒绝后连续重排且保留 original
candidate identity，以及 execution/resolution write-once ledger。52 个 F8-S4 synthetic tests 与完整
knowledge 155 passed；仍无 production indexes/adapters/matcher、真实 known-answer recall/关系精度/
置信标定、temporal false-novelty 或 driver wiring。下一工程切片为 F8-S5 calibrated coverage and
novelty acceptance；最终非 Docker 为 822 passed、1 skipped、29 deselected（296.75 s），真实 Docker
隔离组为 29 passed、823 deselected（26.57 s）。

### F8-S5：Coverage 与 novelty acceptance

- 在已知答案 review set 上标定 recall；
- 用历史时间切分测试 false novelty；
- 冻结强/弱 novelty 的 health 门槛；
- wiring 到 direction gate、scorecard、claim strength 和 write-up；
- coverage unknown 时 novelty claim 最高为 speculative/unverified。

**工程状态（2026-08-15）：校准、coverage、review/claim ceiling 与显式 direction callback 已完成；
scientific exit 未完成。** `NoveltyCalibrationSuite` 冻结 40+40 validation/严格更晚 temporal
holdout、每 split 至少 30 个 known non-strong + 10 个 strong label、每 case 至少三种语义保持 variant、
evaluator-only label commitment、candidate-author exclusion 与 exact system/evaluator/classifier identity。
每 case/variant 必须有不可复用的 HMAC receipt；错误只保留 class/message hash 且任一 trial error fail
closed。两个 split 分别机械计算 known-answer/seed recall、classification、false/missed strong novelty、
perturbation stability、MRR，并以 one-sided 95% Wilson lower/upper bound 而非点估计过门。

live `CalibratedNoveltyCoverageAssessment` 无 caller observation 参数：六项外部 signal 分别从 temporal
calibration lower bound、replayed seed hits、licensed full-text grants、immutable source spans、bound
correction report 推导；F8-S2 继续推导另外四项。global calibration fail、任何 hard signal 或任一
candidate 少于三条 resolved prior relation 均阻断。authorship manifest 与 exact evidence package 之后，
至少 domain expert + research librarian 的 author-excluded confirm 才能授权方向；known work 被拒绝，
incremental/contradictory 只可 weak bounded advance，满足全部条件的 strong class 最高也仅 moderate，
coverage insufficient 固定 indeterminate/speculative。`discover()` 已支持 exact candidate-claim-bound
F8-S5 callback，旧 count+critic 路径仅为兼容路径；default scheduler、scorecard/write-up 尚未自动生产/
消费整套 artifact。59 个 F8-S5 synthetic tests 与完整 knowledge 214 passed。80-case/240-trial fixture
不是 real expert corpus、真实 temporal false-novelty 结果或 scientific exit；production adapters、private
custody、真实领域标定和 prospective run 仍是发布门。下一工程切片为 F8-S6 protocol-safe SOTA
comparator integration。

### F8-S6：SOTA comparator

- 对 DatasetVersion、MetricDefinition、ProtocolSignature 做 canonicalization；
- curated fixtures 覆盖“数字相同但协议不同”；
- 自动生成 comparability matrix；
- 只有 comparable row 能进入 headline delta。

**工程状态（2026-08-15）：已完成；scientific exit 未完成。** 在原有逐维
`ProtocolSignature` 比较之上，现已加入与 F8-S5 authorized direction、coverage、reference search 和
corpus 精确绑定的 pre-sealed reference registry；candidate author 不得选择或 review reference，至少三条
required reference 必须在 candidate protocol/result 前冻结。无工具 evaluator 对 candidate 与每个
reference 的同一组 paired replicate/partition 签发 HMAC receipt，error 保留 class/message hash 而不造
分数；execution/prediction artifact 不得复用。完整 matrix 对每个 comparable row 机械计算
direction-normalized delta、exact one-sided paired sign test、Holm correction 与预注册 practical margin，
只有每条 reference 同时统计且实际优于才生成 `sota_confirmed`，任一 unbeaten row 为
`sota_not_demonstrated`，任一 error/non-comparable 为 `sota_blocked_evidence`。

campaign commit/load 重验 canonical JSON、全部 derivation 和签名。WRITE_UP 另有显式 injected
consumer，必须精确匹配当前 candidate protocol hash、metric identity 与 signed aggregate；provider
错误或 identity mismatch 固定为 weak/unverified 且不会 fallback 到旧 scalar shortcut。36 个 F8-S6
focused tests 和 262 个 write-up/evidence/knowledge integration tests 已通过；最终完整回归数字见
`F8_S6_PROTOCOL_SAFE_SOTA_IMPLEMENTATION_REPORT_2026_08_15.md`：917 个非 Docker 测试通过、1 skip、
29 deselected（532.77 s），29 个真实 Docker 测试通过、918 deselected（45.30 s）。fixture 的
3 references/10 repeats
完全 synthetic，不是 published-method reproduction 或真实 SOTA。F8-S1–S6 工程合同至此完成；真实
expert novelty calibration、private temporal custody、production reference completeness、真实 reference
reproduction/prospective run 与 F7 Frontier Gate 仍是 scientific release gates。仓库能力下一切片为
F9-S1 competing causal-hypothesis graph contract。

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

**工程状态（2026-08-15）：已完成；scientific exit 未完成。** 新增 frozen
`ResearchQuestion`、`HypothesisVersion`、`Assumption`、`Prediction` 和 multi-hypothesis
`BeliefState` 合同。稳定 lineage ID 与 canonical content SHA-256 分离；任何 revision 必须精确引用同
lineage 的直接 parent，旧版本不覆盖。完整 `WorldModelSnapshot` 至少包含 H0、一个主解释和一个可信
替代解释，每个 hypothesis 都有 exact-bound assumption 与 discriminating prediction，belief vector 必须
覆盖相同版本并归一化。Alembic `20260815_0004` 加入 7 张 append-only 表、外键/唯一约束和拒绝
update/delete 的 trigger；commit/load 重验 normalized members 与 payload hash。

既有 K2 `belief_states`、service 和 event 不迁移、不改写；只增加带
`legacy_k2_beta_bernoulli` 标签的 read-only compatibility view，并显式禁止把其单命题 Beta mean 当成
F9 posterior。31 个 schema/persistence/migration/history/compatibility tests 与 136 个 K2/F9 integration
tests 已通过；最终全库非 Docker 938 passed、1 skipped、29 deselected（314.65 s），真实 Docker
29 passed、939 deselected（38.37 s）。该 fixture 完全 synthetic，尚未证明 alternatives 的可信度、
因果 identification、prediction precommitment、likelihood、EIG 或 posterior update。下一切片为 F9-S2
competing-hypothesis generator。

### F9-S2：Competing-hypothesis generator

- 每个 mechanism 题至少生成 H0、主解释和可信替代解释；
- F8 knowledge graph 用于找文献中的常见 confound 和替代机制；
- 去除语义重复假设；
- 要求每个假设给出不同的可观测预测；
- 若无法形成有区分度的 alternatives，mechanism 研究阻塞或降为 descriptive。

**工程状态（2026-08-15）：已完成；scientific exit 未完成。** `HypothesisGenerationRequest`
精确绑定 experiment-authorized F8 gate、candidate、corpus、claim graph、prior-art resolution、完整
claim/relation 集和 frozen manifests/policy；任一 rebinding 都在 generator 调用前失败。generator 与
semantic de-duplicator 使用独立 principal/manifest，均无 tool authority 或 observation access。所有
valid raw drafts 保留；独立 reviewer 必须完成 `n(n-1)/2` 个 exact-draft pair judgments，harness 阻塞
uncertain/low-confidence、cross-role、non-transitive 和 deterministic-normalizer 冲突，并输出显式
duplicate→canonical provenance。

H0/primary 必须绑定 candidate claim，每个 alternative 必须绑定 accepted F8 relation 连接的 prior
claim；每个 kept pair 必须在同一 observable、measurement protocol 和 finite outcome space 上有双向、
不同 expected outcome 的 prediction witness。只有全通过才机械生成 uniform-prior F9-S1 snapshot；
failure 只保留 hash，campaign 可 content-addressed archive，且仅 ready campaign 可持久化。Focused
F9-S2 26 tests、F8 direction + F9 integration 61 tests 已通过。fixture、机制、semantic labels 和
protocol 均为 synthetic；尚未证明真实 hypothesis quality、feasibility、causal identification 或 belief
calibration。最终全库非 Docker 964 passed、1 skipped、29 deselected（321.28 s），真实 Docker
29 passed、965 deselected（38.08 s）。Docker 首轮唯一失败为 evaluator-owned ScienceAgentBench
candidate container 45 秒冷启动超时；精确用例随后 1.27 秒通过，完整 29 项复跑通过，未放宽任何
timeout/sandbox/scorer policy。下一工程切片为 F9-S3 causal contract 与 identification audit。

### F9-S3：Causal contract 与 identification audit

- 明确 variable、edge、latent confound、selection、measurement process；
- 静态检测 cycle、未定义变量、不可观测 endpoint；
- reviewer 审核 identification assumptions；
- assumption unresolved 时限制 claim strength；
- 将 causal graph 作为 evidence artifact，而不是 prompt 内 prose。

**工程状态（2026-08-15）：已完成；scientific exit 未完成。** 新增 shared typed variable registry、
exact-bound per-hypothesis graph、directed edge、latent confounder、measurement/selection process、
single total-effect estimand/adjustment set 和 scoped identification assumptions。request 精确绑定 ready
F9-S2 campaign、F8 gate/claims/relations 与 F9-S1 snapshot/question/hypothesis versions，且 author/
reviewer 均无 tool 或 observation access。harness 机械检测 undefined reference、duplicate relation、DAG
cycle、invalid/descendant adjustment、H0 偷渡 effect path、mechanism 缺 path、endpoint/protocol rebinding、
assumption/evidence closure 和 capacity。

当前数学边界只实现并明确命名 Pearl back-door criterion：删除 exposure outgoing arrows 后构造
ancestral moral graph，记录 causal/open path witness；observed/latent fork 和 conditioned-collider gold
cases 已通过。back-door failure 不冒充 general non-identifiability；front-door、IV、general ID 和
selection recoverability 显式 unsupported/bounded。独立 reviewer 必须完整裁定每条 frozen assumption；
reject 阻塞，unresolved/low-confidence 或 open path 将 future claim ceiling 限为 association。
即使 `ready_identified` 也只表示在 reviewed assumptions 下通过该图准则，proposed evidence kind 继续
将 descriptive/observational/simulation/controlled 的未来主张分级，绝不表示已观察到 causal effect。
Focused 38 tests、截至 F9-S3 的 `tests/epistemics` 85 tests 已通过；fixtures 全为 synthetic。其后
F9-S4 已实现 pre-observation prediction commitment 与 likelihood contract。F9-S3 当时的 F8 direction
+ F9 integration 为 99 passed；当时全库非 Docker 1002 passed、1 skipped、29 deselected
（328.30 s），真实 Docker 29 passed、1003 deselected（26.27 s）。

### F9-S4：Prediction commitment 与 likelihood

- 实现 immutable prediction receipt；
- observation staging 物理依赖 receipt；
- continuous outcome 的预注册 binning/likelihood；
- probability calibration 与 degeneracy probe；
- post-observation mutation 必须被拒绝并记录 security/science violation。

**工程状态（2026-08-15）：已完成；scientific exit 未完成。** `PredictionCommitmentRequest` 精确绑定
authorized F9-S3/F9-S2/F9-S1 chain、causal contract、完整 experiment protocol、outcome schema、author/
independent calibration evaluator manifests、policy 与 issue time，且 author 明确无 observation/tool
access。protocol 冻结 intervention/population、measurement/error model、analysis、exclusion、stopping、
parser，并机械派生 experiment namespace；categorical 或 continuous-binned outcome 必须覆盖每个 F9-S2
prediction 的同一 outcome space，continuous bins 要求 units、连续边界、两端开放且每个边界唯一归属。

probabilistic mode 每个 active hypothesis 提交完整归一化 mass、唯一且与 F9-S2 一致的 modal outcome、
由 calibrated frozen likelihood family + hypothesis version + protocol/schema 机械派生的 likelihood hash，
以及 measurement-error sensitivity scenarios。independent historical report 的每条 prediction 必须先于
其 observation、不得早于 predictor freeze、validation namespace 不得等于 target；harness 重算
multiclass Brier、log loss、top-label ECE 与 zero-probability observations。entropy、probability extremes、
pairwise total variation、sensitivity coverage/stability 再决定 `ready`、`blocked_calibration` 或
`blocked_degeneracy`。ordinal mode 只提交完整排序，可 ready 但永远 `eig_eligible=false`。

campaign canonical archive 读时重算全部 derivation；substantive `commitment_sha256` 排除 operational
retry labels/time。`ObservationStagingStore` 必须先从 archive 读取 ready receipt 并证明
`observed_at > committed_at`，再 atomic seal experiment namespace 与写入 content-addressed raw bytes。
exact retry 可复用；同 namespace 的 changed commitment 在 raw write 前被拒绝，并持久化
`security_and_scientific_integrity` violation。Focused 30 tests、当前完整 `tests/epistemics` 115 tests
通过；最终全库非 Docker 1032 passed、1 skipped、29 deselected（316.57 s），真实 Docker 29
passed、1033 deselected（37.58 s）。fixtures/calibration/observations 全为 synthetic。完整验收见
`F9_S4_PREOBSERVATION_PREDICTION_COMMITMENT_IMPLEMENTATION_REPORT_2026_08_15.md`。下一工程切片为
F9-S5 observation-blind constrained experiment selector；真实 likelihood calibration、experiment
execution/validation、posterior update 与 replication 仍是发布门。

### F9-S5：Experiment selector

- 计算 EIG/discrimination；
- 纳入成本、时间、风险、fresh confirmation availability 和 replication debt；
- 输出候选排名及未选原因；
- selector 本身不能看隐藏 observation；
- 对 proxy gaming 建立 fixture：高 EIG 但无测量效度的实验必须被拒绝。

**工程状态（2026-08-15）：已完成；scientific exit 未完成。**
`ExperimentSelectionRequest` 要求至少两个 canonical candidate，精确共享同一 F9-S2 hypothesis
campaign、F9-S1 snapshot/belief/question，且每个 candidate 必须是不同 experiment namespace 与不同
substantive F9-S4 commitment。selector 在任何评分前从 content-addressed archive 物理读取、rehash 并
重验所有 prediction campaign；任一 missing/corrupt/rebound archive 使整次选择
`blocked_execution`，不保留 partial ranking，只记录 failure class 与 detail hash。

对 `ready + probabilistic + eig_eligible` candidate，harness 从 exact F9-S1 prior 和完整 F9-S4
likelihood 机械计算每个 outcome marginal、每个 hypothetical posterior/entropy、expected posterior
entropy、absolute/normalized EIG，以及所有 hypothesis pair 的 minimum/maximum total variation。
ordinal 或 F9-S4 blocked campaign 仍保留，但不进入概率 EIG。cost/currency、duration、high/prohibited
risk、measurement validity/confidence、任何 proxy risk、missing capability、fresh confirmation 缺失/
过期/复用 calibration 或 target partition，以及 EIG/TV floors 都是 utility 前 hard blockers，高 EIG
不能抵消无效 proxy。

无 blockers 的 candidate 才使用 frozen sum-to-one weights，把 normalized EIG、minimum TV、fresh
confirmation、replication-debt reduction 与 policy-fixed cost/time/risk penalties 合并；不使用
candidate-relative scaling，避免 decoy 改变其他候选的分值。排序、tie-break、selected/
feasible-not-selected/infeasible reasons 与 no-feasible disposition 全由 harness 重算；没有 feasible
candidate 时明确不选，不 fallback。独立 assessor 与 request 均无 tool/observation access，并与此前
hypothesis/causal/prediction/calibration roles 做 principal/model identity 隔离。campaign 可 canonical
archive，commit wrapper 精确绑定提交时间/ledger 并提供 receipt，读取时重算 score/rank/decision。
Focused 30 tests、截至 F9-S5 的 `tests/epistemics` 145 tests
通过；最终全库非 Docker 1062 passed、1 skipped、29 deselected（380.76 s），真实 Docker 29
passed、1063 deselected（31.31 s）。fixtures、assessment evidence、budget、risk、capability、
partition 与 replication debt 全为 synthetic。完整验收见
`F9_S5_CONSTRAINED_EXPERIMENT_SELECTION_IMPLEMENTATION_REPORT_2026_08_15.md`。下一工程切片为
F9-S6 validated-observation posterior update、immutable revision 与 negative-result policy；真实
measurement/surrogate validation、atomic reservation/debt/budget state 与 scheduler execution 仍是发布门。

### F9-S6：Update、revision 与 negative result policy

- observation validator → update 的单向接口；
- 负结果后允许 retire、narrow、fork，不允许覆写；
- contradiction queue；
- 若所有假设预测相同，强制寻找新测量或停止；
- 若 posterior 对 likelihood 极敏感，状态标记 fragile。

**工程状态（2026-08-15）：已完成；scientific exit 未完成。**
F9-S6 把 observation validation 与 belief update 分成两个独立、可提交事务。validator policy、
adapter/parser/schema、principal 与可选 model identity 必须在 F9-S5 selection 前冻结，无 ambient tools，
且与 F9-S2 hypothesis、F9-S3 causal、F9-S4 prediction/calibration 和 F9-S5 assessment roles 做
principal/model 隔离。validation 在调用 adapter 前物理读取并 rehash F9-S5 selection、selected F9-S4
prediction 与 staged raw bytes；selection 必须先于 observation，outcome/experiment/protocol/parser/
analysis/measurement/error-model、fresh confirmation batch/partition 与 custody receipt 全部 exact-bound。

只有 confirmation role、verified experiment identity/custody、valid measurement、intact blinding、exact 或
preregistered-tolerance protocol、resolved-accept audit，以及满足 sample floor 或 exact pre-frozen
small-sample rule 的 observation 才得到 `validated_confirmation`。exploration/calibration、material/unknown
deviation、invalid measurement、blinding/custody/identity failure、unresolved/reject audit、reservation 或
schema rebinding 全部不触发 posterior；missing/corrupt/rebound archive/store、validator exception、malformed
或 future-dated output 只留下 sanitized hash failure，不保留 raw payload 或 partial evidence。

update request 只接受 committed `validated_confirmation`，API 明确
`observation_access=validated_artifact_only`，没有 raw bytes/store handle；在计算前再次从 archive 物理验证
validation campaign。harness 从 exact F9-S1 prior 与 selected F9-S4 likelihood 机械派生每个 hypothesis 的
prior、realized likelihood、unnormalized mass、posterior 与 modal-match，以及 entropy change、prior
predictive、surprisal、winner、primary-negative、all-model-miss 和 uninformative flags。所有 hypothesis
必须提供同一组 policy-minimum likelihood sensitivity scenarios；每个 alternative posterior、TV 与 winner
change 全保留，scenario matrix 不完整或 zero predictive mass 会 `blocked_likelihood`，超过 TV ceiling 或
winner change 标记 `updated_fragile`。

成功更新创建 exact-parent、`version + 1` 的 child `BeliefState` 与新 snapshot，question、hypothesis、
assumption、prediction versions 不变，source 永不 mutation。modal match → retain；modal miss 且 nominal
与全部 sensitivity posterior 均低于 retirement ceiling → retire；其余 miss → narrow。retire/narrow 仅发出
append-only new-version directive。all-model low likelihood 强制 fork 新 hypothesis lineage；realized
likelihood 相同强制 seek-new-measurement-or-stop；prediction miss、surprise、fragility、all-model miss 与
uninformative outcome 均进入 immutable open contradiction queue。validation/update campaign 都可 canonical
archive，model validation 重算 probe、posterior、sensitivity、revision、contradiction 与 disposition。

Focused F9-S6 31 tests（76.56 s）、F9-S5/S6 combined 61 tests（111.74 s）与截至 F9-S6 的完整
`tests/epistemics` 176 tests（149.44 s）已通过；最终全库非 Docker 1093 passed、1 skipped、29
deselected（528.15 s），真实 Docker clean rerun 29 passed、1094 deselected（26.10 s）。首次 Docker
matrix 的 image-environment probe 曾单次 timeout；该用例随即单独通过，完整 29-test matrix 再跑全绿，
未为基础设施抖动修改 application code。fixtures、likelihood、observation、validator evidence 与 custody
均为 synthetic。完整验收见
`F9_S6_VALIDATED_OBSERVATION_BELIEF_UPDATE_IMPLEMENTATION_REPORT_2026_08_15.md`。下一工程切片为
F9-S7 independent K3 acceptance scorer；真实 validator authentication、instrument/measurement audit、
transactional snapshot/directive persistence、reservation consumption、scheduler execution、adaptive loop 与
real-domain calibration/replication 仍是发布门。

### F9-S7：K3 acceptance scorer

在现有 k2_acceptance.py 旁新增独立 scorer，检查：

- active set 中存在非重复 competing hypotheses；
- prediction 均先于 observation；
- 更新数量与 valid observations 一一对应；
- selected experiment 确实区分至少两个高概率假设；
- mechanism claim 只在替代解释被证据排除后升级；
- negative result 导致 belief/model 范围真实变化；
- 所有版本、尝试和停止理由持久化。

**工程状态（2026-08-15）：独立 artifact scorer 已完成；scientific exit 未完成。**
`K3AcceptanceScorerManifest` 与 policy 在首次 F9-S5 selection 前冻结，deterministic、无 tools、仅访问
committed artifacts；scorer principal 与 persistence/terminal 以及 F9-S2–S6 全部 scientific/harness roles
隔离。`aletheia.scheduler.k3_acceptance.score_k3` 直接委托同一个
`aletheia.epistemics.acceptance.run_k3_acceptance`，不存在 scheduler 自己的第二套 verdict 公式。

每个 `K3RoundEvidence` 绑定一个 committed ready-selected F9-S5 campaign、恰好一个 committed F9-S6
validation attempt 与零/一个 update attempt；另有 committed `K3EvidenceLedger` 精确记录 selection/
validation/update receipts、source/child snapshots 与 beliefs、source/revised hypothesis 和 prediction
versions、revision directives/materializations、contradictions、mechanism-claim attempts 与 terminal action/
reasons。scorer 在评分前从四类 content-addressed archive 物理读取、rehash、nested revalidate 并要求
embedded == loaded；任何 missing/corrupt/noncanonical/rebound object 都 `blocked_execution`，不保留 partial
verification/checks。

scorer 机械派生 11 个 canonical checks：nonduplicate null/primary/alternative active set；prediction/
selection-before-observation chronology；validated observation ↔ update attempt 一一对应；selected
likelihood 对 high-belief rivals 的 pairwise TV discrimination；exact child-belief/round lineage；mechanism
claim causal ceiling + nominal/all-sensitivity alternative exclusion；primary-negative append-only revision；
contradiction exact persistence；attempt/snapshot/belief/hypothesis/prediction/directive 完整持久化；terminal
action 与 F9-S6 world directive 一致；以及至少一个 successful validated update。spine/integrity failure →
`rejected_integrity`；archive failure → `blocked_execution`；spine 完整但零 successful update 或 high-belief
discrimination 不足 → `partial_no_scientific_exit`；只有两项 exit gates 加完整 spine 才 `accepted`，因此
`0 valid == 0 updates` 不会真空 full pass。

issued descriptive/association claim 不得超过 F9-S3 ceiling；issued mechanism claim 还要求 robust update、
stable world set、target 在 nominal 和全部 likelihood sensitivity posteriors 均高于 floor、所有 competing
explanations 均低于 exclusion ceiling，且 target 被 retain。安全 withholding 可通过，越权 issuance 是
integrity failure。每个 narrow/retire directive 必须 materialize exact-parent child hypothesis；narrow 还必须
为 exact source prediction set 创建 `version + 1` prediction children 并改变 observable/outcome/direction/
discrimination/measurement 中至少一个可检验字段，只有改写 statement/rationale 而 prediction 不变会被拒绝。
terminal action/reasons 必须在最终证据后持久化并绑定 update/world-revision；scorer 只验证，不执行 outward
action。

Focused F9-S7 26 tests（149.25 s）与截至 F9-S7 的完整 `tests/epistemics` 202 tests（295.32 s）已通过。
最终全库非 Docker 1119 passed、1 skipped、29 deselected（652.80 s），真实 Docker 29 passed、1120
deselected（26.78 s），本轮 Docker 首次执行即全绿。
完整报告见
`F9_S7_INDEPENDENT_K3_ACCEPTANCE_IMPLEMENTATION_REPORT_2026_08_15.md`。所有 fixtures 与 accepted chain
仍为 synthetic；S7 evidence ledger 本身是 isolated content-addressed persistence。其 PostgreSQL/next-round
集成缺口已由 F9-S8 完成；F9-S9 随后完成 frozen K3-hidden-world vs K2/headline protocol 与
posterior-calibration/false-mechanism gate，F9-S10 又完成真实 Matbench
alternatives→experiment→validated update chain。hidden-world 仍无 live/private passing evidence，且 materials
v2 未通过 robust contraction，因此 F9.11 scientific exit 明确未完成。

### F9-S8：Transactional world-model continuation

**目标：** 把 F9-S6 child posterior、F9-S7 revision materialization 与下一轮因果/预测链之间的状态交接
变成原子、可重放、fail-closed 的正式协议，而不是由 scheduler 复制内存对象。

**工程状态（2026-08-15）：已完成；scientific exit 未完成。** 新增 content-addressed
`WorldModelTransition`：精确绑定一个 successful committed update 与 exact revision materialization set。
`narrow` 会在 materialized hypothesis/prediction children 之外，为所有绑定 assumption 创建 exact-parent
child，并创建 probability-preserving `hypothesis_revision` belief child，最终必须重新通过 closed
`WorldModelSnapshot` 验证；`retire` 或 hypothesis-set fork 不会伪装成普通续轮，而是返回
`hypothesis_set_fork_required` 且无 next snapshot。

Alembic `20260815_0005` 新增 immutable `epistemic_world_model_transitions`。source、posterior、standalone
revised versions、revision-closed next snapshot、transition row 与唯一
`f9_world_model_transition_committed` typed event 在同一个 PostgreSQL transaction 中写入；event 注入失败
测试证明全部 child objects 与 transition 一起 rollback，相同 retry 复用原 event。physical loader 重验
payload/index columns、所有 snapshots/version rows 与 exact event projection。

`load_authorized_next_round_source` 只有在 physical transition 与 committed independent F9-S7 verdict 的
final round、update receipt、persistence principal/timestamp、mandatory checks 与 terminal action 全部一致时，
才返回 exact `CausalWorldModelSource`。`continue_research`/`seek_new_measurement` 可继续，stop/fork 不可继续。
F9-S3 request/campaign 现在提交该 source 的完整 snapshot/hash；F9-S4–S7 从 causal campaign 的 effective
snapshot 取数。测试已真正运行第二个 F9-S3 causal audit，并验证其 hypothesis-version bindings 来自 child，
不是原始 F9-S2 prior。

Focused F9-S8 12 tests、完整 `tests/epistemics` 214 tests、全库非 Docker 1131 passed/1 skipped/29
deselected 与最终真实 Docker 29 passed/1132 deselected 已通过；完整 timing 与一次已确认瞬态的 Docker
timeout 见 `F9_S8_TRANSACTIONAL_WORLD_MODEL_CONTINUATION_IMPLEMENTATION_REPORT_2026_08_15.md`。仍缺
automatic retirement→F9-S2 replenishment、contradiction resolution、frozen hidden-world K3-vs-K2、真实
materials chain 与 F10 registered execution，因此下一工程切片是 F9 scientific-exit ablation harness。

### F9-S9：Frozen K3 hidden-world scientific-exit harness

**工程状态（2026-08-15）：协议、执行器、判分与 gate 已完成；live scientific exit 仍 blocked。**
新增独立三臂语义 `headline_metric` / `k2_single_hypothesis` /
`k3_competing_hypotheses`，没有复用或篡改 F7 `ALETHEIA_FULL_K2` 的历史含义。三臂必须共享 exact base
model、public task prompt、tools、budget、wall time、sampling、task/repeat slots 与 paired seeds；validation
至少 4 tasks × 3 repeats，test 至少 4 tasks × 5 repeats并绑定 validation parent。blocked arm order、所有
preregistered cells、infra retry lineage 与 no-best-of-N 都由 evaluator ledger 重建。

DiscoveryWorld trusted scorer 现在从 hidden rule + authoritative action trace 机械派生并签入
`scientific_exit_metrics`：wrong-explanation elimination、genuine discriminating trials、terminal multiclass
Brier、top-label confidence/correctness、mechanism claim/false mechanism 与 truth-preserving hypothesis
contraction。aggregate 物理重验 signed receipt、trace reproduction、metrics evidence hash 与 objective copy；
旧 scorer、缺失 trace、漏 attempt、伪签名或 ledger drift 都是 integrity error，不会伪装成科学负结果。

两个 primary paired effects 使用 task/repeat hierarchical bootstrap、exact sign test 与 Holm correction：
K3-vs-K2 wrong-explanation elimination，以及 K3-vs-headline discriminating-trial rate；另以
K3-vs-K2 scientific success 作 non-inferiority guard。pre-validation threshold policy 把 F7
`calibration_error` 映射到 fixed-bin top-label ECE，把 `false_discovery_rate` 映射到 false-mechanism rate，
并同时冻结 Brier、claim coverage、contraction、validity、practical effect、CI、multiplicity、intervention
和 contamination 门槛。validation 必须先通过，acceptance config 必须早于 test。

`scripts/real_k3_hidden_world_e2e.py` 已支持 protocol inspection、materialize、run、aggregate、
freeze-acceptance 与 decide；`configs/evals/k3_hidden_world_v1.yaml` 的 hash-checked protocol 已冻结。当前
命令诚实返回 `scientific_exit_readiness: blocked`：公开 DiscoveryWorld scorer-bound validation suite 已重新
冻结但只能做 diagnostic；正式 exit 仍需 provider snapshot receipt、三臂 runner、private prospective hidden
suite、passing F7 custody 与 live receipts。完整报告见
`F9_S9_K3_HIDDEN_WORLD_SCIENTIFIC_EXIT_HARNESS_IMPLEMENTATION_REPORT_2026_08_15.md`。

### F9-S10：Authenticated real-materials evidence chain

**工程状态（2026-08-15）：真实链已完成；robust contraction 未通过，scientific exit 仍 blocked。**
新增独立 materials K3 protocol：在 `matbench_expt_gap` 上同时维护“无实质压缩”、“未见化学体系产生额外
外推压缩”和“随机森林通用收缩”三种解释。两个 observation-blind 候选实验共享 prior；EIG 机械选择
unseen-system vs represented-system control（0.380368 nats），而不是 random-holdout-only（0.003148
nats）。模型、chemical-system hash split、cluster-bootstrap、outcome rule、nominal/conservative/skeptical
likelihood 与所有阈值均在加载确认数据前冻结。

measurement 与 validation 使用不同本地 HMAC key/principal；validator 重新加载 4604 条真实材料记录、重建
Magpie features、重新分区/训练/预测/自助法，只有 exact result match 才签发 validation。belief updater 只接受
该 signed validation，完整重算三组 posterior；mechanism claim 因为只是 model diagnostic 而强制 withheld。
retirement 必须在所有 likelihood-sensitivity scenario 中低于 floor，nominal-only retirement 已被拒绝。

v1/seed 20260816 得到 unseen-specific outcome，但 audit 发现旧 revision 指令只看 nominal posterior；证据和
对应源码保留，terminal revision 明确 superseded。v2 使用新 implementation 与未打开的 seed 20260817，得到
unseen/control compression 0.2409/0.1948，delta 0.0461，95% cluster-bootstrap CI
[-0.0140, 0.1145]，按 frozen rule 为 `generic_model_shrinkage`。H2 在所有 sensitivity scenario 中获胜，
但最弱 effective-hypothesis-count contraction 仅 0.0134，低于 0.10 gate；最终 disposition 是
`valid_update_without_robust_contraction`，不得挑选较有利的 v1 伪造 exit。

完整实现与运行报告见
`F9_S10_REAL_MATERIALS_EVIDENCE_CHAIN_IMPLEMENTATION_REPORT_2026_08_15.md`，operator runbook 见
`benchmarks/K3_REAL_MATERIALS_EVIDENCE_CHAIN.md`。后续 F10 矩阵已按要求预注册并完整运行；见 F10-S1
状态。不得继续试 seed 直到显著。

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
aletheia/evals/k3_hidden_world.py
scripts/real_k3_hidden_world_e2e.py
~~~

driver.py 目前职责过多。F9 不应继续把全部逻辑塞进 Driver；先抽出纯函数和 service，再让 driver
编排。科学数学、持久化和 orchestration 三层应能单独测试。

## F9.10 测试

~~~text
tests/epistemics/test_hypothesis_versions.py
tests/epistemics/test_hypothesis_generation.py
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
- K3-vs-K2/headline hidden-world matrix、truth-relative endpoints、paired statistics 与
  validation-before-test scientific-exit gate 可从 signed raw receipts 独立重建。

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

该命令当前只验证冻结协议并列出 live blockers；只有提供独立 runner、provider receipt、private prospective
suite/custody 与 raw signed executions 后，`decide` 才可能给出 formal PASS。公开 DiscoveryWorld 即使 measured
criteria 全过也必须是 BLOCKED，不能冒充 contamination-resistant scientific exit。

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

**工程状态（2026-08-15）：核心 registry 与真实五分区矩阵完成；capability 仍为 provisional。**
新增 immutable semantic-version registry、exact observation-blind planner、四角色绑定、evidence/claim
ceiling、controls/assumptions/failure/resource/nondeterminism/reproduction/safety/license contract，以及
create-only CLI。confirmatory query 对 provisional capability 同时返回 `capability_not_registered` 与
`evidence_level_insufficient`，不做 fuzzy fallback。

首次冻结的 v1 manifest 被发现 output schema 与真实 executor result 不一致；该对象没有改写。v2.0.0 以
exact v1 hash supersede，schema 内容变化现在强制 major bump，registry v2 同时保留两版。真实 replication
plan 在任何测量前冻结 20260818–20260822 五个 seed、每槽一次 measurement、两次 exact recomputation、
全槽保留和 4/5 consensus；同一公开数据集的 partitions 禁止做 joint Bayesian pseudo-replication。

五槽结果是 2 unseen-specific、2 generic-shrinkage、1 ambiguous；五个 delta 全正，但只有两个
cluster-bootstrap interval 严格高于零，故 frozen aggregate 为 `partition_sensitive`。第三轮 audit 又物理
重算全部五槽并验证 signatures/update/aggregation。它是 exploratory capability demonstration，不是
registered capability、external replication 或 mechanism evidence。完整报告见
`F10_S1_CAPABILITY_REGISTRY_AND_REPLICATION_IMPLEMENTATION_REPORT_2026_08_15.md`，runbook 见
`benchmarks/F10_MATERIALS_CAPABILITY_REPLICATION.md`。下一工程切片是 F10-S2 typed observation pipeline。

### F10-S2：Typed observation pipeline

- raw output → parser → candidate observation → validator → validated observation；
- 原始文件和解析后值都保留；
- units、uncertainty 和 condition 必填；
- invalid 与 negative 分开；
- validated observation 才能进入 F9 update。

**工程状态（2026-08-15）：通用 typed pipeline 与真实 materials exact-reexecution 已完成。**
raw executor status/bytes、parser candidate、domain validator report 与 harness-derived admission
成为四个独立、content-addressed 层；raw archive 每次 parse/validate/load 都检查 regular file、byte count 和
SHA-256。successful candidate 必须有 quantity kind、UCUM literal、显式 uncertainty、sample count、method、
typed conditions 与 raw lineage。unit/uncertainty/condition/sample generic checks 不能由 parser 或 domain
validator 自行宣告通过。

终态区分 `validated_positive` / `validated_negative` / `validated_inconclusive`、
`rejected_invalid`、`blocked_execution`、`blocked_parser` 与 `blocked_validator`。F9 exploratory admission
只允许 validated 且 purpose=measurement；confirmatory 还要求 registered capability 与足够 evidence level；
exact reexecution/fixture 永远不能作为新 evidence 重复计数。

materials manifest v2.1.0 追加 typed parser 与独立 raw reparser validator。已预声明重算 frozen slot-03 来
验证 negative-result preservation：真实 model result exact match，typed delta/CI/conditions、protocol、outcome
与 minimum-system checks 全部通过，终态为 `validated_negative`；因为 purpose 是 exact reexecution，两个 F9
admission flags 都是 false。随后 physical raw/ledger replay 与第二次 model recomputation 通过。完整报告见
`F10_S2_TYPED_OBSERVATION_PIPELINE_IMPLEMENTATION_REPORT_2026_08_15.md`，开发/运行说明见
`capabilities/TYPED_OBSERVATION_PIPELINE.md`。下一工程切片是 F10-S3 materials identity and measurement。

### F10-S3：Materials identity and measurement

- 规范 formula、structure、sample、batch；
- content hash 与来源许可；
- 重复/冲突/测量条件检查；
- split ledger 支持多层 identity；
- 建立小型 gold fixtures。

**工程状态（2026-08-15）：核心实现、gold fixtures 与真实数据能力审计已完成。** formula 现在在精确
pymatgen 版本策略下归约为元素整数比；structure 同时绑定 licensed CIF 原始字节与 conventional-standard
cell 投影，保留解析/有序性/空间群/体积质量信号；synthesis batch 与 physical sample 使用显式 issuer ID
和来源记录，sample 必须绑定 exact batch，缺失 structure/batch/sample 必须逐层声明，禁止以 formula
伪造 sample identity。

multi-level split policy 可独立要求 chemical-system/formula/structure/batch/sample/record 隔离，record
永远必选；missing identity 或任一 required level 跨 split 都机械产生 witness 并 fail closed。measurement
audit 冻结 property/unit conversion/method/condition/identity/conflict policy，区分 failed/invalid、exact
duplicate、same-sample repeat、condition-incompatible strata 与 unresolved conflict；冲突值不进入 pooling，
within-sample/between-batch/between-source variance 在样本不足时显式 `unavailable`，不制造 noise floor。

CC0 gold fixtures 证明同为 NaCl 的 rock-salt 与 CsCl-type formula identity 相同而 structure identity
不同，并覆盖 sample leakage、missing identity、bad unit/condition、failed execution、duplicate/conflict、
不兼容条件隔离、同 provenance 异值拒绝、三层 variance 和 derived-result tampering。F10-S3 新测试
15 passed；materials + capability focused suite 43 passed。

最终权威宿主环境 non-Docker 回归为 1180 passed、1 skipped、29 deselected（719.90 s）；沙箱首轮因
localhost PostgreSQL/网络被 policy 拒绝而产生环境失败，不计为代码验收结果。

真实 `matbench_expt_gap` 审计重哈希官方 37,200-byte gzip 并重算 4,604 行：得到 4,601 个 normalized
formula、3,705 个 chemical system 和三组/六行 unresolved same-composition collisions，最大 band-gap
range 2.30 eV。因为原表缺 structure/sample/batch/uncertainty/method/conditions/row-source，unit 仅为
dataset metadata，且 dataset-specific licence 未由 Matminer metadata 声明，终态诚实保持
`composition_benchmark_only`；碰撞不能被武断判为 duplicate、polymorph、repeat 或 conflict。完整报告见
`F10_S3_MATERIALS_IDENTITY_AND_MEASUREMENT_AUDIT_IMPLEMENTATION_REPORT_2026_08_15.md`，开发/审计说明见
`capabilities/MATERIALS_IDENTITY_AND_MEASUREMENT_AUDIT.md`。下一工程切片是 F10-S4 structure-aware
experiment。

### F10-S4：Structure-aware experiment

- 支持结构数据加载和质量 gate；
- 至少一个 structure-aware reference model；
- composition/structure matched protocol；
- ablation/control；
- 锁定 internal/external evaluation；
- 避免因模型容量/训练预算不同产生伪因果结论。

**工程状态（2026-08-15）：结构质量 gate、匹配对照、真实同数据集锁定评估与物理重放完成。**
新增 primitive-standard structure projection、ordered/site/volume/overlap/lattice/symmetry 全行 gate、
species-blind 27 维 geometry receipt，以及 source/formula/chemical-system/structure/feature 的完整 hash
lineage。实现 commitment 同时覆盖 experiment、structure、identity、Magpie 四个源码文件，并在 protocol
冻结 Matminer/NumPy/pandas/pymatgen/scikit-learn/spglib 六个包版本。

真实 `matbench_phonons` protocol 在任何 fit 前冻结 459,672-byte gzip、1,265 行、CC0 evidence、
60/20/20 chemical-system-disjoint split、512-tree fixed RF、三臂、within-role permutation、5,000 次
chemical-system cluster bootstrap 和 5% relative-MAE floor；plan 明确记录 fit count 0。三臂为
composition-only 132 维、composition + aligned structure 159 维、composition + permuted structure
control 159 维，后两者模型预算与 feature capacity 精确相同，无 tuning 或 best-of-N。

759/253/253 行及 650/216/216 个 chemical systems 完全隔离。internal/locked 的 aligned MAE 分别为
47.207/45.666 `cm-1`，matched control 为 98.750/87.530；relative improvement 为 52.20%/47.83%，
cluster-bootstrap 95% CI 分别 `[33.393, 73.451]` 与 `[22.414, 68.321]`，机械终态
`robust_aligned_structure_signal`。全数据/feature/split/model/bootstrap physical replay 精确复现 result hash
`f1384600dfbc8289e6643aae13e6dbb16b0b429c89e7325bd83c88cd8522bb29`。

结论只支持该 frozen retrospective DFPT task 上 aligned structure 的增量预测价值；locked role 仍来自同一
公开数据集，不是 external replication、prospective blind result、intervention、causal 或 mechanism
evidence。独立数据/实现 confirmation 保留为 F10-S6 release gate。完整报告见
`F10_S4_STRUCTURE_AWARE_EXPERIMENT_IMPLEMENTATION_REPORT_2026_08_15.md`，运行说明见
`capabilities/STRUCTURE_AWARE_MATERIALS_EXPERIMENT.md`。下一工程切片是 F10-S5 simulation capability。

### F10-S5：Simulation capability

- 完成 ADR；
- digest-pinned simulation image；
- reference systems 的能量/结构/收敛 gold；
- job receipt、checkpoint、timeout 和 quota；
- parser/validator 独立于 agent；
- 失败原因 taxonomy。

**工程状态（2026-08-15）：digest-pinned 经典势模拟边界与 reference calibration 已完成；能力保持
provisional。** ADR 选择 ASE workflow + pure-Python EMT 作为第一条便宜、确定性的工程校准路径，而不是把
EMT 冒充 DFT。最终 `linux/arm64` image ID、base digest、ASE/NumPy/SciPy 版本、worker/host/parser/validator
源码、exact Cu job、五点 ±4% EOS、quality/gold policy 与 claim ceiling 全部冻结；容器使用 no-network、
read-only root、drop ALL capabilities、no-new-privileges、non-root、32 PIDs、256 MiB、1 CPU、10 s timeout，
并限制 worker 输出为 allowlisted regular files、最多四个/8 MiB。

worker 每次 energy evaluation 后 atomic checkpoint，终态保留 input/checkpoint/result 或 failure、stdout/
stderr、Docker state 与 exact cleanup receipt。raw → parse → validate → bundle 分层重新打开
content-addressed bytes；validator 机械重算 execution/parse、完整点数、单调 volume、bracketing、interior
minimum、residual、bulk modulus、runtime、calculator/scan 与 exact gold 共 11 项。timeout、quota、
infrastructure、unsupported element、parse corruption、bad fit 和 gold mismatch 都保持 invalid/blocked，
不能伪装成 physical negative。

首次 formal v1 因 macOS system temp 未共享进 Colima 而 exit 125；failure bundle 未被覆盖。v2 exact-hash
supersede v1，只把 scratch 移到 workspace-backed archive parent。两次 distinct container attempts 均得到
`validated_classical_reference`，Cu fcc conventional lattice 为 3.589824595554312 Å（frozen ASE reference
3.589825 Å），result payload 精确相同；reproduction receipt 同时声明 same image/implementation repetition
不是 independent replication。

新 manifest `materials.simulation.ase_emt_eos_reference@1.0.0` 已进入 append-only registry v4，但所有角色
仍标记 agent-authored，parser/validator 共用一个源码模块且没有 independent promotion review，因此默认
discovery 拒绝、只有显式 allow-provisional 才可探索使用。它不是 registered capability、DFT、experimental、
transferability、causal 或 mechanism evidence。simulation focused 12 tests、materials + capabilities 64 tests
与最终全库 non-Docker `1201 passed, 1 skipped, 29 deselected`（731.80 s）通过；两个正式 v2 bundle 均从
content-addressed raw archive exact replay。完整报告见
`F10_S5_REPRODUCIBLE_SIMULATION_CAPABILITY_IMPLEMENTATION_REPORT_2026_08_15.md`，运行说明见
`capabilities/ASE_EMT_REFERENCE_SIMULATION.md`。F10-S5 core engineering slice 完成；独立 validator/reviewer、
OCI/SBOM/signature 与 DFT successor 保持 promotion gates，下一工程切片是 F10-S6 mechanistic campaign
template。

### F10-S6：Mechanistic campaign template

- 从 F8 选择一个知识边界清楚的问题；
- F9 创建竞争解释；
- 使用 C1–C4 中至少两类实验；
- 预注册机制判别；
- fresh confirmation；
- external dataset 或 independent implementation；
- 输出完整 evidence bundle。

**工程状态（2026-08-16）：template 已完成；真实 execution/scientific release 均 blocked。**
新增 `MechanisticCampaignProtocol` 将 exact F8 direction、F9 competing-hypothesis/causal campaign、至少
两个 unique probabilistic prediction campaigns、frozen capability registry snapshot、independently reviewed
C1–C4 qualifications/slots、fresh reservation、independence kind、budget 与 robust decision policy 关闭为一个
pre-observation lineage。每个
prediction/experiment namespace 只能被一个 slot 使用；至少需要两个 distinct families 且包含 C3/C4；slot
manifest 必须精确存在于 registry，implementation identity 必须等于 frozen executor。execution authorization
与 mechanism release 分离，因此 provisional capability 可做 exploratory execution，但不能自行升级主张。
generic action enum 不再自动获得 C1–C4 身份；`MechanisticCapabilityQualification` 必须 exact-bind manifest、
family、compatible action、evidence hash、role-independent domain reviewer 与 pre-plan freeze time。

每个结果必须通过 F10-S2 committed raw→parse→validate pipeline，run 在 protocol freeze 后开始且 exact-bind
protocol/input/manifest。另一个 pre-frozen、role-independent mapper 只能映射到对应 F9 outcome schema 已承诺的
bin。scorer 分别在 nominal 与全部 likelihood sensitivity scenarios 中要求同一个 unique winner 和最小概率
margin；ties、low margin 或 winner sensitivity 是 valid-but-inconclusive，lineage/validation failure 才是
invalid。跨 slot 只检查 robust winner concordance，`joint_posterior_computed=false`，不通过相乘 correlated
likelihood 制造 pseudo-replication。最终 ceiling 同时受 F9 causal ceiling、registered capability claim types、
confirmatory admission 与 fresh/independent release gates 约束；bundle validation 全量重算 assessments/decision。

13 个 focused tests 构建了完整但明确 synthetic 的
F8→F9→registered + family-qualified C2/C4→typed observations→fresh independent-implementation bundle，
并覆盖 out-of-registry manifest、same-family、provisional promotion、
low-margin、conflicting winner、preregistration/outcome-schema rebinding 与 decision tamper。该 fixture 的
`mechanism_candidate_supported` 只证明工程合同，绝非材料科学结果。
Materials + capabilities 交叉回归为 77 passed；最终全库 non-Docker 回归为 1214 passed、1 skipped、
29 deselected、2611 个既有 spglib deprecation warnings（814.82 s）。本切片没有新增 container executor，
因此没有把 F10-S5 的同实现 ASE/EMT runs 伪计为 fresh/independent S6 confirmation。

当前 registry v4 的 machine-readable audit hash 为
`d7fe32533ad2ea9853c35a56555d816f27b489e532a47cf6a29a10c7a89d003b`，明确返回
`execution_ready=false`、`scientific_release_ready=false`：缺 production F8 direction、ready F9
hypothesis/causal campaigns、任何 independently reviewed family qualification、两个 registered confirmatory
families、registered C3/C4、mechanism-capable claim contract、fresh reservation 与 independent confirmation。
完整报告见
`F10_S6_MECHANISTIC_CAMPAIGN_TEMPLATE_IMPLEMENTATION_REPORT_2026_08_16.md`，操作说明见
`capabilities/MECHANISTIC_CAMPAIGN_TEMPLATE.md`，架构决策见 ADR 0031。下一工程切片是 F10-S7 signed
capability authoring/promotion boundary；F10 scientific exit 仍需真实 prospective quest、fresh/independent
执行、hypothesis-set change、domain audit 与 private-baseline improvement。

### F10-S7：Capability authoring pipeline

- provisional sandbox authoring；
- test generation 不等于 validator；
- independent audit；
- promotion receipt；
- manifest registry 签名/权限；
- 恶意 capability tests。

**工程状态（2026-08-16）：signed authoring/promotion core 已完成；production registry 仍保持
零 registered。** 新增 `CapabilityPromotionPolicy`，用 raw Ed25519 public key 的 SHA-256 作为 key ID，
分别委派 sandbox attestation、test-suite attestation、independent validation、domain review、promotion
audit 与 registry promotion 六类权限。每类支持 distinct-principal threshold；key 同时受 exact domain、
capability prefix、validity、expiry 和 revocation 约束，跨权限 principal overlap 在 policy freeze 时直接
拒绝。canonical signature message 还绑定 protocol context、artifact kind/hash、policy hash、registry ID、
capability/domain 与 issuance time，避免跨类型/跨域 replay。private keys 从不进入任何 schema；CLI 只从
owner-only、非 symlink regular file 读取 raw/hex key，并 create-only/0600 写出 audit/update。

`SandboxAuthoringReceipt` 只接受 immutable image 下成功、非截断、含 exact sentinel 的 hard-sandbox
execution，并绑定 source-file index、source review 与全部 executable AI-authored role implementation hashes；
local-dev/mutable image 不能产生 promotable receipt。generated suite 在 validation 前冻结 reference、
adversarial、positive 和 negative fixtures。independent validator 必须 non-agent-authored、在执行前冻结、
与 executor adapter/所有 source roles/test generator/domain reviewer 分离，并机械绑定同一 suite、counts、
controls、reexecution 与 reproduction evidence。domain review 限定 claim/evidence ceiling；request 关闭全部
hash lineage 和时间顺序。

promotion auditor 对 exact current registry/latest provisional、四阶段 signatures、sandbox images、roles、
controls 与 reproduction gates 给出 signed approved/rejected receipt。只有 approved audit 才可交给另一
registry-promoter role；promotion receipt 同时绑定 request、audit、policy、source/target registry、source/
registered manifest 和 promoter principals。verifier 从 source 重建唯一 compatible successor 和 append-only
target；post-sign edit、rollback、stale-source/concurrent second promotion 均 fail closed。

20 个 focused tests 使用真实 immutable range-compression v1→v2.0→v2.1 lineage 构造明确 synthetic 的
v2.1 provisional→v2.2 registered 全升级，并覆盖 test-generator/validator 合并、agent self-validator、
control rebinding、pre-artifact attestation、signature forgery/wrong permission/out-of-scope、cross-role key
reuse、revocation、non-Docker authoring、registry tamper/rollback、stale-source race、删减 audit checks 和
group-readable/symlink key 与 frozen-output overwrite。该案例只满足 engineering conformance，不代表真实
独立 reviewer 或 scientific result。

最终 capabilities + materials 交叉回归为 97 passed；权威全库 non-Docker 回归为 1234 passed、1 skipped、
29 deselected、2611 个既有 spglib deprecation warnings（794.56 s）。新增代码没有改动 F9/F10 frozen
executor/planner/validator/source hashes。

真实 materials registry v4 没有改写。冻结 readiness audit object hash
`b1017ae5e7cbb8ffb7628ec9b0ce12a11bd060d272518e69b6d3a3a6f0dad9c0`，返回
`production_promotion_ready=false`、`registered_capability_count=0`。range-compression v2.1.0 与 ASE/EMT
v1.0.0 都仍有 agent-authored validator，并缺 production trust policy、independent validation/domain review、
signed audit 与 authorized update。完整报告见
`F10_S7_CAPABILITY_AUTHORING_AND_PROMOTION_IMPLEMENTATION_REPORT_2026_08_16.md`，runbook 见
`capabilities/CAPABILITY_AUTHORING_AND_PROMOTION.md`，架构决策见 ADR 0032。下一步是 commissioning 一个
真实 bounded capability 的独立 validator/reviewer/key custody/promotion；在此之前不得把 synthetic upgrade
或 registry schema validity 称为 registered scientific capability。

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

**实现状态（2026-08-16）：工程完成。** Alembic `20260816_0006`–`0009` 新增 durable task、
dependency、attempt 和 recovery-audit 表；Postgres `FOR UPDATE SKIP LOCKED` claim、哈希 lease
token、heartbeat、有限重试、内容绑定 idempotency、依赖释放/失败传播和过期恢复均已接通。
launch/resume 只入队，内建 experiment-driver handler 由独立 worker 执行；任务 state/event
同事务提交，SSE 以数据库 event ID 和 `Last-Event-ID` 跨进程回放。验收包含真实子进程
`os._exit`、重启重领、并发抢占、stale callback、duplicate、rollback 和 partial-not-evidence。
这不提前完成 F11-S2：旧 scientific transition、outward action 和一次性 holdout 的统一
transactional outbox/receipt 仍是下一项。

### F11-S2：Transactional scientific transitions

- prediction commit、observation validation、belief update 分事务边界；
- 使用 outbox 或等价机制保证 DB/event 一致；
- duplicate event 不造成 duplicate update；
- one-time holdout/external action 在 worker 重试中仍保持一次；
- 外部 action receipt。

**实现状态（2026-08-17）：工程完成。** `scientific_commands` 将 canonical request、source
event、aggregate、result 和 keyed event receipt 绑定为一个不可变命令；domain callback、命令
结果与 `events` outbox 在同一 PostgreSQL 事务提交，exact replay 不再执行 callback，改变内容的
idempotency/source-event 重放 fail closed。prediction、observation validation、belief update 已拆成
三个显式 wrapper，validation 本身不会推进 posterior；stage decision、artifact batch 与 F9
world-model continuation 也接入同事务 event 边界。

`one_time_external_actions` 在揭示 holdout/调用 provider 前先原子 claim，只向首个 claimant
返回 raw token（数据库仅存 SHA-256），并派生稳定 provider idempotency key。domain result、
provider receipt、不可变 `external_action_receipts` 与 completion event 同事务提交。claim 超时只会
进入 `reconciliation_required`，不会自动发新 token；原 token 的迟到可验证 receipt 仍可完成。
数据库 trigger 同时禁止删除/改绑 action intent、token hash 或 request，并强制 state/event version
单向推进；复合外键保证 holdout/external ledger 不能引用另一个 action 的 receipt。
并发、duplicate、两处 scientific crash point、claim/completion crash、伪造 token、receipt
rebinding、DB trigger immutability、一次性 holdout/external retry 与三段 epistemic replay 均有
验收覆盖。这里保证的是数据库内 exact commit 与 Aletheia 侧 at-most-one authorization，不声称
任意远端系统的全局 exactly-once。下一项为 F11-S3 Quest/program graph。

### F11-S3：Quest/program graph

- 建立层级与 dependency graph；
- 跨 campaign family identity；
- budget 与 data-role allocation 绑定 quest/program；
- program 状态可从 ledger 重建；
- UI 只作为 view/controller。

**实现状态（2026-08-17）：工程完成。** Alembic `20260817_0013`–`0015` 新增 typed
Quest/ResearchProgram/Campaign relational spine、append-only lifecycle transition、同类型 scientific
dependency DAG、Program-owned cross-Campaign scientific family、F9 ResearchQuestion/legacy
Run/Experiment binding，以及 Quest/Program budget 与 data-role allocation。节点 identity/spec、transition、
family、edge、binding、allocation 均由 PostgreSQL FK/unique/check/trigger 保护；Quest-scoped lock 与数据库
recursive cycle trigger 保证并发反向 edge 最多提交一条。

所有 mutation 复用 F11-S2 command/outbox transaction；Quest 出现在 Run 之前，因此 scientific command
扩展为显式 portfolio scope（nullable run_id），其他 aggregate/request/result/event receipt 不变。重建会
重新验证 frozen spec/hash、完整 transition fold、command/event receipt、projection、DAG、family closure、
external binding 与 allocation cap，输出 deterministic graph SHA-256。HypothesisAttempt 会从 bound Run
自动取得 family ID，跨两个 Campaign/Run 聚合，因此新 Campaign 无法清零尝试数。FastAPI 绑定 authenticated
principal，viewer 只读；Next.js panel 仅重新拉取 reconstructed ledger view，不拥有状态。下一切片为
F11-S4 receipt-backed memory compaction。扩展 graph/queue/outbox/migration/budget/F9 integration matrix
为 80 passed；最终全库非 Docker 1266 passed、1 skipped、29 deselected（765.25 s），Next.js production
build、Ruff、Alembic head/current 与 ORM schema diff 均通过。

### F11-S4：Memory compaction with receipts

- artifact-backed summary；
- contradiction、limitation、failed hypothesis 为不可丢字段；
- 随机重建测试；
- provider/model 切换后恢复；
- prompt context 只拉取当前任务必要状态。

**实现状态（2026-08-17）：工程完成。** 新增与 best-effort `MemoryChunk` 分离的 append-only
scientific-memory ledger：immutable fact、显式 task binding、compaction、逐 fact coverage member 与
delivery receipt。每个 fact 绑定 Quest/Program/Campaign scope、稳定来源 hash 与一个或多个 task key；
Campaign 只继承自身 Program/Quest ancestry，不能读取 sibling Campaign。`negative_result`、
`contradiction`、`limitation`、`failed_hypothesis`、`safety_boundary` 和 REQUIRED binding 会被机械地
逐字写入 artifact/context，summary producer 无权删除或降级。

Compaction 只产生 content-addressed derived artifact，不删除 source fact；covered ID 必须与当前 eligible
set 完全相等。PostgreSQL command/outbox transaction、append-only/deferred completeness trigger、单 root/
单 successor partial unique index 与 Quest lock 共同保证 crash replay、并发 fact/compaction 和并发
compaction fail closed。新 fact 会使旧 compaction stale；随机行顺序仍重建同一 snapshot hash，缺失/
损坏 artifact、超出 exact-context 字符预算、provider/model receipt 不匹配都会阻止 delivery。

FastAPI 提供 authenticated fact/compaction/rebuild/context receipt controller；CLI 可重哈希 ledger 与
artifact；worker 只接受重新加载并验证过的 receipt，并把 provider-neutral context 放在用户 prompt 前。
provider/model 切换会创建新的 delivery receipt，不依赖旧 provider session。Alembic
`20260817_0016`–`0018` 和聚焦 graph/memory/API/worker/migration 回归 52 passed。该切片证明
custody、coverage、deterministic recovery 与 task-minimal delivery，不证明 summary 语义忠实、来源真值、
科学有效性或 portfolio 价值；scheduler/domain stages 仍需显式登记 typed fact 并传入 fresh receipt。
最终全库非 Docker 回归为 1280 passed、1 skipped、29 deselected（744.69 s），Ruff、Alembic
head/current、ORM schema diff、CLI smoke 与 `git diff --check` 均通过。下一项为 F11-S5 portfolio
planner。

### F11-S5：Portfolio planner

- deterministic hard filters；
- LLM 提案与解释；
- harness 计算成本、EIG、replication debt；
- 预算分配和 diversity policy；
- shadow mode 与人工计划对比后才启用 autonomous allocation。

**实现状态（2026-08-18）：shadow engineering boundary 完成。** 新增 observation-blind
portfolio contract：LLM 只能提交 typed action 与 rationale，不能提交 cost/EIG/utility/total score；
独立 assessor 冻结证据、成本、likelihood、measurement、capability、data role、risk/approval、
replication debt 与 diversity/correlation 输入，且 principal（模型 assessor 还包括 model identity）必须
与 proposer 分离。Harness 使用 Decimal + integer ppm 重算离散 EIG、hard blocker 和 base/marginal
utility，再按 Program/family/target/correlation/cumulative-budget/replication-quota 约束生成确定性批次。

每个 slate 精确冻结 Quest graph、Program budget availability 与最新 Quest-scoped `portfolio-plan`
memory receipt。人工计划必须以 `planner_output_access=none` 先提交；之后才能物化一次 shadow epoch，
并记录 Jaccard、hard-filter/batch violation 与 utility comparison。五张 append-only 表、
`research_portfolio.mutation` command/outbox、insert guard、deferred completeness trigger 和 Quest/
allocation locks 共同保证 one-shot、并发与重放。图、预算或 memory 在冻结后变化会阻止新 epoch；已提交
epoch 仍从 frozen snapshot 精确重建。

FastAPI 与只读 CLI 已接通；Alembic `20260818_0019`–`0020` 可 upgrade/downgrade/re-upgrade，ORM
diff 为 0。聚焦套件覆盖自评分注入、角色分离、七类 hard gate、100 组随机 EIG 守恒、batch
overspend、replication quota、并发人工计划、graph/budget/memory staleness、旧 epoch 重放和 HTTP
workflow。重要边界：本项没有 enqueue、reserve/charge budget、graph transition 或 activation API；
readiness 只能返回 `eligible_for_human_activation_review`，且
`autonomous_allocation_enabled=false`。生产 autonomous allocation 仍需独立签名/IAM approval 设计、
F11-S6 fault injection、F11-S7 endurance evidence 与人工审查。详见
`programs/SHADOW_RESEARCH_PORTFOLIO.md`、ADR 0037 与 F11-S5 implementation report。下一项为
F11-S6。

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

**实现状态（2026-08-18）：工程完成。** 新增完整十边界 manifest（在原九项之外显式加入
outward-action unknown-outcome 边界）、seed + scenario ID 的稳定哈希执行顺序、真实边界 executor
contract 与独立 evaluator。每个场景都不可省略或放宽六项 exact-zero invariant：scientific state loss、
duplicate scientific state、duplicate budget charge、duplicate outward authorization、未阻断的远端
ambiguity、state/event mismatch。未确认 injection、outcome/recovery 不符、metric/evidence 缺失、超时或
比较失败均由 harness 推导为 blocked/failed；executor exception 不会被包装为通过。

正式 `aletheia.jobs.fault_harness`（测试与 CLI 共用，不再藏在 pytest fixture）冻结 Conda Python、
平台、PostgreSQL target/server/Alembic、依赖版本与全部参与代码 hash；运行前重新捕获并在漂移时零 mutation
失败。其 self-hashed evidence bundle 保留可重算的 diagnostic/metric 证据闭包且绝不保存 lease/outward raw
token。验收 campaign 真实执行两个 `os._exit` 子进程、PostgreSQL transaction rollback/reconnect、
evaluator timeout、provider infrastructure failure、scientific-command duplicate、stale lease、archive
`ENOSPC`、worker-manifest mismatch 与 one-time outward ambiguity/reconciliation，十项全部通过且六个核心
合计均为零。Alembic `20260818_0021` 新增由 `resilience_fault_campaign.commit` + keyed event 保护的 append-only
完整报告；所有读取重新评估 embedded observations 并核对 command/event receipt。通过、失败和阻塞报告
都会保留；只有最新 Quest-scoped campaign 完整通过才可进入 F11-S7 review，且 audit 始终返回
`autonomous_allocation_enabled=false`。生产 harness 与 CLI 聚焦套件 9 passed；详见
`jobs/FAULT_INJECTION_CAMPAIGNS.md`、ADR 0038 与 F11-S6 implementation report。F11-S7 的耐久
门禁工程能力现已实现，真实 72-hour run 仍待执行。最终跨组件回归 63 passed；全库非 Docker 回归为 1300 passed、
2 skipped、29 deselected（772.97 s），Alembic current/head、ORM schema diff、changed-file Ruff、CLI
smoke 与 `git diff --check` 均通过。

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

**2026-08-18 engineering status：** 已实现不可变 start、PostgreSQL wall-clock、可跨进程恢复的
parent-hash checkpoint chain、复制/进程故障/provider 故障/结构性 pivot 强类型收据、负结果因果顺序、
冻结方向/问题/预算/数据角色、最终 portfolio/效率报告以及 pass/blocked/failed 追加式保留。生产
`real_time_72h` 至少 259,200 秒且拒绝任何 caller clock；`accelerated_engineering` 已完成包含两问、
三 Campaign、负结果、复制、两类故障、真正 pivot 和 portfolio epoch 的端到端验收，但永久返回
`real_72h_passed=false`。因此 F11-S7 engineering complete，Scientific exit 的真实 72 小时证据仍未完成。
详见 `programs/RESEARCH_ENDURANCE_GATE.md`、ADR 0039 和 F11-S7 implementation report；当前 Alembic
head/current 为 `20260818_0022`，ORM schema diff 为 0；专项回归 6 passed，F11 跨组件回归
65 passed，全库非 Docker 回归 1306 passed、2 skipped、29 deselected（925.41 s）。

**2026-08-18 supervised-controller status：** 新增仅由外部 supervisor 周期调用的 run-once
controller。每次 tick 先取得 gate-specific PostgreSQL advisory lock，再从数据库时钟与
parent-hash tail 决定 no-op 或 checkpoint；checkpoint command key 由 frozen controller + prior tail
确定。证据先进入不可覆盖的 content-addressed spool，数据库提交后才归档；若进程恰在两者之间退出，
下次 tick 会从 ledger receipt ID 识别并完成归档。同一证据在提交时间/进程变化后重试仍返回首份
envelope。preflight 复核 committed code、frozen Quest/gate sources、空 spool 与冲突 gate；真实模式无
caller-clock 接口。controller 没有自动 finalization 路径，终结仍是独立显式科学审查。PostgreSQL
专项覆盖 start replay、锁竞争、定时/证据 checkpoint 与 commit-before-archive 崩溃窗口，4 passed。
窗口内故障适配器进一步要求完整 F11-S6 bundle 与 append-only store 精确重放，从 API-process / provider
观测自动导出强类型 interruption receipts，拒绝把启动前 prerequisite 冒充窗口内证据，并以
content-addressed envelope 幂等进入 controller spool；专项 2 passed，相关交叉选择 24 passed。
macOS 外部调度也从部署约定升级为 content-addressed launchd manifest/plist：冻结 Conda/Python
可执行文件 hash、controller 文件、仓库/日志路径、label/domain 与五分钟节拍；启动前周期只返回
`waiting_for_explicit_start`，加载状态未验证时最终启动资格为 false，且不存在自动 start/finalize 命令。
专项 3 passed；详见 ADR 0041 与 `programs/ENDURANCE_LAUNCHD_SUPERVISOR.md`。

**2026-08-18 production reproduction-producer status：** 已实现 gate-bound、zero-fit 的同源
implementation-diverse replay。新路径不调用 F10 feature/estimator helpers，而从原始结构经公开
Matminer/Pymatgen API 独立重建 composition/species-blind geometry matrices，并要求 target、split 与
两份 matrix hash 全部等于 pre-fit F10 plan；估计器从 RandomForest 换为冻结的 ExtraTrees，aligned /
permuted 两臂仍保持容量与 fit 次数一致。production protocol 必须绑定 committed Git components、真实
gate/controller 与两个不同 Campaign；preflight 在 gate start 前保持 `model_fit_count=0`。run 只在 gate
live 且两 Campaign active 时执行，完成时间来自 PostgreSQL。confirmed/contradicted/inconclusive 机械
映射到 result/negative-result/limitation memory，再进入 typed endurance evidence spool；negative result
不会自动伪造 structural pivot。synthetic 反伪造专项 3 passed；production outcome 仍未知且尚未 fit。

**2026-08-18 production portfolio-producer status：** 已实现 observation-blind、content-addressed
的四候选工作单：同源独立实现复制、局部/全局机制消融、激活预登记机制 Campaign、独立语料资格审计。
所有 likelihood、cost、capability、data role、replication debt 与 evidence hash 在窗口前冻结；外部语料
因缺少 `external_validation` role 必须进入 hard-filter ledger 而不可执行。staging 只登记唯一
`portfolio-plan` memory/context 与 shadow slate，不产生 planner epoch、graph transition、gate start 或
action enqueue；只有显式 `human:*` 在看不到 planner output 时提交 baseline 后，启动预检才可通过。
epoch 必须在真实 gate 显式启动后、任何 Campaign transition 前由 PostgreSQL 时钟生成，且固定
`shadow_only=true`、`actions_enqueued=false`。专项与交叉回归 26 passed；生产工作单尚未 staging，
人工 baseline 尚未提交，真实时钟仍未启动。详见 ADR 0042 与
`programs/PHONON_ENDURANCE_PORTFOLIO.md`。

**2026-08-18 production negative-pivot status：** 已实现 contradiction-only 的预冻结 pivot
工作单。它同时复核 reproduction commit、controller spool 中逐字节相同的 typed envelope，以及
`pivot-analysis` 中 statement/detail/task binding/source/result identity 全闭合的不可丢弃负结果；只有
`contradicted` 可触发，confirmed/inconclusive 均保持零 graph mutation。适用时以固定 command key 先将
source Campaign 从 active 停止，再把 external-calculation Campaign 从 planned 激活为仅 lineage/target
qualification；不创建 `external_validation` role、不分配数据、不读 external target、不授权 outward
action。before/after fingerprint 改变 prediction、capability/input、analysis 与 discriminated pairs，
transition 时间和因果顺序由 PostgreSQL 验证；partial/full retry 不重复迁移。pivot/endurance 交叉回归
33 passed。production outcome 仍未知，因此此工作单不保证 gate 通过，也绝不伪造负结果。详见 ADR
0043 与 `programs/PHONON_NEGATIVE_RESULT_PIVOT.md`。

**2026-08-18 production efficiency-producer status：** 已把最终 efficiency 从可手写 JSON 收紧为
blind shadow epoch 的独立派生。人工在 planner output 不存在时必须只选一个 replication 或
mechanism-test 实验候选；work order 冻结 candidate→question 映射与 precommitted duration，但启动前
不计算 score/selection。窗口内 epoch 产生后，适配器分别计算 human baseline 与 planner shadow batch
的 distinct frozen-question coverage / estimated-duration microseconds，经 `EnduranceEfficiencyReceipt`
整数交叉乘法机械得到 improvement ppm，并绑定 work order/slate/epoch/decision/comparison/code 证据。
低于 10% 或负 improvement 不得修复；infeasible baseline、blocked/empty batch 或 action enqueue 直接
拒绝。所有工件显式标记这是 expected planning efficiency，不是已实现的科学产出或成本节省。工程样例
超过冻结门槛，portfolio/efficiency/pivot/endurance 交叉回归仍为 33 passed；生产 baseline/epoch 尚
不存在，故真实 improvement 未知。详见 ADR 0044 与
`programs/PHONON_PORTFOLIO_EFFICIENCY.md`。

**2026-08-18 production commissioning status：** 已将真实 F10 `matbench_phonons` 工件从孤立结果
转换为可恢复的生产形态 Quest，而不是复用 pytest fixture。两阶段 commissioning 先验证并冻结 dataset /
pre-fit plan / result 三份本地工件和参与代码矩阵，再以 stable primary key + insert-or-verify 创建三个
legacy Run 与一个 exploration-only DataAsset；两份完整 F9 world model 各含 null/primary/alternative、显式
assumption/risk、discriminating prediction 和 prior。Quest 下创建 independent-implementation replay、
local-vs-global mechanism ablation、independent-calculation corpus 三条共享 scientific-family 的 Campaign，
并冻结 USD、GPU hour、token、wall-clock 与 experiment-count 五类 Quest/Program cap。

已实际应用 commissioning `pcm_2bd8b42a47aab1afadb8781b0eec170d`：Quest
`qst_cd143727c9e8c48fcff45ab6087db3d2` 首次创建 31 个对象，立即重放创建 0 个并复用 31 个；初始
audit 与通用 graph CLI 均重建 SHA-256
`41a47946b28c9685468b5946e6b782c7f9979a8c2e9fada6d201a4b2c34286b8`。只有 independent replay
Campaign 处于 active，其余两条保持 planned。Matbench source 明确禁止被称为 external replication 或
causal mechanism；Phonondb/Alexandria/Phonix 仅记录为未分配 candidate，Materials Project legacy DFPT
因同源被排除。Quest-scoped production fault prerequisite 已实际通过十个边界，六项核心计数均为零；
restart-safe controller 的工程实现与加速验收也已完成。冻结 production gate/controller manifest
并通过只读 preflight 的工作已完成；代码绑定组件变化后会重建 controller/protocol 身份。当前
待办是部署外部 supervisor、窗口内 fault/portfolio/pivot producers，产出独立实现复现结果与独立数据
lineage/target audit，然后才可显式启动真实 72-hour clock。当前未启动时钟，也未开启 autonomous
allocation/outward action。详见
`programs/PHONON_QUEST_COMMISSIONING.md` 与 `programs/RESEARCH_ENDURANCE_GATE.md`。

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
scripts/run_endurance_controller.py
scripts/run_phonon_reproduction.py
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
tests/programs/test_endurance_controller.py
tests/domains/materials/test_phonon_reproduction.py
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
issue 9 也已工程完成：direct model、generic agent、no-K2、full K2 四臂现在强制相同基础模型、
相同 task/repeat/seed 配对、预注册统计与差异披露；执行保留全部 infra retry，聚合会对账完整
hash-chain ledger、验签 scorer receipts、拒绝遗漏 attempt，并自动生成 pass@1、科学有效率、
paired hierarchical bootstrap、Holm 校正、成本和 failure decomposition。全项目现为 635 个
非 Docker 测试（另 1 skip）及 29 个 Docker 测试通过。此为工程能力，不代表已运行真实模型或
获得 K2 科学增益。issue 10 也已工程完成：private prospective suite 现在强制 10–20 题、至少
两个领域和六类科学情形，使用角色分离的加密 envelopes、冻结且限时的双人授权、并发安全的一次性
解锁、runner 前后双重 access guard、污染即退役，以及带 hash-chain 回执的验证后明文清理；完整
operator 流程由 `scripts/manage_private_suite.py` 执行。这里的 19 个测试使用 synthetic custody
fixtures；issue 10 验收时全项目为 654 个非 Docker 测试通过（另 1 skip）及 29 个 Docker 测试
通过，但这不代表真实私有题已经委托或系统通过 Frontier Gate。

issue 11 也已工程完成：validation/reference evidence 现在确定性推导 immutable suite threshold，
独立双人证据冻结 program claim；formal config 强制 ScienceAgentBench、COREBench、DiscoveryWorld、
private prospective 四轨及四项核心科学 objective。report 会重新聚合所有 raw attempt、hash-chain
ledger 与 signed scorer receipts，分别判定 success、validity、invalid/retry、paired superiority /
noninferiority、Holm、cost、intervention、contamination 和 private cleanup/retirement，并生成不可
覆盖的 JSON/Markdown/SVG。13 个新增对抗测试覆盖 pass、measured fail、missing blocked、漏 attempt、
伪造签名、配置漂移、self approval、private custody 和 CLI。最终全项目为 667 个非 Docker 测试
通过（另 1 skip）及 29 个真实 Docker 测试通过。F7 的计划内评价/报告工程切片至此完成；真实科学
退出仍需外部 operator 冻结配置、委托私有题并花费预算运行四轨。仓库开发下一项为 issue 12（F8
knowledge schema ADR/fixture spike），同时真实 F7 继续作为发布门。

issue 12 已按限定范围工程完成：新增隔离、immutable、content-addressed 的 F8 knowledge schema，
冻结 source/corpus/paper/span 的版本与时间边界、可 replay search、十项 hard coverage、atomic claim /
evidence graph、component-wise prior art、author-excluded novelty evidence package、correction /
contradiction report，以及按 dataset bytes、split、leakage、metric formula、statistics、budget 和
external resources 逐维比较的 ProtocolSignature。synthetic temporal-holdout fixture 明确包含一篇
cutoff 后 exact-match paper 和一段 instruction-like 文献文本；13 个 adversarial tests 证明二者分别
不能泄漏进历史 snapshot 或提升工具权限，并覆盖 outage→coverage insufficient→novelty
indeterminate、equivalent blocker、self review、hash tampering 与 non-comparable SOTA 无 headline
delta。此 spike 没有接入 driver、DB、API、provider 或 migration，也不代表真实 novelty/SOTA
能力。验收后全项目为 680 个非 Docker 测试通过（另 1 skip）及 29 个真实 Docker 测试通过；
下一工程切片为 F8-S1 immutable corpus/source-span persistence，同时 F7 真实运行仍是发布门。

F8-S1 storage foundation 也已工程完成：`CorpusIngestionBundle` 现在把 corpus 与逐 paper 的
access grant/provider receipt 一起冻结，明确区分 metadata/abstract/full text、open/institutional/
user-provided、automated retrieval、model input、retention 和 redistribution，unknown license
不能授权文本处理。Alembic `20260814_0003` 新增 16 个 normalized tables 和显式 ordered
membership edges；canonical paper/version/text scope 不能绑定新 bytes，所有表由 PostgreSQL
trigger 拒绝 UPDATE/DELETE，四个 concurrent identical writers 收敛为一个 bundle，读回时逐对象
重验 schema、foreign-key closure、ordinal 与 content hash。operator CLI 只接受非 symlink typed
JSON，执行 validate/persist/inspect，不联网且 DB 中不保存文献原文。聚焦 schema/access/
persistence/migration 验收为 40 passed；此能力仍未接入当前 SURVEY/driver，也没有 live provider、
PDF/HTML/OCR extractor、response archive、coverage/novelty calibration 或真实科学结论。最终全项目
为 697 个非 Docker 测试通过（另 1 skip）及 29 个真实 Docker 测试通过；首次 Docker run 的一个
client-exit transient 已由 exact 单测与完整 29 项复跑通过确认并如实记录。下一切片为 F8-S2
deterministic query planning、multi-source response caching 与 citation traversal。

F8-S2 isolated evidence harness 也已工程完成：`QueryTermSet` 强制九类 deterministic core axes，
model 只能追加 synonym/adjacent-field；`ProviderAdapterManifest` 冻结 adapter/parser/schema、字段、
pagination、pacing 与预算。structured metadata response 经过 abstract/body/full-text 字段检查后以
exclusive create、content hash、read-only mode 和 readback rehash 保存；每页成功、429、circuit-open、
transport、parse、重复页和未终止 cursor 都有 immutable receipt/failure ledger，且同 manifest/parser
完整 replay。citation campaign 把上一轮所有新 paper hash 机械派生为下一轮双向、全 capable-source
查询，在整轮开始前检查预算，每轮提交 ledger/replay audit，只允许 saturation 或 source exhaustion
进入 coverage。query-family、source diversity、citation saturation、uncovered-source 四项由 harness
推导且固定 hard thresholds，调用方不能提交或放宽；其余六项仍需 F8-S5 的真实测量。新增 36 tests，
完整 `tests/knowledge` 为 66 passed。该路径未接入当前 SURVEY/driver，无 live provider 或真实文献
结果，也不产生 novelty/SOTA claim。下一工程切片为 F8-S3 exact-source-span atomic claim extraction；
F7 真实 Frontier Gate 与 F8-S5 科学校准仍是发布门。权威回归结果为 733 个非 Docker 测试通过
（另 1 skip、29 deselected）及 29 个真实 Docker 测试通过（734 deselected）。

F8-S3 isolated atomic-claim extraction harness 也已工程完成：frozen manifest 将 deterministic/
model extractor、parser、exact output schema、instruction/model identity、零工具权限与 byte/claim
预算固定；每个 target span 在读取前重验 grant/paper/content hash，并将 `span_extraction` 与
`model_input` 分权。原文只存在于 `repr=False` runtime envelope，ledger 仅保存 content receipt hash
与严格 AtomicClaim 字段；全文复制、prompt-like tool 指令、extra authority、错误 request/span、重复
claim 均 fail closed。OCR/低 source/claim/evidence/numeric confidence 自动进入独立人类或 second-model
review queue，accept/revise/reject 全部内容寻址，refutes/qualifies 不被支持性摘要覆盖；最终 graph bundle
精确绑定 resolution 且每个 prior-art claim 闭合到 source span。新增 37 tests；真实回归数字见
`F8_S3_CLAIM_EXTRACTION_IMPLEMENTATION_REPORT_2026_08_15.md`：完整 knowledge 103 passed，全项目
非 Docker 770 passed、1 skipped、29 deselected，真实 Docker 29 passed、771 deselected。该路径仍
只使用 synthetic licensed fixtures，无 production resolver/extractor、真实精度/召回标定或 novelty
wiring；下一切片为 F8-S4 multi-channel nearest-prior-art matcher，F8-S5 科学校准仍是发布门。

F8-S4 isolated nearest-prior-art matching harness 也已工程完成：lexical/embedding/citation/entity
四路 recall 分别冻结 code/scorer/index/model identity 并记录完整 attempts；union 保留所有 unique hit，
单路命中仅供审计，至少双路支持才可形成正式 relation。reranker 必须对完整 union 同序逐项打分，
harness 机械排序、分配 budget 且保留下限外候选。strict judgment 精确覆盖 selected pairs、prior claim
全部 source spans、六类 relation 与十类 component difference；blocking/低支持/低置信结果进入独立
review，accept/revise/reject 后 accepted ranks 连续且原始 candidate hash 不丢失。执行模型会离线重算
union、rerank order、judgment derivation、review reasons/package，抵抗 ledger 伪造。新增 52 tests，完整
knowledge 155 passed；全项目非 Docker 822 passed、1 skipped、29 deselected，真实 Docker 29 passed、
823 deselected。完整证据见 `F8_S4_PRIOR_ART_MATCHING_IMPLEMENTATION_REPORT_2026_08_15.md`。
该路径仍仅有 synthetic adapters/matcher，无真实 recall/关系精度/temporal false-novelty 标定或 novelty
wiring；下一切片为 F8-S5，F7 真实 Frontier Gate 与 F8-S5 scientific exit 仍是发布门。

F8-S5 calibrated novelty acceptance engineering 也已完成：evaluator-owned validation/strictly-later
temporal split、sealed label commitment、complete signed variant receipts 与 one-sided Wilson bounds
共同控制 recall、false/missed strong novelty、stability 和 ranking；live coverage 的六项原 external
signals 现在从 calibration/search/grant/span/correction artifacts 推导，调用方不能填数。global
calibration fail、hard coverage failure 与每 candidate 少于三条 prior 均 fail closed。candidate authors
被排除在 domain-expert/research-librarian review 外，classification、exact differences、claim ceiling 与
direction disposition 全部机械重算；discovery optional callback 还要求 gate 与 atomic candidate claim
SHA-256 精确一致。新增 59 tests，完整 knowledge 214 passed。该验收仍只使用 80-case/240-trial
synthetic suite，没有 production expert labels/private custody/live false-novelty 结果；default scheduler、
scorecard/write-up 的整链自动 materialization 也仍待后续 integration。完整证据见
`F8_S5_CALIBRATED_NOVELTY_IMPLEMENTATION_REPORT_2026_08_15.md`。

F8-S6 protocol-safe SOTA engineering 也已完成：author-excluded selectors 在 candidate protocol/result
之前封存至少三条 required reference，并把 registry 精确绑定到 F8-S5 direction/coverage/search/corpus。
unprivileged evaluator 为 candidate 与所有 reference 的同序 paired replicate 签发不可复用 HMAC
receipts；reference paper/span 必须闭合进 exact F8-S1 corpus，error 显式保留，protocol comparator 对 dataset bytes、split、metric、statistics、budget 等
逐维 fail closed。compatible rows 采用 exact one-sided paired sign test、跨 references 的 Holm correction
和 frozen practical margin；全局 headline 要求每条 sealed reference 都被击败。campaign 与 WRITE_UP
claim 都机械重算并 exact-bind protocol/metric/score，audited provider 错误不 fallback。新增 36 focused
tests，最终全库非 Docker 917 passed、1 skipped、29 deselected，真实 Docker 29 passed、918
deselected；完整证据见 `F8_S6_PROTOCOL_SAFE_SOTA_IMPLEMENTATION_REPORT_2026_08_15.md`。该验收仅为
synthetic engineering fixture，未证明真实 reference completeness、published reproduction 或 SOTA。

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
