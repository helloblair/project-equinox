# Project Equinox — Architecture Overview

## System Purpose

Equinox is a prototype that answers a core feasibility question: **can we programmatically identify equivalent prediction markets across venues and make intelligent routing decisions between them?**

The answer is yes, with documented tradeoffs.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    FastAPI Server                     │
│                   (src/api/app.py)                    │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│                  Pipeline Orchestrator                │
│                  (src/pipeline.py)                    │
│                                                      │
│   Stage 1: FETCH ──→ Stage 2: MATCH ──→ Stage 3: ROUTE │
└───┬──────────┬──────────┬──────────────┬────────────┘
    │          │          │              │
    ▼          ▼          ▼              ▼
┌────────┐ ┌────────┐ ┌──────────┐ ┌──────────┐
│Poly-   │ │Kalshi  │ │Equiv.    │ │Routing   │
│market  │ │Adapter │ │Engine    │ │Engine    │
│Adapter │ │        │ │          │ │          │
└───┬────┘ └───┬────┘ └──────────┘ └──────────┘
    │          │
    ▼          ▼
┌─────────────────────┐
│  CanonicalMarket    │  ← Venue-agnostic model
│  (src/models/)      │
└─────────────────────┘
```

## Layer Separation (Key Design Decision)

The system has **four clean layers**, each with a single responsibility:

| Layer | Responsibility | Knows about venues? |
|-------|---------------|-------------------|
| **Venue Adapters** | API integration, normalization | Yes (one per venue) |
| **Canonical Model** | Shared data contract | No |
| **Matching Engine** | Equivalence detection | No |
| **Routing Engine** | Venue selection + explanation | No |

This means **adding a third venue** (e.g., Metaculus, PredictIt) requires only writing a new adapter — matching and routing work unchanged.

---

## Key Design Decisions

### 1. Canonical Market Model

**Decision:** Define a single `CanonicalMarket` dataclass that every venue normalizes into.

**Why:** The alternative — having matching and routing handle venue-specific schemas — creates O(n²) coupling as venues are added. A shared model keeps it O(n).

**Tradeoff:** Some venue-specific information is lost in normalization. We preserve the `raw_data` field for audit, but the matching engine only sees normalized fields. If a venue has a unique feature (e.g., Kalshi's series ticker hierarchy), it must be mapped into a generic field or ignored.

### 2. Equivalence Detection: Hybrid Pipeline

**Decision:** Use a multi-stage pipeline: category filter → temporal filter → text similarity (Jaccard) → entity extraction → weighted confidence score.

**Why each stage exists:**
- **Category filter:** Prunes the search space from O(n×m) to O(n×m/k) where k = number of categories. A crypto market should never be compared to a sports market.
- **Temporal filter:** Markets about "Bitcoin > $100k by March 2026" and "Bitcoin > $100k by December 2026" are different events despite similar titles.
- **Jaccard text similarity:** Simple, interpretable, and effective for prediction market titles which share key terms. No ML dependencies.
- **Entity extraction:** Catches cases where titles differ but reference the same numbers/names ("$150,000" vs "$150K" vs "150000").
- **Weighted scoring:** Allows tuning the relative importance of each signal.

**What I'd add with more time:**
- Sentence embeddings (e.g., `all-MiniLM-L6-v2`) for semantic similarity when titles are phrased very differently
- LLM-assisted disambiguation for ambiguous cases
- Feedback loop: human-confirmed matches used to tune thresholds

### 3. Routing: Explainable Scoring

**Decision:** Score each venue on three axes (price, liquidity, spread), weight them, and generate a human-readable explanation.

**Why:** The spec explicitly values reasoning over optimization. A black-box "route to venue A" is less useful than "route to venue A because it offers a 3% cheaper YES price and has 2x the trading volume."

**Scoring details:**
- **Price score (weight 0.5):** For a BUY_YES order, lower yes_price = better. Inverted to 0–1 scale.
- **Liquidity score (weight 0.3):** Log-scaled volume. Log because volume differences can be 1000x.
- **Spread score (weight 0.2):** Tighter spread = more efficient market = lower execution cost.

**Tradeoff:** These weights are hardcoded. In production, they'd be tuned per-market-category (politics markets have different liquidity profiles than crypto) and per-order-size (large orders care more about liquidity than price).

### 4. Mock Data with Live API Architecture

**Decision:** Write adapters against real APIs, fall back to mock data when APIs are unrestricted.

**Why:** The codebase is ready for production API integration — the mock fallback is a demo convenience, not a design choice. Mock data mirrors the exact JSON schemas of Polymarket's Gamma API and Kalshi's REST API.

---

## What "Equivalent" Means (Required by Spec)

Two markets are considered equivalent when they:

1. **Same category** — both must be in the same normalized category (crypto, politics, economics, etc.)
2. **Similar timeframe** — expiration dates within 7 days of each other (configurable)
3. **Textual overlap** — Jaccard similarity of tokenized titles above threshold, with stopwords removed
4. **Shared entities** — key terms (numbers, proper nouns) overlap between titles
5. **Confidence above threshold** — weighted combination of all signals exceeds minimum confidence (default 0.4)

Confidence is explicitly a probability estimate, not a binary yes/no. A 0.85 confidence match between "Will the Fed cut rates at the March 2026 meeting?" and "Fed to cut rates at March 2026 meeting?" means we're highly confident these are the same event. A 0.45 confidence match is flagged but should be human-reviewed.

---

## Testing Strategy

- **Unit tests:** Each component (models, normalization, matching, routing) tested in isolation
- **Property tests:** Invariants like "spread = yes + no - 1.0" always hold
- **Integration test:** Full pipeline runs end-to-end and validates output structure
- **25 tests, all passing**

---

## Future Work (Given More Time)

| Feature | Why | Complexity |
|---------|-----|-----------|
| Embedding-based matching | Catches semantic equivalence that keyword matching misses | Medium |
| Real-time price streaming | WebSocket connections for live price updates | Medium |
| Arbitrage detection | Flag matched pairs where price divergence > threshold | Low |
| React dashboard | Visual comparison of matched markets | Medium |
| Historical divergence tracking | How do cross-venue prices converge over time? | High |
| Confidence calibration | Use confirmed matches to tune scoring weights | High |
| Additional venues | Metaculus, Manifold, PredictIt | Low per venue |

---

## Running the System

```bash
# Install
pip install -r requirements.txt

# Run pipeline (CLI)
python -m src.pipeline

# Run API server
uvicorn src.api.app:app --reload

# Run tests
pytest tests/ -v
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health check |
| POST | `/pipeline/run?limit=50` | Execute full pipeline |
| GET | `/markets/{venue}` | Fetch normalized markets from one venue |
| GET | `/matches` | Get matched pairs from last run |
| GET | `/routing` | Get routing decisions from last run |
