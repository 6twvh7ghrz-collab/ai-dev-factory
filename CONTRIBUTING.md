# Contributing

## Before you start

- Work from the public repository, not the source project copy.
- Do not commit secrets, logs, databases, or runtime artifacts.
- Keep changes focused and reviewable.

## Local checks

Backend regression:

```bash
python -m pytest backend/tests -m "not e2e" -q
```

## Pull request guidance

- Describe the user-visible change
- List the commands you ran
- Call out any behavior that depends on local configuration

## Code style

- Prefer existing patterns in the repository
- Use relative paths or environment variables instead of hardcoded personal paths
- Keep open-source fixtures free of real credentials
