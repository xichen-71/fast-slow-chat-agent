# 快慢通道 Agent：最小后台任务版

完整设计见 [新方案说明](新方案说明.md)。Python 3.10+，标准库实现，无需安装运行依赖。

## 离线演示

```bash
cd '/Users/chenxi/Desktop/聊天agent/项目实现'
python3 -m chat_agent.main --demo
```

依次输入：

1. `比较方案`：固定示例启动慢任务（默认延迟 4 秒）。
2. 在其完成前输入 `你好`：FAST 接话。
3. 输入 `比较另一个方案`：提示上一任务仍在处理。
4. 等待完成提醒，输入 `要` 或 `/result`：查看结果。

演示按关键词返回固定数据，不会真实推理。可用 `--demo-delay 10` 延长等待，便于手动实验。

## 修改旧任务与提出新任务

真实模式由 Fast LLM 判断，Workflow 校验并执行：

| 用户表达 | action | 实际处理 |
| --- | --- | --- |
| “刚才那个，改为只比较成本” | replace | 校验目标 ID，更新完整要求，revision +1 |
| “另外分析一下另一个项目” | start_slow | 原任务运行中则提示等待，不自动接下或排队 |
| “顺便解释一下异步” | direct | 快答，原任务继续 |
| 指代不明，如“换一个呢” | direct | 先询问确认，保留旧任务 |

同一任务修改时保留 task_id，原始问题不变；analysis_task 保存完整最新要求。
连续修改只保留一个最新待执行快照。例如 v1 运行时收到 v2/v3，只在 v1 结束后执行 v3。
取消会清空待执行快照；旧 ID、旧 revision、已取消的结果一律不采用。

演示模式可输入 `比较方案` → `改为只比较成本` → `改为加入维护难度`，观察
`task_revised`、`slow_queued_latest`、`slow_ignored` 日志。演示只做固定拼接，不能用于评估语义分类或条件合并质量。
真实模型的条件合并仍可能出错；默认 trace 显示修改前后要求，方便核对。

## 连接模型

```bash
cp .env.example .env
# 编辑 .env：LLM_BASE_URL、LLM_API_KEY、FAST_MODEL
python3 -m chat_agent.main
```

模型服务须支持 Chat Completions 的 system/user/assistant 消息和 JSON 文本输出。
`LLM_BASE_URL` 填接口前缀（如 https://服务域名/v1），不含 /chat/completions。
`SLOW_MODEL` 留空则共用快模型。按供应商要求选择 `LLM_TOKEN_FIELD`，支持时可开启 JSON mode。
请求格式参考 [官方接口文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

`.env` 为简单 KEY=value，可使用成对引号；不执行脚本、不展开变量，已有环境变量优先。
当前消息和截取后的最近对话会发送给配置的服务；未接搜索和长期记忆。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/status` | 查看当前任务状态 |
| `/result` | 将已完成结果交给快通道整理后显示 |
| `/cancel` | 取消任务，忽略晚到结果 |
| `/discard` | 丢弃旧任务或结果；之后重新提出新问题 |
| `/new` | 清空会话，旧任务不再向新会话输出 |
| `/mode auto` | 模型判断是否调用慢通道 |
| `/mode fast` | 不创建慢任务 |
| `/mode slow` | 强制普通消息启动慢分析；任务管理用命令 |
| `/exit` | 退出；已发送的 HTTP 可能需等返回或超时 |

`--quiet` 隐藏 trace，仍显示 FAST/SLOW 标签。模型自然语言识别存在误判，命令入口是确定性的。

## 当前边界

- memory 为进程内消息列表，state 只存当前慢任务。重启不恢复，旧版 SQLite 不再读取。
- 一个运行中的慢任务，不自动接收、排队第二个复杂问题。
- 已完成未展示的结果不会被新问题覆盖。
- 取消不保证中断远端 HTTP，但旧结果一定不再被采用。
- 先实现深度分析；尚未接联网、知识库或长期记忆检索。
- 快通道默认输出预算 800，慢通道 2400；这是上限，不保证速度或质量。
- 不流式输出；快调用期间输入可入队，输出与状态变更仍串行处理。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用模拟模型，不访问网络，覆盖连续修改仅执行最新版、目标 ID 错误、取消清空待执行任务、取消后新建、旧版本晚到及原有快慢流程。真实模型分流质量、条件保留情况、服务并发能力与响应速度需配置后验证。
