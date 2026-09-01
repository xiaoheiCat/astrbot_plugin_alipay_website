from __future__ import annotations

import json
import uuid

from astrbot.api import logger
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    TextPart,
    ToolCall,
    ToolCallMessageSegment,
)
from astrbot.core.astr_main_agent import MainAgentBuildConfig, _get_session_conv, build_main_agent
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.provider.entities import ProviderRequest, ToolCallsResult
from astrbot.core.utils.config_number import coerce_int_config
from astrbot.core.utils.history_saver import persist_agent_history


async def inject_payment_reminder(context, session_str: str, out_trade_no: str, mode: str) -> bool:
    reminder = (
        "<system_reminder>用户已完成付款，商户订单号："
        f"{out_trade_no}。请使用 verify_alipay_bill 复核支付状态。</system_reminder>"
    )
    session = MessageSession.from_str(session_str)
    event = CronMessageEvent(
        context=context,
        session=session,
        message="支付宝付款回调已通过验证。",
        message_type=session.message_type,
        extras={"alipay_out_trade_no": out_trade_no},
    )
    cfg = context.get_config(umo=session_str) or {}
    provider_settings = cfg.get("provider_settings", {}) or {}
    misc = cfg.get("agent_runner", {}).get("config", {}).get("misc", {})
    max_steps = coerce_int_config(
        misc.get("max_steps", 30),
        default=30,
        min_value=1,
        field_name="agent_runner.config.misc.max_steps",
    )
    build_config = MainAgentBuildConfig(
        tool_call_timeout=misc.get("tool_call_timeout", 120),
        streaming_response=False,
        provider_settings=provider_settings,
    )
    req = ProviderRequest()
    conversation = await _get_session_conv(event=event, plugin_context=context)
    req.conversation = conversation
    req.contexts = json.loads(conversation.history)
    req.system_prompt += (
        "这是经过支付服务端验证的一次性付款提醒。请复核订单状态，并直接生成一条面向用户的"
        "普通最终回复。你的最终回复会由插件发送到原会话。不要把 system_reminder 标签原样"
        "发送给用户。"
    )

    if mode == "user_message":
        req.extra_user_content_parts.append(TextPart(text=reminder))
    elif mode == "fake_tool_call":
        call_id = "alipay_callback_" + uuid.uuid4().hex
        req.prompt = "Payment callback received."
        req.tool_calls_result = ToolCallsResult(
            tool_calls_info=AssistantMessageSegment(
                content=None,
                tool_calls=[
                    ToolCall(
                        id=call_id,
                        function=ToolCall.FunctionBody(
                            name="verify_alipay_bill",
                            arguments=json.dumps(
                                {"out_trade_no": out_trade_no}, ensure_ascii=False
                            ),
                        ),
                    )
                ],
            ),
            tool_calls_result=[
                ToolCallMessageSegment(content=reminder, tool_call_id=call_id)
            ],
        )
    else:
        return False

    result = await build_main_agent(
        event=event, plugin_context=context, config=build_config, req=req
    )
    if not result:
        raise RuntimeError("当前会话没有可用的 LLM Provider")
    if req.func_tool:
        # 本次主动回复由插件统一发送，避免模型自行发送后与最终回复重复。
        req.func_tool.remove_tool("send_message_to_user")

    async for _ in result.agent_runner.step_until_done(max_steps):
        pass
    llm_response = result.agent_runner.get_final_llm_resp()
    if not llm_response or llm_response.role != "assistant":
        logger.warning(
            "支付宝付款提醒 Agent 未产生有效最终回复；订单将在维护任务中重试，订单号：%s",
            out_trade_no,
        )
        return False

    completion_text = (llm_response.completion_text or "").strip()
    if not completion_text:
        logger.warning(
            "支付宝付款提醒 Agent 的最终回复为空；订单将在维护任务中重试，订单号：%s",
            out_trade_no,
        )
        return False

    delivered = await context.send_message(session, MessageChain().message(completion_text))
    if not delivered:
        logger.warning(
            "支付宝付款提醒无法找到原会话平台；订单将在维护任务中重试，订单号：%s",
            out_trade_no,
        )
        return False

    try:
        await persist_agent_history(
            context.conversation_manager,
            event=event,
            req=req,
            summary_note=(
                f"[Alipay] verified payment callback for {out_trade_no}. "
                f"Final response sent to user: {completion_text}"
            ),
        )
    except Exception:
        logger.exception("保存支付宝付款提醒 Agent 历史失败，订单号：%s", out_trade_no)

    logger.info("支付宝付款提醒最终回复已发送到原会话，订单号：%s", out_trade_no)
    return True
