"""Finto provider OpenID Connect (come Authentik) per le prove: python3 fake_oidc.py <porta>

L'identità che «entra» e gli eventuali difetti del token si scelgono con POST /control (JSON):
  {"sub": "...", "email": "...", "email_verified": true, "mode": "ok"}
mode: ok | hs256 | badsig | wrongaud | wrongiss | expired | badnonce | none | denied
Il provider controlla davvero client secret, PKCE, redirect_uri e uso unico del codice.
Chiave RSA generata all'avvio senza librerie esterne (solo per le prove: lenta e non adatta all'uso reale).
"""
import base64
import hashlib
import hmac
import json
import secrets
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8125
ISSUER = f"http://127.0.0.1:{PORT}/application/o/assist/"
CLIENT_ID, CLIENT_SECRET = "assist-client", "segreto-del-client-123"
STATE = {"sub": "authentik-1", "email": "", "email_verified": True, "mode": "ok"}
CODES = {}
LOG = []


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def is_prime(n):
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2; r += 1
    for _ in range(20):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def prime(bits):
    while True:
        c = secrets.randbits(bits) | (1 << bits - 1) | (1 << bits - 2) | 1
        if is_prime(c):
            return c


def rsa_key():
    e = 65537
    while True:
        p, q = prime(1024), prime(1024)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e and (p * q).bit_length() == 2048:
            return p * q, e, pow(e, -1, phi)


N, E, D = rsa_key()
OTHER_N, OTHER_E, OTHER_D = rsa_key()   # chiave di un altro: firma non valida
KID = "chiave-1"
PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def rs256(signed, n=N, d=D):
    k = (n.bit_length() + 7) // 8
    t = PREFIX + hashlib.sha256(signed).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def id_token(nonce):
    mode = STATE["mode"]
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": STATE["sub"], "aud": CLIENT_ID, "exp": now + 300, "iat": now, "nonce": nonce,
              "preferred_username": STATE["sub"]}
    if STATE.get("email"):
        claims["email"] = STATE["email"]
        claims["email_verified"] = STATE["email_verified"]
    if mode == "wrongaud":
        claims["aud"] = "altra-app"
    if mode == "wrongiss":
        claims["iss"] = "http://127.0.0.1:1/altro/"
    if mode == "expired":
        claims["exp"] = now - 3600
    if mode == "badnonce":
        claims["nonce"] = "nonce-di-un-altro"
    alg = {"hs256": "HS256", "none": "none"}.get(mode, "RS256")
    header = {"alg": alg, "typ": "JWT"} | ({"kid": KID} if alg == "RS256" else {})
    signed = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}".encode()
    if alg == "HS256":
        sig = hmac.new(CLIENT_SECRET.encode(), signed, hashlib.sha256).digest()
    elif alg == "none":
        sig = b""
    else:
        sig = rs256(signed, OTHER_N, OTHER_D) if mode == "badsig" else rs256(signed)
    return signed.decode() + "." + b64(sig)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        base = f"http://127.0.0.1:{PORT}"
        if url.path == "/application/o/assist/.well-known/openid-configuration":
            return self.send_json({"issuer": ISSUER, "authorization_endpoint": base + "/application/o/authorize/",
                                   "token_endpoint": base + "/application/o/token/",
                                   "jwks_uri": base + "/application/o/assist/jwks/",
                                   "end_session_endpoint": base + "/application/o/assist/end-session/"})
        if url.path == "/application/o/assist/jwks/":
            return self.send_json({"keys": [{"kty": "RSA", "alg": "RS256", "use": "sig", "kid": KID,
                                             "n": b64(N.to_bytes(256, "big")), "e": b64(E.to_bytes(3, "big"))}]})
        if url.path == "/log":
            return self.send_json(LOG)
        if url.path == "/application/o/authorize/":
            LOG.append({"authorize": q})
            ok = (q.get("client_id") == CLIENT_ID and q.get("response_type") == "code"
                  and q.get("code_challenge_method") == "S256" and q.get("code_challenge") and q.get("state")
                  and "openid" in q.get("scope", "").split())
            target = q.get("redirect_uri", "")
            if not ok or not target:
                return self.send_json({"error": "invalid_request"}, 400)
            if STATE["mode"] == "denied":
                params = {"error": "access_denied", "state": q["state"]}
            else:
                code = secrets.token_urlsafe(16)
                CODES[code] = {"challenge": q["code_challenge"], "redirect_uri": target, "nonce": q.get("nonce", "")}
                params = {"code": code, "state": q["state"]}
            self.send_response(302)
            self.send_header("Location", target + "?" + urllib.parse.urlencode(params))
            self.end_headers()
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/control":
            STATE.clear()
            STATE.update({"sub": "authentik-1", "email": "", "email_verified": True, "mode": "ok"} | json.loads(body))
            return self.send_json({"ok": True})
        if self.path == "/application/o/token/":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            auth = self.headers.get("Authorization", "")
            good_auth = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            LOG.append({"token": {k: v for k, v in form.items() if k != "code_verifier"}, "auth_ok": auth == good_auth})
            if auth != good_auth:
                return self.send_json({"error": "invalid_client"}, 401)
            entry = CODES.pop(form.get("code", ""), None)
            if not entry or form.get("grant_type") != "authorization_code" or form.get("redirect_uri") != entry["redirect_uri"]:
                return self.send_json({"error": "invalid_grant"}, 400)
            challenge = b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
            if challenge != entry["challenge"]:
                return self.send_json({"error": "invalid_grant", "error_description": "PKCE"}, 400)
            return self.send_json({"access_token": "at", "token_type": "Bearer", "id_token": id_token(entry["nonce"])})
        self.send_json({"error": "not found"}, 404)


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
