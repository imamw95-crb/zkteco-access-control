"""Boot the real ASGI app on a socket, hit a few endpoints, then shut down.

    python -m scripts.boot_check

This exercises the lifespan (table creation + scheduler start), routing, and
Swagger generation over real HTTP.
"""

from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("DATABASE_URL", "sqlite:///./live_check.db")
os.environ.setdefault("SCHEDULER_ENABLED", "true")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

HOST, PORT = "127.0.0.1", 8123


def main() -> int:
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        print("server did not start")
        return 1

    print(f"server started on http://{HOST}:{PORT}\n")

    failures = 0
    with httpx.Client(base_url=f"http://{HOST}:{PORT}", timeout=30) as client:
        checks = [
            ("GET", "/health", None),
            ("GET", "/api/dashboard/summary", None),
            ("GET", "/api/dashboard/devices", None),
            ("GET", "/api/devices", None),
            ("GET", "/api/personnel", None),
            ("GET", "/api/logs", None),
            ("GET", "/api/logs/count", None),
            ("GET", "/api/door-schedules", None),
            ("GET", "/openapi.json", None),
            ("GET", "/", None),
        ]
        for method, path, payload in checks:
            response = client.request(method, path, json=payload)
            ok = response.status_code < 400
            failures += 0 if ok else 1
            body = response.text.replace("\n", " ")
            print(f"  {response.status_code} {method:<5} {path:<32} {body[:96]}")

        openapi = client.get("/openapi.json").json()
        tags = sorted(
            {
                tag
                for path in openapi["paths"].values()
                for operation in path.values()
                for tag in operation.get("tags", [])
            }
        )
        print(f"\n  OpenAPI paths : {len(openapi['paths'])}")
        print(f"  OpenAPI tags  : {tags}")

        scheduler_running = server.started
        from app.workers.scheduler import scheduler

        print(f"  scheduler jobs: {[j.id for j in scheduler.get_jobs()]}")

    server.should_exit = True
    thread.join(timeout=10)
    print(f"\nshutdown complete; sched={'was' if scheduler_running else 'not'} started")
    print(f"\nRESULT: {'OK' if failures == 0 else f'{failures} endpoint(s) failed'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
