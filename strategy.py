from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from almanak.framework.intents import Intent
from almanak.framework.market import MarketSnapshot
from almanak.framework.market.errors import (
    BalanceUnavailableError,
    MarketSnapshotError,
    PriceUnavailableError,
    SlippageEstimateUnavailableError,
)
from almanak.framework.strategies import IntentStrategy, almanak_strategy

logger = logging.getLogger(__name__)


@almanak_strategy(
    name="l_p_full_range_curve_2crv",
    description="Passive Curve 2pool LP with depeg safety exits",
    version="1.0.0",
    author="Almanak",
    tags=["curve", "lp", "stablecoin", "passive"],
    supported_chains=["arbitrum"],
    supported_protocols=["curve"],
    intent_types=["LP_OPEN", "LP_CLOSE", "HOLD"],
    default_chain="arbitrum",
    quote_asset="USD",
)
class LPFullRangeCurve2crvStrategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def cfg(key: str, default: Any) -> Any:
            if isinstance(self.config, dict):
                return self.config.get(key, default)
            return getattr(self.config, key, default)

        self.target_chain = str(cfg("chain", "arbitrum"))
        self.protocol = str(cfg("protocol", "curve"))
        self.pool = str(cfg("pool", "2pool"))
        self.lp_token_address = str(cfg("lp_token_address", "0x7f90122bf0700f9e7e1f688fe926940e8839f353"))

        self.asset_0 = str(cfg("asset_0", "USDC.e"))
        self.asset_1 = str(cfg("asset_1", "USDT"))

        self.deploy_fraction_of_available = Decimal(str(cfg("deploy_fraction_of_available", "0.995")))
        self.min_deploy_usd = Decimal(str(cfg("min_deploy_usd", "25")))
        self.action_cooldown_seconds = int(cfg("action_cooldown_seconds", 3600))

        self.peg_band_lower = Decimal(str(cfg("peg_band_lower", "0.995")))
        self.peg_band_upper = Decimal(str(cfg("peg_band_upper", "1.005")))
        self.exit_on_material_depeg = bool(cfg("exit_on_material_depeg", True))
        self.exit_on_withdraw_impossible_or_unsafe = bool(cfg("exit_on_withdraw_impossible_or_unsafe", True))

        self.reopen_after_exit = bool(cfg("reopen_after_exit", False))
        self.require_slippage_for_reentry = bool(cfg("require_slippage_for_reentry", True))
        self.max_slippage_pct = Decimal(str(cfg("max_slippage_pct", "0.003")))
        self.slippage_probe_amount = Decimal(str(cfg("slippage_probe_amount", "5000")))
        self.fail_closed_on_slippage_unavailable = bool(cfg("fail_closed_on_slippage_unavailable", True))

        self.claim_enabled = bool(cfg("claim_enabled", True))
        self.claim_threshold_usd = Decimal(str(cfg("claim_threshold_usd", "5")))
        self.auto_sell_claimed_rewards = bool(cfg("auto_sell_claimed_rewards", False))
        self.auto_compound_claimed_rewards = bool(cfg("auto_compound_claimed_rewards", False))

        self.force_action = str(cfg("force_action", "") or "").strip().lower()

        self._has_lp_position = False
        self._awaiting_reentry_after_exit = False
        self._last_action_ts: datetime | None = None
        self._last_exit_reason = ""

        self._lp_token_balance = Decimal("0")
        self._total_deposited_value = Decimal("0")
        self._current_position_value = Decimal("0")
        self._fees_earned = Decimal("0")
        self._rewards_earned = Decimal("0")
        self._rewards_claimed = Decimal("0")
        self._claim_timestamps: list[str] = []
        self._realized_pnl_after_exit = Decimal("0")

        self._rewards_claim_supported = False

    def decide(self, market: MarketSnapshot) -> Intent | None:
        if self.force_action:
            return self._forced_intent(market)

        if self._in_action_cooldown(market.timestamp):
            return Intent.hold(reason="In action cooldown")

        try:
            price_0 = Decimal(str(market.price(self.asset_0)))
            price_1 = Decimal(str(market.price(self.asset_1)))
            self._lp_token_balance = self._read_lp_balance(market)
            self._has_lp_position = self._lp_token_balance > Decimal("0")
            self._current_position_value = self._read_lp_value_usd(market)
        except (PriceUnavailableError, BalanceUnavailableError, MarketSnapshotError, ValueError) as exc:
            return Intent.hold(reason=f"Market data unavailable: {exc}")

        in_band_0 = self.peg_band_lower <= price_0 <= self.peg_band_upper
        in_band_1 = self.peg_band_lower <= price_1 <= self.peg_band_upper
        both_in_band = in_band_0 and in_band_1

        if self._has_lp_position:
            if self.exit_on_material_depeg and not both_in_band:
                self._last_exit_reason = "depeg"
                return self._build_lp_close_intent()

            if self.exit_on_withdraw_impossible_or_unsafe and not self._slippage_gate_ok(market):
                self._last_exit_reason = "unsafe_withdrawal"
                return self._build_lp_close_intent()

            if self.claim_enabled and self.claim_threshold_usd > Decimal("0"):
                return Intent.hold(reason="Rewards claim not supported on Curve LP intents")

            return Intent.hold(reason="Passive LP active")

        if self._awaiting_reentry_after_exit and not self.reopen_after_exit:
            if not both_in_band:
                return Intent.hold(reason="Waiting re-entry: stables not back in peg band")
            if self.require_slippage_for_reentry and not self._slippage_gate_ok(market):
                return Intent.hold(reason="Waiting re-entry: slippage unacceptable")

        if not both_in_band:
            return Intent.hold(reason="No LP position: peg outside safe band")

        if self.require_slippage_for_reentry and not self._slippage_gate_ok(market):
            return Intent.hold(reason="Deposit slippage unacceptable")

        try:
            bal_0 = market.balance(self.asset_0, price=price_0)
            bal_1 = market.balance(self.asset_1, price=price_1)
        except (BalanceUnavailableError, MarketSnapshotError, ValueError) as exc:
            return Intent.hold(reason=f"Balance data unavailable: {exc}")

        amount0 = Decimal(str(bal_0.balance)) * self.deploy_fraction_of_available
        amount1 = Decimal(str(bal_1.balance)) * self.deploy_fraction_of_available
        total_usd = Decimal(str(bal_0.balance_usd)) + Decimal(str(bal_1.balance_usd))

        if total_usd < self.min_deploy_usd:
            return Intent.hold(reason="Insufficient stablecoin balance for LP open")

        if amount0 <= Decimal("0") and amount1 <= Decimal("0"):
            return Intent.hold(reason="No deployable stablecoin balance")

        self._total_deposited_value = total_usd * self.deploy_fraction_of_available
        return self._build_lp_open_intent(amount0=amount0, amount1=amount1)

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        if self.force_action == "open":
            try:
                p0 = Decimal(str(market.price(self.asset_0)))
                p1 = Decimal(str(market.price(self.asset_1)))
                b0 = market.balance(self.asset_0, price=p0)
                b1 = market.balance(self.asset_1, price=p1)
            except (PriceUnavailableError, BalanceUnavailableError, MarketSnapshotError, ValueError) as exc:
                return Intent.hold(reason=f"Force open blocked by unavailable data: {exc}")

            amount0 = Decimal(str(b0.balance)) * self.deploy_fraction_of_available
            amount1 = Decimal(str(b1.balance)) * self.deploy_fraction_of_available
            if amount0 <= Decimal("0") and amount1 <= Decimal("0"):
                return Intent.hold(reason="Force open blocked: no stablecoin balance")

            self._total_deposited_value = (
                Decimal(str(b0.balance_usd)) + Decimal(str(b1.balance_usd))
            ) * self.deploy_fraction_of_available
            return self._build_lp_open_intent(amount0=amount0, amount1=amount1)

        if self.force_action == "close":
            return self._build_lp_close_intent()

        return Intent.hold(reason=f"Unsupported force_action: {self.force_action}")

    def _build_lp_open_intent(self, amount0: Decimal, amount1: Decimal) -> Intent:
        return Intent.lp_open(
            pool=self.pool,
            amount0=amount0,
            amount1=amount1,
            protocol=self.protocol,
            chain=self.target_chain,
        )

    def _build_lp_close_intent(self) -> Intent:
        return Intent.lp_close(
            position_id=self.lp_token_address,
            pool=self.pool,
            collect_fees=True,
            protocol=self.protocol,
            chain=self.target_chain,
        )

    def _in_action_cooldown(self, now: datetime) -> bool:
        if self._last_action_ts is None:
            return False
        return (now - self._last_action_ts).total_seconds() < self.action_cooldown_seconds

    def _slippage_gate_ok(self, market: MarketSnapshot) -> bool:
        try:
            envelope = market.estimate_slippage(
                token_in=self.asset_0,
                token_out=self.asset_1,
                amount=self.slippage_probe_amount,
                chain=self.target_chain,
                protocol=self.protocol,
            )
        except SlippageEstimateUnavailableError:
            return not self.fail_closed_on_slippage_unavailable
        except (MarketSnapshotError, ValueError):
            return not self.fail_closed_on_slippage_unavailable

        estimate = getattr(envelope, "data", envelope)
        slip = self._extract_slippage_pct(estimate)
        if slip is None:
            return not self.fail_closed_on_slippage_unavailable
        return slip <= self.max_slippage_pct

    def _extract_slippage_pct(self, estimate: Any) -> Decimal | None:
        for attr in ("slippage_pct", "price_impact_pct"):
            value = getattr(estimate, attr, None)
            if value is not None:
                return abs(Decimal(str(value)))

        value_bps = getattr(estimate, "price_impact_bps", None)
        if value_bps is not None:
            return abs(Decimal(str(value_bps))) / Decimal("10000")

        return None

    def _read_lp_balance(self, market: MarketSnapshot) -> Decimal:
        bal = market.balance(self.lp_token_address, chain=self.target_chain)
        return Decimal(str(bal.balance))

    def _read_lp_value_usd(self, market: MarketSnapshot) -> Decimal:
        bal = market.balance(self.lp_token_address, chain=self.target_chain)
        return Decimal(str(bal.balance_usd))

    def on_intent_executed(self, intent, success: bool, result) -> None:
        if not success:
            return

        intent_type = getattr(intent.intent_type, "value", str(intent.intent_type))
        self._last_action_ts = datetime.now(UTC)

        if intent_type == "LP_OPEN":
            self._has_lp_position = True
            self._awaiting_reentry_after_exit = False

        if intent_type == "LP_CLOSE":
            self._has_lp_position = False
            self._awaiting_reentry_after_exit = not self.reopen_after_exit
            self._realized_pnl_after_exit += self._current_position_value - self._total_deposited_value
            self._current_position_value = Decimal("0")

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "l_p_full_range_curve_2crv",
            "chain": self.target_chain,
            "protocol": self.protocol,
            "pool": self.pool,
            "assets": [self.asset_0, self.asset_1],
            "has_lp_position": self._has_lp_position,
            "awaiting_reentry_after_exit": self._awaiting_reentry_after_exit,
            "last_exit_reason": self._last_exit_reason,
            "rewards_claim_supported": self._rewards_claim_supported,
            "claim_enabled": self.claim_enabled,
            "claim_threshold_usd": str(self.claim_threshold_usd),
            "accounting": {
                "lp_token_balance": str(self._lp_token_balance),
                "total_deposited_value": str(self._total_deposited_value),
                "current_position_value": str(self._current_position_value),
                "fees_earned": str(self._fees_earned),
                "rewards_earned": str(self._rewards_earned),
                "rewards_claimed": str(self._rewards_claimed),
                "claim_timestamps": list(self._claim_timestamps),
                "realized_pnl_after_exit": str(self._realized_pnl_after_exit),
            },
        }

    def get_persistent_state(self) -> dict[str, Any]:
        return {
            "has_lp_position": self._has_lp_position,
            "awaiting_reentry_after_exit": self._awaiting_reentry_after_exit,
            "last_action_ts": self._last_action_ts.isoformat() if self._last_action_ts else None,
            "last_exit_reason": self._last_exit_reason,
            "lp_token_balance": str(self._lp_token_balance),
            "total_deposited_value": str(self._total_deposited_value),
            "current_position_value": str(self._current_position_value),
            "fees_earned": str(self._fees_earned),
            "rewards_earned": str(self._rewards_earned),
            "rewards_claimed": str(self._rewards_claimed),
            "claim_timestamps": list(self._claim_timestamps),
            "realized_pnl_after_exit": str(self._realized_pnl_after_exit),
            "rewards_claim_supported": self._rewards_claim_supported,
        }

    def load_persistent_state(self, state: dict[str, Any]) -> None:
        if not state:
            return

        self._has_lp_position = bool(state.get("has_lp_position", False))
        self._awaiting_reentry_after_exit = bool(state.get("awaiting_reentry_after_exit", False))
        ts = state.get("last_action_ts")
        self._last_action_ts = datetime.fromisoformat(ts) if ts else None
        self._last_exit_reason = str(state.get("last_exit_reason", ""))
        self._lp_token_balance = Decimal(str(state.get("lp_token_balance", "0")))
        self._total_deposited_value = Decimal(str(state.get("total_deposited_value", "0")))
        self._current_position_value = Decimal(str(state.get("current_position_value", "0")))
        self._fees_earned = Decimal(str(state.get("fees_earned", "0")))
        self._rewards_earned = Decimal(str(state.get("rewards_earned", "0")))
        self._rewards_claimed = Decimal(str(state.get("rewards_claimed", "0")))
        self._claim_timestamps = [str(v) for v in state.get("claim_timestamps", [])]
        self._realized_pnl_after_exit = Decimal(str(state.get("realized_pnl_after_exit", "0")))
        self._rewards_claim_supported = bool(state.get("rewards_claim_supported", False))

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions: list[PositionInfo] = []
        if self._has_lp_position or self._lp_token_balance > Decimal("0"):
            positions.append(
                PositionInfo(
                    position_type=PositionType.LP,
                    position_id=self.lp_token_address,
                    chain=self.target_chain,
                    protocol=self.protocol,
                    value_usd=self._current_position_value,
                    details={
                        "pool": self.pool,
                        "lp_token_address": self.lp_token_address,
                    },
                )
            )

        return TeardownPositionSummary(
            deployment_id=getattr(self, "deployment_id", "l_p_full_range_curve_2crv"),
            timestamp=datetime.now(UTC),
            positions=positions,
        )

    def generate_teardown_intents(self, mode, market=None) -> list[Intent]:
        if not (self._has_lp_position or self._lp_token_balance > Decimal("0")):
            return []
        return [self._build_lp_close_intent()]
