# 0.2.3-preview：写入完成、网格与正式读回审查

本候选仅离线修改与测试，未启动 CST，也未增加案例、材料或频点。既有 0.2.2
合成平板已实测失败：求解器 SUCCESS、owned 自动关闭，但网格比较和归档完整性
未通过。失败记录、旧哈希及私有原始结果保持原状；本说明不把它们升级为通过。

## 归档变化能够定位到哪里

0.2.2 在求解前保存并绑定归档，随后调用异步 `start_solver`。后续 owned 控制
检查在首次及之后的运行状态查询、求解信息查询和固定网格读回前后观察归档。
既有记录未保存“首次观察漂移的 SDK 调用名/时间”，因此只能将首次发现限制在
异步启动后至网格读回最终检查之间；不能断言是某一次 poll、保存或特定 CST
后台线程造成，也不能把合成自改写测试当作实际写入者证据。

`archive-write-trace.json` 保留清理时的缓存观察，失败和 pending 也不丢调用边界；pending 快照标明事件可能继续，不能据此推断完成或清除 quarantine。

本候选记录 operation ID、阶段、before/after、单调时间、观察身份与可用哈希，
用于后续授权运行定位首先在哪个调用边界发生变化。观察边界并不等于写入因果。

## 独立的同步完成路径

安装版正式 Python API 中，`Model3D.start_solver` 异步返回，
`Model3D.run_solver` 等待选定求解器及后处理完成。`is_solver_running=False`
和 `get_solver_run_info` 本身没有承诺归档写入已排空，后者也没有文档化 run ID
字典字段。候选采用一次 `run_solver` 的独立路径；不在异步启动后再调用它等待，
因为那可能再次发起求解。原有异步 start/poll/abort 路径保留其严格门禁。

1. 严格核验旧归档身份/哈希、普通单链接文件及目录链、held DE/project、完整
   owned 进程身份、SDK filename、停止状态和剩余工作预算。既有 quarantine 拒绝进入。
2. 在内部生成不可由调用者指定的 operation token。允许的窗口仅覆盖这一次
   同步求解、实际停止/成功查询和显式 held-project `save(include_results=True)`。
   每个潜在阻塞调用前后核对同一绑定；允许窗口内的归档变化先记录，旧 pin 不变。
3. 正式 `run_solver` 返回后，要求实际 running 为 False、实际 solver info 为
   SUCCESS，再执行显式保存。失败、未知、身份变化或期限届满不进入提交路径。
4. 保存后取连续一致的严格完整快照，核对文件句柄、当前路径、身份、元数据、
   单链接状态和 SHA-256。工作线程仅准备结果；只有期限内收到并接受完成的调用方，
   在既有请求锁下以纯内存操作提交新 pin。取消、超时或拒绝后，迟到回调不能提交。
5. 完成窗口之外的变化继续 quarantine；关闭后和导出前重新核验。历史 quarantine
   不被清除，历史失败不改写。未完成 SDK 请求继续受监督，不并发发起 abort/close。

这个流程是显式 SDK 操作信任边界。正式文档没有给出 archive writer 身份、
排空后台写入、独占写者或 fsync/持久化保证。窗口内稳定的其他写者与稳定的 CST
改写可呈现相同路径/身份/哈希，不能靠 token 或多次哈希区分；路径检查也仍有
check/use 窗口。候选保留这些限制，不能宣称全面防篡改或数值验收成功。
同步调用使用剩余工作预算，但调用方超时不保证立即终止底层求解。

## 网格：名义尺寸与实测验收

正式 **Mesh Properties (Tetrahedral)** 的 **Definition of cell size** 将单元尺寸
描述为顶点周围平均边长，并说明为改善质量，尺寸限制并非严格上限。
历史 `Mesh.MinimumStepNumberTet`、`Solid.SetMeshStepwidthTet` 和密度传播设置
因此只能作为名义控制，不能承诺最终优化/自适应网格每条边都小于目标。
`Mesh.GetMaximumEdgeLength` 针对当前选中网格，检查过的文档未明确返回单位或
与实际求解/自适应 pass 的身份关联；不得执行 postsolve Mesh.Update 冒充读回刷新。

| 输入 | 含义 | 实测超限的处理 |
| --- | --- | --- |
| 旧 `mesh.max_edge_m` | 同时保留名义目标和原实测验收阈值 | 保存完整诊断报告后拒绝，不放宽旧值 |
| `mesh.target_edge_m` | 名义目标 | 记录目标超出；不凭未设置阈值宣称 native 通过 |
| `target_edge_m` 加 `acceptance_max_edge_m` | 名义目标和独立实测最长边阈值 | 按显式阈值比较，超出后保存报告并拒绝 |

旧字段与新字段互斥；可选验收阈值不自动由目标复制。无自动扩大阈值、经验倍数、
细化或再次求解。1 mm 请求对应的既有 raw max=1.16412，在 project mm 与 final-mesh
假设下是 1.16412 mm（+16.412%）；这是旧验收阈值失败，仍保留单位/新鲜度未知。
命名为“maximum”或输出值接近几何尺度不证明硬上限被 enforce。

## 正式材料 getter 与实际记录关联

正式 **Material Object** 提供 `Material.GetSigma` 的 XYZ 电导率和
`Material.GetRho` 密度；**Solid Object** 提供实际形状材料名。
候选对实际 shape 取得实际材料名后读取全部 Sigma 分量及 Rho，保留 getter/单位
来源、case signature、operation ID 和文件哈希，核对完整形状覆盖及真实材料分配。
σ 已合并到 ε 时，原生 XYZ σ 都须为零以避免双计。各材料使用各自声明密度；
缺少输入密度时只保留观察值，不擅设质量默认值。文件一致性不证明 solver 使用。

检查过的 **FDSolver Object** 只发现 `TDCompatibleMaterials` setter，
**FloquetPort Object** 只发现 `SetDistanceToReferencePlane` setter；没有核实实际
FD 策略/参考面的正式 getter，不生成猜测的 Get 方法或将 requested 值复制成 actual。
FD setter 对特定材料类型的描述不能扩大为各向同性体 ε/μ 表的通用插值承诺。
实际 Data、FD、Nth-order Fit 按完整树叶分开，保留真实频点与缺失样点，不最近邻
替代或插值补齐验收值。FD 样点吻合只是一致性诊断，不能证明实际策略或材料响应链。

`solver-binding.json` 保留 SDK 原样信息、prepared case、输入内容哈希、owned
operation 和归档快照。`result-provenance.json` 保留实际叶、curve run ID、参数、
来源角色及真实频率，并重新核验归档。两者只证明受限的同次调用/文件关联；
正式 solver info 没有可凭空补出的 native run ID。native curve-run authentication、
solver-material linkage、FD 策略和参考面保持未证实，数值资格为 false。

## 离线回归矩阵

| 场景 | 必须成立的行为 |
| --- | --- |
| 合法 operation 内同步求解/保存更换归档 | 稳定绑定下仅调用方接受后提交一次新 pin，保留操作轨迹 |
| 窗口前后替换、同 inode 改写、链接、路径或 held 对象变化 | 拒绝并保留旧 pin/quarantine，不误用其他会话 |
| late、cancel、timeout、求解失败或未知 running/info | 不提交、不重开窗口；保留 pending 生命周期，不并发 SDK mutation |
| 旧 1 mm 输入、实测 1.16412（unit factor .001） | 完整报告先保存，旧阈值失败且单位/新鲜度仍未知 |
| 仅目标与显式独立验收阈值 | 区分目标偏差/验收结果，case 哈希绑定真实验收策略 |
| Sigma Y/Z 非零、密度错配、材料错配、缺失/重复/不安全 TSV | 拒绝；原输入 σ 不被重复加入；实际 shape 覆盖完整 |
| 伪造 FD/参考面 getter、只有 requested 设置或数值吻合 | actual 保持 unknown，不能清除证据门禁 |
| 输入读取 A-B-A、导出时归档替换/删除、曲线身份不完整 | 内容与解析同一快照；关联失效，不认证 native run |
| 合成/注入接口全部一致 | 仍非 native 验收、非数值认证、非物理认证 |

最终离线测试 489/489 通过（62.058 秒）；首次全套的一项旧竞态测试未到达指定回调，改为事件门驱动后通过，两次日志保留在回归包。focused RED/GREEN 与独立审查不合并计数，不作为实机通过。
来源为安装版 CST 2025 Python **cst.interface** 中 Project.save、Model3D.start_solver、
run_solver、is_solver_running、get_solver_run_info，以及上述正式 Help 页面。
本仓库仅包含原创释义、通用代码与合成测试，不分发厂商帮助正文、SDK 或私有模型结果。

本候选本地源基点为 `ca33b46`。主对话报告 GitHub 已更新至
`a938754e1bd28b794844e6af85565b86659c6b33`；本轮未 fetch 或验证该远端提交。
补丁与打包内容待审查后再由主对话安排发布，不安装、不推送、不增设许可证或公开性。
