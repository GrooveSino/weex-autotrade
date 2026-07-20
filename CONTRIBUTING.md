# Contributing

1. Keep all automated tests offline. Never use real API credentials, account payloads, mnemonics, private keys, addresses
   from funded wallets, or private service URLs in tests and CI.
2. Add a fake-client test for every new exchange or chain mutation and every recovery path.
3. Preserve the default Demo/preview modes, exact confirmation phrases, and live execution environment gates.
4. Never add automatic mutation retries or price-chasing fallbacks. Read-only verification retries must remain bounded.
5. Use placeholders such as `EXAMPLE_ACCOUNT_ID`, `example.test`, and published test vectors in documentation.
6. Run the relevant local gates before pushing.

WEEX CLI:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=weex_cli
```

Control center:

```bash
cd control-center
npm ci
npm run lint
npm run build
cd server
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Aptos wallet:

```bash
cd aptos-wallet
pnpm install --frozen-lockfile
pnpm test
pnpm test:e2e
pnpm build
```

Before staging or publishing:

```bash
gitleaks git --redact --log-opts="--all" .
gitleaks git --redact --staged .
git status --short --ignored
```
