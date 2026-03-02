# Position Arbitrage Strategy: First Principles Analysis

> Last updated: 2026-03-02
> Status: Living document — single source of truth for strategy design

---

## Table of Contents

1. [Mathematical Foundations](#1-mathematical-foundations)
2. [System Architecture](#2-system-architecture)
3. [Current Strategy: How It Works](#3-current-strategy-how-it-works)
4. [Performance Analysis: Why We Lose Money](#4-performance-analysis-why-we-lose-money)
5. [Root Cause Analysis](#5-root-cause-analysis)
6. [Proposed Solution: Taker-Hybrid Architecture](#6-proposed-solution-taker-hybrid-architecture)
7. [Implementation Plan](#7-implementation-plan)
8. [Appendix: Parameter Reference](#8-appendix-parameter-reference)

---

## 1. Mathematical Foundations

### 1.1 Binary Market Arbitrage Theorem

In Polymarket's binary UP/DOWN markets:

- **Two tokens**: UP and DOWN
- **Settlement**: Winner pays $1/share, loser pays $0/share
- **Exactly one side wins** — it's a complete probability space

**Theorem**: If you hold `n` shares of UP and `n` shares of DOWN, your settlement payout is **exactly `n × $1`**, regardless of outcome.

```
Proof:
  If UP wins:  payout = n × $1 (UP) + n × $0 (DOWN) = $n
  If DOWN wins: payout = n × $0 (UP) + n × $1 (DOWN) = $n
  ∴ Payout = $n in all cases  □
```

**The Arbitrage Condition**: If total cost to acquire `n` UP + `n` DOWN < `$n`, profit is guaranteed.

Define **ECR** (Effective Cost Rate):

```
ECR = total_cost / min(up_shares, down_shares)

If ECR < 1.0 → guaranteed profit = min(up, down) × (1 - ECR)
If ECR = 1.0 → break-even
If ECR > 1.0 → guaranteed loss (on hedged portion)
```

### 1.2 The Profit Source: Bid-Ask Spread

Market makers earn the bid-ask spread by providing liquidity:

```
Market mid-prices:  UP = $0.50, DOWN = $0.50  (sum = $1.00)
Maker buy limits:   UP = $0.48, DOWN = $0.48  (sum = $0.96)
```

The $0.04 discount per pair ($0.96 vs $1.00) is the maker's reward for waiting.

**Fee structure** (Polymarket crypto markets):
- Makers: **0% fee** (plus daily 20% rebate from taker fees)
- Takers: **~1.56% max** at 50¢, formula: `0.25 × (p × (1-p))²`

```
Taker fee examples:
  p = 0.50 → 1.5625%
  p = 0.45 → 1.53%
  p = 0.40 → 1.44%
  p = 0.30 → 1.10%
```

### 1.3 The Fill Risk Problem

The arbitrage requires BOTH sides to fill. Single-side fills create directional exposure:

```
Scenario: Buy UP@$0.48 (fills), Buy DOWN@$0.48 (doesn't fill)

  State: Holding 5.4 UP shares, cost = $2.59
  If UP wins:  payout = $5.40, PnL = +$2.81
  If DOWN wins: payout = $0.00, PnL = -$2.59
  
  Expected PnL = P(UP) × $2.81 + P(DOWN) × (-$2.59)
```

**Critical insight**: When one side fails to fill, the market has typically **trended away** from that side. If DOWN didn't fill, UP probably went up → P(DOWN wins) increased → our UP position is likely the losing side.

### 1.4 Break-Even Analysis

Let:
- `p` = probability of balanced fill (both sides)
- `W` = profit per balanced market
- `L` = loss per one-sided market

```
Break-even: p × W = (1-p) × |L|
  → p = |L| / (W + |L|)

With current data (W = $0.11, L = $0.42):
  → p = 0.42 / (0.11 + 0.42) = 79.2%

We need 79% balanced fill rate to break even.
Currently at 54%. This is the fundamental problem.
```

---

## 2. System Architecture

### 2.1 Component Diagram

```plantuml
@startuml component_diagram
skinparam componentStyle rectangle

package "Data Layer" {
  [MarketDataProvider] as MDP
  [WebSocket Client] as WS
  [RealDataFetcher] as RDF
  [Binance Feed] as BF
}

package "Strategy Layer" {
  [PositionArbitrageStrategy] as PAS
  [TrendDetector] as TD
  [BaseStrategy] as BS
  PAS --|> BS
  PAS --> TD
}

package "Execution Layer" {
  [SimulatedExecutor] as SE
  [LiveExecutor] as LE
  interface "OrderExecutor" as OE
  SE ..|> OE
  LE ..|> OE
}

package "Orchestration" {
  [TradingRunner] as TR
  [MarketContext] as MC
  [FillManager] as FM
}

MDP --> TR : market events
WS --> MDP : prices, orderbooks
RDF --> MDP : market discovery
BF --> PAS : directional signals

TR --> MC : price updates
MC --> PAS : on_price_update()
PAS --> MC : OrderSignal[]
TR --> OE : submit_order()
OE --> TR : FillEvent[]
TR --> MC : apply_fill()
TR --> FM : process_fills()

@enduml
```

### 2.2 Market Lifecycle

```plantuml
@startuml market_lifecycle
skinparam activityFontSize 12

|MarketDataProvider|
start
:Scan for new markets\n(HTTP, every 60s);
:Validate market\n- min_trading_time > 300s\n- max_entry_skew < 0.70\n- price_sum > 0.98;

|TradingRunner|
:Create MarketContext;
:Register with Executor;
:Subscribe WebSocket;
:strategy.on_market_start();

|Strategy|
repeat
  :Receive price update;
  if (exit phase?) then (yes)
    :Generate sell signals;
  elseif (abandon?) then (yes)
    :Generate abandon-sell signals;
  else (normal)
    :Calculate limit prices;
    :Generate buy signals;
  endif
  :Return OrderSignal[];
repeat while (market active?)

|TradingRunner|
:Settlement detected\n(WS or HTTP);
:Finalize market;
:Calculate PnL;
:Record results;
stop

@enduml
```

### 2.3 Data Flow

```plantuml
@startuml data_flow
participant "Polymarket\nCLOB" as PM
participant "WebSocket" as WS
participant "MarketData\nProvider" as MDP
participant "Trading\nRunner" as TR
participant "Market\nContext" as MC
participant "Strategy" as S
participant "Executor" as E

== Discovery ==
TR -> MDP: scan markets
MDP -> PM: HTTP GET /events
MDP -> TR: on_market_discovered(slug)
TR -> MC: new MarketContext
TR -> WS: subscribe(token_ids)

== Trading Loop ==
loop every price tick
  PM -> WS: token_price update
  WS -> MDP: on_ws_message
  MDP -> TR: on_price_update(price_data)
  TR -> MC: process_price_update(price)
  MC -> S: on_price_update(price_data)
  S --> MC: OrderSignal[]
  
  loop for each signal
    TR -> E: submit_order(execution_order)
    E --> TR: OrderResult
    alt filled immediately
      TR -> MC: apply_fill(side, size, price)
    else pending
      TR -> TR: track pending order
    end
  end
  
  TR -> E: check_fills()
  E --> TR: FillEvent[]
  loop for each fill
    TR -> MC: apply_fill/apply_sell
  end
end

== Settlement ==
PM -> WS: market_resolved(winner)
WS -> TR: on_settlement(slug, winner)
TR -> MC: finalize(winner)
TR -> TR: record_result()
@enduml
```

---

## 3. Current Strategy: How It Works

### 3.1 Order Pricing (Pseudocode)

```python
def calculate_limit_prices(up_price, down_price) -> (up_limit, down_limit):
    price_sum = up_price + down_price
    effective_target = get_adaptive_target()  # 0.94 to 0.955
    
    if price_sum <= effective_target:
        return (up_price, down_price)  # already below target
    
    # Step 1: Base allocation (proportional scaling)
    if max(up_price, down_price) > trend_patience_threshold:  # 0.57
        # Asymmetric: trending side gets small discount, other absorbs rest
        dominant_limit = dominant_price * (1 - 0.005)  # 0.5% below market
        minority_limit = effective_target - dominant_limit
    else:
        # Symmetric: same % discount on both sides
        scale = effective_target / price_sum
        up_limit = up_price * scale
        down_limit = down_price * scale
    
    # Step 2: Urgency adjustment (move limits closer to market as time passes)
    urgency = calculate_urgency(time_elapsed, imbalance)
    offset_reduction = urgency * urgency_price_factor  # max 0.25
    up_limit += (up_price - up_limit) * offset_reduction
    down_limit += (down_price - down_limit) * offset_reduction
    
    # Step 3: Floor (minimum viable price)
    up_limit = max(up_limit, 0.05)
    down_limit = max(down_limit, 0.05)
    
    # Step 4: HARD CEILING (pair cost must not exceed target)
    if up_limit + down_limit > effective_target:
        scale = effective_target / (up_limit + down_limit)
        up_limit *= scale
        down_limit *= scale
    
    # Step 5: ECR counterpart cap (when imbalanced)
    if balance_ratio < 0.85:
        # Cap limit so pair with historical avg doesn't exceed 1.10
        ...
    
    # Step 6: Minimum maker discount (stay 1.5%+ below market)
    up_limit = min(up_limit, up_price * (1 - min_maker_discount))
    down_limit = min(down_limit, down_price * (1 - min_maker_discount))
    
    # Step 7: FINAL HARD CEILING (re-enforce after all adjustments)
    if up_limit + down_limit > effective_target:
        scale = effective_target / (up_limit + down_limit)
        up_limit *= scale
        down_limit *= scale
    
    return (up_limit, down_limit)
```

### 3.2 Position Building Flow

```plantuml
@startuml position_building
skinparam activityFontSize 11

start
:Price update received;

if (exit phase?\nremaining <= 180s) then (yes)
  :Sell positions;
  stop
endif

if (abandon conditions met?) then (yes)
  :Set abandon mode;
  :Sell at market - 0.5%;
  stop
endif

:Calculate limit prices;
:Check ECR recovery mode;

if (ECR > threshold AND imbalanced?) then (yes)
  :Restrict to minority side only;
endif

:Check hedge-complete gate;
note right
  fills_est >= 1.5 AND balanced AND ECR <= 1.0
  → STOP (lock in profit)
  
  fills_est >= 2.5
  → STOP (recovery cap)
  
  fills_est >= 0.8 AND balance < 0.40
  → STOP (imbalanced hard cap)
end note

if (gate triggered?) then (yes)
  stop
endif

:Generate order signals;
note right
  max_pending_per_side = 1
  Sequential ordering:
  1. Lagging side first
  2. Leading side only if gap < threshold
end note

:Submit to executor;
stop

@enduml
```

### 3.3 Abandon Decision Tree

```plantuml
@startuml abandon_logic
start
:Evaluate abandon conditions;

if (one-sided AND cost > 4×batch\nAND time > 12%?) then (yes)
  :ABANDON (large one-sided);
  stop
endif

if (ECR == inf?) then (yes)
  :Skip (wait for data);
  stop
endif

if (ECR > 3.0 AND balance < 0.10\nAND time > 30%?) then (yes)
  :ABANDON (extreme imbalance);
  stop
endif

if (ECR > 1.20 AND balance < 0.45\nAND time > 15%?) then (yes)
  :ABANDON (medium imbalance);
  stop
endif

if (ECR > 1.02 AND balance < 0.30\nAND time > 50%?) then (yes)
  :ABANDON (normal);
  stop
endif

:Continue trading;
stop

@enduml
```

---

## 4. Performance Analysis: Why We Lose Money

### 4.1 Production Data (1698 Markets)

| Category | Count | % | Total PnL | Avg PnL/mkt |
|----------|-------|---|-----------|-------------|
| **Balanced** (both sides filled, bal > 0.85) | 1038 | 61% | -$3.80 | **-$0.004** |
| **Non-balanced** (one-sided or imbalanced) | 660 | 39% | -$341.54 | **-$0.517** |
| **Total** | 1698 | 100% | **-$345.34** | **-$0.203** |

### 4.2 Non-Balanced Breakdown by Fill Count

| Fills | Count | Avg PnL | Description |
|-------|-------|---------|-------------|
| 0 | 281 | $0.000 | Never entered (no fills at all) |
| 1 | 70 | -$0.772 | One side only |
| 2 | 51 | -$0.803 | Two fills but imbalanced |
| 3 | 69 | -$1.019 | Three fills, asymmetric |
| 4 | 148 | -$0.872 | Four fills but imbalanced |
| 5+ | 41 | -$0.949 | Many fills, still imbalanced |

### 4.3 Abandon-Sell vs Hold

| Exit Method | Count | Avg PnL |
|-------------|-------|---------|
| Abandon-sell (sell before settlement) | 351 | **-$0.885** |
| Hold to settlement (no sell) | 309 | **-$0.100** |

**Key finding**: Abandon-sell loses 8.85× more per market than holding. The sell mechanism destroys value because it sells at a discount during an unfavorable price movement.

### 4.4 ECR Distribution (Balanced Markets Only)

```
ECR < 1.0 (profitable):  535/1038 = 52%
ECR >= 1.0 (losing):     503/1038 = 48%

Median ECR: 0.9957
```

**48% of "balanced" markets have ECR ≥ 1.0** — they're losing money even with both sides filled. The target cost (0.96) is being eroded by urgency pricing, fill asymmetry, and cross-temporal drift.

### 4.5 Per-Coin Performance

| Coin | Markets | Balanced Rate | Total PnL | Avg PnL |
|------|---------|--------------|-----------|---------|
| BTC | 569 | 63% | -$116.80 | -$0.205 |
| ETH | 569 | 61% | -$120.40 | -$0.212 |
| SOL | 560 | 60% | -$108.13 | -$0.193 |

All three coins lose money at similar rates. The problem is structural, not coin-specific.

### 4.6 Recent Post-Hotfix Performance (48 Markets)

After parameter tightening (fills limited to 1-2 per market):

| Category | Count | Avg PnL |
|----------|-------|---------|
| Balanced (fills=2) | 26 (54%) | **+$0.112** |
| One-sided with abandon-sell | 21 (44%) | **-$0.418** |

Balanced markets are consistently profitable when ECR is controlled. But one-sided losses wipe out the gains.

---

## 5. Root Cause Analysis

### 5.1 Problem Taxonomy

```plantuml
@startuml root_cause
skinparam rectangleFontSize 12

rectangle "Strategy Loses Money\n-$0.203/market" as ROOT #ff6666

rectangle "Problem 1: One-Sided Fills\n39% of markets, -$0.517/mkt" as P1 #ffaaaa
rectangle "Problem 2: ECR Erosion\n48% of balanced have ECR≥1.0" as P2 #ffaaaa
rectangle "Problem 3: Abandon-Sell\nDestroys value vs holding" as P3 #ffaaaa

rectangle "Cause 1a: Market trends\nduring fill window" as C1A #ffffaa
rectangle "Cause 1b: Hard ceiling forces\nminority limit too low" as C1B #ffffaa
rectangle "Cause 2a: Urgency pricing\npushes limits near market" as C2A #ffffaa
rectangle "Cause 2b: Cross-temporal\nprice drift across pairs" as C2B #ffffaa
rectangle "Cause 3a: Sells at 0.5% discount\nduring adverse movement" as C3A #ffffaa
rectangle "Cause 3b: Triggers too early\n(12% of market duration)" as C3B #ffffaa

ROOT --> P1
ROOT --> P2
ROOT --> P3

P1 --> C1A
P1 --> C1B
P2 --> C2A
P2 --> C2B
P3 --> C3A
P3 --> C3B

@enduml
```

### 5.2 The Fundamental Issue: Maker-Only Doesn't Work

The current strategy uses **maker-only orders** (limit orders below market). This creates an inherent vulnerability:

```
Time T=0:   UP=0.50, DOWN=0.50
            Place: BUY UP@0.48, BUY DOWN@0.48

Time T=10s: Market trends → UP=0.55, DOWN=0.45
            DOWN@0.48 fills (above market, sellers hit our bid)
            UP@0.48 is 12.7% below market → won't fill

Time T=60s: Market continues → UP=0.60, DOWN=0.40
            Hard ceiling: UP_limit = 0.955 - 0.40×0.97 = 0.567
            But UP@0.567 is 5.5% below market → still hard to fill

Time T=108s: Abandon fires → sell DOWN at 0.40×0.995 = $0.398
             Cost was $2.59, recover $2.15, loss = -$0.44
```

**The paradox**: The side that fills easily is the one trending AGAINST us. The side we need (to complete the hedge) is trending AWAY, making it increasingly expensive.

### 5.3 Why Maker-Only Has a Hard Ceiling on Fill Rate

In a 50/50 market with 4% total discount:
- Each limit is ~2% below market
- In a trending market, one side quickly moves beyond 2%
- With min_maker_discount=1.5%, the limit is at most 1.5% below market
- A 2% market move (common in 10s for crypto) puts the limit out of reach

The **theoretical maximum balanced fill rate** for pure maker orders depends on market volatility:
- Low volatility: ~80% balanced (most fills complete before market moves)
- Medium volatility: ~55% balanced (current reality)
- High volatility: ~30% balanced (rapid trends invalidate limits)

**Conclusion**: With crypto volatility, maker-only strategy cannot reach the 79% balanced rate needed for profitability.

---

## 6. Proposed Solution: Taker-Hybrid Architecture

### 6.1 Core Concept

**Never hold a one-sided position.** When one side fills as maker, immediately complete the hedge using a taker order if profitable, or scratch the position if not.

```plantuml
@startuml taker_hybrid
skinparam activityFontSize 11

start
:Place maker BUY on BOTH sides\n(UP@limit, DOWN@limit);

fork
  :Wait for fills (up to 30s);
fork again
  :Monitor market prices;
end fork

if (Both sides filled?) then (yes)
  :Perfect hedge ✓\nECR = maker pair cost\nProfit guaranteed;
  #palegreen:HOLD to settlement;
  stop
elseif (One side filled?) then (one fill)
  :Calculate taker completion cost;
  note right
    taker_price = market × (1 + slippage)
    taker_fee = 0.25 × (p×(1-p))²
    pair_cost = maker_fill + taker_cost
  end note
  
  if (pair_cost < max_pair_cost?) then (yes)
    :TAKER BUY other side;
    #palegreen:Hedge complete ✓\nLower profit but guaranteed;
    stop
  else (too expensive)
    :SCRATCH: Sell filled side;
    note right
      Loss ≈ spread + taker fee
      Typically -$0.05 to -$0.15
      Much better than -$0.42 (one-sided hold)
    end note
    #lightyellow:Small loss, try next market;
    stop
  endif
else (neither filled)
  :Retry with tighter limits\nor skip market;
  stop
endif

@enduml
```

### 6.2 Economics Model

**Notation**:
- `n` = shares per side (e.g., 5.4)
- `p_m` = maker fill price (e.g., 0.48)
- `p_t` = taker fill price (e.g., market price)
- `f_t` = taker fee rate (~1.56% at 50¢)
- `s` = bid-ask spread (~1-2%)

#### Case A: Both Maker Fill (Best Case)

```
Probability: ~54% (current data)
Cost:    n × (p_m_up + p_m_down) = 5.4 × 0.96 = $5.18
Payout:  n × $1 = $5.40
PnL:     +$0.22
```

#### Case B: Maker + Taker Completion

```
Probability: ~30% (estimated — currently these become one-sided)
Maker fill: n shares at $p_m, cost = 5.4 × $0.48 = $2.59
Taker fill: n shares at $p_t, cost = 5.4 × $0.50 = $2.70
  (taker receives 5.4 × (1 - 0.0156) = 5.316 shares)

Total cost:  $5.29
If side A wins: payout = n × $1 = $5.40, PnL = +$0.11
If side B wins: payout = 5.316 × $1 = $5.32, PnL = +$0.03
Expected PnL: ~+$0.07
```

#### Case C: Scratch Trade

```
Probability: ~16% (market moved too far for profitable taker completion)
Maker fill: bought 5.4 shares at $0.48, cost = $2.59
Scratch sell: 5.4 shares at market (e.g., $0.46), taker fee ~1.5%
  Proceeds = 5.4 × $0.46 × (1 - 0.015) = $2.45
Loss: $2.59 - $2.45 = -$0.14
```

#### Expected Value

```
EV = 0.54 × $0.22 + 0.30 × $0.07 + 0.16 × (-$0.14)
   = $0.119 + $0.021 - $0.022
   = +$0.118 per market

Daily (840 markets): +$99/day
Monthly: +$2,970/month
```

vs current: **-$0.203/market = -$170/day = -$5,100/month**

### 6.3 Decision Logic (Pseudocode)

```python
# Maximum pair cost for taker completion.
# Must be < 1.0 for guaranteed profit. Budget for fees.
MAX_TAKER_PAIR_COST = 0.99  # 1% margin after fees

# Time window to wait for maker fill before attempting taker
MAKER_WINDOW_SECONDS = 30

# Maximum acceptable scratch loss per market (USD)
MAX_SCRATCH_LOSS = 0.20

def on_first_fill(filled_side, fill_price, fill_size):
    """Called when one side fills as maker."""
    
    other_side = opposite(filled_side)
    other_market_price = get_market_price(other_side)
    
    # Calculate taker completion cost
    taker_fee_rate = 0.25 * (other_market_price * (1 - other_market_price)) ** 2
    taker_effective_cost = other_market_price / (1 - taker_fee_rate)
    pair_cost = fill_price + taker_effective_cost
    
    if pair_cost < MAX_TAKER_PAIR_COST:
        # Profitable completion — execute immediately
        submit_taker_buy(other_side, fill_size, other_market_price)
        log(f"TAKER COMPLETE: {other_side} @{other_market_price}, "
            f"pair_cost={pair_cost:.4f}, guaranteed_profit={1-pair_cost:.4f}")
    else:
        # Too expensive — wait for maker fill or scratch
        start_timer(MAKER_WINDOW_SECONDS, on_maker_timeout)


def on_maker_timeout():
    """Called when maker fill window expires without second fill."""

    other_market_price = get_market_price(other_side)
    taker_fee_rate = 0.25 * (other_market_price * (1 - other_market_price)) ** 2
    taker_effective_cost = other_market_price / (1 - taker_fee_rate)
    pair_cost = fill_price + taker_effective_cost
    
    if pair_cost < MAX_TAKER_PAIR_COST:
        # Market returned — taker complete is now profitable
        submit_taker_buy(other_side, fill_size, other_market_price)
    else:
        # Scratch the position
        submit_taker_sell(filled_side, fill_size, get_market_price(filled_side))
        log(f"SCRATCH: sold {filled_side} @market, "
            f"loss={calculate_scratch_loss():.2f}")


def on_second_maker_fill(side, fill_price, fill_size):
    """Both sides filled as maker — perfect hedge."""
    cancel_timer()
    log(f"PERFECT HEDGE: ECR={calculate_ecr():.4f}, "
        f"guaranteed_profit={min(up_shares,down_shares)*(1-ecr):.2f}")
    # Hold to settlement — no further action needed
```

### 6.4 Sequence Diagram

```plantuml
@startuml taker_hybrid_sequence
skinparam sequenceFontSize 11

participant Strategy as S
participant Executor as E
participant "Polymarket\nCLOB" as PM

== Phase 1: Place Maker Orders ==
S -> E: submit_order(BUY UP@0.48, maker)
S -> E: submit_order(BUY DOWN@0.48, maker)
E -> PM: POST /order (GTC)
E -> PM: POST /order (GTC)

== Phase 2: Monitor Fills ==
PM --> E: UP@0.48 FILLED (maker)
E --> S: FillEvent(UP, 0.48, maker)

note over S: First fill! Start taker assessment

S -> S: Calculate taker pair cost\nDOWN market=0.50\ntaker_cost=0.508\npair=0.988 < 0.99 ✓

alt Taker completion profitable
  S -> E: submit_order(BUY DOWN@market, taker/FOK)
  E -> PM: POST /order (FOK)
  PM --> E: DOWN@0.50 FILLED (taker)
  E --> S: FillEvent(DOWN, 0.50, taker)
  note over S #palegreen: Hedge complete!\nECR = 0.988\nMin profit = +$0.03
  
else Taker too expensive (pair > 0.99)
  note over S: Wait 30s for maker fill...
  
  alt Maker fills within window
    PM --> E: DOWN@0.48 FILLED (maker)
    E --> S: FillEvent(DOWN, 0.48, maker)
    note over S #palegreen: Perfect hedge!\nECR = 0.96\nProfit = +$0.22
    
  else Timeout: scratch trade
    S -> E: submit_order(SELL UP@market, taker)
    E -> PM: POST /order (FOK SELL)
    PM --> E: UP SOLD@0.47
    note over S #lightyellow: Scratch loss: -$0.14\n(much better than -$0.42)
  end
end

== Phase 3: Hold to Settlement ==
PM --> E: market_resolved(winner=UP)
note over S: Settlement payout received

@enduml
```

### 6.5 Comparison: Current vs Proposed

```plantuml
@startuml comparison
skinparam legendFontSize 11

legend
  |= Metric |= Current (Maker-Only) |= Proposed (Taker-Hybrid) |
  | Balanced rate | 54% | ~84% (maker+taker) |
  | One-sided rate | 46% | 0% (scratch instead) |
  | Avg balanced PnL | +$0.11 | +$0.17 (weighted) |
  | Avg one-sided PnL | -$0.42 | N/A |
  | Avg scratch PnL | N/A | -$0.14 |
  | **Expected PnL/mkt** | **-$0.13** | **+$0.12** |
  | **Daily PnL (840 mkts)** | **-$109** | **+$99** |
end legend

@enduml
```

### 6.6 Risk Analysis

**What could go wrong?**

| Risk | Impact | Mitigation |
|------|--------|------------|
| Taker fill slippage | Higher completion cost | Use FOK orders with price limit |
| Scratch sell slippage | Higher scratch loss | Set max_scratch_loss cap |
| Both fills fail | No position (zero PnL) | Retry or skip — no directional risk |
| Taker fees increase | Lower margin | Monitor fee rates, adjust thresholds |
| High volatility periods | More scratches | Widen maker discount in volatile markets |
| API latency | Late taker execution | Use WebSocket for instant fill detection |

**Key invariant**: The strategy NEVER holds an unhedged position longer than `MAKER_WINDOW_SECONDS` (30s). If the hedge cannot be completed profitably, the position is immediately scratched.

---

## 7. Implementation Plan

### 7.1 Code Changes Required

```plantuml
@startuml implementation_plan
skinparam rectangleFontSize 11

rectangle "Phase 1: Core Logic" as P1 #lightblue {
  rectangle "1.1 Add taker completion logic\nto on_price_update()" as T11
  rectangle "1.2 Add scratch trade logic\n(sell-back mechanism)" as T12
  rectangle "1.3 Add fill state tracking\n(first_fill_time, first_fill_side)" as T13
}

rectangle "Phase 2: Execution" as P2 #lightgreen {
  rectangle "2.1 Support FOK taker buy\nin SimulatedExecutor" as T21
  rectangle "2.2 Support FOK taker buy\nin LiveExecutor" as T22
  rectangle "2.3 Taker fee accounting\nin MarketContext" as T23
}

rectangle "Phase 3: Simplification" as P3 #lightyellow {
  rectangle "3.1 Remove abandon-sell logic\n(replaced by scratch)" as T31
  rectangle "3.2 Remove urgency pricing\n(no longer needed)" as T32
  rectangle "3.3 Remove rebalancing\n(positions always balanced)" as T33
  rectangle "3.4 Remove multi-pair logic\n(1 pair per market)" as T34
}

rectangle "Phase 4: Config" as P4 #lightcoral {
  rectangle "4.1 New parameters:\nmax_taker_pair_cost\nmaker_window_seconds\nmax_scratch_loss" as T41
  rectangle "4.2 Remove obsolete params:\nurgency, rebalancing, abandon" as T42
}

P1 -down-> P2
P2 -down-> P3
P3 -down-> P4
@enduml
```

### 7.2 Simplified Strategy (Target State)

The taker-hybrid approach dramatically simplifies the strategy:

**Current complexity** (~2800 lines):
- Urgency pricing
- Fill-rate EMA tracking
- Rebalancing (buy + sell)
- Abandon detection (4 conditions)
- Abandon-sell generation
- ECR recovery mode
- Directional recovery
- Multi-pair hedge-complete gate
- Sequential ordering
- Counterpart ECR cap
- Phase-based behavior (3 phases)
- Exit-phase selling

**Target simplicity** (~800 lines):
- Place maker orders on both sides
- Monitor fills
- On first fill: assess taker completion
- Execute taker or scratch
- Hold balanced position to settlement

```python
# Simplified on_price_update (target state)
def on_price_update(self, price_data: PriceData) -> List[OrderSignal]:
    signals = []
    up_price = price_data.up_price
    down_price = price_data.down_price
    
    # State: NO_POSITION → FIRST_FILL → HEDGED
    
    if self._state == "NO_POSITION":
        # Place maker orders on both sides
        up_limit, down_limit = self._calculate_limits(up_price, down_price)
        signals.extend(self._place_maker_orders(up_limit, down_limit))
        
    elif self._state == "FIRST_FILL":
        elapsed = time.time() - self._first_fill_time
        other_side = opposite(self._first_fill_side)
        other_price = price_data[other_side]
        
        # Can we complete the hedge as taker?
        pair_cost = self._first_fill_price + other_price * (1 + self._taker_fee(other_price))
        
        if pair_cost < self.max_taker_pair_cost:
            # Yes — execute taker completion
            signals.append(self._taker_buy(other_side, other_price))
            self._state = "HEDGED"
            
        elif elapsed > self.maker_window_seconds:
            # Timeout — scratch the position
            signals.append(self._scratch_sell(self._first_fill_side))
            self._state = "SCRATCHED"
            
        else:
            # Still waiting for maker fill on other side
            signals.extend(self._refresh_maker_order(other_side, other_price))
    
    elif self._state == "HEDGED":
        pass  # Hold to settlement
    
    return signals
```

### 7.3 Migration Strategy

1. **Keep the current strategy as-is** (rename to `PositionArbitrageV1`)
2. **Build the new strategy in a new file** (`position_arbitrage_v2.py`)
3. **Run both in parallel** (V1 on paper, V2 on paper)
4. **Compare results** over 24-48 hours
5. **Switch to V2** once validated

---

## 8. Appendix: Parameter Reference

### 8.1 Current Parameters (strategy_defaults.yaml)

| Section | Parameter | Value | Purpose |
|---------|-----------|-------|---------|
| strategy | target_cost | 0.96 | Base pair cost target |
| strategy | adaptive_target_max | 0.955 | Max adaptive target |
| strategy | adaptive_target_min | 0.94 | Min adaptive target |
| strategy | min_maker_discount | 0.03 | Min distance below market |
| strategy | batch_ratio | 0.001 | Order size fraction |
| strategy | min_order_shares | 5 | Polymarket minimum |
| strategy | position_size | 100.0 | Max per-market |
| strategy | order_timeout | 60 | Cancel unfilled orders |
| risk | ecr_threshold | 1.05 | ECR stop-loss |
| risk | max_skew_threshold | 0.85 | Skip skewed markets |
| risk | trend_patience_threshold | 0.57 | Asymmetric pricing trigger |
| stop_loss | abandon_ecr_threshold | 1.02 | Abandon trigger |
| simulation | max_entry_skew | 0.70 | Entry filter |
| simulation | min_trading_time | 300 | Min time to enter |
| fund | max_concurrent_markets | 6 | Concurrent limit |
| fund | max_total_exposure | 500 | Total exposure cap |

### 8.2 Proposed New Parameters (V2)

| Parameter | Proposed Value | Purpose |
|-----------|---------------|---------|
| max_taker_pair_cost | 0.99 | Max pair cost for taker completion |
| maker_window_seconds | 30 | Wait for maker before scratch |
| max_scratch_loss_pct | 0.05 | Max acceptable scratch loss |
| taker_slippage_buffer | 0.005 | Price buffer for taker orders |
| enable_taker_completion | true | Master switch for taker logic |
| min_maker_discount | 0.015 | Tighter for V2 (was 0.03) |

### 8.3 Polymarket Fee Reference

| Fee Type | Rate | Applied To |
|----------|------|-----------|
| Maker buy | 0% | Limit order that provides liquidity |
| Taker buy | ~1.56% max | Aggressive order that takes liquidity |
| Maker sell | 0% | Limit sell that provides liquidity |
| Taker sell | ~1.56% max | Market sell that takes liquidity |
| Maker rebate | ~20% of taker fees | Daily distribution |

Fee formula: `fee_rate = 0.25 × (price × (1 - price))²`

---

## Document History

| Date | Change |
|------|--------|
| 2026-03-02 | Initial creation. First-principles analysis based on 1698 markets of production data. |
