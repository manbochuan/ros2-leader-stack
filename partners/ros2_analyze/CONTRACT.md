# ① 需求分析 · 工作流契约（CONTRACT）

> 这个文件是**在百炼上复现工作流 ①** 的依据。接入层（`config.toml`）已经把请求按下面的格式
> 送进来了，你只要保证工作流**按契约输出**，Leader 就能读懂。

---

## 一、它在整条链路里的位置

```
学生原话
   │
   ▼
Leader（:9200）── 调 ① ──► 接入层（:9021）──► shim（:9099）──► 百炼工作流 ①
                                                                    │
   Leader 拿到 JSON ◄──────────────────────────────────────────────┘
   │
   ▼
   交给 ② 项目规划
```

**① 的唯一职责：** 把一句人话（可能很含糊）变成**机器能读的规格 JSON**。
它不做规划、不写代码。

---

## 二、输入（你会收到什么）

接入层的 production 阶段会把 Leader 传来的内容作为 `prompt` 送进来，形如：

```json
{
  "direction": "ROS2 学习项目",
  "level": "beginner",
  "user_text": "帮我做一个避障小车"
}
```

> 字段可能增减，但**核心是 `user_text`（学生原话）**。如果上游是直连模式，
> 这个 JSON 会同时出现在 `prompt` 和 `biz` 里 —— 以 `prompt` 为准。

---

## 三、输出（你必须产出什么）

**只输出一个 JSON 对象**，不要包 ```json 围栏，不要加任何解释文字。

```json
{
  "valid": true,
  "need_ask": false,
  "ask_question": "",
  "data": {
    "schema_version": "1.0",
    "project": {
      "name_cn": "避障小车",
      "name_en": "obstacle_avoidance",
      "package_name": "obstacle_avoidance",
      "difficulty": "入门",
      "summary": "基于激光雷达检测障碍物距离并自动避障的两轮差速小车"
    },
    "modules": [
      {
        "module_name": "obstacle_detector",
        "responsibility": "订阅 /scan，计算前方最近障碍物距离",
        "inputs":  [{"topic": "/scan", "message_type": "sensor_msgs/msg/LaserScan"}],
        "outputs": [{"topic": "/obstacle_distance", "message_type": "std_msgs/msg/Float32"}]
      }
    ]
  }
}
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `valid` | bool | ✅ | 需求是否可分析。`false` 时 Leader 会走"转平台/澄清"分支 |
| `need_ask` | bool | ✅ | 信息是否不足、需要追问 |
| `ask_question` | string | 否 | `need_ask=true` 时给学生的追问（一次只问一个问题） |
| `data.schema_version` | string | ✅ | 固定 `"1.0"`，用于将来兼容 |
| `data.project.name_cn` | string | ✅ | 中文项目名 |
| `data.project.name_en` | string | ✅ | 英文名（小写+下划线） |
| `data.project.package_name` | string | ✅ | ROS2 包名（`[a-z][a-z0-9_]*`） |
| `data.project.difficulty` | string | ✅ | `入门` / `进阶` / `综合` |
| `data.modules[]` | array | ✅ | **至少 1 个**；每个 module ≈ 一个节点 |
| `modules[].module_name` | string | ✅ | 小写下划线，不用 `node` 后缀 |
| `modules[].inputs/outputs` | array | ✅ | `topic` 以 `/` 开头；`message_type` 用 `pkg/msg/Type` |

---

## 四、三条硬规则

1. **永远返回 JSON**，哪怕是拒绝：`{"valid": false, "reason": "这不是 ROS2 需求"}`
2. **`modules` 至少 1 个、最多 6 个**（再多 Leader 会分批调 ③，很慢）
3. **话题名以 `/` 开头、消息类型写全 `pkg/msg/Type`** —— ② 和 ③ 都直接吃这两个字段

---

## 五、常见错误（我们踩过的）

| 错误 | 后果 | 正确做法 |
|---|---|---|
| 输出 ` ```json ` 围栏 | Leader 早期版本解析失败（现在能容错，但别依赖） | 裸 JSON |
| JSON 后面又补一段"以上就是分析结果…" | 解析器报 `Extra data` | 只输出 JSON，什么都不加 |
| `modules` 里放字符串数组 `["obstacle_detector"]` | ② 拿不到 inputs/outputs | 必须是对象数组 |
| 话题写成 `scan`（不带 `/`） | 生成代码里 topic 不一致 | `/scan` |
| 需要追问却直接 `valid:true` | 学生拿到错误的规格，后面全废 | `need_ask:true` + 一个问题 |
