# Contributing

1. Keep all automated tests offline. Never use real API credentials in tests or CI.
2. Add a fake-client test for every new exchange mutation or recovery path.
3. Preserve the default Demo mode, exact confirmation phrases, and `WEEX_LIVE_TRADING_ENABLED` gate.
4. Never add automatic order retries or price-chasing fallbacks.
5. Run the full local gate before pushing:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=weex_cli
```
