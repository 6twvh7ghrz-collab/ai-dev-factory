# AI Dev Factory

AI Dev Factory is a local-first task execution and review system for controlled code changes.

## What it includes

- Worker registration, claim, heartbeat, and lease recovery
- Result Packet and Evidence generation
- Reviewer, handoff, and supervisor flows
- Persistent runtime state
- Sandboxed patch application and rollback
- Codex Provider Bridge for trusted patch proposal generation

## Safety boundary

This release keeps real production AI execution closed.

- The bridge only accepts a narrow set of model-controlled draft fields.
- Trusted metadata, hashes, diffs, task IDs, and test allowlists are generated or enforced by the system.
- Task packets are recursively sanitized before provider prompts are built.

## Quick start

Backend:

```bash
cd backend
pip install -r requirements.txt
python run.py
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

## Testing

Run the non-e2e backend regression suite:

```bash
python -m pytest backend/tests -m "not e2e" -q
```

## Repository layout

- `backend/`: FastAPI backend, agent runtime, supervisor, and tests
- `frontend/`: React + TypeScript UI
- `scripts/`: startup and runtime helper scripts
- `docs/`: design and status notes

## Notes

- Use `.env.example` as the starting point for local configuration.
- Do not commit secrets, databases, logs, or runtime outputs.
