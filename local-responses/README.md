# Local Responses Hub (M0 — Bootstrap + Responses + SSE + Threads)

Minimal FastAPI skeleton with JSON logging, SQLite via SQLAlchemy + Alembic, and endpoints.

Endpoints
- GET /health
- GET /config
- POST /responses (non-stream)
- POST /responses?stream=true (SSE stream)
- POST /responses/{id}/cancel

SSE stream events
- meta: { "id":"resp_<uuid>", "created": <unix>, "model": "...", "provider": {"name":"lmstudio","base_url":"..."}, "status":"in_progress" }
- delta: { "index": 0, "type": "output_text.delta", "text": "..." }
- usage: { "input_tokens": N, "output_tokens": M, "total_tokens": N+M }
- done: { "status": "completed" | "cancelled" }
- ping: { "ts": "<UTC ISO8601>" }
- error: { "message": "..." }

Curl (Windows PowerShell)
- $body = @{ model = "lm:qwen2.5-instruct"; input = "Скажи привет одному предложению"; system = "Ты лаконичный ассистент."; temperature = 0.3; max_output_tokens = 128 } | ConvertTo-Json -Depth 5
- curl -N -H "Content-Type: application/json" -X POST --data $body http://127.0.0.1:8000/responses?stream=true

JavaScript (EventSource)
const es = new EventSource("http://127.0.0.1:8000/responses?stream=true", { withCredentials: false });
es.addEventListener("meta", e => console.log("meta", JSON.parse(e.data)));
es.addEventListener("delta", e => console.log("delta", JSON.parse(e.data)));
es.addEventListener("usage", e => console.log("usage", JSON.parse(e.data)));
es.addEventListener("done", e => console.log("done", JSON.parse(e.data)));
es.addEventListener("ping", e => console.log("ping", JSON.parse(e.data)));
es.onerror = (e) => { console.error("error", e); es.close(); };

Cancel
- Invoke-RestMethod -Method POST -Uri http://127.0.0.1:8000/responses/resp_<uuid>/cancel

Threads and context
- Provide `thread_id` in the request to continue a thread.
- Or set `create_thread: true` to create a new thread automatically.
- The response `metadata` includes `thread_id`.
- Context budget is controlled by CTX_MAX_INPUT_TOKENS.
- Auto-summary is triggered when total tokens exceed SUMMARY_TRIGGER_TOKENS.

Examples
Create thread on-the-fly
{
  "model": "lm:qwen2.5-instruct",
  "input": "Привет!",
  "create_thread": true
}

Continue thread
{
  "model": "lm:qwen2.5-instruct",
  "input": "Продолжай.",
  "thread_id": "<id>"
}

SSE (PowerShell)
$body = @{ model = "lm:qwen2.5-instruct"; input = "Скажи привет"; create_thread = $true } | ConvertTo-Json -Depth 5
curl.exe --% -N -H "Content-Type: application/json; charset=utf-8" -H "Accept: text/event-stream" --data-raw $body "http://127.0.0.1:8000/responses?stream=true"

# Response
