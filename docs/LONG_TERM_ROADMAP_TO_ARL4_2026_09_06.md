# Aletheia 长期路线图：ARL-1 到独立确认的自主发现

状态更新：2026-09-09。本文是能力规划，不是研究结果或科学证据。
ARL 定义沿用控制面架构 RFC；资格由对应的冻结协议和签名 receipt 决定。

## 当前状态

| 等级 | 当前状态 | 出口 |
|---|---|---|
| ARL-0 Integrity | 已实现相应完整性原语；系统资格仍须累积验证 | Ledger、隔离、all-attempt、claim ceiling、schema、依赖审计 |
| ARL-1 Protocol Executor | 软件合同与生产入口已实现；尚无生产资格 receipt | 给定协议的完整运行、独立验证、admission、Kernel incorporation、prepare/issue/verify |
| ARL-2 Question-bound Scientist | F8/F9/F10 原语已有；真实问题的完整新控制面闭环尚未通过资格验收 | 自主竞争解释、判别实验、负结果处理与回退 |
| ARL-3 Mission-bound Researcher | 尚未取得资格证据 | 从 mission 自主形成问题和测量／方法设计 |
| ARL-4 Independently Confirmed Autonomous Discovery | 尚未取得资格证据 | 前瞻性新颖性审核及与 claim type 匹配的独立确认 |

现有部署 receipt 只覆盖其签名中冻结的安装。下一轮必须使用当前源码的新 freeze、
当前 schema head 和新的 commissioning window，不能沿用历史代际的授权窗口。
部署资格和 ARL-1 receipt 都不能作为科学有效性或独立复现主张。

## 顺序一：完成 ARL-1 恢复和验收

冻结前必须满足：

1. 以 scientific slot 恢复已提交 validation 和 admission；恢复时重新验证原有签名、
   绑定关系和 Kernel receipt，保持唯一 observation。
2. 覆盖提交后响应丢失、跨 challenge TTL 重启、归档中断后恢复、错绑和篡改拒绝。
3. CI 必跑独立 PostgreSQL 数据库回归。完整迁移、实际 deferred triggers、schema
   对齐和只读权限验证必须通过；简化夹具不替代完整部署链路。
4. 源码和迁移冻结清单与经审查的当前实现一致。
5. custody 发布／读取和 worker／driver 生命周期有确定的执行顺序及恢复状态。
6. 对外文档遵循公开证据边界；内部审计记录不进入公开产物。

随后在新的隔离 Linux deployment 上完整执行：

- 冻结 release、验证 Python/runtime、迁移数据库、commission、安装并运行 PR-8h。
- 预注册全部 exact reexecution，再执行给定协议、独立验证、原子 admission 和 Kernel incorporation。
- 保留 campaign receipt、all-attempt manifest、evidence archive 及确定性报告。
- 由不同 principal 执行 `prepare`、`issue`、重启后的无密钥 `verify`。
- 每类源材料的单字节篡改必须被拒绝。

验收对象是可独立复验的生产 ARL-1 资格 receipt。没有该 receipt 时不提升系统等级。
操作步骤见 [ARL-1 出口 runbook](GENERATION_I_REQUALIFICATION_AND_ARL1_EXIT_RUNBOOK_2026_09_06.md)。

## 顺序二：最小 ARL-2 问题闭环

先固定一个研究问题，接通新 Kernel 的一条完整路径：

1. F8 提供有时间截止、来源跨度和覆盖度的知识输入。
2. 提出至少两个可区分的竞争解释；候选与拒绝理由全部保留。
3. F9 在观察前承诺预测，机械选择有约束的判别实验。
4. F10 能力经 Protocol IR 编译执行，结果通过独立 validator 和 admission 入库。
5. 根据已接纳的负／不确定结果合法更新 world model，选择继续、修正测量、分支或停止。
6. 完成第二轮 observation，并从 bundle 重放问题、协议、数据、代码、观察和结论。

该路径不得依赖 legacy ExperimentDriver；新颖性与机制主张分别受真实校准和判别证据约束。
首选已具备数据／能力基础的材料问题，先完成候选外部数据的 lineage 和 target 审计。
领域选择不免除独立确认要求；材料中的两个问题也不能自动算作两个明显不同的领域。

验收同时包括“应继续”“应回退”“应修测量”“应停止”的冻结挑战。
科学上有效的正、负和不确定结果按协议保留；开发测试不能充当这些科学结果。
F8 live calibration、真实 reference matrix 和独立数据复现分别保留各自出口。

## 顺序三：ARL-3 到 ARL-4

ARL-3 增加 MissionAdmission、问题形成、measurement/design-space 演化、跨 Quest 的
受审计知识转移，以及有界能力创建。Portfolio 激活须遵守其独立资格和冻结策略，
不因某个单独 campaign 完成而自动获得更大权限。

ARL-4 使用 Prospective Discovery Suite：独立委托、时间冻结、污染审计、权限分离、
预冻结的 promotion/missingness 规则，以及完整的组合级成功率和错误发现率报告。

- 计算主张：独立实现、未见数据与冻结 evaluator。
- 经验主张：预注册的外部站点复现，明确 operator/instrument/site 的独立性。
- 理论主张：对应形式系统中的机器检查证明。

一次 mission 只验收具体 claim。系统级资格要求多个前瞻性 mission 和独立确认，
跨领域资格还要求在明显不同的领域重复，而非更换同领域数据集。

## 持续约束

模型／数据／协议／实现版本进入 provenance；预算和现实权限保持独立治理。
Development 和 post-hoc 分析保留标签，不提升为独立确证证据。
每个出口根据实际证据更新状态；排期不代替验收。
