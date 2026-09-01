from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace

import pytest


class Box:
    def __init__(self, *args, **kwargs):
        self.args = args
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeProviderRequest:
    def __init__(self):
        self.extra_user_content_parts = []
        self.func_tool = None
        self.system_prompt = ""
        self.prompt = None
        self.tool_calls_result = None
        self.conversation = None
        self.contexts = []


class FakeTool:
    def __init__(self, name: str):
        self.name = name


class FakeToolSet:
    def __init__(self, tools=None):
        self.tools = list(tools or [])

    def remove_tool(self, name: str) -> None:
        self.tools = [tool for tool in self.tools if tool.name != name]


class FakeMessageChain:
    def __init__(self):
        self.text = ""

    def message(self, text: str):
        self.text = text
        return self


class FakeEvent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeContext:
    conversation_manager = object()

    def __init__(self, *, send_result: bool):
        self.send_result = send_result
        self.sent_messages = []

    def get_config(self, *, umo):
        return {}

    async def send_message(self, session, message):
        self.sent_messages.append((session, message))
        return self.send_result


class FakeRunner:
    def __init__(self, final_response):
        self.final_response = final_response

    async def step_until_done(self, max_steps):
        assert max_steps == 30
        if False:
            yield None

    def get_final_llm_resp(self):
        return self.final_response


def _module(name: str, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@pytest.fixture
def reminder_module(monkeypatch):
    logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    modules = {
        "astrbot": _module("astrbot"),
        "astrbot.api": _module("astrbot.api", logger=logger),
        "astrbot.core": _module("astrbot.core"),
        "astrbot.core.agent": _module("astrbot.core.agent"),
        "astrbot.core.agent.message": _module(
            "astrbot.core.agent.message",
            AssistantMessageSegment=Box,
            TextPart=Box,
            ToolCall=type("ToolCall", (Box,), {"FunctionBody": Box}),
            ToolCallMessageSegment=Box,
        ),
        "astrbot.core.astr_main_agent": _module(
            "astrbot.core.astr_main_agent",
            MainAgentBuildConfig=Box,
            _get_session_conv=None,
            build_main_agent=None,
        ),
        "astrbot.core.cron": _module("astrbot.core.cron"),
        "astrbot.core.cron.events": _module("astrbot.core.cron.events", CronMessageEvent=FakeEvent),
        "astrbot.core.message": _module("astrbot.core.message"),
        "astrbot.core.message.message_event_result": _module(
            "astrbot.core.message.message_event_result", MessageChain=FakeMessageChain
        ),
        "astrbot.core.platform": _module("astrbot.core.platform"),
        "astrbot.core.platform.message_session": _module(
            "astrbot.core.platform.message_session",
            MessageSession=SimpleNamespace(
                from_str=lambda session: SimpleNamespace(
                    message_type="FriendMessage", session=session
                )
            ),
        ),
        "astrbot.core.provider": _module("astrbot.core.provider"),
        "astrbot.core.provider.entities": _module(
            "astrbot.core.provider.entities",
            ProviderRequest=FakeProviderRequest,
            ToolCallsResult=Box,
        ),
        "astrbot.core.utils": _module("astrbot.core.utils"),
        "astrbot.core.utils.config_number": _module(
            "astrbot.core.utils.config_number",
            coerce_int_config=lambda value, **kwargs: int(value),
        ),
        "astrbot.core.utils.history_saver": _module(
            "astrbot.core.utils.history_saver", persist_agent_history=None
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules.pop("reminder", None)
    module = importlib.import_module("reminder")

    async def get_conversation(**kwargs):
        return SimpleNamespace(history="[]")

    persisted = []

    async def persist(*args, **kwargs):
        persisted.append(kwargs)

    monkeypatch.setattr(module, "_get_session_conv", get_conversation)
    monkeypatch.setattr(module, "persist_agent_history", persist)
    module.persisted_calls = persisted
    return module


async def _run_reminder(
    reminder_module,
    monkeypatch,
    *,
    send_result: bool,
    final_response,
    mode: str = "user_message",
):
    captured = {}

    async def build_agent(*, event, plugin_context, config, req):
        req.func_tool = FakeToolSet(
            [FakeTool("send_message_to_user"), FakeTool("verify_alipay_bill")]
        )
        captured["request"] = req
        return SimpleNamespace(agent_runner=FakeRunner(final_response))

    monkeypatch.setattr(reminder_module, "build_main_agent", build_agent)
    context = FakeContext(send_result=send_result)
    delivered = await reminder_module.inject_payment_reminder(
        context,
        "platform:FriendMessage:user",
        "AIP20260901120000000000000000",
        mode,
    )
    return delivered, captured["request"], context


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["user_message", "fake_tool_call"])
async def test_plain_final_response_is_sent_directly(reminder_module, monkeypatch, mode) -> None:
    delivered, request, context = await _run_reminder(
        reminder_module,
        monkeypatch,
        send_result=True,
        final_response=SimpleNamespace(role="assistant", completion_text="用户已完成付款"),
        mode=mode,
    )

    assert delivered is True
    assert [tool.name for tool in request.func_tool.tools] == ["verify_alipay_bill"]
    assert "send_message_to_user" not in request.system_prompt
    assert context.sent_messages[0][1].text == "用户已完成付款"
    assert len(reminder_module.persisted_calls) == 1
    assert "用户已完成付款" in reminder_module.persisted_calls[0]["summary_note"]


@pytest.mark.asyncio
async def test_empty_final_response_is_retried_without_sending(
    reminder_module, monkeypatch
) -> None:
    delivered, _, context = await _run_reminder(
        reminder_module,
        monkeypatch,
        send_result=True,
        final_response=SimpleNamespace(role="assistant", completion_text="  "),
    )

    assert delivered is False
    assert context.sent_messages == []
    assert reminder_module.persisted_calls == []


@pytest.mark.asyncio
async def test_platform_send_failure_is_retried_without_persisting(
    reminder_module, monkeypatch
) -> None:
    delivered, _, context = await _run_reminder(
        reminder_module,
        monkeypatch,
        send_result=False,
        final_response=SimpleNamespace(role="assistant", completion_text="用户已完成付款"),
    )

    assert delivered is False
    assert context.sent_messages[0][1].text == "用户已完成付款"
    assert reminder_module.persisted_calls == []
