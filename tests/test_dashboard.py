from __future__ import annotations

import sys
import types
from unittest.mock import patch

sys.modules.setdefault("streamlit", types.SimpleNamespace(title=lambda *args, **kwargs: None))

from dashboard import ui


def test_dashboard_module_imports() -> None:
    assert callable(ui.render_custom_dashboard)


@patch("dashboard.ui.render_lp_dashboard")
@patch("dashboard.ui.prepare_lp_session_state")
@patch("dashboard.ui.st.title")
def test_render_custom_dashboard_builds_lp_config(
    title_mock,
    prepare_mock,
    render_mock,
) -> None:
    prepare_mock.return_value = {"prepared": True}
    strategy_config = {
        "protocol": "curve",
        "chain": "arbitrum",
        "display_assets": ["USDC", "USDT"],
        "token_funding": [
            {"symbol": "USDC", "address": "0x111"},
            {"symbol": "USDT", "address": "0x222"},
        ],
    }

    ui.render_custom_dashboard("dep-1", strategy_config, object(), {"raw": True})

    title_mock.assert_called_once_with("LP Full Range Curve 2crv")
    assert prepare_mock.call_count == 1
    config = prepare_mock.call_args.kwargs["config"]
    assert config.protocol == "curve"
    assert config.chain == "arbitrum"
    assert config.token0 == "USDC"
    assert config.token1 == "USDT"
    assert config.fee_tier == "N/A"
    assert config.token0_address == "0x111"
    assert config.token1_address == "0x222"

    render_mock.assert_called_once()
    assert render_mock.call_args.args[0] == "dep-1"
    assert render_mock.call_args.args[1] == strategy_config
    assert render_mock.call_args.args[2] == {"prepared": True}


@patch("dashboard.ui.render_lp_dashboard")
@patch("dashboard.ui.prepare_lp_session_state")
@patch("dashboard.ui.st.title")
def test_render_custom_dashboard_falls_back_to_asset_tokens(
    _title_mock,
    prepare_mock,
    _render_mock,
) -> None:
    prepare_mock.return_value = {}
    strategy_config = {
        "protocol": "curve",
        "chain": "arbitrum",
        "asset_0": "USDC.e",
        "asset_1": "USDT",
        "token_funding": [
            {"symbol": "USDC.e", "address": "0xaaa"},
            {"symbol": "USDT", "address": "0xbbb"},
        ],
    }

    ui.render_custom_dashboard("dep-2", strategy_config, object(), {})

    config = prepare_mock.call_args.kwargs["config"]
    assert config.token0 == "USDC.e"
    assert config.token1 == "USDT"
    assert config.token0_address == "0xaaa"
    assert config.token1_address == "0xbbb"
