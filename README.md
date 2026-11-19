# Fast–Slow Chat Agent V2（最小运行版）

Fast 负责理解和全部自然回复，Slow 后台分析，Workflow 校验动作并维护真实状态。
Python 3.10+，仅标准库，无需安装运行依赖。详细设计见 [新方案说明](新方案说明.md)。

## 运行

```bash
python3 -m chat_agent.main --demo
```

离线演示依次输入：

1. `比较两个方案`：启动 Slow（默认模拟 4 秒）。
2. 未完成前输入 `线程是什么`：Fast 回答，旧任务继续。
3. `另外分析一个项目`：Fast 说明忙碌，不接新任务。
4. `刚才改为重点看成本`：新任务 ID，旧结果失效。
5. `再加入维护难度`：只保留最新待执行要求。
6. 聊点别的，等 Fast 提醒后输入 `要` 或 `/result` 查看。

可用 `--demo-delay 10` 留出实验时间，`--quiet` 隐藏 trace，始终保留 FAST/SLOW respond 标签。
Demo 用固定响应和关键词演示协议，不做真实分析，不可用于评价语义判断。

## 连接真实模型

```bash
cp .env.example .env
# 编辑 .env：LLM_BASE_URL、LLM_API_KEY、FAST_MODEL
python3 -m chat_agent.main
```

BASE_URL 为 Chat Completions 接口前缀（如 https://服务域名/v1），程序追加 /chat/completions。
SLOW_MODEL 留空则共用快模型。按服务要求选 LLM_TOKEN_FIELD；支持时可打开 LLM_JSON_MODE。
.env 不执行 shell、不展开变量，已有环境变量优先。最近对话会发送给配置的服务。

## 最小工作流

```text
用户消息 → 同步后台事实 → Fast 返回 respond + action
    → Workflow 校验执行 → 成功则展示；失败/状态变化则 Fast 修正一次
Slow 完成/失败 → 保存事实 → 系统事件进入 Fast → 交付、提醒或暂不打断
```

所有普通话术都由 Fast 生成，不再由 Workflow 固定输出“忙碌/完成/取消”答案。
程序只在技术错误或 CLI 控制时输出诊断。Fast prompt 包含逐场景规则和示例，见
[fast.md](chat_agent/prompts/fast.md)。

统一格式：

```json
{"respond":"自然回复", "action":{"type":"none", "task":null, "target_task_id":null}}
```

动作：none、start_slow、update_slow、cancel_slow、get_status、get_result、defer_result、discard_result。
只有系统事件的 none 可 respond=null，不打断用户。修改任务必须带完整新要求和有效目标 ID。
动作失败只修正一次回复，不重试动作；非法输出安全报错。代码无法完全检测任意自然语言中的虚假承诺，仍需真实模型评测。

## CLI

| 命令 | 作用 |
| --- | --- |
| `/status` | 经 Fast 查询真实任务状态 |
| `/result` | 经 Fast 整理已完成结果 |
| `/cancel`、`/discard` | 经 Fast 明确取消/丢弃，Workflow 验证动作 |
| `/new` | 清空会话；旧结果不会串入新会话 |
| `/mode auto` | Fast 按问题和状态分流 |
| `/mode fast` | 禁止启动/更新 Slow，管理旧任务仍可 |
| `/mode slow` | 普通问题要求 Slow，仍经 Fast 并受单任务约束 |
| `/exit` | 退出；已发出 HTTP 可能需等返回或超时 |

## 范围和限制

- 进程内 memory/state，无长期记忆、检索、联网、工具操作、LangGraph 或 Web UI。
- 一个有效慢任务，一个 worker；独立的新复杂任务不排队；已完成未交付结果不被静默覆盖。
- update_slow 新建 ID；连续更新只保留最新快照，取消清空快照，旧结果无法写回。
- 完成/失败事件最多处理一次；结果保存，稍后可查询。事件生成失败可 /result 或 /status 恢复。
- 主线程串行处理 Fast 和输出，后台 Slow 独立运行；服务端也需允许并发，否则快回复可能被拖慢。
- Fast/Slow 主要由角色与预算区分，不保证同模型深分析一定更准；未接真实模型验证。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用模拟模型与模拟 HTTP，不需要密钥。覆盖协议、后台任务、状态同步、失败修正、隔离与 CLI 模式。

V1 的 revision+固定话术协议已被 V2 替换；保留原有模块布局、配置方法和 CLI 入口。
