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

# --- Trading Mode ---
TRADING_MODE = os.getenv("TRADING_MODE", "paper")  # "paper" or "live"

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
RISK = {
    "max_position_pct": 0.15,       # Max 15% of portfolio per position (more strategies = smaller positions)
    "stop_loss_pct": 0.07,          # 7% stop loss
    "max_daily_loss_pct": 0.05,     # 5% max daily loss
    "max_drawdown_pct": 0.20,       # 20% max total drawdown → halt trading
    "min_cash_reserve_pct": 0.15,   # Keep 15% in cash (more strategies need buffer)
    "max_trades_per_day": 25,       # 20+ active strategies
    "min_volume_ratio": 0.30,       # Block entries when volume < 30% of 7d average
    "volume_exit_ratio": 0.20,      # Exit positions when volume < 20% of 7d average
    "max_spread_bps": 50,           # Block entries when bid-ask spread > 50 basis points
    "max_market_impact_pct": 0.01,  # Block entries when order > 1% of recent 4h quote volume
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
    # Disabled
    "dxy_dollar": False,            # Disabled: requires Alpaca ETF access
    "garch_volatility": False,       # Disabled: vol regimes don't predict direction
    "breakout_detection": False,     # Disabled: too many false breakouts
}

# --- Data Sources ---
# CoinGecko removed in v3 — all crypto data comes from Alpaca Data API (free, no rate limits)
# ETF data: Alpaca IEX feed (free) with yfinance fallback
# Sentiment: alternative.me Fear & Greed (Alpaca doesn't provide sentiment)
# Economic: FRED (Alpaca doesn't provide macro data)
FEAR_GREED_URL = "https://api.alternative.me/fng/"
FRED_BASE = "https://api.stlouisfed.org/fred"

# --- Crypto Symbol Mapping ---
# Internal coin ID → Alpaca-style trading symbol (used by signals for internal tracking)
CRYPTO_SYMBOLS = {
    # Major L1
    "bitcoin": "BTC/USD",
    "ethereum": "ETH/USD",
    "solana": "SOL/USD",
    "bnb": "BNB/USD",
    "xrp": "XRP/USD",
    "avalanche-2": "AVAX/USD",
    "polkadot": "DOT/USD",
    "cardano": "ADA/USD",
    "toncoin": "TON/USD",
    "near": "NEAR/USD",
    "sui": "SUI/USD",
    "aptos": "APT/USD",
    "cosmos": "ATOM/USD",
    "internet-computer": "ICP/USD",
    "sei": "SEI/USD",
    "stacks": "STX/USD",
    "tron": "TRX/USD",
    # L2 / Infrastructure
    "arbitrum": "ARB/USD",
    "optimism": "OP/USD",
    "starknet": "STRK/USD",
    "polygon": "POL/USD",
    "linea": "LINEA/USD",
    "zksync": "ZK/USD",
    "movement": "MOVE/USD",
    "scroll": "ZKUSDT",
    # DeFi
    "chainlink": "LINK/USD",
    "uniswap": "UNI/USD",
    "aave": "AAVE/USD",
    "maker": "MKR/USD",
    "curve-dao": "CRV/USD",
    "synthetix": "SNX/USD",
    "dydx": "DYDX/USD",
    "pendle": "PENDLE/USD",
    "jupiter": "JUP/USD",
    "ondo": "ONDO/USD",
    "lido-dao": "LDO/USD",
    "injective": "INJ/USD",
    "pancakeswap": "CAKE/USD",
    "ethfi": "ETHFI/USD",
    "cow-protocol": "COW/USD",
    "eigenlayer": "EIGEN/USD",
    # AI / Compute
    "fetch-ai": "FET/USD",
    "bittensor": "TAO/USD",
    "render": "RENDER/USD",
    "virtual-protocol": "VIRTUAL/USD",
    "artificial-intelligence": "AIO/USD",
    "grass": "GRASS/USD",
    # Meme
    "dogecoin": "DOGE/USD",
    "shiba-inu": "1000SHIB/USD",
    "pepe": "1000PEPE/USD",
    "bonk": "1000BONK/USD",
    "floki": "1000FLOKI/USD",
    "peanut": "PNUT/USD",
    "trump": "TRUMP/USD",
    "fartcoin": "FARTCOIN/USD",
    "melania": "MELANIA/USD",
    "bome": "BOME/USD",
    "moodeng": "MOODENG/USD",
    "turbo": "TURBO/USD",
    "bonk-earner": "1000BONK/USD",
    # Storage / Misc
    "filecoin": "FIL/USD",
    "arweave": "AR/USD",
    "litecoin": "LTC/USD",
    "bitcoin-cash": "BCH/USD",
    "ethereum-classic": "ETC/USD",
    "stellar": "XLM/USD",
    "monero": "XMR/USD",
    "zcash": "ZEC/USD",
    "hedera": "HBAR/USD",
    "kaspa": "KAS/USD",
    "flow": "FLOW/USD",
    "axie-infinity": "AXS/USD",
    "gala": "GALA/USD",
    "ape": "APE/USD",
    "worldcoin": "WLD/USD",
    "hyperliquid": "HYPE/USD",
    "pyth": "PYTH/USD",
    "solv": "SOLV/USD",
    "jasmy": "JASMY/USD",
    # Stocks (AsterDex perps)
    "apple": "AAPL/USD",
    "amazon": "AMZN/USD",
    "microsoft": "MSFT/USD",
    "nvidia": "NVDA/USD",
    "tesla": "TSLA/USD",
    "google": "GOOG/USD",
    "meta": "META/USD",
    "intel": "INTC/USD",
    "robinhood": "HOOD/USD",
    # Commodities (AsterDex perps)
    "gold": "XAU/USD",
    "silver": "XAG/USD",
    "copper": "XCU/USD",
    "platinum": "XPT/USD",
    "palladium": "XPD/USD",
    "natural-gas": "NATGAS/USD",
    "pax-gold": "PAXG/USD",
    # Indices (AsterDex perps)
    "sp500": "SPX/USD",
    "nasdaq100": "QQQ/USD",
}

# --- AsterDex Symbol Mapping (coin ID → AsterDex perpetual futures symbol) ---
ASTER_SYMBOLS = {
    # Major L1
    "bitcoin": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "solana": "SOLUSDT",
    "bnb": "BNBUSDT",
    "xrp": "XRPUSDT",
    "avalanche-2": "AVAXUSDT",
    "polkadot": "DOTUSDT",
    "cardano": "ADAUSDT",
    "toncoin": "TONUSDT",
    "near": "NEARUSDT",
    "sui": "SUIUSDT",
    "aptos": "APTUSDT",
    "cosmos": "ATOMUSDT",
    "internet-computer": "ICPUSDT",
    "sei": "SEIUSDT",
    "stacks": "STXUSDT",
    "tron": "TRXUSDT",
    # L2 / Infrastructure
    "arbitrum": "ARBUSDT",
    "optimism": "OPUSDT",
    "starknet": "STRKUSDT",
    "polygon": "POLUSDT",
    "linea": "LINEAUSDT",
    "zksync": "ZKUSDT",
    "movement": "MOVEUSDT",
    # DeFi
    "chainlink": "LINKUSDT",
    "uniswap": "UNIUSDT",
    "aave": "AAVEUSDT",
    "curve-dao": "CRVUSDT",
    "synthetix": "SNXUSDT",
    "dydx": "DYDXUSDT",
    "pendle": "PENDLEUSDT",
    "jupiter": "JUPUSDT",
    "ondo": "ONDOUSDT",
    "lido-dao": "LDOUSDT",
    "injective": "INJUSDT",
    "pancakeswap": "CAKEUSDT",
    "ethfi": "ETHFIUSDT",
    "cow-protocol": "COWUSDT",
    "eigenlayer": "EIGENUSDT",
    # AI / Compute
    "fetch-ai": "FETUSDT",
    "bittensor": "TAOUSDT",
    "render": "RENDERUSDT",
    "virtual-protocol": "VIRTUALUSDT",
    "artificial-intelligence": "AIOUSDT",
    "grass": "GRASSUSDT",
    # Meme
    "dogecoin": "DOGEUSDT",
    "shiba-inu": "1000SHIBUSDT",
    "pepe": "1000PEPEUSDT",
    "bonk": "1000BONKUSDT",
    "floki": "1000FLOKIUSDT",
    "peanut": "PNUTUSDT",
    "trump": "TRUMPUSDT",
    "fartcoin": "FARTCOINUSDT",
    "melania": "MELANIAUSDT",
    "bome": "BOMEUSDT",
    "moodeng": "MOODENGUSDT",
    "turbo": "TURBOUSDT",
    # Storage / Misc
    "filecoin": "FILUSDT",
    "arweave": "ARUSDT",
    "litecoin": "LTCUSDT",
    "bitcoin-cash": "BCHUSDT",
    "ethereum-classic": "ETCUSDT",
    "stellar": "XLMUSDT",
    "monero": "XMRUSDT",
    "zcash": "ZECUSDT",
    "hedera": "HBARUSDT",
    "kaspa": "KASUSDT",
    "flow": "FLOWUSDT",
    "axie-infinity": "AXSUSDT",
    "gala": "GALAUSDT",
    "ape": "APEUSDT",
    "worldcoin": "WLDUSDT",
    "hyperliquid": "HYPEUSDT",
    "pyth": "PYTHUSDT",
    "solv": "SOLVUSDT",
    "jasmy": "JASMYUSDT",
    # Stocks (AsterDex perps on equities)
    "apple": "AAPLUSDT",
    "amazon": "AMZNUSDT",
    "microsoft": "MSFTUSDT",
    "nvidia": "NVDAUSDT",
    "tesla": "TSLAUSDT",
    "google": "GOOGUSDT",
    "meta": "METAUSDT",
    "intel": "INTCUSDT",
    "robinhood": "HOODUSDT",
    # Commodities (AsterDex perps on commodities)
    "gold": "XAUUSDT",
    "silver": "XAGUSDT",
    "copper": "XCUUSDT",
    "platinum": "XPTUSDT",
    "palladium": "XPDUSDT",
    "natural-gas": "NATGASUSDT",
    "pax-gold": "PAXGUSDT",
    # Indices (AsterDex perps on indices)
    "sp500": "SPXUSDT",
    "nasdaq100": "QQQUSDT",
}

# --- Asset Categories (for strategy targeting) ---
CRYPTO_L1 = ["bitcoin", "ethereum", "solana", "bnb", "xrp", "avalanche-2",
             "polkadot", "cardano", "toncoin", "near", "sui", "aptos",
             "cosmos", "internet-computer", "sei", "stacks", "tron"]
CRYPTO_L2 = ["arbitrum", "optimism", "starknet", "polygon", "linea", "zksync", "movement"]
CRYPTO_DEFI = ["chainlink", "uniswap", "aave", "curve-dao", "synthetix", "dydx",
               "pendle", "jupiter", "ondo", "lido-dao", "injective", "pancakeswap",
               "ethfi", "cow-protocol", "eigenlayer"]
CRYPTO_AI = ["fetch-ai", "bittensor", "render", "virtual-protocol",
             "artificial-intelligence", "grass"]
CRYPTO_MEME = ["dogecoin", "shiba-inu", "pepe", "bonk", "floki", "peanut",
               "trump", "fartcoin", "melania", "bome", "moodeng", "turbo"]
STOCK_PERPS = ["apple", "amazon", "microsoft", "nvidia", "tesla", "google",
               "meta", "intel", "robinhood"]
COMMODITY_PERPS = ["gold", "silver", "copper", "platinum", "palladium",
                   "natural-gas", "pax-gold"]
INDEX_PERPS = ["sp500", "nasdaq100"]

# All tradeable assets
ALL_TRADEABLE = CRYPTO_L1 + CRYPTO_L2 + CRYPTO_DEFI + CRYPTO_AI + CRYPTO_MEME + \
                STOCK_PERPS + COMMODITY_PERPS + INDEX_PERPS

# --- Commodity ETF Symbols (legacy — now traded as AsterDex perps) ---
COMMODITY_ETFS = {
    "gold": "XAUUSDT",
    "silver": "XAGUSDT",
    "oil": "USO",               # Not on AsterDex
    "natural_gas": "NATGASUSDT",
    "agriculture": "DBA",       # Not on AsterDex
}

# --- Notifications ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Raised when a required configuration value is missing or invalid."""


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

    # -- Optional but warned --------------------------------------------------
    if not ALPACA_API_KEY:
        warnings.append(
            "ALPACA_API_KEY not set — ETF strategies (dxy_dollar, cross_asset_momentum) disabled."
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
    "default": 2,
    "kalman_trend": 5,       # Sharpe 3.49, zero DD at 5x
    "whale_flow": 3,         # Some evidence for 7x but risky
    "taker_divergence": 1,   # NEVER leverage — loses 93% at 3x
    "cross_basis_rv": 1,     # NEVER leverage — liquidation-prone
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
LEVERAGE_PROFILE = os.getenv("LEVERAGE_PROFILE", "conservative")  # conservative|moderate|aggressive|greedy

def get_leverage(strategy_name: str) -> int:
    """Get leverage multiplier for a strategy based on active profile."""
    profiles = {
        "conservative": LEVERAGE_CONSERVATIVE,
        "moderate": LEVERAGE_MODERATE,
        "aggressive": LEVERAGE_AGGRESSIVE,
        "greedy": LEVERAGE_GREEDY,
    }
    profile = profiles.get(LEVERAGE_PROFILE, LEVERAGE_CONSERVATIVE)
    return profile.get(strategy_name, profile.get("default", 1))

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
