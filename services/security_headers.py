"""Small HTTP boundary: bounded request bodies and private response defaults."""
from starlette.responses import JSONResponse


class BodyLimitExceeded(Exception):
    pass


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        limit = 64 * 1024**2 if b"multipart/form-data" in headers.get(b"content-type", b"") else 256 * 1024
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = limit + 1
        failure = JSONResponse({"code": 413, "detail": "請求內容過大"}, status_code=413)
        if declared < 0 or declared > limit:
            return await failure(scope, receive, send)
        size = 0
        exceeded = False
        sent = False
        async def bounded_receive():
            nonlocal size, exceeded
            message = await receive()
            size += len(message.get("body", b""))
            if size > limit:
                exceeded = True
                raise BodyLimitExceeded()
            return message
        async def protected_send(message):
            nonlocal sent
            if exceeded:
                if not sent:
                    sent = True
                    await failure(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                values = list(message.get("headers", []))
                values = [(key, value) for key, value in values if key.lower() not in
                          {b"x-content-type-options", b"referrer-policy", b"x-frame-options"}]
                values += [(b"x-content-type-options", b"nosniff"),
                           (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"SAMEORIGIN")]
                if scope.get("path", "").startswith("/api/"):
                    values = [(key, value) for key, value in values if key.lower() != b"cache-control"]
                    values.append((b"cache-control", b"no-store"))
                if scope.get("path") == "/":
                    values.append((b"content-security-policy", b"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' data: https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; frame-src 'self' blob:; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'"))
                message = {**message, "headers": values}
            await send(message)
        try:
            await self.app(scope, bounded_receive, protected_send)
        except Exception:
            if not exceeded:
                raise
            if not sent:
                await failure(scope, receive, send)
