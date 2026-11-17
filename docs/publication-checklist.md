# Public-release checklist

1. Confirm `.env` is absent from staged files and rotate any credential that has ever appeared in source or logs.
2. Keep `logs/`, data exports, archives, screenshots containing account information, and generated model artifacts out of Git.
3. Exclude `Kaggle-Pairs-Trading/`, `Statistical-Arbitrage/`, `Statistical-Arbitrage-Model/`, and `IV-Surface-Prediction/`; they are reference material, not claimed original work.
4. Search staged content for passwords, tokens, account identifiers, email addresses, and real hostnames.
5. Review execution-facing files under `Action/` and `program_factory/` separately; public code must use environment configuration and must not embed account data.
6. Verify the README accurately distinguishes research, monitoring, and execution capability; do not claim live performance or profitability.
7. Keep local exit rules out of Git; publish only `program_factory/pair_exit_rules.example.json`.
