# Security Policy

## Credential handling

- Never commit `.env`, API keys, signatures, passphrases, raw account payloads, balances, or order journals containing private identifiers.
- Use a dedicated WEEX API Key with only the required contract permissions.
- Bind the key to a stable egress IP and do not enable withdrawal permission.
- Rotate the key immediately if it appears in terminal history, logs, screenshots, issues, commits, or CI output.

## Reporting

This is a private repository. Report vulnerabilities through the repository's private GitHub Security Advisory interface. Do not open an issue containing secrets or exploitable account details.

## Live-trading changes

Changes affecting order submission, cancellation, position closure, leverage, margin, or risk orders must include fake-client tests and must preserve the dry-run, exact-confirmation, and live-environment gates.
