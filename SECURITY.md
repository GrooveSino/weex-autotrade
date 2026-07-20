# Security Policy

## Supported code

Security fixes are applied to the current `main` branch. This repository controls financial actions and local signing keys;
all reports should assume that credential exposure or an unintended mutation can cause irreversible loss.

## Credential handling

- Never commit `.env`, API keys, signatures, passphrases, proxy credentials, raw account payloads, balances, order journals,
  Aptos mnemonics, private keys, wallet databases, or unredacted backups.
- Use a dedicated WEEX API Key with only the required contract permissions.
- Bind the key to a stable egress IP and do not enable withdrawal permission.
- Keep wallet mnemonics offline. An encrypted local backup does not replace the mnemonic.
- Rotate or revoke a credential immediately if it appears in terminal history, logs, screenshots, issues, commits, or CI
  output. Move Aptos assets to a new mnemonic if a mnemonic or private key may have been exposed.

## Reporting

Use the repository's [private security advisory](https://github.com/GrooveSino/weex-autotrade/security/advisories/new)
for vulnerabilities. Do not open a public Issue or PR containing secrets, exploitable account details, transaction
identifiers, private infrastructure addresses, or reproduction data copied from a funded account.

If a real secret has already been committed, deleting the current file is not sufficient: revoke or rotate it first, then
remove it from Git history before publishing another revision.

## Repository checks

- `.gitignore` excludes local credentials, databases, backups, logs, build outputs, coverage, and tool caches.
- `.gitleaks.toml` extends the default Gitleaks rules with WEEX and Aptos-specific patterns.
- CI and tests must use fake clients and must never contact WEEX or submit Aptos transactions.

## Live-trading changes

Changes affecting order submission, cancellation, position closure, leverage, margin, or risk orders must include fake-client tests and must preserve the dry-run, exact-confirmation, and live-environment gates.
