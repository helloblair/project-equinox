# Project Equinox

**Cross-Venue Prediction Market Aggregation & Routing Simulation**

Equinox connects to multiple prediction market venues (Polymarket, Kalshi), normalizes their data into a shared internal model, detects equivalent markets across platforms, and simulates intelligent routing decisions for hypothetical trades.

## Quick Start

```bash
# Clone and install
cd equinox
pip install -r requirements.txt

# Run the full pipeline
python -m src.pipeline

# Run the API server
uvicorn src.api.app:app --reload --port 8000

# Run tests (25 tests)
pytest tests/ -v
```

## Project Structure

```
equinox/
├── src/
│   ├── models/          # Canonical market model, routing decision model
│   │   ├── market.py    # CanonicalMarket, MarketPair
│   │   └── routing.py   # HypotheticalOrder, RoutingDecision
│   ├── venues/          # Venue-specific adapters (one per platform)
│   │   ├── base.py      # Abstract adapter interface
│   │   ├── polymarket.py
│   │   ├── kalshi.py
│   │   └── mock_data.py # Realistic mock data mirroring real API schemas
│   ├── matching/
│   │   └── engine.py    # Multi-stage equivalence detection
│   ├── routing/
│   │   └── engine.py    # Scoring-based venue selection with explanations
│   ├── api/
│   │   └── app.py       # FastAPI endpoints
│   └── pipeline.py      # Orchestrates fetch → match → route
├── tests/
│   └── test_equinox.py  # 25 tests covering all components
├── docs/
│   └── ARCHITECTURE.md  # Design decisions and tradeoffs
├── requirements.txt
└── README.md
```

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed design decisions, tradeoffs, and future work.

**Key principle:** Venue adapters are the only components that know about specific platforms. The matching engine and routing engine are completely venue-agnostic — adding a third venue requires only a new adapter.

## API Usage

```bash
# Run pipeline
curl -X POST "http://localhost:8000/pipeline/run?limit=50"

# Get matched markets
curl "http://localhost:8000/matches"

# Get routing decisions
curl "http://localhost:8000/routing"

# Fetch markets from one venue
curl "http://localhost:8000/markets/polymarket"
```

## Tech Stack

- **Python 3.12** — Core language
- **FastAPI** — API framework
- **httpx** — Async HTTP client for venue APIs
- **pytest** — Testing

No ML dependencies, no database, no infrastructure overhead. This is an intentional choice — the prototype validates the architecture, not the infra.

## AI Usage Disclosure

Claude (Anthropic) was used as a development assistant for code generation, architecture discussion, and documentation drafting. All design decisions, architectural tradeoffs, and equivalence/routing logic were reviewed and validated by the developer. The matching methodology (hybrid pipeline with Jaccard similarity, entity extraction, temporal filtering) and routing scoring weights were chosen through deliberate analysis, not generated blindly.
