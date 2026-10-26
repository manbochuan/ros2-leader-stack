# ③ 代码生成 · 百炼工作流（DSL 说明）

本目录里的两个文件是同一个东西：

| 文件 | 用途 |
|---|---|
| `workflow.dsl.zip` | **可直接导入百炼**的原始包（含 `template.yml` + `md5.txt`）。⚠️ 不要解压后改 yml，md5 会失效 |
| `workflow.template.yml` | 同样内容的可读副本，方便在 GitHub 上直接看节点与提示词 |

---

## 一、怎么导进你自己的百炼

```
① 百炼控制台 → 工作流 → 新建/导入
② 工作流画布右上角【⋯ 更多】→【导入百炼 DSL】→ 选 workflow.dsl.zip
③ 导入后必须做三件事（见下）
④ 发布 → 进入发布渠道 → 复制 app_id → 填到 adapters/bailian-shim/.env 的 BAILIAN_APPS
```

## 二、导入后必须手动补的三件事

| # | 事项 | 为什么 |
|---|---|---|
| 1 | **重新关联知识库插件** | DSL 只存了插件 ID，换账号后 ID 不存在，需重新绑定自己的「ROS2知识库检索」 |
| 2 | **确认模型可用** | 原工作流用的模型：`qwen3-coder-flash`、`qwen3.7-flash`、`qwen3.8-flash`；没开通就在对应节点换成你有的 |
| 3 | **改输入/输出对齐契约** | 输入必须能吃 `partners/ros2_codegen/CONTRACT.md` 里定义的 JSON；输出必须带标记段 |

## 三、节点构成（18 个）

| 节点 ID | 类型 | 名称 | 备注 |
|---|---|---|---|
| `Start_IFrk` | Start | 开始 | — |
| `End_TSmm` | End | 结束 | — |
| `LLM_Mdnl` | LLM | 大模型2 | qwen3-coder-flash |
| `Output_KlOn` | Output | 流程输出1 | — |
| `LLM_3GUn` | LLM | 大模型3 | qwen3.8-flash |
| `Iterator_3aWV` | Iterator | 循环1 | qwen3-coder-flash |
| `Judge_U4pP` | Judge | 条件判断1 | — |
| `LLM_ipDd` | LLM | 大模型1 | qwen3.7-flash |
| `LLM_ips1` | LLM | 大模型7 | qwen3.7-flash |
| `Output_jI8n` | Output | 流程输出2 | — |
| `Output_UFJP` | Output | 流程输出4 | — |
| `Output_VfQG` | Output | 流程输出5 | — |
| `Script_B9sf` | Script | 脚本1 | — |
| `Plugin_lhKK` | Plugin | 检索ROS2知识库-Plugin_lhKK | 插件「ROS2知识库检索」 |
| `Script_EnvA` | Script | 脚本3-信封组装A(直通) | — |
| `Script_EnvB` | Script | 脚本4-信封组装B(修复) | — |
| `Script_9tSI` | Script | 脚本5 | — |
| `Output_wBaB` | Output | 流程输出6 | — |

**模型**：`qwen3-coder-flash`、`qwen3.7-flash`、`qwen3.8-flash`
**外部插件**：`ROS2知识库检索`（导入后需重新关联）

## 四、数据流

```
Start_IFrk  [Start] 开始
    LLM_3GUn  [LLM] 大模型3
        Plugin_lhKK  [Plugin] 检索ROS2知识库-Plugin_lhKK
            LLM_Mdnl  [LLM] 大模型2
                Output_UFJP  [Output] 流程输出4
                    Script_B9sf  [Script] 脚本1
                        Judge_U4pP  [Judge] 条件判断1
                            Iterator_3aWV  [Iterator] 循环1
                                LLM_ips1  [LLM] 大模型7
                                    Script_EnvB  [Script] 脚本4-信封组装B(修复)
                                        Output_KlOn  [Output] 流程输出1
                                            Script_9tSI  [Script] 脚本5
                                                Output_wBaB  [Output] 流程输出6
                                                    End_TSmm  [End] 结束
                            LLM_ipDd  [LLM] 大模型1
                                Script_EnvA  [Script] 脚本3-信封组装A(直通)
                                    Output_jI8n  [Output] 流程输出2
                                        Script_9tSI  [Script] 脚本5  ⟲
                            Iterator_3aWV  [Iterator] 循环1  ⟲
                            LLM_ipDd  [LLM] 大模型1  ⟲
                        Output_VfQG  [Output] 流程输出5
                            Judge_U4pP  [Judge] 条件判断1  ⟲
```
（`⟲` 表示该节点在别的分支已经出现过，不重复展开）

---

## 五、和 `CONTRACT.md` 的关系

| 文件 | 回答什么问题 |
|---|---|
| `CONTRACT.md` | **契约**：输入什么、必须输出什么、标记段怎么写（人读） |
| `WORKFLOW.md`（本文件） | **实现**：这个工作流由哪些节点组成、怎么导入（机器可复制） |

**→ 想复现：导 DSL + 按 CONTRACT 校验输出；想改造：直接改 yml 里的提示词，再重新导出。**
