"""Dashboard UI for LP Full Range Curve 2crv."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    import streamlit as st
except ModuleNotFoundError:
    class _StreamlitFallback:
        @staticmethod
        def title(*_args: Any, **_kwargs: Any) -> None:
            return None

    st = _StreamlitFallback()

try:
    from almanak.framework.dashboard.templates import (
        LPDashboardConfig,
        prepare_lp_session_state,
        render_lp_dashboard,
    )
except ModuleNotFoundError:
    @dataclass
    class LPDashboardConfig:
        protocol: str = "curve"
        token0: str = "USDC.e"
        token1: str = "USDT"
        fee_tier: str = "N/A"
        chain: str = "arbitrum"
        token0_address: str | None = None
        token1_address: str | None = None

    def prepare_lp_session_state(api_client: Any, session_state: dict[str, Any], config: Any, deployment_id: str):
        return session_state

    def render_lp_dashboard(
        deployment_id: str,
        strategy_config: dict[str, Any],
        session_state: dict[str, Any],
        config: Any,
        api_client: Any = None,
    ) -> None:
        return None


def _pick_tokens(strategy_config: dict[str, Any]) -> tuple[str, str]:
    display_assets = strategy_config.get("display_assets")
    if isinstance(display_assets, list) and len(display_assets) >= 2:
        return str(display_assets[0]), str(display_assets[1])

    token0 = strategy_config.get("asset_0") or "USDC.e"
    token1 = strategy_config.get("asset_1") or "USDT"
    return str(token0), str(token1)


def _resolve_token_address(strategy_config: dict[str, Any], symbol: str) -> str | None:
    token_funding = strategy_config.get("token_funding", [])
    if not isinstance(token_funding, list):
        return None

    normalized_symbol = symbol.upper()
    for token in token_funding:
        if not isinstance(token, dict):
            continue
        candidate = str(token.get("symbol", "")).upper()
        if candidate == normalized_symbol:
            address = token.get("address")
            return str(address) if address else None
    return None


def render_custom_dashboard(
    deployment_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    st.title("LP Full Range Curve 2crv")

    token0, token1 = _pick_tokens(strategy_config)
    config = LPDashboardConfig(
        protocol=str(strategy_config.get("protocol", "curve")),
        token0=token0,
        token1=token1,
        fee_tier="N/A",
        chain=str(strategy_config.get("chain", "arbitrum")),
        token0_address=_resolve_token_address(strategy_config, token0),
        token1_address=_resolve_token_address(strategy_config, token1),
    )

    prepared_state = prepare_lp_session_state(
        api_client,
        session_state=session_state,
        config=config,
        deployment_id=deployment_id,
    )

    render_lp_dashboard(
        deployment_id,
        strategy_config,
        prepared_state,
        config,
        api_client=api_client,
    )
