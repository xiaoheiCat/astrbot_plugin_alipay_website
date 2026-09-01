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
from astrbot.core.agent.tool import ToolSet
from astrbot.core.astr_main_agent import MainAgentBuildConfig, _get_session_conv, build_main_agent
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.provider.entities import ProviderRequest, ToolCallsResult
from astrbot.core.tools.message_tools import SendMessageToUserTool
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
    req.func_tool = ToolSet()
    req.func_tool.add_tool(
        context.get_llm_tool_manager().get_builtin_tool(SendMessageToUserTool)
    )
    req.system_prompt += (
        "这是经过支付服务端验证的一次性付款提醒。请复核订单状态。你必须调用 "
        "send_message_to_user 向当前会话发送付款完成提醒，并将 session 参数留空；仅输出普通"
        "文本不会送达用户。只有该工具明确返回发送成功才算完成；如果工具返回错误，请修正参数"
        "后重试。不要把 system_reminder 标签原样发送给用户。"
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
    try:
        async for _ in result.agent_runner.step_until_done(max_steps):
            pass
        llm_response = result.agent_runner.get_final_llm_resp()
    except Exception:
        if not event._has_send_oper:
            raise
        logger.exception(
            "支付宝付款提醒已实际发送，但 Agent 后续执行失败，订单号：%s",
            out_trade_no,
        )
        llm_response = None

    delivered = bool(event._has_send_oper)
    try:
        await persist_agent_history(
            context.conversation_manager,
            event=event,
            req=req,
            summary_note=f"[Alipay] verified payment callback for {out_trade_no}",
        )
    except Exception:
        logger.exception("保存支付宝付款提醒 Agent 历史失败，订单号：%s", out_trade_no)

    if delivered:
        logger.info("支付宝付款提醒已发送到原会话，订单号：%s", out_trade_no)
        return True
    if llm_response:
        logger.warning(
            "支付宝付款提醒 Agent 仅产生了普通回复，未调用 send_message_to_user；"
            "订单将在维护任务中重试，订单号：%s",
            out_trade_no,
        )
    else:
        logger.warning(
            "支付宝付款提醒 Agent 未产生响应且未发送消息；订单将在维护任务中重试，订单号：%s",
            out_trade_no,
        )
    return False
