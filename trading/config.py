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

