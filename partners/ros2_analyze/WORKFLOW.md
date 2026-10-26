# ① 需求分析 · 百炼工作流（DSL 说明）

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
| 2 | **确认模型可用** | 原工作流用的模型：`qwen3.8-flash`、`qwen3.8-max`；没开通就在对应节点换成你有的 |
| 3 | **改输入/输出对齐契约** | 输入必须能吃 `partners/ros2_analyze/CONTRACT.md` 里定义的 JSON；输出必须带标记段 |

## 三、节点构成（9 个）

| 节点 ID | 类型 | 名称 | 备注 |
|---|---|---|---|
| `Start_Y0nM` | Start | 开始 | — |
| `End_7JNw` | End | 结束 | — |
| `LLM_7DJ6` | LLM | 大模型1 | qwen3.8-max |
| `Output_upR5` | Output | 流程输出1 | — |
| `Script_rtRL` | Script | 脚本1 | — |
| `Judge_E7GH` | Judge | 条件判断1 | — |
| `Iterator_171V` | Iterator | 循环1 | qwen3.8-flash |
| `Script_Ijod` | Script | 脚本3 | — |
| `Output_WRy3` | Output | 流程输出2 | — |

**模型**：`qwen3.8-flash`、`qwen3.8-max`
**外部插件**：无（导入后需重新关联）

## 四、数据流

```
Start_Y0nM  [Start] 开始
    LLM_7DJ6  [LLM] 大模型1
        Output_upR5  [Output] 流程输出1
            Script_rtRL  [Script] 脚本1
                Judge_E7GH  [Judge] 条件判断1
                    Iterator_171V  [Iterator] 循环1
                        Script_Ijod  [Script] 脚本3
                            Output_WRy3  [Output] 流程输出2
                                End_7JNw  [End] 结束
                    Script_Ijod  [Script] 脚本3  ⟲
```
（`⟲` 表示该节点在别的分支已经出现过，不重复展开）

---

## 五、和 `CONTRACT.md` 的关系

| 文件 | 回答什么问题 |
|---|---|
| `CONTRACT.md` | **契约**：输入什么、必须输出什么、标记段怎么写（人读） |
| `WORKFLOW.md`（本文件） | **实现**：这个工作流由哪些节点组成、怎么导入（机器可复制） |

**→ 想复现：导 DSL + 按 CONTRACT 校验输出；想改造：直接改 yml 里的提示词，再重新导出。**
