# Public repository scope

The public repository should present East Indies Demo as a systems project, not as an exposed live-trading installation.

## Include after final audit

- `quant_gui.py`, `app.py`, dashboard assets, and `runtime_settings.py`: the control plane and API boundary.
- `livedata.py`, `waiting_entry.py`, `flagship_board.py`, `scheduler.pyw`: the observable worker pipeline, after their final configuration audit.
- `program_factory/` and `Action/`: only the original modules that have been migrated away from embedded credentials and account identifiers.
- `docs/`, `README.md`, `.env.example`, and dependency declarations.

## Exclude

- Third-party or learning copies. The prior `Kaggle-Pairs-Trading`, `Statistical-Arbitrage`, `Statistical-Arbitrage-Model`, and `IV-Surface-Prediction` directories have been removed rather than retained as ignored content.
- Live logs, screenshots containing client/account data, local caches, data exports, archives, virtual environments, and generated model artifacts.
- The active `program_factory/pair_exit_rules.json` file and all notebooks under `program_factory/`. The repository supplies `pair_exit_rules.example.json` instead.
- Exploratory notebooks that contain account responses, names, or unreviewed external content.

## Review order

1. The GUI/API control path: configuration, ports, static assets, and documentation.
2. The worker path: data collection, signals, portfolio monitoring, and scheduler.
3. Execution utilities: remove account values and ensure a public clone is safe by default.
4. Research modules: retain only modules you can accurately describe as your own implementation or documented adaptation.

Until step 3 is complete, do not publish this directory as a public repository.
