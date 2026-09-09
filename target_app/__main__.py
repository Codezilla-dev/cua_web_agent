"""Run the target app: `uv run python -m target_app`."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("target_app.app:app", host="127.0.0.1", port=5000, log_level="warning")
