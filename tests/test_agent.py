"""V2 离线协议、状态隔离与后台交互测试，不调用真实 API。"""

import io
import json
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from chat_agent.config import Config
from chat_agent.llm import ChatLLM, DemoLLM, parse_json_object
from chat_agent.memory import Memory
from chat_agent.schemas import Action, AgentError, FastResponse
from chat_agent.workflow import Workflow

DIRECT = {"respond": "你好", "action": {"type": "none"}}
ANALYSIS = {"conclusion": "先验证最小流程", "reasons": ["减少变量"],
            "assumptions": ["实验阶段"], "uncertainties": ["尚未真实评估"], "follow_up_question": ""}


def response(kind="none", respond="模型回复", task=None, target=None):
    return {"respond": respond, "action": {"type": kind, "task": task, "target_task_id": target}}


class ControlledLLM(DemoLLM):
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.slow_inputs = []
        self.fast_inputs = []
        self.fail = False
        self.override = None

    def generate(self, *, stage, messages):
        context = json.loads(messages[-1]["content"])
        if stage == "slow":
            self.slow_inputs.append(context)
            self.entered.set()
            if not self.release.wait(2):
                raise AgentError("TEST_TIMEOUT")
            if self.fail:
                raise AgentError("TEST_FAILURE")
            return ANALYSIS
        self.fast_inputs.append(context)
        if self.override:
            result = self.override(context)
            if result is not None:
                return result
        return super().generate(stage=stage, messages=messages)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.llm = ControlledLLM()
        self.memory = Memory()
        self.events = []
        self.workflow = Workflow(self.llm, self.memory, Config(), lambda e, d: self.events.append((e, d)))

    def tearDown(self):
        self.llm.release.set()
        self.workflow.close()
        self.workflow.executor.shutdown(wait=True)

    def finish(self):
        self.llm.release.set()
        replies = []
        deadline = time.monotonic() + 2
        while self.workflow.pending or self.workflow.completion_event:
            self.assertLess(time.monotonic(), deadline)
            reply = self.workflow.poll()
            if reply:
                replies.append(reply)
            time.sleep(.002)
        return replies

    def start(self):
        reply = self.workflow.handle("比较方案 A 和 B")
        self.assertTrue(self.llm.entered.wait(1))
        self.assertEqual(self.workflow.state.status, "running")
        return reply

    def test_direct_goes_through_fast_and_saves_only_real_messages(self):
        reply = self.workflow.handle("线程是什么")
        self.assertEqual(reply.channel, "fast")
        self.assertEqual(self.llm.fast_inputs[0]["event"], "user_message")
        self.assertEqual([m["role"] for m in self.memory.messages], ["user", "assistant"])
        self.assertIsNone(self.workflow.state)

    def test_simple_interruption_and_unrelated_complex_task(self):
        self.start()
        task_id = self.workflow.state.task_id
        self.workflow.handle("线程是什么")
        reply = self.workflow.handle("另外分析一个项目")
        self.assertIn("不接下新任务", reply.answer)
        self.assertEqual(self.workflow.state.task_id, task_id)
        self.assertEqual(len(self.llm.slow_inputs), 1)
        self.assertEqual(self.llm.fast_inputs[-1]["task_state"]["analysis_task"], "比较方案 A 和 B")

    def test_completion_waiting_delivers_once_through_fast(self):
        self.start()
        replies = self.finish()
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].channel, "slow")
        self.assertTrue(self.workflow.state.delivered)
        self.assertEqual(self.llm.fast_inputs[-1]["event"], "slow_completed")
        self.assertIsNone(self.workflow.poll())
        self.assertEqual(sum(m["role"] == "user" for m in self.memory.messages), 1)

    def test_completion_after_topic_switch_asks_once_then_delivers(self):
        self.start()
        self.workflow.handle("你好")
        reply = self.finish()[0]
        self.assertIn("现在要看吗", reply.answer)
        self.assertFalse(self.workflow.state.delivered)
        self.assertIsNone(self.workflow.poll())
        reply = self.workflow.handle("要")
        self.assertEqual(reply.channel, "slow")
        self.assertTrue(self.workflow.state.delivered)

    def test_related_updates_new_ids_keep_only_latest(self):
        self.start()
        original = self.workflow.state
        self.workflow.handle("改为重点看成本")
        second = self.workflow.state
        self.workflow.handle("再加入维护难度")
        third = self.workflow.state
        self.assertEqual(len({original.task_id, second.task_id, third.task_id}), 3)
        self.assertEqual(original.status, "cancelled")
        self.assertIn("成本", third.analysis_task)
        self.assertIn("维护难度", third.analysis_task)
        self.assertEqual(self.workflow.waiting[0], third.task_id)
        self.finish()
        self.assertEqual(len(self.llm.slow_inputs), 2)
        self.assertEqual(self.llm.slow_inputs[-1]["analysis_task"], third.analysis_task)
        self.assertIsNone(original.result)

    def test_cancel_clears_waiting_and_ignores_old_future(self):
        self.start()
        self.workflow.handle("改为看成本")
        self.workflow.handle("/cancel")
        self.assertIsNone(self.workflow.waiting)
        self.assertEqual(self.workflow.state.status, "cancelled")
        self.assertEqual(self.llm.fast_inputs[-1]["explicit_intent"], "cancel_slow")
        self.assertEqual(self.finish(), [])
        self.assertIsNone(self.workflow.state.result)
        self.assertEqual(len(self.llm.slow_inputs), 1)

    def test_cancel_then_new_task_isolated(self):
        self.start()
        old_id = self.workflow.state.task_id
        self.workflow.handle("/cancel")
        self.workflow.handle("分析新方案")
        self.assertNotEqual(self.workflow.state.task_id, old_id)
        self.finish()
        self.assertEqual(len(self.llm.slow_inputs), 2)
        self.assertEqual(self.workflow.state.status, "completed")

    def test_completed_undelivered_not_overwritten(self):
        self.start()
        self.workflow.handle("你好")
        self.finish()
        task_id = self.workflow.state.task_id
        self.workflow.handle("分析一个新方案")
        self.assertEqual(self.workflow.state.task_id, task_id)
        self.assertFalse(self.workflow.state.delivered)

    def test_slow_failure_enters_fast_event_without_fake_analysis(self):
        self.llm.fail = True
        self.start()
        reply = self.finish()[0]
        self.assertIn("失败", reply.answer)
        self.assertEqual(self.workflow.state.status, "failed")
        self.assertEqual(self.llm.fast_inputs[-1]["event"], "slow_failed")
        self.assertIsNone(self.llm.fast_inputs[-1]["analysis_result"])

    def test_sync_before_user_message_routes_latest_completed_state(self):
        self.start()
        self.llm.release.set()
        self.workflow.pending[1].result(timeout=1)
        reply = self.workflow.handle("/status")
        self.assertIn("completed", reply.answer)
        self.assertEqual(self.llm.fast_inputs[-1]["task_state"]["status"], "completed")

    def test_rejected_action_corrected_once_without_false_promise(self):
        self.start()
        task_id = self.workflow.state.task_id
        def override(ctx):
            if ctx["event"] == "user_message":
                return response("start_slow", "错误承诺：新任务已经开始", "新任务")
        self.llm.override = override
        reply = self.workflow.handle("另一个问题")
        self.assertNotIn("错误承诺", reply.answer)
        self.assertIn("SLOW_BUSY", reply.answer)
        self.assertEqual(self.workflow.state.task_id, task_id)
        self.assertEqual(len(self.llm.slow_inputs), 1)
        self.assertEqual(self.llm.fast_inputs[-1]["event"], "action_failed")
        self.assertFalse(any("错误承诺" in m["content"] for m in self.memory.messages))

    def test_wrong_target_and_repeated_action_in_correction_never_execute(self):
        self.start()
        task_id = self.workflow.state.task_id
        self.llm.override = lambda ctx: response("cancel_slow", "取消了", target="wrong")
        with self.assertRaises(AgentError):
            self.workflow.handle("取消")
        self.assertEqual(self.workflow.state.task_id, task_id)
        self.assertEqual(self.workflow.state.status, "running")
        self.assertEqual(len(self.llm.fast_inputs), 3)  # start + decision + one correction

    def test_invalid_json_action_or_missing_task_leaves_state_unchanged(self):
        self.start()
        task_id = self.workflow.state.task_id
        for invalid in ({"respond": "x", "action": {"type": "shell"}}, response("start_slow", task=None)):
            self.llm.override = lambda ctx, value=invalid: value
            with self.assertRaises(AgentError):
                self.workflow.handle("无效输出")
            self.assertEqual(self.workflow.state.task_id, task_id)
            self.assertEqual(self.workflow.state.status, "running")
        with self.assertRaises(AgentError):
            parse_json_object("not JSON")

    def test_fast_and_slow_modes_still_enter_fast(self):
        self.workflow.handle("分析问题", "fast")
        self.assertIsNone(self.workflow.state)
        self.workflow.handle("你好", "slow")
        self.assertEqual(self.workflow.state.status, "running")
        self.assertEqual([c["mode"] for c in self.llm.fast_inputs], ["fast", "slow"])

    def test_explicit_cancel_cannot_be_ignored_by_model(self):
        self.start()
        self.llm.override = lambda ctx: response("none", "已经取消") if ctx["event"] == "user_message" else None
        reply = self.workflow.handle("/cancel")
        self.assertIn("COMMAND_ACTION_MISMATCH", reply.answer)
        self.assertEqual(self.workflow.state.status, "running")

    def test_state_change_during_fast_call_corrects_stale_status(self):
        self.start()
        def override(ctx):
            if ctx["event"] == "user_message":
                self.llm.release.set()
                self.workflow.pending[1].result(timeout=1)
                return response("get_status", "旧状态：仍在分析", target=self.workflow.state.task_id)
        self.llm.override = override
        reply = self.workflow.handle("/status")
        self.assertNotIn("旧状态", reply.answer)
        self.assertEqual(self.workflow.state.status, "completed")
        self.assertEqual(self.llm.fast_inputs[-1]["task_state"]["status"], "completed")

    def test_defer_and_silent_event_do_not_repeat(self):
        self.start()
        self.workflow.handle("稍后再看")
        self.assertEqual(self.finish(), [])
        self.assertIsNotNone(self.workflow.state.result)

    def test_silent_completion_not_written_as_message(self):
        self.start()
        self.llm.override = lambda ctx: response(respond=None) if ctx["event"] == "slow_completed" else None
        size = len(self.memory.messages)
        self.assertEqual(self.finish(), [])
        self.assertEqual(len(self.memory.messages), size)
        self.assertIsNotNone(self.workflow.state.result)
        self.assertIsNone(self.workflow.poll())

    def test_same_topic_does_not_force_topic_switch(self):
        self.start()
        self.llm.override = lambda ctx: {**response(respond="明白"), "topic_relation": "same_task"}
        self.workflow.handle("我的意思是保持原范围")
        self.assertFalse(self.workflow.state.topic_switched)

    def test_close_discards_background_events(self):
        self.start()
        self.workflow.close()
        self.llm.release.set()
        self.assertIsNone(self.workflow.poll())
        self.assertEqual(self.workflow.state.status, "cancelled")
        fresh = Workflow(self.llm, Memory(), Config())
        try:
            self.assertIsNone(fresh.state)
            self.assertIsNone(fresh.poll())
        finally:
            fresh.close()


class SchemaTests(unittest.TestCase):
    def test_required_fields_and_action_whitelist(self):
        for value in [None, {"type": []}, {"type": "exec"}, {"type": "update_slow", "task": "full"},
                      {"type": "none", "task": "unexpected"}, {"type": "none", "target_task_id": "id"}]:
            with self.assertRaises(AgentError):
                Action.parse(value)

    def test_only_event_none_may_be_silent(self):
        with self.assertRaises(AgentError):
            FastResponse.parse(response(respond=None))
        self.assertIsNone(FastResponse.parse(response(respond=None), allow_silent=True).respond)
        with self.assertRaises(AgentError):
            FastResponse.parse(response("get_status", respond=None), allow_silent=True)


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
