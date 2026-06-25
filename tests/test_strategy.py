from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from strategy import LPFullRangeCurve2crvStrategy


@pytest.fixture
def config() -> dict:
    with Path("config.json").open() as fp:
        return json.load(fp)


@pytest.fixture
def strategy(config: dict) -> LPFullRangeCurve2crvStrategy:
    return LPFullRangeCurve2crvStrategy(
        config=config,
        chain="arbitrum",
        wallet_address="0x" + "1" * 40,
    )


def _mock_balance(balance: str, balance_usd: str) -> MagicMock:
    b = MagicMock()
    b.balance = Decimal(balance)
    b.balance_usd = Decimal(balance_usd)
    return b


def _mock_market(
    *,
    usdc_price: str = "1.0",
    usdt_price: str = "1.0",
    usdc_balance: str = "1000",
    usdt_balance: str = "1000",
    usdc_usd: str = "1000",
    usdt_usd: str = "1000",
    lp_balance: str = "0",
    lp_usd: str = "0",
    slippage_pct: str | None = "0.001",
) -> MagicMock:
    market = MagicMock()
    market.timestamp = datetime.now(UTC)

    prices = {
        "USDC.e": Decimal(usdc_price),
        "USDT": Decimal(usdt_price),
    }

    def price(token: str):
        return prices[token]

    market.price.side_effect = price

    def balance(token: str, *args, **kwargs):
        if token == "USDC.e":
            return _mock_balance(usdc_balance, usdc_usd)
        if token == "USDT":
            return _mock_balance(usdt_balance, usdt_usd)
        return _mock_balance(lp_balance, lp_usd)

    market.balance.side_effect = balance

    if slippage_pct is None:
        market.estimate_slippage.side_effect = ValueError("no slippage")
    else:
        envelope = MagicMock()
        envelope.data = MagicMock(slippage_pct=Decimal(slippage_pct))
        market.estimate_slippage.return_value = envelope

    return market


def test_open_lp_when_in_band_and_balances_available(strategy: LPFullRangeCurve2crvStrategy):
    market = _mock_market()
    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_OPEN"
    assert intent.protocol == "curve"
    assert intent.pool == "2pool"


def test_single_sided_open_is_allowed(strategy: LPFullRangeCurve2crvStrategy):
    market = _mock_market(usdc_balance="1000", usdc_usd="1000", usdt_balance="0", usdt_usd="0")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_OPEN"
    assert Decimal(str(intent.amount1)) == Decimal("0")


def test_hold_when_total_balance_below_min_deploy(strategy: LPFullRangeCurve2crvStrategy):
    market = _mock_market(usdc_balance="1", usdc_usd="2", usdt_balance="1", usdt_usd="2")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"


def test_hold_when_depeg_without_position(strategy: LPFullRangeCurve2crvStrategy):
    market = _mock_market(usdc_price="0.992")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"


def test_close_on_depeg_when_position_open(strategy: LPFullRangeCurve2crvStrategy):
    strategy._has_lp_position = True
    market = _mock_market(lp_balance="1", lp_usd="1000", usdc_price="0.992")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_CLOSE"


def test_close_on_unsafe_withdrawal_signal(strategy: LPFullRangeCurve2crvStrategy):
    strategy._has_lp_position = True
    market = _mock_market(lp_balance="1", lp_usd="1000", slippage_pct="0.02")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_CLOSE"


def test_reentry_lock_waits_for_band_and_slippage(strategy: LPFullRangeCurve2crvStrategy):
    strategy._awaiting_reentry_after_exit = True
    market = _mock_market(usdc_price="0.992")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"


def test_reentry_opens_when_gates_pass(strategy: LPFullRangeCurve2crvStrategy):
    strategy._awaiting_reentry_after_exit = True
    market = _mock_market()
    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_OPEN"


def test_hold_when_slippage_unavailable_and_fail_closed(strategy: LPFullRangeCurve2crvStrategy):
    market = _mock_market(slippage_pct=None)
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert "Deposit slippage unavailable" in intent.reason


def test_hold_reason_includes_deposit_slippage_value(strategy: LPFullRangeCurve2crvStrategy):
    market = _mock_market(slippage_pct="0.02")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert "Deposit slippage" in intent.reason
    assert "max" in intent.reason


def test_force_open_returns_lp_open(strategy: LPFullRangeCurve2crvStrategy):
    strategy.force_action = "open"
    market = _mock_market()
    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_OPEN"


def test_force_close_returns_lp_close(strategy: LPFullRangeCurve2crvStrategy):
    strategy.force_action = "close"
    market = _mock_market()
    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_CLOSE"


def test_claim_capability_gap_holds_safely(strategy: LPFullRangeCurve2crvStrategy):
    strategy._has_lp_position = True
    market = _mock_market(lp_balance="1", lp_usd="1000")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert "Rewards claim not supported" in intent.reason
    assert strategy.get_status()["rewards_claim_supported"] is False


def test_teardown_methods_return_curve_close(strategy: LPFullRangeCurve2crvStrategy):
    strategy._has_lp_position = True
    strategy._lp_token_balance = Decimal("1")

    summary = strategy.get_open_positions()
    intents = strategy.generate_teardown_intents(mode=MagicMock(), market=None)

    assert len(summary.positions) == 1
    assert summary.positions[0].position_id == strategy.lp_token_address
    assert len(intents) == 1
    assert intents[0].intent_type.value == "LP_CLOSE"


def test_on_intent_executed_updates_state(strategy: LPFullRangeCurve2crvStrategy):
    open_intent = MagicMock()
    open_intent.intent_type.value = "LP_OPEN"

    strategy.on_intent_executed(open_intent, success=True, result=MagicMock())
    assert strategy._has_lp_position is True

    strategy._total_deposited_value = Decimal("100")
    strategy._current_position_value = Decimal("110")

    close_intent = MagicMock()
    close_intent.intent_type.value = "LP_CLOSE"
    strategy.on_intent_executed(close_intent, success=True, result=MagicMock())

    assert strategy._has_lp_position is False
    assert strategy._awaiting_reentry_after_exit is True
    assert strategy._realized_pnl_after_exit == Decimal("10")


def test_persistence_roundtrip(strategy: LPFullRangeCurve2crvStrategy, config: dict):
    strategy._has_lp_position = True
    strategy._last_action_ts = datetime.now(UTC) - timedelta(hours=1)
    strategy._claim_timestamps = [datetime.now(UTC).isoformat()]
    payload = strategy.get_persistent_state()

    fresh = LPFullRangeCurve2crvStrategy(
        config=config,
        chain="arbitrum",
        wallet_address="0x" + "1" * 40,
    )
    fresh.load_persistent_state(payload)

    assert fresh.get_persistent_state()["has_lp_position"] is True
    assert len(fresh.get_persistent_state()["claim_timestamps"]) == 1
