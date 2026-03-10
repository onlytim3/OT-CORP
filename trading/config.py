"""Central configuration for the trading system."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# --- Alpaca ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# --- FRED ---
FRED_API_KEY = os.getenv("FRED_API_KEY", "")

# --- Trading Mode ---
TRADING_MODE = os.getenv("TRADING_MODE", "paper")  # "paper" or "live"

# --- Paths ---
DB_PATH = PROJECT_ROOT / "trading" / "db" / "trading.db"
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
JOURNALS_DIR = KNOWLEDGE_DIR / "journals"
REVIEWS_DIR = KNOWLEDGE_DIR / "reviews"
STRATEGIES_DIR = KNOWLEDGE_DIR / "strategies"

# --- Risk Parameters ---
RISK = {
    "max_position_pct": 0.33,       # Max 33% of portfolio per position
    "stop_loss_pct": 0.07,          # 7% stop loss (tighter for 2x leveraged ETFs)
    "max_daily_loss_pct": 0.05,     # 5% max daily loss
    "max_drawdown_pct": 0.20,       # 20% max total drawdown → halt trading
    "min_cash_reserve_pct": 0.10,   # Keep 10% in cash
    "max_trades_per_day": 10,          # Increased for 10 strategies
}

# --- Strategy Parameters ---
MOMENTUM = {
    "lookback_days": 7,
    "top_n": 3,
    "entry_threshold": 0.05,    # 5% return to trigger buy
    "exit_threshold": -0.05,    # -5% return to trigger sell
    "rebalance_day": "sunday",
    "coins": [
        "bitcoin", "ethereum", "solana", "avalanche-2", "polkadot",
        "chainlink", "uniswap", "aave", "litecoin", "bitcoin-cash",
    ],
}

MEAN_REVERSION = {
    "fear_buy_threshold": 25,       # Buy when F&G < 25
    "greed_sell_threshold": 75,     # Sell when F&G > 75
    "dca_days": 3,                  # Dollar-cost average over 3 days
    "symbol": "BTC/USD",
}

GOLD_BTC = {
    "std_dev_threshold": 2.0,       # Trade when ratio deviates > 2 std devs
    "lookback_days": 30,
    "gold_symbol": "UGL",           # ProShares Ultra Gold (2x leveraged)
    "btc_symbol": "BTC/USD",
}

RSI_DIVERGENCE = {
    "rsi_period": 14,
    "ohlc_days": 30,                # 30 days → 4-hour candles (~180 data points)
    "divergence_lookback": 14,      # Candles to scan for divergence
    "min_rsi_oversold": 30,         # RSI below this + bullish divergence → buy
    "min_rsi_overbought": 70,       # RSI above this + bearish divergence → sell
    "coins": ["bitcoin", "ethereum", "solana"],
}

EMA_CROSSOVER = {
    "fast_period": 8,
    "slow_period": 21,
    "trend_period": 50,             # EMA(50) trend filter
    "ohlc_days": 30,                # 30 days → 4-hour candles (~180 data points)
    "coins": ["bitcoin", "ethereum", "solana"],
}

BOLLINGER_SQUEEZE = {
    "bb_period": 20,
    "bb_std": 2.0,
    "squeeze_percentile": 10,       # Bandwidth below 10th percentile = squeeze
    "ohlc_days": 30,                # 30 days → 4-hour candles (~180 data points)
    "coins": ["bitcoin", "ethereum", "solana"],
}

BTC_ETH_RATIO = {
    "lookback_days": 90,
    "entry_z": 1.5,                 # Enter when z-score exceeds ±1.5
    "exit_z": 0.3,                  # Exit when z-score returns within ±0.3
    "btc_symbol": "BTC/USD",
    "eth_symbol": "ETH/USD",
}

TIPS_YIELD = {
    "fred_series": "DFII10",        # 10-Year TIPS real yield
    "lookback_days": 60,
    "z_threshold": 1.0,             # Trade when z-score exceeds ±1.0
    "gold_symbol": "UGL",           # ProShares Ultra Gold (2x leveraged)
}

FG_MULTI_TIMEFRAME = {
    "extreme_fear": 15,             # Tier 1 buy: F&G daily < 15
    "fear": 25,                     # Tier 2 buy: F&G daily < 25
    "greed": 75,                    # Tier 2 sell: F&G daily > 75
    "extreme_greed": 85,            # Tier 1 sell: F&G daily > 85
    "symbol": "BTC/USD",
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
    "momentum": True,
    "mean_reversion": True,
    "gold_btc": True,
    "rsi_divergence": True,
    "ema_crossover": True,
    "bollinger_squeeze": True,
    "btc_eth_ratio": True,
    "tips_yield": True,
    "fg_multi_timeframe": True,
    "dxy_dollar": True,
}

# --- Data Sources ---
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
FEAR_GREED_URL = "https://api.alternative.me/fng/"
FRED_BASE = "https://api.stlouisfed.org/fred"

# --- Alpaca Crypto Symbol Mapping ---
# CoinGecko ID → Alpaca trading symbol
CRYPTO_SYMBOLS = {
    "bitcoin": "BTC/USD",
    "ethereum": "ETH/USD",
    "solana": "SOL/USD",
    "avalanche-2": "AVAX/USD",
    "polkadot": "DOT/USD",
    "chainlink": "LINK/USD",
    "uniswap": "UNI/USD",
    "aave": "AAVE/USD",
    "litecoin": "LTC/USD",
    "bitcoin-cash": "BCH/USD",
}

# --- Commodity ETF Symbols (traded on Alpaca as equities) ---
COMMODITY_ETFS = {
    "gold": "UGL",              # ProShares Ultra Gold (2x leveraged)
    "silver": "AGQ",            # ProShares Ultra Silver (2x leveraged)
    "oil": "USO",
    "natural_gas": "UNG",
    "agriculture": "DBA",
}

# --- Learning ---
LEARNING = {
    "min_trades_for_adaptation": 20,    # Need 20+ trades before suggesting changes
    "auto_apply": False,                 # Human approval required
    "review_frequency": "weekly",
}
