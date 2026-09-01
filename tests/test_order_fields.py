from __future__ import annotations

import pytest

from order_fields import validate_subject


@pytest.mark.parametrize(
    "value",
    ["", "   ", "超过十个字符的转账备注内容", "午餐\n费用", "午餐\t费用"],
)
def test_subject_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="subject"):
        validate_subject(value)


def test_subject_is_trimmed_and_accepts_ten_characters() -> None:
    assert validate_subject("  1234567890  ") == "1234567890"


def test_subject_rejects_non_string_value() -> None:
    with pytest.raises(ValueError, match="subject"):
        validate_subject(None)  # type: ignore[arg-type]
