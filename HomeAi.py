from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


def _prepare_paths() -> Path:
    base_dir = Path(__file__).resolve().parent
    app_dir = base_dir / "local-responses"
    # Ensure project package path is importable
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    # Set working directory so relative paths (e.g., data/) resolve correctly
    os.chdir(app_dir)
    return app_dir


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def main() -> None:
    _prepare_paths()

    # Safe defaults; can be overridden via .env or VS Debug env settings
    os.environ.setdefault("DB_URL", "sqlite:///data/app.db")

    host = _env("APP_HOST", "127.0.0.1")
    port_str = _env("APP_PORT", "8000")
    try:
        port = int(port_str)
    except ValueError:
        port = 8000

    # Import after sys.path/cwd are prepared
    from apps.api.main import app  # noqa: WPS433

    # VS2022 debug: disable reload to avoid immediate exit
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
