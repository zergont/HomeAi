# Local Responses Hub (M0 — Bootstrap + Responses + SSE + Threads)

Minimal FastAPI skeleton with JSON logging, SQLite via SQLAlchemy + Alembic, and endpoints.

Endpoints
- GET /health
- GET /config
- POST /responses (non-stream)
- POST /responses?stream=true (SSE stream)
- POST /responses/{id}/cancel
- GET /profile
- PUT /profile
- GET /providers/lmstudio/context-length?model=<id>
- GET /threads/{id}/memory

SSE stream events
- meta: { "id":"resp_<uuid>", "created": <unix>, "model": "...", "provider": {"name":"lmstudio","base_url":"..."}, "status":"in_progress", "metadata": { "thread_id": "...", "context_budget": { ... }, "context_assembly": { ... }, "memory": { ... } } }
- meta.update: { "memory": { ... } }
- delta: { "index": 0, "type": "output_text.delta", "text": "..." }
- usage: { "input_tokens": N, "output_tokens": M, "total_tokens": N+M }
- done: { "status": "completed" | "cancelled" }
- ping: { "ts": "<UTC ISO8601>" }
- error: { "message": "..." }

Context Budget (P2/P2.1)
- C_eff, R_out, R_sys, Safety, B_total_in, core_reserved, core_sys_pad, B_work
- CTX_CORE_SYS_PAD_TOK — фиксированный запас ядра (100 по умолчанию), который прибавляется к core_cap при резервировании и уменьшает B_work

Context Assembly (P3)
- Core/Profile → Tool Results → L3 → L2 → L1, строго в бюджетах
- Контроль капов: core_cap (не ниже CONTEXT_MIN_CORE_SKELETON_TOK), tools_cap (доля от B_work), L1/L2/L3 — по compute_level_caps
- Сжатия (squeeze) при нехватке: drop_l1 → drop_l2 → drop_tools → shrink_core
- Метаданные context_assembly включают tokens/caps/budget/squeezes

Memory L1/L2/L3 (P3)
- L1 — пары user→assistant, капы по ролям (user≤120, assistant≤80)
- L2 — пакетное сжатие L1 в тезисы; L3 — микротезисы из L2
- Капы уровней рассчитываются из B_work, с учётом tool_results_tokens
- Метаданные памяти в /responses.metadata.memory и в meta.update

Smoke
curl "http://127.0.0.1:8000/threads/<id>/memory"
