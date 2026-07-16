## Summary

- Describe the change.

## Safety impact

- [ ] No trading mutation behavior changed
- [ ] Mutation behavior changed and demo/fake-client tests cover it
- [ ] No credentials, account data, or runtime artifacts are included

## Validation

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run pytest --cov=weex_cli`
