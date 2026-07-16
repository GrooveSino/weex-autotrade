# Trading Safety Contract

## Mutation Gates

Require all of the following:

1. A rendered dry-run showing mode, symbol, side, position side, type, quantity, price, TIF, client ID, TP, and SL.
2. An explicit execution flag.
3. An exact confirmation phrase derived from the normalized intent.
4. A separate environment opt-in for live trading.

## Submission Uncertainty

Use a unique client order ID for every intended submission. On timeout, disconnect, or network error:

1. Query open orders and history for that client ID.
2. Return a recovered result when found.
3. Otherwise report an uncertain outcome and stop.
4. Never resubmit automatically.

## Existing Exposure

Before opening, check existing positions for the symbol. In live mode, also check regular open orders and block by default when either exists. The documented Demo surface has no regular-open-order endpoint, so Demo can only precheck positions; review Demo order history separately before execution. Require an explicit override after account review. Allow reduce-only/close paths to bypass the opening guard.

Validate intent direction before submission: opening a long uses buy, opening a short uses sell, reducing a long uses sell, and reducing a short uses buy. For attached protection on limit entries, require long TP above and SL below entry, with the inverse geometry for shorts.

## Maker Semantics

`POST_ONLY` rejection is terminal for that attempt. Do not adjust price, switch to GTC, or use a market order.

## Stop Replacement

Submit the new full-position stop, verify it appears in open algorithm orders, then cancel the old stop. If verification fails, leave the old stop untouched and report uncertainty.
