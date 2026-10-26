# ③ 代码生成 · 工作流契约（CONTRACT）

> 这个文件是**在百炼上复现工作流 ③** 的依据。③ 会被**每个节点调用一次**，
> 所以它的输出必须小而规整 —— 一个信封，装一份代码。

---

## 一、输入（你会收到什么）

Leader 把 ② 的 `nodes[i]` 序列化后送进来：

```json
{"node_name":"obstacle_detector_node",
 "file_name":"obstacle_detector_node.py",
 "what_to_do":"订阅 /scan，计算前方最近距离并发布",
 "subscribe_topics":[{"topic":"/scan","message_type":"sensor_msgs/msg/LaserScan"}],
 "publish_topics":[{"topic":"/obstacle_distance","message_type":"std_msgs/msg/Float32"}],
 "parameters":[{"name":"stop_distance","type":"float","default":0.3}]}
```

---

## 二、输出（一个信封）

```markdown
<!--ENV_START-->
{"valid": true,
 "node_name": "obstacle_detector_node",
 "file_name": "obstacle_detector_node.py",
 "passed": true,
 "rounds": 1,
 "code": "import rclpy\nfrom rclpy.node import Node\n...",
 "teaching": "【知识点解析】...\n【易错避坑】...\n【练习拓展】..."}
<!--ENV_END-->
```

> `code` 里用 `\n` 换行（JSON 字符串），**不要把代码放在行外的裸文本里**。

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `valid` | bool | ✅ | 本次生成是否成功 |
| `node_name` | string | ✅ | 原样回传，Leader 靠它对账 |
| `file_name` | string | ✅ | 一般 `node_name + ".py"` |
| `code` | string | ✅ | 完整可运行文件内容（含 shebang 和 `main()`） |
| `passed` | bool | 建议 | 自检是否通过 |
| `rounds` | int | 建议 | 自修了几轮（1 = 一次过） |
| `teaching` | string | 建议 | 教学说明，三段：知识点 / 易错 / 练习 |

---

## 三、代码硬性要求（③ 的提示词要约束这些）

1. **完整可运行**：`import` → 类定义 → `main()` → `if __name__ == '__main__': main()`
2. **`main()` 必须有 `rclpy.init()` / `spin()` / `destroy_node()` / `rclpy.shutdown()`**
3. **命名规范**
   - 类名：大驼峰，且以 `Node` 结尾（`ObstacleDetectorNode`）
   - 节点名：与 `node_name` 一致（`obstacle_detector_node`）
   - 话题：以 `/` 开头，和输入里给的完全一致（别自己改名）
4. **只用 `rclpy`**（不产 C++）；消息类型严格用输入里给的 `pkg/msg/Type`
5. **日志必须用 `self.get_logger()`**，不要 `print`
6. **参数**：从输入里的 `parameters` 生成 `declare_parameter` + `get_parameter`

---

## 四、常见错误（我们踩过的）

| 错误 | 后果 | 正确做法 |
|---|---|---|
| 代码里写 `ament_pep8`（应该 `ament_pep257` + `python3-pytest`） | `colcon test` 失败 | 测试依赖写准 |
| 忘了 `main()` 或 `if __name__` | 节点跑不起来 | 两个都要 |
| 话题名自己加了前缀 `/ros2/...` | 和别人对不上 | 原样使用输入的 topic |
| 信封外面又包 ```json 围栏 | 早期解析失败 | 只留 ENV 标记 |
| JSON 后面补"代码已通过 AST 校验" | `Extra data` | 说明放 `teaching` 里 |
| `code` 里用了真实换行而不是 `\n` | JSON 非法 | 转义成 `\n` |

---

## 五、Leader 拿到后怎么用

```
1. 按 <!--ENV_START--> / <!--ENV_END--> 切出信封
2. 宽容解析 JSON（容错：尾部多余字符会被括号配平切掉）
3. valid=false → 该节点标记"生成失败"，不影响其他节点
4. valid=true  → render_codes() 把 code 包成 ```python 代码块
                 并把 teaching 接在后面
```
