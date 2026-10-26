# ② 项目规划 · 工作流契约（CONTRACT）

> 这个文件是**在百炼上复现工作流 ②** 的依据。② 是三个工作流里**输出最复杂、最容易踩坑**的一个：
> 输出必须是"三段拼接"，Leader 靠**标记段**把三段切开。

---

## 一、输入（你会收到什么）

Leader 把 ① 的 `data` 序列化成一个字符串送进来：

```json
{"schema_version":"1.0",
 "project":{"name_cn":"避障小车","name_en":"obstacle_avoidance",
            "package_name":"obstacle_avoidance","difficulty":"入门"},
 "modules":[{"module_name":"obstacle_detector",
             "inputs":[{"topic":"/scan","message_type":"sensor_msgs/msg/LaserScan"}],
             "outputs":[{"topic":"/obstacle_distance","message_type":"std_msgs/msg/Float32"}]}]}
```

---

## 二、输出（三段，顺序固定）

```markdown
## 📚 项目学习指南

（这里写给人看的 Markdown：项目介绍、整体架构、节点职责、知识点、学习路径、易错点）

<!--NODE_LIST_START-->
{"nodes":[{"node_name":"obstacle_detector_node",
           "file_name":"obstacle_detector_node.py",
           "what_to_do":"订阅 /scan，计算前方最近距离并发布",
           "subscribe_topics":[{"topic":"/scan","message_type":"sensor_msgs/msg/LaserScan"}],
           "publish_topics":[{"topic":"/obstacle_distance","message_type":"std_msgs/msg/Float32"}],
           "parameters":[{"name":"stop_distance","type":"float","default":0.3}]}]}
<!--NODE_LIST_END-->

<!--PLAN_JSON_START-->
{"plan":{"project":{…},"nodes":[…],"topic_overview":[…],"dependencies":[…],"file_structure":[…]}}
<!--PLAN_JSON_END-->
```

### 三段分别给谁用

| 段 | 谁消费 | 作用 |
|---|---|---|
| 指南正文（第一个标记之前的所有内容） | 直接给学生看 | 学习指南 |
| `NODE_LIST` | **Leader**（派活） | 决定"要调几次 ③、每次传什么节点规格" |
| `PLAN_JSON` | **Leader**（渲染） | 渲染成"项目规划书"表格（节点清单/话题总览/依赖/文件清单） |

---

## 三、标记段的四条硬规则（违反就会被 Leader 判为解析失败并**熔断**）

1. **四个标记必须原样出现，大小写与横线都不能差**：
   `<!--NODE_LIST_START-->` `<!--NODE_LIST_END-->`
   `<!--PLAN_JSON_START-->` `<!--PLAN_JSON_END-->`
2. **标记段内只放 JSON** —— 不要在 JSON 后面补"（注：以上节点清单已按难度排序）"这类话。
   （Leader 现在能靠括号配平把尾巴切掉，但**别依赖**。）
3. **`nodes` 必须是对象数组，每项必须有 `node_name`** —— 缺了会被熔断，整条流水线停下。
4. **指南正文里不要出现这两个 START 标记**（否则会被误当成段开头）。

---

## 四、`nodes[]` 必填字段

| 字段 | 说明 |
|---|---|
| `node_name` | 小写下划线，**必须以 `_node` 结尾**（③ 会据此生成文件名） |
| `file_name` | 一般等于 `node_name + ".py"` |
| `what_to_do` | 一句话职责，③ 拿它当生成指令 |
| `subscribe_topics` | 数组，元素 `{topic, message_type}`；没有就空数组 |
| `publish_topics` | 同上 |
| `parameters` | 可选，元素 `{name, type, default}` |

---

## 五、常见错误（我们踩过的）

| 错误 | 后果 | 正确做法 |
|---|---|---|
| 漏写 `<!--NODE_LIST_END-->` | 早期版本直接判"没有标记"丢掉结果（现在会按括号配平兜住） | 四个标记成对写全 |
| 标记段内 JSON 后面补说明文字 | `json.loads` 报 `Extra data: char N` | 说明文字放到段外面 |
| `node_name` 叫 `ObstacleDetector` | 生成的文件名/类名混乱 | 小写下划线 + `_node` 结尾 |
| 一次给 8~10 个节点 | Leader 分批，回复被截成"已生成 2/10" | 3~5 个以内 |
| `PLAN_JSON` 里的 `plan` 外面又套一层 | 渲染不出规划书 | 顶层就是 `{"plan": {...}}` |
| 指南里用了大量代码块 | 学生看晕 | 指南讲思路，代码交给 ③ |

---

## 六、Leader 拿到后的处理（便于你对齐）

```
1. 按四个标记切成三段
2. nodes → 逐个（默认并发 4）调 ③
3. plan  → render_plan_book() 画成规划书表格
4. guide → 原样跟在规划书后面
5. 代码 → render_codes() 包成 ```python 代码块 + ③ 的教学说明
```
