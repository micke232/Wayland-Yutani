from decimal import Decimal

import pytest

from wayland.broker.ibkr import midpoint


@pytest.mark.parametrize("bid,ask", [(-1, 10), (0, 10), (11, 10), (float("nan"), 10), (1, float("inf"))])
def test_unusable_underlying_or_fx_quote_is_rejected(bid, ask):
    with pytest.raises(ValueError):
        midpoint(bid, ask)


def test_valid_midpoint():
    assert midpoint(10, 12) == Decimal(11)
