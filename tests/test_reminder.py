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


class FakeToolSet:
    def __init__(self):
        self.tools = []

    def add_tool(self, tool) -> None:
        self.tools.append(tool)


class FakeEvent:
    def __init__(self, **kwargs):
        self._has_send_oper = False
        self.kwargs = kwargs


class FakeContext:
    conversation_manager = object()

    def get_config(self, *, umo):
        return {}

    def get_llm_tool_manager(self):
        return SimpleNamespace(get_builtin_tool=lambda tool: tool)


class FakeRunner:
    def __init__(self, event, *, sent: bool, final_response):
        self.event = event
        self.sent = sent
        self.final_response = final_response

    async def step_until_done(self, max_steps):
        assert max_steps == 30
        self.event._has_send_oper = self.sent
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
        "astrbot.core.agent.tool": _module("astrbot.core.agent.tool", ToolSet=FakeToolSet),
        "astrbot.core.astr_main_agent": _module(
            "astrbot.core.astr_main_agent",
            MainAgentBuildConfig=Box,
            _get_session_conv=None,
            build_main_agent=None,
        ),
        "astrbot.core.cron": _module("astrbot.core.cron"),
        "astrbot.core.cron.events": _module("astrbot.core.cron.events", CronMessageEvent=FakeEvent),
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
        "astrbot.core.tools": _module("astrbot.core.tools"),
        "astrbot.core.tools.message_tools": _module(
            "astrbot.core.tools.message_tools", SendMessageToUserTool=Box
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

    async def persist(*args, **kwargs):
        return None

    monkeypatch.setattr(module, "_get_session_conv", get_conversation)
    monkeypatch.setattr(module, "persist_agent_history", persist)
    return module


async def _run_reminder(reminder_module, monkeypatch, *, sent: bool, final_response):
    captured = {}

    async def build_agent(*, event, plugin_context, config, req):
        captured["request"] = req
        return SimpleNamespace(
            agent_runner=FakeRunner(event, sent=sent, final_response=final_response)
        )

    monkeypatch.setattr(reminder_module, "build_main_agent", build_agent)
    delivered = await reminder_module.inject_payment_reminder(
        FakeContext(),
        "platform:FriendMessage:user",
        "AIP20260901120000000000000000",
        "user_message",
    )
    return delivered, captured["request"]


@pytest.mark.asyncio
async def test_successful_tool_send_is_reported_as_delivered(reminder_module, monkeypatch) -> None:
    delivered, request = await _run_reminder(
        reminder_module,
        monkeypatch,
        sent=True,
        final_response=SimpleNamespace(completion_text="已处理"),
    )

    assert delivered is True
    assert "必须调用 send_message_to_user" in request.system_prompt
    assert "session 参数留空" in request.system_prompt


@pytest.mark.asyncio
async def test_plain_agent_response_is_not_mistaken_for_delivery(
    reminder_module, monkeypatch
) -> None:
    delivered, _ = await _run_reminder(
        reminder_module,
        monkeypatch,
        sent=False,
        final_response=SimpleNamespace(completion_text="用户已完成付款"),
    )

    assert delivered is False


@pytest.mark.asyncio
async def test_failed_tool_send_is_not_mistaken_for_delivery(reminder_module, monkeypatch) -> None:
    delivered, _ = await _run_reminder(
        reminder_module,
        monkeypatch,
        sent=False,
        final_response=None,
    )

    assert delivered is False
