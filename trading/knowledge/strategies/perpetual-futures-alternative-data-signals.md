# Perpetual Futures Alternative Data Signals — AsterDex Alpha Research

**Date**: 2026-03-14
**Trading Cycle**: Every 4 hours (6 cycles per day)
**Exchange**: AsterDex (Binance-compatible `/fapi/v3/` API)
**Execution**: AsterDex perps (primary) + Alpaca spot crypto (secondary)

---

## Executive Summary

The AsterDex perpetual futures API exposes seven distinct categories of non-traditional data through public endpoints requiring zero authentication. Two strategies (`funding_arb` and `liquidation_cascade`) are already implemented. This document catalogs all extractable alpha signals, grades their expected edge, and identifies the highest-priority gaps in the current pipeline.

**Key finding**: The highest untapped alpha sources are (1) open interest divergence, (2) funding rate term structure, and (3) cross-pair basis relative value. These three signals are not captured by any existing strategy and can be constructed entirely from public endpoints already wrapped in `aster_client.py`.

---

## 1. Funding Rate Signals

### Available Endpoints
- `/fapi/v3/premiumIndex` — current funding rate per symbol (via `get_aster_mark_prices`)
- `/fapi/v3/fundingRate` — historical funding rates (via `get_aster_funding_rates`)

### Current Implementation Status
**Already implemented** in `trading/strategy/funding_arb.py` (FundingArbStrategy). Covers:
- Extreme funding rate mean reversion
- Funding rate z-score
- Funding acceleration z-score
- Cross-asset funding divergence (BTC vs altcoin)
- Basis spread confirmation

### Signal 1A: Funding Rate Mean Reversion (IMPLEMENTED)

- **Construction**: When 8-hour funding exceeds +/-0.05%, fade the direction. Extreme at +/-0.1%.
- **Holding period**: 4-24 hours (aligns with 4h trading cycle)
- **Expected hit rate**: 55-60% on extreme readings (>2 z-score), lower on moderate
- **Best pairs**: BTC, ETH, SOL — highest OI means funding rates reflect real positioning, not noise
- **Implementation**: Complete. See `funding_arb.py` lines 153-189 for scoring logic.
- **Decay risk**: Low. Funding rate mean reversion is a structural property of perpetual futures mechanics, not a discovered anomaly. It will persist as long as perpetual futures exist.

### Signal 1B: Funding Rate Term Structure (NOT IMPLEMENTED)

- **Construction**: Compare the current funding rate against the trailing 7-day and 30-day average funding rate. A rising term structure (current > 7d avg > 30d avg) signals accelerating leverage. A flat-to-inverted structure signals capitulation.
- **Methodology**:
  1. Fetch 500 historical funding rates via `get_aster_funding_rates(symbol, limit=500)` — each entry is one 8-hour period, giving ~166 days of history
  2. Compute rolling averages: 3-period (1 day), 21-period (7 days), 90-period (30 days)
  3. Term structure slope = (current - 30d_avg) / stdev(30d series)
  4. Positive slope > 1.5 sigma: overleveraged longs building — contrarian sell
  5. Negative slope < -1.5 sigma: capitulation — contrarian buy
- **Holding period**: 1-3 days (slower signal than spot funding rate)
- **Expected hit rate**: 58-62%. The term structure adds a regime filter that reduces false signals from one-off funding spikes.
- **Best pairs**: BTCUSDT, ETHUSDT. Altcoins have noisier funding histories.
- **Implementation complexity**: Low. All data is already available via `get_funding_rate_history()`. Requires a new function in `trading/data/aster.py` and minor additions to `funding_arb.py`.
- **Combination**: Use term structure as a confirmation filter for the existing funding rate z-score signal. Only trade extreme z-scores when the term structure slope agrees.

### Signal 1C: Cross-Asset Funding Divergence (PARTIALLY IMPLEMENTED)

- **Construction**: Compare BTC funding rate against altcoin funding rates. When BTC funding is negative but altcoin funding is positive, altcoins are being speculatively leveraged without BTC confirmation — this is fragile and tends to reverse.
- **Current state**: Partially implemented in `funding_arb.py` lines 240-243, but only as a +0.1 strength boost on sell signals. The full signal would:
  1. Compute funding rate spread: altcoin_funding - BTC_funding for each alt
  2. Z-score this spread against its own 30-day history
  3. When spread z-score > 2.0: altcoin overleveraged relative to BTC — sell alt
  4. When spread z-score < -2.0: altcoin underleveraged relative to BTC — buy alt
- **Holding period**: 4-12 hours
- **Expected hit rate**: 55-58%. Works best during altcoin rotation cycles.
- **Best pairs**: SOL, AVAX, LINK — mid-cap alts with enough OI to have meaningful funding
- **Implementation complexity**: Low. Data already available.

---

## 2. Open Interest Signals

### Available Endpoints
- `/fapi/v3/ticker/24hr` — includes `openInterest` and `volume` fields (via `get_aster_ticker_24h`)
- No dedicated OI history endpoint identified yet. OI must be sampled over time and stored locally.

### Current Implementation Status
**NOT IMPLEMENTED**. No strategy currently uses open interest data. This is the largest gap.

### Signal 2A: OI-Price Divergence

- **Construction**: When price makes a new high but OI is declining (or vice versa), the move lacks conviction and is likely to reverse.
  1. Every 4h cycle, fetch 24h ticker data for all tracked symbols
  2. Store OI snapshots in a local time series (SQLite or in-memory rolling window)
  3. Compute 24h OI change % and 24h price change %
  4. Divergence flag: price_change > +3% AND oi_change < -2% (bearish divergence)
  5. Divergence flag: price_change < -3% AND oi_change > +2% (bullish divergence)
  6. Strength scaled by magnitude of divergence
- **Holding period**: 4-24 hours
- **Expected hit rate**: 60-65% on strong divergences (>5% price move with opposite OI direction). This is one of the highest-conviction signals in derivatives data.
- **Best pairs**: BTCUSDT, ETHUSDT, SOLUSDT. Requires sufficient OI to be meaningful.
- **Implementation complexity**: Medium. The main challenge is that OI history must be built over time — there is no historical OI endpoint. Requires a storage mechanism to accumulate OI snapshots each trading cycle.
- **Combination**: Stack with funding rate direction. OI-price divergence + extreme funding in the same direction = highest-conviction trade.

### Signal 2B: OI Momentum (Trend Confirmation)

- **Construction**: Rising OI during a price trend confirms the trend has institutional participation. Falling OI during a trend signals the trend is exhausting.
  1. Track OI change over rolling 6-period (24h) and 18-period (3-day) windows
  2. Trend = Kalman or EMA trend direction from price
  3. OI rising + price trending up: confirm long (strengthen existing long signals)
  4. OI rising + price trending down: confirm short
  5. OI falling + price trending: weaken existing signal strength by 30-50%
- **Holding period**: 1-3 days (trend confirmation, not a standalone signal)
- **Expected hit rate**: Not standalone. Used as a filter, it improves other strategy hit rates by 3-5%.
- **Best pairs**: All tracked symbols
- **Implementation complexity**: Medium. Same OI storage requirement as 2A.
- **Combination**: Designed specifically as a filter for `kalman_trend`, `hmm_regime`, and `cross_asset_momentum` strategies.

### Signal 2C: OI Concentration / Cross-Pair OI Rotation

- **Construction**: When OI flows from BTC/ETH into altcoins, it signals speculative rotation (typically late-cycle behavior). When OI concentrates back into BTC, it signals risk-off within crypto.
  1. Compute each symbol's OI as % of total tracked OI
  2. Track BTC OI share over time
  3. BTC OI share declining (< 30-day average by 1+ sigma): alt rotation, fragile — reduce alt exposure
  4. BTC OI share rising (> 30-day average by 1+ sigma): flight to quality — increase BTC allocation
- **Holding period**: 1-5 days (slow, macro-like signal)
- **Expected hit rate**: 55-60%. More useful for position sizing than entry/exit.
- **Best pairs**: Cross-portfolio allocation signal, not per-pair
- **Implementation complexity**: Medium. Requires OI snapshots across all symbols.

---

## 3. Liquidation Cascade Detection

### Available Endpoints
- No dedicated public liquidation endpoint identified on AsterDex.
- `/fapi/v3/income` with `incomeType=LIQUIDATION` — **requires auth**, shows own account only.
- Liquidations must be inferred from microstructure signals.

### Current Implementation Status
**Partially implemented** in `trading/strategy/liquidation_cascade.py` (LiquidationCascadeStrategy). Uses taker volume imbalance, orderbook pressure, and basis-funding divergence as proxy signals. Does not detect actual liquidation events.

### Signal 3A: Inferred Liquidation Detection (via Volume Spikes + Price Acceleration)

- **Construction**: Liquidation cascades produce distinctive signatures: sudden volume spikes with price acceleration and taker volume strongly skewed in one direction.
  1. Fetch 5m or 15m klines for the last 4 hours
  2. Compute volume z-score per candle relative to 24h rolling average
  3. Compute taker buy ratio per candle
  4. Flag when: volume_zscore > 3.0 AND taker_buy_ratio < 0.25 (liquidation cascade of longs) or > 0.75 (shorts getting liquidated)
  5. After a cascade, the opposite direction typically reverses within 1-4 hours
- **Holding period**: 1-8 hours (post-cascade reversal)
- **Expected hit rate**: 55-60% for post-cascade reversal, but the edge is in sizing — cascades produce outsized moves.
- **Best pairs**: BTCUSDT, ETHUSDT, SOLUSDT
- **Implementation complexity**: Low-Medium. Requires fetching shorter-interval klines (5m/15m) which is already supported by `get_aster_klines()`.
- **Combination**: Use with orderbook imbalance (Signal 4A). If orderbook bids are being rebuilt after a long liquidation cascade, the reversal signal strengthens.

### Signal 3B: Liquidation Level Estimation

- **Construction**: From klines and OI data, estimate where liquidation clusters are likely to exist.
  1. Identify recent high-volume price levels where OI increased (entries are clustered there)
  2. Estimate liquidation prices assuming 5x-20x leverage ranges
  3. When price approaches estimated liquidation clusters, expect volatility expansion
- **Holding period**: Event-driven (hours)
- **Expected hit rate**: Hard to quantify. More useful as a volatility forecast than a directional signal.
- **Best pairs**: BTCUSDT, ETHUSDT
- **Implementation complexity**: High. Requires OI-at-price data which is not available from public endpoints. Must be approximated from volume profile analysis.
- **Recommendation**: Defer. The approximation quality is too low to justify the implementation cost.

---

## 4. Order Book Microstructure

### Available Endpoints
- `/fapi/v3/depth` — order book snapshots up to 1000 levels (via `get_aster_orderbook`)
- `/fapi/v3/ticker/bookTicker` — best bid/ask (via `get_aster_book_ticker`)

### Current Implementation Status
**Implemented** in `trading/data/aster.py` (`get_orderbook_imbalance`) and consumed by `liquidation_cascade.py`. Only uses top-20 levels.

### Signal 4A: Bid/Ask Volume Imbalance (IMPLEMENTED)

- **Construction**: Sum bid volume vs ask volume across top N levels. Persistent imbalance predicts short-term price direction.
- **Holding period**: Minutes to 1 hour. With 4h rebalancing, this signal decays significantly between cycles.
- **Expected hit rate**: 52-55% on a 4h horizon. Much better on shorter timeframes (60%+ on 5-minute).
- **Best pairs**: BTCUSDT, ETHUSDT
- **Current implementation**: `get_orderbook_imbalance()` uses top-20 levels. Used in `liquidation_cascade.py` with 30% weight.
- **Improvement opportunity**: The current implementation uses a single snapshot. For a 4h trading cycle, a time-weighted average of snapshots taken every 5-15 minutes would be more stable. However, this requires a background polling mechanism.
- **Decay risk**: High at 4h frequency. This signal is most useful at sub-minute frequencies.

### Signal 4B: Spread Dynamics as Volatility Predictor

- **Construction**: Bid-ask spread widening predicts upcoming volatility (market makers pulling liquidity before expected moves).
  1. Track spread_bps from `get_orderbook_imbalance()` over multiple snapshots
  2. Compare current spread to 24h rolling average spread
  3. Spread z-score > 2.0: expect higher volatility — reduce position sizes
  4. Spread z-score < -1.0 (unusually tight): low volatility regime — increase position sizes
- **Holding period**: Not directional. Position sizing signal only.
- **Expected hit rate**: N/A (volatility forecast, not directional)
- **Best pairs**: All
- **Implementation complexity**: Low. Data already returned by `get_orderbook_imbalance()`. Requires spread time series storage.
- **Combination**: Feed into `garch_volatility` strategy as an additional volatility input. Use for dynamic stop-loss sizing.

### Signal 4C: Large Order Detection / Iceberg Detection

- **Construction**: Look for unusually large resting orders (walls) in the order book. These can act as support/resistance.
  1. Fetch 100-500 level depth
  2. Identify orders > 5x median order size at that depth level
  3. Track whether these walls persist across snapshots (real) or disappear (spoofing)
  4. Persistent walls on the bid side = potential support
  5. Persistent walls on the ask side = potential resistance
- **Holding period**: Hours
- **Expected hit rate**: 53-55%. Walls are informative but can be pulled.
- **Best pairs**: BTCUSDT, ETHUSDT
- **Implementation complexity**: Medium. Requires multiple orderbook snapshots over time and wall persistence tracking.
- **Recommendation**: Interesting but secondary to OI signals. Implement after Signal 2A.

---

## 5. Taker Buy/Sell Volume Flow

### Available Endpoints
- `/fapi/v3/klines` — each candle includes `taker_buy_base_vol` and total `volume` (via `get_aster_klines`)

### Current Implementation Status
**Implemented** in `trading/data/aster.py` (`get_taker_volume_ratio`) and consumed by `liquidation_cascade.py` with 40% weight.

### Signal 5A: Taker Buy Volume Ratio (IMPLEMENTED)

- **Construction**: `taker_buy_vol / total_vol` over trailing N candles. Ratio > 0.58 = aggressive buying. Ratio < 0.42 = aggressive selling.
- **Holding period**: 4-12 hours (aligns with trading cycle)
- **Expected hit rate**: 53-56%. Taker flow is a momentum signal — works well in trending markets, noisy in range-bound.
- **Best pairs**: BTCUSDT, ETHUSDT, SOLUSDT
- **Current implementation**: `get_taker_volume_ratio()` computes over 24 1h candles. `liquidation_cascade.py` uses 6 1h candles.

### Signal 5B: Taker Volume Delta Divergence (NOT IMPLEMENTED)

- **Construction**: Compare taker buy ratio trajectory against price trajectory to detect exhaustion.
  1. Compute rolling taker buy ratio over 6h, 12h, 24h windows
  2. Compute price change over same windows
  3. If price is rising but taker buy ratio is declining: buying exhaustion — bearish
  4. If price is falling but taker buy ratio is rising: selling exhaustion — bullish
- **Holding period**: 4-12 hours
- **Expected hit rate**: 57-60%. Divergence signals are inherently higher-conviction than momentum signals.
- **Best pairs**: BTCUSDT, ETHUSDT
- **Implementation complexity**: Low. All data available via `get_aster_klines()`. Only requires computing taker ratio over multiple windows and comparing trajectories.
- **Combination**: Excellent complement to Signal 2A (OI-Price Divergence). When taker volume AND OI both diverge from price, the reversal probability increases significantly.

### Signal 5C: Volume Profile — High Volume Nodes as Support/Resistance

- **Construction**: Aggregate volume at price levels to identify where most trading has occurred. High-volume nodes act as magnets / support-resistance zones.
  1. Fetch 500+ 1h candles
  2. Create volume profile by bucketing volume into price ranges
  3. Identify high-volume nodes (local maxima in volume histogram)
  4. When price approaches a high-volume node from above: potential support
  5. When price approaches from below: potential resistance
- **Holding period**: 1-5 days (structural levels)
- **Expected hit rate**: 55-58% for identifying reversal zones
- **Best pairs**: BTCUSDT, ETHUSDT
- **Implementation complexity**: Medium
- **Recommendation**: Lower priority. VWAP from the klines data serves a similar function more simply.

---

## 6. Mark Price vs Index Price (Basis Spread)

### Available Endpoints
- `/fapi/v3/premiumIndex` — mark price, index price, funding rate per symbol (via `get_aster_mark_prices`)

### Current Implementation Status
**Implemented** in `trading/data/aster.py` (`get_basis_spread`). Used in both `funding_arb.py` (as confirmation) and `liquidation_cascade.py` (basis-funding divergence).

### Signal 6A: Basis Spread Mean Reversion (PARTIALLY IMPLEMENTED)

- **Construction**: The basis (mark - index) / index reflects futures premium/discount. Extreme basis readings tend to mean-revert.
  1. Current basis is already computed in `get_basis_spread()`
  2. Need: historical basis time series for z-score computation
  3. Basis z-score > 2.0: extreme premium — expect mean reversion — sell
  4. Basis z-score < -2.0: extreme discount — expect mean reversion — buy
- **Holding period**: 4-24 hours
- **Expected hit rate**: 58-62%. Basis mean reversion is structural (same reasoning as funding rate mean reversion).
- **Best pairs**: BTCUSDT, ETHUSDT
- **Implementation complexity**: Low. Requires storing basis snapshots over time. Data already fetched.
- **Current gap**: `funding_arb.py` uses basis as a binary confirmation filter (above/below 0.2% threshold) rather than as a standalone z-scored signal. Adding z-score treatment would improve signal quality.

### Signal 6B: Cross-Pair Basis Relative Value (NOT IMPLEMENTED)

- **Construction**: Compare basis spread across pairs to identify relative mispricing.
  1. Compute basis spread for all tracked symbols simultaneously
  2. Rank symbols by basis (most premium to most discount)
  3. Long the pair with lowest basis (cheapest) + short the pair with highest basis (richest)
  4. This is a relative value trade that is somewhat market-neutral
- **Holding period**: 1-3 days
- **Expected hit rate**: 60-65%. Relative value signals are higher-conviction because they exploit cross-pair mean reversion rather than absolute levels.
- **Best pairs**: Trade the extremes of the basis ranking (could be any pair)
- **Implementation complexity**: Low-Medium. All basis data is already available from `get_basis_spread()` (no-argument version returns all symbols). Requires cross-pair comparison logic.
- **Combination**: This is one of the most promising new signals. It works independently of market direction and can be combined with funding rate term structure (Signal 1B) for even higher conviction.

### Signal 6C: Basis-Funding Disagreement (IMPLEMENTED)

- **Construction**: When basis spread and funding rate disagree (e.g., positive basis but near-zero funding), it signals smart money positioning that retail hasn't caught up to.
- **Current implementation**: `_basis_funding_signal()` in `liquidation_cascade.py` covers this.
- **No changes needed**.

---

## 7. Long/Short Ratio

### Available Endpoints
- **NOT IDENTIFIED** on AsterDex public API. Binance exposes this via `/futures/data/globalLongShortAccountRatio` and `/futures/data/topLongShortAccountRatio`, but these may not exist on AsterDex.
- Could potentially be fetched from Binance directly as a supplementary data source (BTC/ETH long-short ratios are crypto-wide, not exchange-specific).

### Signal 7A: Retail Long/Short Ratio as Contrarian Signal

- **Construction**: Retail traders are directionally wrong at extremes. When long/short ratio is > 2.0 (everyone long), the contrarian trade is to sell. When < 0.5 (everyone short), buy.
- **Holding period**: 4-24 hours
- **Expected hit rate**: 57-62% at extreme readings. This is one of the most well-documented contrarian signals in derivatives markets.
- **Best pairs**: BTCUSDT, ETHUSDT
- **Implementation complexity**: Medium. Requires finding the endpoint on AsterDex or adding a Binance data fetcher as supplementary source.
- **Data source alternatives**:
  - Binance public API (no auth needed): `GET /futures/data/globalLongShortAccountRatio`
  - CoinGlass API (free tier available)
  - Coinalyze API
- **Recommendation**: High priority if the endpoint exists on AsterDex. If not, adding a Binance supplementary fetcher is worthwhile — long/short ratio is exchange-agnostic for major pairs.

---

## Signal Priority Matrix

Ranked by expected edge / implementation effort ratio:

| Priority | Signal | Expected Hit Rate | Holding Period | Status | Effort |
|----------|--------|-------------------|----------------|--------|--------|
| 1 | 2A: OI-Price Divergence | 60-65% | 4-24h | Not implemented | Medium |
| 2 | 6B: Cross-Pair Basis RV | 60-65% | 1-3d | Not implemented | Low-Med |
| 3 | 5B: Taker Volume Divergence | 57-60% | 4-12h | Not implemented | Low |
| 4 | 1B: Funding Term Structure | 58-62% | 1-3d | Not implemented | Low |
| 5 | 7A: Long/Short Ratio | 57-62% | 4-24h | Blocked (endpoint) | Medium |
| 6 | 2B: OI Momentum Filter | +3-5% improvement | 1-3d | Not implemented | Medium |
| 7 | 3A: Inferred Liquidation | 55-60% | 1-8h | Partial proxy | Low-Med |
| 8 | 6A: Basis Z-Score | 58-62% | 4-24h | Partial (no z-score) | Low |
| 9 | 4B: Spread Volatility | N/A (sizing) | N/A | Not implemented | Low |
| 10 | 2C: OI Concentration | 55-60% | 1-5d | Not implemented | Medium |

---

## Composite Signal Architecture

The highest-conviction trades come from stacking multiple independent signals. Here is the recommended signal combination framework for the 4-hour trading cycle:

### Tier 1 — Standalone Alpha (trade on their own)
- Funding rate z-score (existing)
- OI-Price divergence (priority 1)
- Cross-pair basis relative value (priority 2)

### Tier 2 — Confirmation Filters (boost/dampen Tier 1 signals)
- Funding term structure slope (confirms funding z-score)
- Taker volume divergence (confirms OI-price divergence)
- OI momentum (confirms trend strategies: kalman, HMM, factor)
- Orderbook imbalance (existing — confirms microstructure)

### Tier 3 — Risk/Sizing Adjustments (not directional)
- Spread dynamics / volatility forecast (adjust position size)
- OI concentration (adjust portfolio allocation)
- Event risk from intelligence engine (existing)

### Conviction Stacking Rules

```
Conviction = base_signal_strength

If 1 Tier-2 filter confirms: conviction *= 1.2
If 2 Tier-2 filters confirm: conviction *= 1.4
If Tier-2 filter contradicts: conviction *= 0.7
If 2 Tier-2 filters contradict: conviction = 0 (no trade)

Final position_size = base_size * conviction * volatility_adjustment
```

---

## Implementation Roadmap

### Phase 1 (Immediate — no new endpoints needed)
1. **Funding Term Structure** (Signal 1B) — add to `funding_arb.py`
2. **Taker Volume Divergence** (Signal 5B) — add to `liquidation_cascade.py` or new strategy
3. **Basis Z-Score** (Signal 6A) — upgrade existing basis code in `funding_arb.py`
4. **Cross-Pair Basis RV** (Signal 6B) — new strategy file

### Phase 2 (Requires OI storage mechanism)
5. **OI-Price Divergence** (Signal 2A) — new strategy, needs local OI time series
6. **OI Momentum Filter** (Signal 2B) — add as filter to existing trend strategies
7. **OI Concentration** (Signal 2C) — add to portfolio risk layer

### Phase 3 (Requires endpoint discovery or external data)
8. **Long/Short Ratio** (Signal 7A) — investigate AsterDex endpoint or add Binance fetcher
9. **Spread Volatility** (Signal 4B) — requires spread time series storage
10. **Inferred Liquidation** (Signal 3A) — enhanced version with 5m/15m candle analysis

### Data Storage Design for OI Tracking

Since AsterDex does not expose historical OI, we need to accumulate it ourselves:

```
Table: oi_snapshots
- timestamp (datetime, UTC)
- symbol (text, e.g. BTCUSDT)
- open_interest (float)
- price (float)
- volume_24h (float)

Retention: 90 days rolling
Insert frequency: Every trading cycle (4 hours)
Query patterns: Rolling 24h/3d/7d OI change, OI-price divergence
```

This can be stored in the existing SQLite database at `trading/db/trading.db`.

---

## Signal Decay and Monitoring

All alternative data signals lose their edge over time as more participants discover and exploit them. The following monitoring framework tracks signal health:

1. **Monthly hit rate tracking**: For each signal, compute rolling 30-day hit rate (did the predicted direction materialize within the holding period?)
2. **Alpha decay detection**: If a signal's hit rate drops below 52% for 60+ days, flag it for review
3. **Crowding indicator**: If multiple signals fire in the same direction on the same symbol simultaneously, the trade may be crowded — reduce size rather than increase it
4. **New signal discovery cadence**: Investigate at least one new data source per month (GitHub commit activity, whale wallet tracking via Etherscan, DeFiLlama TVL flows)

---

## Appendix: AsterDex Public Endpoints Reference

All endpoints below require NO authentication and are already wrapped in `trading/execution/aster_client.py`:

| Endpoint | Function | Data Available | TTL |
|----------|----------|---------------|-----|
| `/fapi/v3/premiumIndex` | `get_aster_mark_prices()` | markPrice, indexPrice, lastFundingRate, nextFundingTime | 60s |
| `/fapi/v3/fundingRate` | `get_aster_funding_rates()` | Historical funding rates, up to 1000 entries | 600s |
| `/fapi/v3/klines` | `get_aster_klines()` | OHLCV + taker_buy_base_vol + trade_count | 300s |
| `/fapi/v3/depth` | `get_aster_orderbook()` | Up to 1000 levels bid/ask | 60s |
| `/fapi/v3/ticker/24hr` | `get_aster_ticker_24h()` | 24h stats including volume, OI | 300s |
| `/fapi/v3/ticker/bookTicker` | `get_aster_book_ticker()` | Best bid/ask price and qty | 60s |
| `/fapi/v3/exchangeInfo` | `get_aster_exchange_info()` | Symbol specs, filters, contract details | 3600s |

### Endpoints NOT Yet Wrapped (investigate availability)

| Potential Endpoint | Binance Equivalent | Data |
|---|---|---|
| `/futures/data/globalLongShortAccountRatio` | Yes | Global long/short ratio |
| `/futures/data/topLongShortAccountRatio` | Yes | Top traders long/short |
| `/futures/data/openInterestHist` | Yes | Historical OI (would eliminate need for local storage) |
| `/futures/data/takerlongshortRatio` | Yes | Taker long/short ratio |

**Action item**: Test these Binance-style analytics endpoints against AsterDex. If they exist, Signal 7A and Signal 2A become significantly easier to implement.
