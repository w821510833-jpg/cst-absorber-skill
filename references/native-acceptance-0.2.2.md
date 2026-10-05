# 合成平板实机验收：0.2.1 历史与 0.2.2 已实测失败

0.2.1 和后续 0.2.2 实测各自只运行同一个原创合成平板、一次真实求解；两次自动端到端验收均失败。0.2.1 通过准确 owned 对象不保存关闭分别收尾；0.2.2 已确认 owned 自动关闭，无人工终止。旧失败记录及私有 raw 证据未改写。当前 0.2.3-preview 仅离线修复，未运行 CST、增加案例或安装软件，详见 [完成协议审查](native-write-completion-review.md)。本说明不包含机器路径、进程标识、账户、研究材料、模型文件或原生日志。

## 0.2.1 历史记录

平板为 5×5×1 mm，周期 5×5 mm，PEC 背板和 3 mm 空气，TE 正入射；合成 ε=3−j0.2、μ=1、σ=0，频点 1、1.5、2 GHz。求解记录 3750 个网格单元、2 秒总求解时间。此前使用 0.2.1 raw exporter 只读导出 40 条曲线、5246 个点，零读取错误；0.2.2 离线开发时复用该保存 JSON，随后进行了下文单次实机重测。

| 本例证据 | 已观察到的内容与限制 |
| --- | --- |
| 模型读回 | 单平板的单位、25 mm³ 体积、bbox、材料分配、周期域与 PEC 边界符合输入；不代表 STL 或多材料案例已验收 |
| 频点及激励 | 实际三点齐全，TE/TM 基模和入射列、功率激励分支一致；不覆盖高阶或截止模式 |
| Gamma | 原始标签明确为 1/m；两个基模三点与真空传播值一致；不泛化到任意模式或版本 |
| 功率 | 实际刺激为 0.5 W；Outgoing 与独立材料体损耗的最大相对闭合残差 2.31×10⁻⁹；Accepted 没有充当材料损耗 |
| S 归一化 | 本例 modal S 与独立反射功率比最大差 1.11×10⁻¹⁶；两个等阻抗基模不能验收全部 Floquet 归一化情形 |
| 自动生命周期 | 保存 archive 换代后旧 inode 守卫阻断 poll、abort、close，故原验收失败；安全人工收尾不将该失败改成通过 |

材料偏差须区分三个实际来源角色。`Data list` 是原表，`FD - Interpolated` 是频域插值响应，`Fit` 是 Nth-order pole 拟合曲线；它们的 title 可能都含 Fit，不能只看标题。合成本例 `Eps'' (Fit)` 在 1/2 GHz 为 0.124952/0.247053，相对输入 0.2 差 −37.52%/+23.53%，而 Fit 的密集原始网格没有精确 1.5 GHz 点。FD 插值四个实/损耗分量在计划三点完全吻合输入。

代码请求 `FDSolver.TDCompatibleMaterials=False`。检查过的正式 setter 文档描述特定材料类型，不能扩大为本各向同性体 ε/μ 表的通用线性插值保证。实际 FD 曲线吻合是诊断事实；Nth-order Fit 的近似偏差不能直接当作该 FD 求解实际使用的材料偏差。尚无已核实的 actual policy getter 与 solver-response linkage，不据此判定材料门禁通过。此分析只涉及原创合成常量损耗表，没有分析或推断任何研究材料。

## 0.2.2 离线修复与后续实测

0.2.2 先修复以下软件问题并以离线回归验证，随后按原配置进行了一次实机重测：

- 会话控制保留 held DE/project 对象、完整进程创建身份、程序路径、owned 路径及 SDK filename 核对；archive inode/SHA256 变化只隔离数据操作，不再阻断准确 owned 对象的 poll/info/abort 与不保存关闭。未知身份、PID reuse、链接路径或待处理请求仍不能触发不安全操作。
- 写入、启动和导出继续核对稳定 archive 身份与 SHA256；异步换代不自动重新绑定。已结束 owned 项目后仍可严格核对保存 archive，但隔离 archive 不可自动另存或导出。现有 pathname SDK 的竞态和异步写入者归属仍未解决。
- 原控制器预算返回之后，native CLI 继续监督实际 worker、cleanup、启动与 SDK 请求；所有安全退出证据满足之前保留解释器。独立监督记录不会升级 blocked/failed receipt 或删除锁。持续未知可能需要人工介入，工作预算不是强制终止进程的权限。
- 实际材料 split 实/正损耗曲线使用明确叶身份、频率、分量、符号和电导率规则。只有真实存在的精确计划频点能比较；没有补点或默插值。原表、FD 插值、Nth-order Fit 不可互充，吻合的 FD 曲线也不关闭 Fit 或求解来源关联门禁。
- 已有公开 mesh getter 候选现在保留类型、单元数、最小/最大边长、getter 来源及配置 mesher 模式/进程数，并严格解析、比对。固定 mesh 查询宏可针对 held owned 对象读回，不能通过任意 VBA 绕过 archive 门禁。

### 0.2.2 单次实机重测结果

原配置未改：同一 5×5×1 mm 平板、合成材料与 1/1.5/2 GHz 三点，只派发一次真实求解。solver info 为 SUCCESS，3750 cells；本次求解记录总时间为 1 秒。owned 会话自动关闭及监督收尾确认，无人工杀进程，未触及其他会话。控制器结果仍为 `failed/native_archive_integrity_failed`，数值/物理资格均为 false。

名义 1 mm 网格请求的 getter raw max 为 1.16412，按 project mm 与 final-mesh 假设为 1.16412 mm（+16.412%），未满足原 `max_edge_m` 的 1 mm 实测阈值。getter 单位与本次求解网格新鲜度仍未独立证实。首次错误来自网格比较，随后失败保留路径因 archive identity 已变而 quarantine，未能自动保存/导出。原记录没有首次漂移的调用名/时间，不能确定某个 SDK poll 或后台线程就是写入原因。

关闭后另行只读导出保存结果，保留 40 curves / 5246 samples、零读取错误；此为私有诊断，不是控制器自动成功，也未重绑定原 quarantine。FD 四分量三点与合成输入相符，Fit 仍有偏差与缺失精确样点；native Sigma/Rho、FD policy、参考面及实际 solver-material response 绑定没有因此通过。

当前 0.2.3 候选改为独立的一次同步 `run_solver` 完成/后处理→显式 owned 保存→稳定快照→调用方提交 pin，并增加阶段观察，详见 [完成协议审查](native-write-completion-review.md)。这只是有期限和身份限制的 SDK 操作信任边界，不证明写入者或文件持久化，不清除旧 quarantine，未实机验收。

| 0.2.2 尚未闭合的门禁 | 保留原因 |
| --- | --- |
| 实机修复验收 | 0.2.2 已单次实测但自动端到端失败；0.2.3 同步完成候选尚未运行 CST |
| archive 写入归属/稳定快照 | 仅靠 stat、PID 或 filename 不能证明异步写入者；隔离会继续阻断自动保存/导出 |
| 材料 solver-response linkage | 输入表、FD、Fit 已分清；0.2.2 未执行 Sigma/Rho getter；0.2.3 加正式参数读回，但 actual FD policy/source 使用与 solver-response linkage 仍未知 |
| 参考面与复相位 | 当前公开 FloquetPort 文档没有已确认的参考距离 getter；配置距离和实际复 S 不等于 plane 读回或相位基准 |
| CPU 实际限制 | 0.2.2 solver-stage 实际记录为 2，两段 initial mesher 使用记录为 2；配置 getter 与这些局部证据均不证明全程/全进程 peak enforcement |
| mesh 单位/新鲜度/严格边长保证 | 原始统计及项目单位假设保留，getter 的实际单位和对应本次 solver run 的绑定仍待验收 |
| 任意高阶模式与多材料/导入几何 | 本实机案例只覆盖两个基模和一个 brick，离线支持不等于原生验收 |

原 raw 曲线仅作诊断；即使保留采样点吸收和频宽，也不作为可排名的物理结果、研究结论或数值认证。新预览包只包含通用原创代码、文档、合成示例与测试，不包含 CST 项目、结果、私有日志、厂商实现或新增许可证。
