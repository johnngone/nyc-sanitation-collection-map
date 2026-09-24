import pytest

from scripts.build_pilot import normalize_days


def test_normalize_explicit_schedule() -> None:
    assert normalize_days("Mon, Thu") == ["MON", "THU"]


def test_normalize_rejects_frequency_code() -> None:
    with pytest.raises(ValueError):
        normalize_days("A")


@pytest.mark.parametrize("value", ["Mon, Funday", "Mondaygarbage", "Mon,"])
def test_normalize_rejects_every_unknown_or_empty_token(value: str) -> None:
    with pytest.raises(ValueError, match="token"):
        normalize_days(value)


def test_normalize_accepts_full_names_without_truncating_them() -> None:
    assert normalize_days("Thursday / Monday") == ["MON", "THU"]

