---
name: cst-absorber-skill
description: Use when a user requests CST periodic PEC-backed absorber simulation preparation or execution, single-sample or explicitly confirmed batch analysis, geometry/material validation, Floquet complex-S and power exports, or reproducible reflection-loss figures. Applies to isotropic frequency-dependent materials and brick or closed STL regions, including non-TPMS geometry.
---

# CST absorber — single first

**0.2.2-preview：一次合成平板原生求解完成，自动端到端验收失败；本补丁仅离线修复，未重跑 CST。**
先读 [能力边界](references/capabilities.json)。注入接口测试不能证明实机兼容、
材料拟合或数值精度。技能不提供 CST 许可证，也不替代执行授权和工具审批。

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
   区域材料映射、相对 εμ、时谐约定/损耗符号、电导率避免双计、测量频段、
   外推授权和不同材料密度。缺项明确指出；自相交或区域交集的 `unverified`
   不得改为已验证。模式覆盖按实际频率和晶格独立计算，没有固定周期上限。
2. 只要求模型准备时，执行 `prepare-cst` 到新目录，交付原创命令和验收清单。
   要求真实求解时，先确认用户已授权且资源独占，再使用 `run`/`resume` 的
   `--backend cst --authorize-live --exclusive-resources`，并明确选择
   `--acceptance-run` 或 `--result-profile PATH`。未满足条件不创建会话。
   0.2.2 修复阶段未运行 CST；此前 0.2.1 合成实机自动验收失败。剩余门禁见 [native acceptance](references/cst-validation.md)。
3. 首次验收通过 `--acceptance-run` 保存实际树、run ID、原始复数/单位、功率和
   读回记录。验收未完成时返回 `failed/native_validation_pending`，不缓存物理成功。
   结果映射必须绑定真实树、单位、模态编号及参数，不能猜叶节点、Gamma 单位，
   或把 Accepted 功率当材料吸收。原始失败结果在剩余预算内尽力保留。
   还须从实际标签核对 S 入射列及所有功率分支，与所请求 TE/TM 一致；缺失或
   矛盾时保留专项门槛并阻断分析。冲突原始读数保留为无效证据，不参与计算。
   对同一次保存的原始树使用 `analyze-native` 做映射与诊断图；该离线路径不
   创建会话、不再次求解。来源标为未验证，PEC 的 T=0 标为边界假设。
4. 对明确来源的外部 CSV 执行 `analyze`；合成输入注明 `--export-origin synthetic`。
   保留全传播模式 R、R00、独立功率 A、原始 T 与 PEC 理论 T=0、复 S/Gamma 和实际频点。
   模式/频点缺失、Gamma 或功率闭合失败时停止。分析始终为筛选/诊断。
5. 交付指标 JSON、实际点 CSV、至少 300 dpi PNG、可编辑 SVG、样式/版本和独立
   Python 绘图脚本。用 `plot` 或脚本复现。模型、材料表、原始谱和验收记录存于
   用户指定私有位置；发布仓库只放通用代码与合成示例。

## 结果与运行

明确报告执行/来源状态和 `numerically_qualified=false`。平均 A 要求完整频段覆盖；
总阈值频宽及最长连续频宽在 R 上分段线性插值，默认阈值 0.1 对应 −10 dB。
谷深和位置仅指实际采样点；频移仅对用户给定比较对象计算。零反射的 dB 为 null
并保留零标记，图示下限不代表谷深。不要把平均反射的 dB 当作 dB 均值。

缓存绑定输入内容与文件哈希。新建独立会话，仅经 PID、创建时间、可执行路径、
会话 ID 和 owned 路径核对后操作；不得接管、按进程名清理或杀死其他会话。
暂停/恢复、串行资源检查、预算按实际历史和用户效率目标执行。超时的 SDK 请求
可能继续运行，未确认关闭则保留锁并阻止自动重试。更多限制见
[validation](references/validation.md)。
