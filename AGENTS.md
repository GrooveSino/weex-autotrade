# Repository Instructions

- This repository controls financial trading actions. Treat every mutation path as safety-critical.
- Keep Demo as the default mode. Never weaken `--execute`, exact confirmation, or the live environment gate.
- Never read credentials from sibling projects, user-global locations, or above the current project root. Only load an explicitly supplied env file or `.env` files between the current directory and the nearest Git/`pyproject.toml` root.
- Never retry order submission automatically. After network errors, query by client order ID and report an uncertain outcome when verification is inconclusive.
- Preserve maker semantics: `POST_ONLY` rejection is a failure, not permission to chase price.
- For stop replacement, submit and verify the new full-size stop before canceling the old stop.
- Unit and CI tests must use fake clients and must never call WEEX.
- Do not commit `.env`, account payloads, order journals, databases, logs, or generated artifacts.
