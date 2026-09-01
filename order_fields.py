from __future__ import annotations


def validate_subject(value: str) -> str:
    """规范化 AI 提供的支付宝订单标题。"""
    if not isinstance(value, str):
        raise ValueError("subject 必须是字符串")
    subject = value.strip()
    if not subject or len(subject) > 10:
        raise ValueError("subject 必须是 1 到 10 个字符")
    if any(ord(character) < 32 or ord(character) == 127 for character in subject):
        raise ValueError("subject 不能包含换行或其他控制字符")
    return subject
