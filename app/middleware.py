"""Middleware ASGI: limite sulla dimensione delle richieste e intestazioni di sicurezza."""
from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse

MAX_REQUEST = 1024 * 1024  # 1 MB: moduli della dashboard e API agente (che ha già un suo limite più basso)

# La dashboard non carica nulla da altri siti e non deve poter essere inserita in pagine altrui.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")


class TooLarge(Exception):
    pass


class BodyLimit:
    """Rifiuta con 413 le richieste oltre MAX_REQUEST, anche quelle senza Content-Length
    (chunked): il conteggio avviene mentre il corpo arriva, non dopo averlo caricato tutto."""

    def __init__(self, app, limit=MAX_REQUEST):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    too_big = int(value) > self.limit
                except ValueError:
                    too_big = True
                if too_big:
                    return await PlainTextResponse("Richiesta troppo grande", 413)(scope, receive, send)
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise TooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except TooLarge:
            if not started:
                await PlainTextResponse("Richiesta troppo grande", 413)(scope, receive, send)


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def with_headers(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h.setdefault("X-Frame-Options", "DENY")
                h.setdefault("X-Content-Type-Options", "nosniff")
                h.setdefault("Referrer-Policy", "same-origin")
                h.setdefault("Content-Security-Policy", CSP)
                h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                if scope.get("scheme") == "https":
                    h.setdefault("Strict-Transport-Security", "max-age=31536000")
                path = scope.get("path", "")
                if not path.startswith("/static/"):
                    h.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, with_headers)
