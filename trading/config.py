"""Central configuration for the trading system."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# --- AsterDex (perpetual futures exchange) ---
ASTER_USER_ADDRESS = os.getenv("ASTER_USER_ADDRESS", "")
ASTER_SIGNER_ADDRESS = os.getenv("ASTER_SIGNER_ADDRESS", "")
ASTER_PRIVATE_KEY = os.getenv("ASTER_PRIVATE_KEY", "")
ASTER_API_KEY = os.getenv("ASTER_API_KEY", "")
ASTER_API_SECRET = os.getenv("ASTER_API_SECRET", "")
ASTER_FUTURES_BASE = "https://fapi.asterdex.com"
ASTER_SPOT_BASE = "https://sapi.asterdex.com"
ASTER_WS_BASE = "wss://fstream.asterdex.com"

# --- FRED ---
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# --- LLM (AI Co-Pilot) ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")             # Groq free tier (primary, $0/mo)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")         # Gemini Flash (fallback)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")   # Claude (unused, legacy)

# --- Trading Mode ---
TRADING_MODE = os.getenv("TRADING_MODE", "paper")  # "paper" or "live"

# --- Security ---
DASHBOARD_PIN = os.getenv("DASHBOARD_PIN", None)   # Required to unlock dashboard if set

# --- Timezone ---
# Display timezone for dashboard and logs (internal storage stays UTC)
DISPLAY_TIMEZONE = os.getenv("DISPLAY_TIMEZONE", "Africa/Lagos")  # WAT (UTC+1)

# --- Initial Capital ---
# In paper mode, default to PAPER_BALANCE; in live mode, use funded amount
PAPER_BALANCE = float(os.getenv("PAPER_BALANCE", "1000"))
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", str(PAPER_BALANCE) if TRADING_MODE == "paper" else "1000"))

# --- Paths ---
# Use /data on Render (persistent disk) or local path for development
_DATA_DIR = Path(os.getenv("DATA_DIR", ""))
if _DATA_DIR and _DATA_DIR.exists():
    DB_PATH = _DATA_DIR / "trading.db"
    # Load persisted mode override (set via dashboard toggle)
    _mode_file = _DATA_DIR / ".env.mode"
    if _mode_file.exists():
        for line in _mode_file.read_text().strip().splitlines():
            if line.startswith("TRADING_MODE="):
                TRADING_MODE = line.split("=", 1)[1].strip()
else:
    DB_PATH = PROJECT_ROOT / "trading" / "db" / "trading.db"
KNOWLEDGE_DIR = PROJECT_ROOT / "trading" / "knowledge"
JOURNALS_DIR = KNOWLEDGE_DIR / "journals"
REVIEWS_DIR = KNOWLEDGE_DIR / "reviews"
STRATEGIES_DIR = KNOWLEDGE_DIR / "strategies"

# --- Risk Parameters ---
# NOTE: Tightened 2026-04-13 after -36.7% drawdown analysis.
# Root cause: single-strategy signals opened 6 simultaneous altcoin shorts
# at 5-11% each during a violent rally. Fixes applied:
#   - max_position_pct: 0.25 → 0.08   (max 8% per trade)
#   - max_drawdown_pct: 0.20 → 0.12   (halt earlier at 12%)
#   - max_daily_loss_pct: 0.05 → 0.03 (halt day at 3%)
#   - stop_loss_pct: 0.05 → 0.03      (tighter per-trade stop)
#   - max_open_positions: 6 (NEW)     (hard cap on open trades)
#   - max_same_strategy_positions: 3 (NEW) (prevent pile-on)
RISK = {
    "risk_per_trade_pct": 0.01,            # Risk 1% of portfolio per trade
    "max_position_pct": 0.08,             # Max 8% of portfolio per position (was 25%)
    "stop_loss_pct": 0.03,                # 3% stop loss (was 5%)
    "max_daily_loss_pct": 0.03,           # 3% max daily loss (was 5%)
    "max_drawdown_pct": 0.12,             # Halt at 12% drawdown (was 20%)
    "min_cash_reserve_pct": 0.10,         # Keep 10% cash minimum (was 5%)
    "max_trades_per_day": 15,             # Max 15 trades/day (was 25)
    "max_open_positions": 6,              # [NEW] Hard cap on simultaneous open positions
    "max_same_strategy_positions": 3,     # [NEW] No strategy can drive > 3 open trades
    "min_volume_ratio": 0.30,             # Block entries when volume < 30% of 7d average
    "volume_exit_ratio": 0.20,            # Exit positions when volume < 20% of 7d average
    "max_spread_bps": 50,                 # Block entries when bid-ask spread > 50 bps
    "max_market_impact_pct": 0.01,        # Block entries when order > 1% of recent 4h volume
}

# --- Short Selling ---
ALLOW_SHORT_SELLING = os.getenv("ALLOW_SHORT_SELLING", "true").lower() == "true"
SHORT_ALLOWED_STRATEGIES = {"cross_basis_rv", "multi_factor_rank", "pairs_trading"}

# --- Default Coins (used by data layer for multi-coin fetches) ---
# Top assets by liquidity — strategies can use wider subsets from ASTER_SYMBOLS
DEFAULT_COINS = [
    "bitcoin", "ethereum", "solana", "bnb", "xrp",
    "avalanche-2", "polkadot", "chainlink", "uniswap", "aave",
    "litecoin", "bitcoin-cash", "dogecoin", "sui", "aptos",
    "near", "injective", "arbitrum", "optimism", "toncoin",
]

# --- Symbol Mappings ---
# Maps CoinGecko IDs to AsterDex symbols (Perps)
ASTER_SYMBOLS = {
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "solana": "SOLUSDT",
    "bnb": "BNBUSDT",
    "xrp": "XRPUSDT",
    "avalanche-2": "AVAXUSDT",
    "polkadot": "DOTUSDT",
    "chainlink": "LINKUSDT",
    "uniswap": "UNIUSDT",
    "aave": "AAVEUSDT",
    "litecoin": "LTCUSDT",
    "bitcoin-cash": "BCHUSDT",
    "dogecoin": "DOGEUSDT",
    "sui": "SUIUSDT",
    "aptos": "APTUSDT",
    "near": "NEARUSDT",
    "injective": "INJUSDT",
    "arbitrum": "ARBUSDT",
    "optimism": "OPUSDT",
    "toncoin": "TONUSDT",
    "gold": "GOLDUSDT",
}

# Maps CoinGecko IDs to Alpaca symbols (Legacy/Compatibility)
CRYPTO_SYMBOLS = {
    "bitcoin": "BTC/USD",
    "ethereum": "ETH/USD",
    "solana": "SOL/USD",
    "bnb": "BNB/USD",
    "xrp": "XRP/USD",
    "avalanche-2": "AVAX/USD",
    "polkadot": "DOT/USD",
    "chainlink": "LINK/USD",
    "uniswap": "UNI/USD",
    "aave": "AAVE/USD",
    "litecoin": "LTC/USD",
    "bitcoin-cash": "BCH/USD",
    "dogecoin": "DOGE/USD",
    "sui": "SUI/USD",
    "aptos": "APT/USD",
    "near": "NEAR/USD",
    "injective": "INJ/USD",
    "arbitrum": "ARB/USD",
    "optimism": "OP/USD",
    "toncoin": "TON/USD",
    "gold": "GLD",
}

# --- Strategy Parameters ---
# Only strategies with positive 2-year backtest returns are kept.
# Deleted: momentum (-39%), mean_reversion (-13%), fg_multi_timeframe (-14%),
#          ema_crossover (-13%), bollinger_squeeze (-14%), btc_eth_ratio (-20%),
#          gold_btc (-3%), tips_yield (0 trades)

RSI_DIVERGENCE = {
    "rsi_period": 14,
    "ohlc_days": 30,                # 30 days → 4-hour candles (~180 data points)
    "divergence_lookback": 14,      # Candles to scan for divergence
    "min_rsi_oversold": 30,         # RSI below this + bullish divergence → buy
    "min_rsi_overbought": 70,       # RSI above this + bearish divergence → sell
    "coins": ["bitcoin", "ethereum", "solana"],
}

DXY_DOLLAR = {
    "dxy_ticker": "DX-Y.NYB",      # Dollar Index via yfinance
    "sma_fast": 20,
    "sma_slow": 50,
    "gold_symbol": "UGL",           # ProShares Ultra Gold (2x leveraged)
    "silver_symbol": "AGQ",         # ProShares Ultra Silver (2x leveraged)
}

# --- Strategy Enable/Disable ---
STRATEGY_ENABLED = {
    # Core crypto strategies (proven)
    "rsi_divergence": True,
    "hmm_regime": True,
    "pairs_trading": True,
    "kalman_trend": True,
    "regime_mean_reversion": True,
    "factor_crypto": True,
    # Perps-specific strategies (AsterDex alpha)
    "funding_arb": True,
    "microstructure_composite": True,
    "basis_zscore": True,
    "funding_term_structure": True,
    "taker_divergence": True,
    "cross_basis_rv": True,
    "oi_price_divergence": True,
    "whale_flow": True,
    # Cross-asset strategies (stocks/commodities/indices on AsterDex)
    "cross_asset_momentum": True,
    "gold_crypto_hedge": True,
    "equity_crypto_correlation": True,
    # Advanced / Experimental
    "multi_factor_rank": True,
    "volatility_regime": True,
    "meme_momentum": True,
    # On-chain & funding
    "onchain_flow": True,
    "funding_forecast": True,
    # News & sentiment
    "news_sentiment": True,
    # Disabled
    "dxy_dollar": False,            # Disabled: requires Alpaca ETF access
    "garch_volatility": False,       # Disabled: vol regimes don't predict direction
    "breakout_detection": False,     # Disabled: too many false breakouts
}

# --- Strategy Regime Requirements ---
# Strategies that should only run in specific market regimes
STRATEGY_REGIME_REQUIREMENTS = {
    "regime_mean_reversion": ["sideways"],
    "kalman_trend": ["bull", "bear"],
    "meme_momentum": ["bull"],
    "pairs_trading": ["sideways", "bear"],
}

# --- Data Sources ---
# CoinGecko removed in v3 — all crypto data comes from Alpaca Data API (free, no rate limits)
# ETF data: Alpaca IEX feed (free) with yfinance fallback
# Sentiment: alternative.me Fear & Greed (Alpaca doesn't provide sentiment)
# Economic: FRED (Alpaca doesn't provide macro data)
FEAR_GREED_URL = "https://api.alternative.me/fng/"
FRED_BASE = "https://api.stlouisfed.org/fred"


class ConfigError(Exception):
    """Raised for fatal configuration errors."""
    pass


def validate_config(test_api: bool = True) -> list[str]:
    """Validate all required config values and optionally test API connectivity.

    Returns a list of warnings (non-fatal issues).
    Raises ConfigError for fatal misconfigurations.
    """
    warnings: list[str] = []

    # -- Required keys: AsterDex (primary execution venue) --------------------
    if not ASTER_API_KEY or not ASTER_API_SECRET:
        raise ConfigError(
            "ASTER_API_KEY and ASTER_API_SECRET are required. "
            "Get keys at https://www.asterdex.com → sign with wallet → create API key."
        )

    # -- Trading mode sanity --------------------------------------------------
    if TRADING_MODE not in ("paper", "live"):
        raise ConfigError(
            f"TRADING_MODE must be 'paper' or 'live', got '{TRADING_MODE}'"
        )


    if not FRED_API_KEY:
        warnings.append(
            "FRED_API_KEY not set — macro data strategies will have limited data."
        )

    # -- API connectivity test (AsterDex) -------------------------------------
    if test_api:
        try:
            from trading.execution.router import get_account
            account = get_account()
            if account.get("trading_blocked"):
                raise ConfigError(
                    "AsterDex account is inactive or not configured. "
                    "Check your API keys and wallet address."
                )
            status = account.get("status", "UNKNOWN")
            if status not in ("ACTIVE", "active"):
                warnings.append(f"Account status is '{status}', expected ACTIVE.")
        except ConfigError:
            raise
        except Exception as e:
            raise ConfigError(
                f"Failed to connect to AsterDex API: {e}. "
                "Check your API keys and network connectivity."
            ) from e

    # -- Strategy file existence check -----------------------------------------
    strategy_dir = PROJECT_ROOT / "trading" / "strategy"
    missing_strategies = []
    for name, enabled in STRATEGY_ENABLED.items():
        if not enabled:
            continue
        module_file = strategy_dir / f"{name}.py"
        if not module_file.exists():
            missing_strategies.append(name)
    if missing_strategies:
        raise ConfigError(
            f"Enabled strategies missing files: {', '.join(missing_strategies)}. "
            "Either disable them in STRATEGY_ENABLED or add the strategy files."
        )

    return warnings

# --- Leverage Configuration ---
# Per-strategy leverage based on 90-day backtest analysis (Dec 2025 - Mar 2026)
# Conservative: capital preservation, minimal liquidation risk
LEVERAGE_CONSERVATIVE = {
    "default": 1,
}

# Moderate: balanced risk/reward for proven strategies
LEVERAGE_MODERATE = {
    "default": 1,
    "kalman_trend": 3,       # Sharpe 3.49, no liquidations up to 10x
}

# Aggressive: high returns for high risk tolerance
LEVERAGE_AGGRESSIVE = {
    "default": 2, "kalman_trend": 5, "hmm_regime": 3, "whale_flow": 3,
    "funding_arb": 3, "pairs_trading": 2, "taker_divergence": 1,
    "cross_basis_rv": 1, "meme_momentum": 1, "basis_zscore": 2,
    "regime_mean_reversion": 2, "factor_crypto": 2, "funding_term_structure": 3,
    "oi_price_divergence": 2, "microstructure_composite": 2,
    "cross_asset_momentum": 2, "gold_crypto_hedge": 2,
    "equity_crypto_correlation": 2, "multi_factor_rank": 2,
    "volatility_regime": 2, "rsi_divergence": 2,
    "onchain_flow": 2, "funding_forecast": 3,
}

# Greedy: maximum returns, accepts heavy losses and liquidations
LEVERAGE_GREEDY = {
    "default": 3,
    "kalman_trend": 10,      # Sharpe 3.49 even at 10x
    "whale_flow": 7,         # +1.4% at 7x, Sharpe 0.40
    "taker_divergence": 1,   # STILL never leverage
    "cross_basis_rv": 1,     # STILL never leverage
    "factor_crypto": 2,
}

# Active leverage profile (change this to switch)
LEVERAGE_PROFILE = os.getenv("LEVERAGE_PROFILE", "aggressive")  # conservative|moderate|aggressive|greedy

def _get_active_profile() -> str:
    """Get the active profile — DB setting takes precedence over env/default."""
    try:
        from trading.db.store import get_setting
        return get_setting("trading_profile", LEVERAGE_PROFILE)
    except Exception:
        return LEVERAGE_PROFILE

def get_leverage(strategy_name: str) -> int:
    """Get leverage multiplier for a strategy based on active profile."""
    profiles = {
        "conservative": LEVERAGE_CONSERVATIVE,
        "moderate": LEVERAGE_MODERATE,
        "aggressive": LEVERAGE_AGGRESSIVE,
        "greedy": LEVERAGE_GREEDY,
    }
    active = _get_active_profile()
    profile = profiles.get(active, LEVERAGE_CONSERVATIVE)
    return profile.get(strategy_name, profile.get("default", 1))

def validate_leverage_profile() -> list[str]:
    """Check the active leverage profile for dangerous configurations.

    Returns a list of warnings. Called during daemon startup.
    """
    warnings: list[str] = []
    profiles = {
        "conservative": LEVERAGE_CONSERVATIVE,
        "moderate": LEVERAGE_MODERATE,
        "aggressive": LEVERAGE_AGGRESSIVE,
        "greedy": LEVERAGE_GREEDY,
    }
    active = LEVERAGE_PROFILE
    try:
        from trading.db.store import get_setting
        active = get_setting("trading_profile", LEVERAGE_PROFILE)
    except Exception:
        pass

    profile = profiles.get(active, LEVERAGE_CONSERVATIVE)

    # Check for strategies with leverage > 5x (high liquidation risk)
    high_lev = {k: v for k, v in profile.items() if k != "default" and v > 5}
    if high_lev:
        for strat, lev in high_lev.items():
            warnings.append(
                f"LEVERAGE WARNING: {strat} at {lev}x — liquidation risk is significant. "
                f"A {100/lev:.0f}% adverse move wipes the position."
            )

    # Check total portfolio leverage exposure
    # With max_position_pct=25% and 22 strategies, worst case is many concurrent positions
    enabled_count = sum(1 for k, v in STRATEGY_ENABLED.items() if v)
    default_lev = profile.get("default", 1)
    max_lev = max(profile.values()) if profile else 1
    if max_lev >= 7:
        warnings.append(
            f"LEVERAGE WARNING: Profile '{active}' has max {max_lev}x leverage. "
            f"Backtests don't capture exchange outages, cascading liquidations, "
            f"or flash crashes beyond historical data."
        )

    # Check total leverage cap vs risk manager setting
    total_cap = RISK.get("max_total_leverage", 5)
    if default_lev > total_cap:
        warnings.append(
            f"LEVERAGE CONFLICT: Default leverage ({default_lev}x) exceeds "
            f"risk manager cap ({total_cap}x). Risk manager will block most trades."
        )

    if active == "greedy":
        warnings.append(
            "LEVERAGE WARNING: 'greedy' profile is active — this accepts heavy losses "
            "and possible liquidations. Switch to 'aggressive' for production use."
        )

    return warnings


# --- Learning ---
LEARNING = {
    "min_trades_for_adaptation": 20,    # Need 20+ trades before suggesting changes
    "auto_apply": True,                  # Autonomous agents auto-apply safe actions
    "review_frequency": "weekly",
}

# --- Startup Validation ---
RUN_STARTUP_BACKTEST = os.getenv("RUN_STARTUP_BACKTEST", "false").lower() == "true"

# --- Autonomous Improvement ---
# The system continuously self-evaluates and improves through agent conversations.
# Safe actions (disable losers, tighten risk, shift allocation) are auto-applied.
# Dangerous actions (enable new strategies, loosen risk) require human review.
AUTONOMOUS = {
    "enabled": True,                     # Enable autonomous improvement loop
    "auto_disable_losers": True,         # Auto-disable strategies with <25% win rate
    "auto_rebalance": True,              # Auto-shift allocation toward winners
    "auto_tighten_risk": True,           # Auto-tighten risk during drawdown
    "auto_defensive_posture": True,      # Auto-reduce long bias in bearish regimes
    "log_conversations": True,           # Log all agent-to-agent conversations
}

