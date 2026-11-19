"""输入线程只入队；主线程串行处理输入与任务完成，避免回复互相抢占。"""

import argparse
import json
import queue
import sys
import threading

from .config import Config
from .llm import ChatLLM, DemoLLM
from .memory import Memory
from .schemas import AgentError
from .workflow import Workflow


def show_trace(event, data):
    print(f"[TRACE {event}] " + json.dumps(data, ensure_ascii=False), flush=True)


def read_input(events):
    for line in sys.stdin:
        events.put(line.strip())
    events.put(None)


def main():
    parser = argparse.ArgumentParser(description="会话内记忆＋单后台慢任务实验")
    parser.add_argument("--demo", action="store_true", help="固定示例，不是真实推理")
    parser.add_argument("--demo-delay", type=float, default=4, help="演示慢任务耗时秒数")
    parser.add_argument("--mode", choices=("auto", "fast", "slow"), default="auto")
    parser.add_argument("--quiet", action="store_true", help="隐藏 trace，保留通道标签")
    args = parser.parse_args()
    if not 0 <= args.demo_delay <= 60:
        parser.error("--demo-delay 必须在 0 到 60 之间")
    try:
        config = Config.from_env()
        llm = DemoLLM(args.demo_delay) if args.demo else ChatLLM(config)
    except (ValueError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    workflow = Workflow(llm, Memory(), config, None if args.quiet else show_trace)
    mode = args.mode
    events = queue.Queue()
    threading.Thread(target=read_input, args=(events,), daemon=True).start()
    print("快慢通道实验：会话结束即清空 memory/state。输入文字并回车。", flush=True)
    print("/status /result /cancel /discard /new /mode auto|fast|slow /exit", flush=True)
    if args.demo:
        print("【固定演示】‘比较方案’启动慢任务；可继续输入‘你好’、‘查好了吗’、‘要’。", flush=True)

    def display(reply):
        if reply:
            print(f"[{reply.channel.upper()} respond] {reply.answer}", flush=True)

    try:
        while True:
            try:
                # 优先处理已到达的输入，取消等指令先于完成反馈生效。
                message = events.get(timeout=0.1)
            except queue.Empty:
                message = ""
            try:
                if message is None or message == "/exit":
                    break
                if message == "/new":
                    workflow.close()
                    workflow = Workflow(llm, Memory(), config, None if args.quiet else show_trace)
                    print("新会话：memory/state 已清空。", flush=True)
                elif message.startswith("/mode"):
                    parts = message.split()
                    if len(parts) == 2 and parts[1] in {"auto", "fast", "slow"}:
                        mode = parts[1]
                        print(f"模式：{mode}", flush=True)
                    else:
                        print("用法：/mode auto|fast|slow", flush=True)
                elif message:
                    display(workflow.handle(message, mode))
                display(workflow.poll())
            except (AgentError, ValueError, OSError) as exc:
                # 用户消息保留；失败内容不冒充助手回答。已完成结果仍可 /result 重试整理。
                print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        print("已退出。")
    finally:
        workflow.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
