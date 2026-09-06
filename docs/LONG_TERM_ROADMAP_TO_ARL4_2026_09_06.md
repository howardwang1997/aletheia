# Aletheia 长期路线图:从 ARL-1 资格到独立确认的自主发现(2026-09-06)

- 性质:长期规划综合文档(不新增工程承诺,不改任何权威合同)
- 上游权威文档:
  - 控制面迁移与 ARL 定义:`END_TO_END_AUTONOMOUS_RESEARCH_ARCHITECTURE_2026_08_22.md`(§13.1 ARL 等级为其定义)
  - F 系列执行计划:`FRONTIER_SCIENTIST_F7_F12_DETAILED_PLAN_2026_08_13.md`
  - 资格基底:`ARL1_PROTOCOL_EXECUTOR_QUALIFICATION.md` 与 PR-8a..8j 各指南
- 本文回答三个问题:**实验现在做到了哪儿;距离终极目标还差什么;以什么顺序补齐。**

## 1. 终极目标(不变)

用 AI 做最前沿的科学研究,端到端:自主提出有文献根基的新问题、用前沿方法设计与执行实验、
对结果做因果推理、写出可复现的带引用报告——全程在 guardrail 内、lights-out。
按 F7–F12 计划 §2 的毕业标准,这分两级:先在**一个领域**成为自主前沿科学家候选,再在
**第二个明显不同的领域**无任务特定硬编码地重复。"可靠"要求 ≥3 个前瞻性 campaign、
新颖性审计、判别实验、独立复现、组合级成功率/错误发现率报告和零不变量违规。

## 2. 当前真实位置(2026-09-06,诚实盘点)

### 2.1 ARL 阶梯状态

| 等级 | 状态 | 依据 / 缺口 |
|---|---|---|
| ARL-0 Integrity | **已具备** | ledger、sandbox、hidden boundary、all-attempt、replay、claim ceiling 不变量已实现并有 ARL-1 合同内的 canonical replay |
| ARL-1 Protocol Executor | **差最后一段执行** | 软件闭环完成;generation `20260904h` 目标 campaign 已合格(receipt `qtx_1a328607...`,`deployment_qualified=true`);但 ARL-1 出口步骤 4–9 未执行,**尚无可签发的 ARL-1 资格 receipt** |
| ARL-2 Question-bound Scientist | **原语齐备,未贯通** | F8 知识边界、F9 竞争假设/因果链、F10 能力注册、K2/F9 信念回路均已建成,但"未由主控制面贯通"(§13.1 原话);无 ARL-2 证据 |
| ARL-3 Mission-bound Researcher | 无证据 | mission→问题的自主形成、measurement/design space 演化、按 modality 获取新 evidence 均未建 |
| ARL-4 独立确认的自主发现 | 无证据 | Prospective Discovery Suite、按 claim type 的独立确认(F12)未建 |

### 2.2 真实实验结果清单(全部诚实裁决)

| 实验/运行 | 结果 | 诚实裁决 |
|---|---|---|
| Real Campaign Gate v1(run `4443d7d2`,2026-08-13) | 内部 final holdout 支持诊断;外部 SuperCon2 前瞻评估**不支持** | 协议全过、科学结果为负 → `results_rejected`(正确归档);这是流程证据不是科学主张 |
| K3 真实材料证据链(F9-S10,Matbench band-gap) | 三解释竞争中 generic shrinkage 胜出 | 假设空间收缩仅 1.34% → `valid_update_without_robust_contraction`,非科学 exit |
| 能力复制(F10-S1,五分区) | 全部点估计 delta 为正 | 仅 2 个区间排除零 → `partition_sensitive`,能力保持 provisional |
| 结构感知材料实验(F10-S4,`matbench_phonons`) | 对齐几何 vs 匹配控制:内部验证 MAE −52.2%,锁定 holdout −47.8% | **迄今最强正向科学证据**;但限于同公共数据集 DFPT 边界,非外部复现、非机制主张 |
| ASE/EMT 参考模拟(F10-S5) | Cu fcc EOS 两次精确重放,晶格常数匹配参考 | 校准的是经典势执行边界,非 DFT/实验/迁移性 |
| 72 小时 endurance Quest(v1) | 全程跑完,一次 reproduction + 多类故障恢复 | 终态 `blocked`(structural pivots 0/1)——不是 pass,不可事后修复 |
| PR-8h generation h(2026-09-04) | 10 个破坏性场景全过,`deployment_qualified=true` | **部署资格(工程)证据**,`scientific_admission_allowed=false`,与科学有效性无关 |
| 物理ideation 佐证(旧控制面) | cuprate 平面掺杂候选(UCI Tc)通过廉价 hold-test | 已验证的候选方向,尚未移植到新控制面 |

**总结**:反过申明(anti-overclaiming)脊柱已经过硬——每一次真实运行都留下了正确的负/混合
裁决。但**尚没有一个经独立确认的正面科学主张**;全部真实科学证据的强度上限是
"同公共数据集内的有界对比证据"。

### 2.3 工程/部署状态(资格基底)

- PR-0→PR-8i:本地源码/测试闭合;PR-8h generation h 在真实 Linux 目标合格。
- **generation h 之后 main 又改了 unit 字节**(timezone 绑定 #143、role-config 收敛 #144):
  h 只认证冻结的 `e0dc06c` 部署;当前树需要**新的 freeze + 目标重认证**才能部署为合格代。
- **PR-8j 清理恢复已闭环为"被授权退役合法取代"**(2026-09-06 决策):generation h 的前置退役
  (2026-09-04)移除了第 4 次调用依赖的全部物理前提(quota receipt 绑定 `mount_id` 不可复原、
  workspace bind/inode pin 失效、watchdog unit 已停),且无任何前向 gate 依赖释放这些历史库 holds。
  详见 `docs/PR8J_ATTEMPT_SCOPED_PRE_RUNTIME_CLEANUP.md` 的闭环章节。#141 的 stable-semantics
  修复对未来的活体 never-started 清理仍然有效。
- ARL-1 出口步骤 4–9(given-protocol campaign + prepare/issue/verify + 篡改测试)未执行。
- F11 portfolio:shadow 层建成,autonomous allocation 恒为 disabled,等待资格 gate。

## 3. 距离终极目标还差什么(按层级)

1. **ARL-1 收尾**(工程执行,非新能力):新 freeze → 目标重认证 →
   步骤 4–9 → 第一份 ARL-1 资格 receipt。(PR-8j 已按 2026-09-06 决策闭环,不再阻塞。)
2. **ARL-2 贯通**(能力整合,真正的第一道科学关):把 F8 新颖性门、F9 竞争假设/因果链、
   F10 能力注册、K2/F9 信念回路接进 research-kernel 主控制面;在真实问题(首选已 commission 的
   phonon Quest)上跑完整的"竞争解释→判别实验→负结果处理→回退"闭环。这一步开始产生
   真实科学价值,也是首次检验控制面迁移后的科学能力(而非工程能力)。
3. **新颖性/知识边界的真实校准**(F8 release gates):live provider/extractor/matcher 校准、
   真实 prospective suite、真实 reference matrix——没有它,任何"新"主张都无法成立。
4. **ARL-3 mission 形成**:MissionAdmission、问题自主形成与分支、measurement/capability 自建
   (F10-S7 已有工程模板)、跨 suite 的克制性任务设计。
5. **ARL-4 独立确认 / F12**:按 claim type 的独立确认合同(经验=外部站点、计算=独立实现、
   理论=机器检查)、Prospective Discovery Suite 的 12 项要求、多 mission 聚合的
   成功率/错误发现率阈值。
6. **横切缺口**(README 明示的 load-bearing 项):更丰富的因果/机制实验 repertoire、
   production provider receipt/reconciliation commissioning、资格 gate 后的 portfolio 激活、
   重复的低重叠或实验室级复现、完整 research bundle(问题/文献/数据卡/代码/工件/指标/主张/
   审计/复现/局限/论文/复现包)。

## 4. 长期规划:三个 Horizon

> 排期原则沿用 F7–F12 计划 §5.2:以下是量级估算而非日期承诺;外部条件(F12 尤甚)主导 H3。
> 每个 slice 仍走 §5.3 的九步节奏(RFC→primitive→ledger→wiring→fixtures→adversarial→
> frozen live→bundle→复盘)。

### Horizon 1 —— 关闭 ARL-1(量级:2–4 周,主要是目标机操作与 freeze 周期)

目标:拿到第一份可独立重验的 ARL-1 资格 receipt。**只做资格,不扩权**;
`scientific_admission_allowed=false` 与 engineering claim ceiling 全程不动。

1. **新 freeze + 目标重认证**:当前 main(含 #143/#144,CI 绿、focused 门绿)冻结为新一代
   (generation i);重跑 PR-8f→8g→8b→8h 全链,沿用 generation h 的 sibling-unit/非惰性检查;
   显式记录"h 只认证 `e0dc06c`,新代认证新冻结字节"。
2. **ARL-1 步骤 4–9**:生产 given-protocol campaign(含全部预注册 exact reexecution)→
   证据/all-attempt manifest → `prepare`(source-verifier principal)→ `issue`(资格 signer)→
   重启后 `verify`(无密钥 auditor)→ 每类证据单字节篡改必须失败。
3. (可选并行)PR-2 store 的 `O(N²)` lifecycle audit 与第二 policy epoch——纯工程债,不阻塞 H2。

验收:ARL-1 receipt 签发、离线重验通过、篡改测试全红转绿;README/文档同步降级"未资格"表述。

### Horizon 2 —— ARL-2 Question-bound Scientist + 第一个真实科学闭环(量级:3–6 个月)

目标:给定一个研究问题,系统自主完成 §2.1(领域级)的 1–6 步,并在真实数据上留下
(正或负都诚实的)科学结论。这是**价值转折点**:此前的一切都是支撑结构。

1. **控制面贯通(纯工程,先行)**:research kernel 的 action/protocol 管线接入
   F8-S5/S6 新颖性门(exact-claim callback 已有)、F9-S2..S8 假设→因果→预测→选择→验证→
   信念更新链、F10 能力注册与 typed observation。判定标准:一条 hypothesis fork 能从
   Kernel action 走到 observation_incorporated,全程无 legacy `ExperimentDriver`。
2. **真实科学 campaign #1(首选 phonon Quest `qst_cd1437...`)**:两个竞争世界模型、三个
   Campaign 已 commission;补齐候选数据源(Phonondb/Alexandria/Phonix)lineage/target 审计后
   跑首轮。产出无论正负都进入 scientific memory 与 research bundle。
3. **F8 真实校准**:live provider 检索/抽取/匹配校准 + temporal holdout 低 false-novelty +
   一个真实 reference matrix 复现——解锁"新颖性=有覆盖度证据的搜索协议结果"。
4. **耐久 gate v2(修 §1.3 的设计错位)**:把"该 pivot 时正确 pivot"做成独立挑战场景,
   而不是要求正向 campaign 必然 pivot;v1 的 blocked 不追溯修复。
5. **portfolio 激活(受资格 gate 约束)**:ARL-1 receipt 之后,按 F11-S5 预冻结规则首次允许
   有界 autonomous allocation;保留 human `*` baseline 对照。
6. **第二领域验证(ARL-2 的"通用性"预演)**:旧控制面已验证的 cuprate 候选移植为新控制面的
   第二个 question-bound campaign;或选 RAG/评估类计算型问题。

验收(对齐 F7–F12 §2.3 可靠性标准的最小子集):≥1 个结论在冻结知识快照下通过新颖性审计、
≥1 个机制结论通过判别实验、≥1 个结论由独立数据源复现、组合级报告(含失败)、零不变量违规、
第三方可从 bundle 复算主要结论。

### Horizon 3 —— ARL-3 Mission-bound → ARL-4 独立确认(量级:6–18 个月,外部条件主导)

目标:从"给定问题"升级到"给定 mission",并把最有价值的 claim 送去独立确认。

1. **MissionAdmission + Prospective Discovery Suite**(§13.3 全部 12 项):独立委托、temporal
   cutoff、污染审计、权限四分离、跨 suite 的克制性任务、预冻结 promotion/missingness 规则。
2. **capability/measurement 自建闭环**:F10-S7 供应链晋升已备;打通"实验需要新测量→自主
   注册新能力→独立验证→晋升"的回路,含"不该建能力"的对照任务。
3. **ARL-4 独立确认(F12)**:按 claim type 分轨——计算主张=独立实现+未见数据+冻结 evaluator;
   经验主张=外部站点预注册复现(operator/instrument/site 至少一维不同);理论主张=机器检查证明。
   先从**计算主张**起步(最可控),物理主张依赖外部实验室,最后做。
4. **系统级 ARL-4 评定**:多 mission 聚合的可靠性/错误发现率/独立确认率/新颖性/重要性阈值;
   一次 mission 只验收一个 claim,不授予系统级资格。

### 横切(所有 Horizon 持续)

- **模型策略**:默认轨道跟踪当前前沿模型;benchmarks/复现/资格运行 pin 明确模型 ID 并入
  provenance(不变量 6)。模型换代只允许通过配对重跑进入证据,不允许静默替换。
- **安全脊柱**:六条不变量(README)与 F7–F12 十条共同不变量(I1–I10)零放松;
  资格基底与科学运行时的隔离(`qualification_only` / `scientific_admission_allowed`)在
  ARL-4 之前不合并;reality-facing 权限永远人类治理。
- **成本纪律**:沿用 per-run token/成本核算与 resume/checkpoint;每个 Horizon 出口做一次
  成本-证据比复盘,防止"资格工程吞噬科学预算"。

## 5. 主要风险与对策

| 风险 | 对策 |
|---|---|
| 资格工程惯性:H1/H2 的 freeze-重认证循环持续吞掉日历时间 | H1 严格限定清单,不扩 scope;H2 的贯通工程与新 freeze 并行;每个 PR 仍走既有九步节奏 |
| 外部负结果再次出现(H2 科学 campaign 可能又是负的) | 负结果是 ARL-2 的合法证据(过程正确性);报告永远按组合而非挑成功项 |
| 新颖性校准做不出低 false-novelty | F8-S5 的 claim ceiling 与 engineering-only 上限保持,直到校准达标;不降阈值 |
| F12 外部实验室不可得 | 计算主张先行(独立实现即可);经验主张降级为"多数据源独立复现"并如实标注 |
| 单人推进的序列化瓶颈 | 每个 Horizon 标出可并行的纯工程 slice(如 PR-2 优化、bundle 工具),供批量执行 |

## 6. 下一步(唯一)

按 Horizon 1 第 1 项启动:**新 freeze + 目标重认证**——把当前 main(`3e65cca`,CI 绿、
focused 门绿)冻结为新一代 release,在资格目标上重跑 PR-8f bootstrap → PR-8g commissioning
→ PR-8b installation → PR-8h campaign 全链(新隔离数据库、非复用身份、sibling 非惰性检查)。
随后执行 ARL-1 步骤 4–9,签发第一份 ARL-1 资格 receipt。
(原第 1 项"PR-8j 收尾"已按 2026-09-06 决策闭环为被授权退役取代,见 §2.3。)
