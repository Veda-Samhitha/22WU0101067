from datetime import datetime, timezone

class LoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = datetime.now(timezone.utc)
        method = scope["method"]
        path = scope["path"]
        client = scope.get("client")
        client_ip = client[0] if client else "unknown"

        status_code_holder = {"status": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_code_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000.0
            print(f"[REQ] {method} {path} from {client_ip} -> {status_code_holder['status']} in {duration_ms:.2f}ms")
