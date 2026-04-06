"""python -m src — start the uvicorn HTTP server.

Usage:
    python -m src               # listens on $PORT (default 8080)
    PORT=9000 python -m src     # custom port

Local dev: place a .env file in the project root (copy from .env.example).
It is loaded automatically here; in production (Cloud Run) env vars are
injected by the runtime so python-dotenv is not installed there.
"""
import os

import uvicorn

try:
    from dotenv import load_dotenv
    load_dotenv()          # loads .env from cwd; silently no-ops if file absent
except ImportError:
    pass                   # production image — python-dotenv not installed

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("src.app:app", host="0.0.0.0", port=port, reload=False)
