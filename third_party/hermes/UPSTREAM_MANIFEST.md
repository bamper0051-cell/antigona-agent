# Hermes — Upstream Manifest

## Source

Repository: NousResearch/hermes-agent
License: MIT
Upstream commit: 3be565fb
Local commit: 78c06525 (+1 carried)
Version (pip): 0.18.2
Version (CLI): 0.19.0 (2026.7.20)

## Component map

### Adapted (Hermes concept → Antigona implementation)

- Iteration budget → durable/agent_loop.py (MAX_ATTEMPTS_PER_STEP etc.)
- Error fingerprint → durable/agent_loop.py (fingerprint dedup)
- Tool result classification → durable/execution_models.py
- Event bus → events/bus.py, events/event_types.py
- Operation lifecycle → durable/operation_store.py, presentation/presenter.py
- Gateway result API → core/gateway_client.py, gateway/api.py
- Pre-commit safety → result_safety.py, repository.py

### Conceptual borrow (no code copied)

- Tool result → tool role message
- Context compression (not yet implemented)
- Delegation with live logs
- Provider fallback (planned Phase 6)

## Baseline

```bash
cd /opt/antigona
.venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Current: 256 passed, 1 warning (StarletteDeprecationWarning).
