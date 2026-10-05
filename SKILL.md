---
name: cst-absorber-skill
description: Use when a user requests CST periodic PEC-backed absorber simulation preparation or execution, single-sample or explicitly confirmed batch analysis, geometry/material validation, Floquet complex-S and power exports, or reproducible reflection-loss figures. Applies to isotropic frequency-dependent materials and brick or closed STL regions, including non-TPMS geometry.
---

# CST absorber — single first

**0.2.3-preview 已实测：求解 SUCCESS，自动保存、失败证据导出和 owned 关闭恢复；原网格验收仍失败，数值资格为 false。本次发布只更新文档和清单，代码及测试保持实测版本原样，没有再次运行 CST。**
先读 [能力边界](references/capabilities.json)。注入测试不证明实机兼容、材料响应或
数值精度。技能不提供 CST 许可证，也不替代执行授权和工具审批。

## 范围

默认恰好一个 case；多个频点、材料和区域都属于该样品。不得自行增加试跑、
扫描、对照、入射角或偏振。批量必须是用户确认的有限显式列表，配置为
`execution: {mode: batch, confirmed: true}`；单元素列表仍走单样品路径。
v1 为真空、矩形周期单元、PEC 背板、各向同性频变体材料；theta 为 0–45°、
azimuth 为 0°、一次 TE 或 TM。透射双端口、各向异性、表面阻抗和有限 RCS 不支持。

## 工作流

从技能目录使用现有 Python 环境，先查看 `python scripts/absorber_cli.py --help`。
命令和字段见 [usage](references/usage.md)、[schema](references/config.schema.json)。

1. 对用户配置执行 `validate`，再按需 `plan`。核对几何单位、STL 闭合/变换、
   区域材料映射、相对 εμ、时谐约定/损耗符号、电导率避免双计、数据频段、
   外推授权和各材料密度。自相交或区域交集的 `unverified` 不得改为已验证。
   模式覆盖按实际频率和晶格独立计算，没有固定周期上限。网格输入是名义尺寸；
   `target_edge_m` 与可选 `acceptance_max_edge_m` 分开。旧 `max_edge_m` 仍同时
   约束目标与实测验收，禁止自动放宽或宣称 CST 已 enforce 硬上限。
2. 仅准备模型时，执行 `prepare-cst` 到新目录，交付原创命令和验收清单。
   用户已授权真实求解且资源独占时，使用 `run`/`resume` 的
   `--backend cst --authorize-live --exclusive-resources`，明确选择
   `--acceptance-run` 或 `--result-profile PATH`。未满足条件不创建会话。
   后续实机运行须符合该次用户授权；本候选没有端到端实机通过记录。
3. 验收保存实际树、run ID、原始复数/单位、功率和读回。同步完成路径只调用一次
   `run_solver`，经 owned 检查、显式保存、稳定快照及调用方接受后绑定归档哈希；
   不得在异步启动后用第二次求解冒充等待，也不得修复历史 quarantine 为成功。
   [完成协议](references/native-write-completion-review.md)只是受限 SDK 操作信任边界，
   不证明写入者或持久化。未知完成、超时和中断保留待完成义务，不并发 abort/清理。
   验收未完成返回失败，不缓存物理成功；尽力保留失败原始证据。
4. 实际形状材料的正式 Sigma XYZ 与 Rho getter 只证明参数一致性。输入 σ 已并入
   ε 时原生 σ 必须全轴为零；密度不得擅设默认值。FD 策略及参考面没有已核实 getter，
   保持 unknown 并阻断资格。Data、FD、Fit 不可混用；同一 owned 调用/文件哈希关联
   不等于 native curve run 身份或求解实际材料响应。映射还须核对实际树、单位、
   入射列、模态编号/参数、TE/TM 及全部功率分支；不得猜叶节点或 Gamma 单位。
   冲突读数保留为无效证据，不参与分析。`analyze-native` 只诊断同次保存的原始树，
   不创建会话或再次求解，来源未验证，PEC 的 T=0 为边界假设。
   [本次证据](references/native-acceptance-0.2.3.md)中的实际参数限定于一个合成样品。
   后续只读目录仅返回 Current ID 0，40 个 leaf 的参数组合均为空；
   没有非零存档 ID，不据此清除 native run 关联或数值资格门禁。
5. 对明确来源 CSV 执行 `analyze`；合成输入标 `--export-origin synthetic`。保留全传播
   模式 R、R00、独立功率 A、原始 T 与 PEC 理论 T=0、复 S/Gamma 和实际频点。
   模式/频点缺失、Gamma 或功率闭合失败则停止。交付指标 JSON、实际点 CSV、
   至少 300 dpi PNG、可编辑 SVG、样式/版本及独立 Python 绘图脚本，用 `plot` 复现。
   模型、材料表、原始谱与验收记录存私有位置；仓库只放通用代码与合成示例。

## 结果与运行

报告执行/来源状态及 `numerically_qualified=false`。平均 A 要求完整频段覆盖；
总阈值频宽及最长连续频宽在 R 上分段线性插值，默认 0.1 对应 −10 dB。
谷深/位置仅指实际采样点；频移仅对用户给定对象计算。零反射 dB 为 null 并保留
零标记，图示下限不代表谷深。不要把平均反射的 dB 当作 dB 均值。

缓存绑定输入内容与文件哈希。新建独立会话，经 PID、创建时间、可执行路径、
会话 ID 和 owned 路径核对后操作；不得接管、按进程名清理或杀死其他会话。
暂停/恢复、串行资源检查及预算按实际历史和用户效率目标执行。超时 SDK 请求
可能继续运行；未确认关闭保留锁并阻止自动重试。更多限制见
[validation](references/validation.md)。筛选结果不升级为数值认证。
