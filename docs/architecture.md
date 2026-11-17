# Application architecture

## Control plane

`quant_gui.py` is the desktop control plane. It owns process launch/stop status, embeds local HTTP views, and provides the operational UI for pair configuration, orders, and service health. It is not a trading algorithm.

## Read model and UI API

`app.py` provides the Flask API consumed by the GUI and browser dashboards. It is responsible for pair metadata and point-in-time statistics, fixed-beta rolling-baseline publication and retrieval, worker snapshots, market-session state, and a restricted static-asset allowlist. The browser does not connect directly to MySQL or Redis.

## Workers

| Component | Responsibility | Primary output |
| --- | --- | --- |
| `livedata.py` | Collects live quotes and completed bars | Redis prices and bar history |
| `waiting_entry.py` | Evaluates entry conditions and schedules workflow | Redis waiting-entry snapshot / scheduled orders |
| `flagship_board.py` | Monitors active pair state and risk rules | Redis flagship snapshot / exit actions |
| `scheduler.pyw` | Coordinates scheduled local tasks | Process orchestration |

## Shared layer

`program_factory/` contains reusable operational code: market time handling, broker-port detection, live-bar conventions, pair lifecycle state, order scheduling, execution helpers, and trade-event logging. Its `pdt.py` module originated as a Pattern Day Trader (PDT) gate for U.S. sub-$25k accounts; that gate is currently disabled, while its `NEW`/`HOLD` lifecycle synchronization remains active.

## Configuration boundary

`runtime_settings.py` is the configuration entry point for the core control path. It reads environment variables and an optional local `.env` file. Credentials and account mappings must never be committed. `.env.example` documents required names without real values.

## Public-release boundary

Research/visualization modules can be presented as implementation artifacts. Broker connection, order placement, and runtime state should be described as architectural capabilities but remain disabled or separately reviewed for a public portfolio release.
