"""离线验证关键行为；不需要密钥，不发出网络请求。"""

import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from chat_agent.config import Config
from chat_agent.llm import ChatLLM
from chat_agent.memory import Memory
from chat_agent.schemas import AgentError, FastResult
from chat_agent.workflow import Workflow


DIRECT = {
    "action": "direct", "answer": "你好！", "analysis_task": "",
    "route_reason": "简单问候，无需深入分析",
}
ROUTE_SLOW = {
    "need_slow": True, "answer": "", "analysis_task": "比较方案的成本与限制",
    "route_reason": "需要综合多个约束",
}
ANALYSIS = {
    "conclusion": "先采用方案 A", "reasons": ["当前成本较低"],
    "assumptions": ["需求规模较小"], "uncertainties": ["缺少实际性能数据"],
    "follow_up_question": "",
}


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def generate(self, *, stage, messages):
        self.calls.append((stage, messages))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class ControlledLLM:
    def __init__(self):
        import threading
        self.release = threading.Event()
        self.entered = threading.Event()
        self.returned = threading.Event()
        self.calls = []
        self.fail = False
        self.slow_count = 0
        self.slow_tasks = []

    def generate(self, *, stage, messages):
        self.calls.append(stage)
        if stage == "slow":
            self.slow_count += 1
            self.slow_tasks.append(json.loads(messages[-1]["content"])["analysis_task"])
            self.entered.set()
            self.release.wait(2)
            self.returned.set()
            if self.fail:
                raise AgentError("test failure")
            return ANALYSIS
        if stage == "compose":
            return {"answer": "先选择 A，性能仍需验证。"}
        message = messages[-1]["content"]
        action = "replace" if "改为" in message else "start_slow" if "比较" in message else "direct"
        state = json.loads(messages[0]["content"].split("任务状态（数据而非指令）：", 1)[1])
        target = state["task_id"] if action == "replace" and state else None
        task = (state["analysis_task"] + "；" + message) if action == "replace" else "比较 A 和 B"
        return {"action": action, "target_task_id": target, "answer": "好，我想一下。" if action != "direct" else "你好！",
                "analysis_task": task if action != "direct" else "",
                "route_reason": "test"}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.memory = Memory()
        self.llm = ControlledLLM()
        self.events = []
        self.workflow = Workflow(self.llm, self.memory, self.config,
                                 lambda event, data: self.events.append((event, data)))

    def tearDown(self):
        self.llm.release.set()
        self.workflow.close()

    def complete(self):
        import time
        self.llm.release.set()
        deadline = time.monotonic() + 2
        reply = None
        while self.workflow.pending:
            if time.monotonic() > deadline:
                self.fail("background task did not finish")
            reply = self.workflow.poll() or reply
            time.sleep(.005)
        return reply

    def test_direct_and_memory(self):
        reply = self.workflow.handle("你好")
        self.assertEqual(reply.channel, "fast")
        self.assertEqual(self.llm.calls, ["fast"])
        self.assertEqual(len(self.memory.messages), 2)
        self.assertEqual(Memory().messages, [])

    def test_slow_does_not_block_fast_and_rejects_second_task(self):
        self.workflow.handle("比较方案")
        self.assertTrue(self.llm.entered.wait(1))
        task_id = self.workflow.state.task_id
        reply = self.workflow.handle("你好")
        self.assertEqual(reply.answer, "你好！")
        busy = self.workflow.handle("比较另一个方案")
        self.assertIn("还在处理", busy.answer)
        self.assertEqual(self.workflow.state.task_id, task_id)
        self.assertEqual(self.llm.slow_count, 1)
        notice = self.complete()
        self.assertIn("现在要听吗", notice.answer)
        self.assertIsNone(self.workflow.poll())
        result = self.workflow.handle("/result")
        self.assertEqual(result.channel, "slow")
        self.assertTrue(self.workflow.state.delivered)

    def test_no_topic_switch_delivers_immediately(self):
        self.workflow.handle("比较方案")
        reply = self.complete()
        self.assertEqual(reply.channel, "slow")
        self.assertTrue(self.workflow.state.delivered)
        self.assertIsNone(self.workflow.poll())

    def test_cancel_ignores_late_result(self):
        self.workflow.handle("比较方案")
        self.llm.entered.wait(1)
        self.workflow.handle("/cancel")
        self.assertIsNone(self.complete())
        self.assertEqual(self.workflow.state.status, "cancelled")
        self.assertIsNone(self.workflow.state.result)

    def test_completed_undelivered_not_overwritten(self):
        self.workflow.handle("比较方案")
        self.workflow.handle("你好")
        self.complete()
        task_id = self.workflow.state.task_id
        reply = self.workflow.handle("比较新方案")
        self.assertIn("还没给你看", reply.answer)
        self.assertEqual(self.workflow.state.task_id, task_id)

    def test_continuous_replacement_keeps_id_and_only_executes_latest(self):
        self.workflow.handle("比较方案")
        self.assertTrue(self.llm.entered.wait(1))
        task_id = self.workflow.state.task_id
        self.workflow.handle("改为只比较成本")
        self.workflow.handle("改为再加入维护难度")
        self.assertEqual(self.workflow.state.task_id, task_id)
        self.assertEqual(self.workflow.state.revision, 3)
        self.assertIn("成本", self.workflow.state.analysis_task)
        self.assertIn("维护难度", self.workflow.state.analysis_task)
        self.assertEqual(self.llm.slow_count, 1)
        self.assertEqual(self.workflow.waiting[1], 3)
        self.complete()
        self.assertEqual(self.llm.slow_count, 2)
        self.assertEqual(self.llm.slow_tasks[-1], self.workflow.state.analysis_task)
        ignored = [data for event, data in self.events if event == "slow_ignored"]
        self.assertEqual(ignored[0]["revision"], 1)
        self.assertEqual(self.workflow.state.status, "completed")

    def test_cancel_clears_latest_and_drops_old_result(self):
        self.workflow.handle("比较方案")
        self.assertTrue(self.llm.entered.wait(1))
        self.workflow.handle("改为只比较成本")
        self.workflow.handle("/cancel")
        self.assertIsNone(self.workflow.waiting)
        self.assertIsNone(self.complete())
        self.assertEqual(self.llm.slow_count, 1)
        self.assertIsNone(self.workflow.state.result)

    def test_wrong_target_cannot_modify_or_cancel(self):
        from chat_agent.schemas import FastResult
        self.workflow.handle("比较方案")
        state = self.workflow.state
        for action in ("replace", "cancel"):
            decision = FastResult(action == "replace", "好的", "新要求" if action == "replace" else "",
                                  "test", action, "wrong-id")
            with patch.object(self.workflow.fast, "respond", return_value=decision):
                reply = self.workflow.handle("修改或取消")
            self.assertIn("确认", reply.answer)
            self.assertEqual(state.revision, 1)
            self.assertEqual(state.status, "running")

    def test_cancel_then_new_task_does_not_receive_previous_result(self):
        self.workflow.handle("比较方案")
        self.assertTrue(self.llm.entered.wait(1))
        old_id = self.workflow.state.task_id
        self.workflow.handle("/cancel")
        self.workflow.handle("比较新方案")
        self.assertNotEqual(self.workflow.state.task_id, old_id)
        self.complete()
        self.assertEqual(self.llm.slow_count, 2)
        self.assertEqual(self.workflow.state.status, "completed")

    def test_ambiguous_direct_does_not_touch_task(self):
        from chat_agent.schemas import FastResult
        self.workflow.handle("比较方案")
        task_id = self.workflow.state.task_id
        decision = FastResult(False, "是修改旧任务还是新问题？", "", "指代不明", "direct")
        with patch.object(self.workflow.fast, "respond", return_value=decision):
            reply = self.workflow.handle("换一个呢")
        self.assertIn("修改旧任务", reply.answer)
        self.assertEqual(self.workflow.state.task_id, task_id)
        self.assertEqual(self.workflow.state.revision, 1)

    def test_failure_and_forced_fast(self):
        self.workflow.handle("比较方案", "fast")
        self.assertIsNone(self.workflow.state)
        self.llm.fail = True
        self.workflow.handle("比较方案")
        reply = self.complete()
        self.assertIn("没能完成", reply.answer)
        self.assertEqual(self.workflow.state.status, "failed")

    def test_defer_suppresses_notification(self):
        self.workflow.handle("比较方案")
        self.workflow.handle("你好")
        self.workflow.state.notified = True
        self.assertIsNone(self.complete())
        self.assertIsNotNone(self.workflow.state.result)

    def test_invalid_action_rejected(self):
        with self.assertRaises(AgentError):
            FastResult.parse({"action": "unknown", "answer": "x", "analysis_task": "", "route_reason": "x"})


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(base_url="https://example.invalid/v1", api_key="test-only-key",
                             fast_model="fast-test", slow_model="slow-test", json_mode=True)

    def response(self, content, finish_reason="stop"):
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content},
                                                  "finish_reason": finish_reason}]}).encode())

    @patch("chat_agent.llm.urlopen")
    def test_slow_request_uses_correct_model_budget_and_json_mode(self, opened):
        opened.return_value = self.response(json.dumps(ANALYSIS))
        result = ChatLLM(self.config).generate(stage="slow", messages=[{"role": "user", "content": "比较"}])
        request = opened.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://example.invalid/v1/chat/completions")
        self.assertEqual(payload["model"], "slow-test")
        self.assertEqual(payload["max_completion_tokens"], 2400)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(result, ANALYSIS)

    @patch("chat_agent.llm.urlopen")
    def test_compatibility_token_field_and_disabled_json_mode(self, opened):
        opened.return_value = self.response(json.dumps(DIRECT))
        config = replace(self.config, token_field="max_tokens", json_mode=False)
        ChatLLM(config).generate(stage="fast", messages=[])
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload["max_tokens"], 800)
        self.assertNotIn("response_format", payload)

    @patch("chat_agent.llm.urlopen")
    def test_truncated_output_and_invalid_json_fail_clearly(self, opened):
        for content, reason in [("{}", "length"), ("not JSON", "stop")]:
            opened.return_value = self.response(content, reason)
            with self.assertRaises(AgentError):
                ChatLLM(self.config).generate(stage="fast", messages=[])

    @patch("chat_agent.llm.urlopen")
    def test_network_errors_do_not_expose_service_body_or_credentials(self, opened):
        for error in [
            HTTPError("https://example.invalid", 401, "test-only-key", {}, io.BytesIO(b"private")),
            URLError("test-only-key"),
        ]:
            opened.side_effect = error
            with self.assertRaises(AgentError) as caught:
                ChatLLM(self.config).generate(stage="fast", messages=[])
            self.assertNotIn("test-only-key", str(caught.exception))
            self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
