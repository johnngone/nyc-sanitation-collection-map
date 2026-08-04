import pytest

from scripts.build_pilot import normalize_days


def test_normalize_explicit_schedule() -> None:
    assert normalize_days("Mon, Thu") == ["MON", "THU"]


def test_normalize_rejects_frequency_code() -> None:
    with pytest.raises(ValueError):
        normalize_days("A")

