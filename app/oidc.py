"""Accesso con un provider OpenID Connect (Authentik, Keycloak, Zitadel…), scelto da ogni azienda.

Ogni azienda può collegare il proprio provider da Impostazioni. Il login con nome utente e
password resta sempre disponibile: il provider è una strada in più, non l'unica.

Come funziona:
  - ogni azienda ha un link d'accesso suo (/accesso/<chiave>): la chiave è casuale, così la
    pagina di accesso non elenca le aziende del server. Dopo il primo uso il browser se la
    ricorda e la pagina di accesso mostra il pulsante;
  - flusso «authorization code» con PKCE (S256), state e nonce legati alla sessione;
  - il token d'identità si verifica davvero: firma (RS256/384/512 con le chiavi pubbliche del
    provider, oppure HS256 con il client secret), emittente, destinatario, scadenza e nonce;
  - entra solo chi esiste già come utente attivo della stessa azienda, legato all'identità del
    provider (claim «sub»). Il legame si crea:
      * dall'utente stesso, in «Il mio account», dopo essere entrato con la password: è il modo
        sicuro, perché dimostra che l'utente della dashboard e quello del provider sono la stessa persona;
      * in automatico per email, solo se l'amministratore lo attiva: va bene quando nel provider gli
        utenti non possono cambiarsi l'email da soli (altrimenti un tecnico potrebbe mettersi l'email
        dell'amministratore e entrare al suo posto). Un'email dichiarata non verificata non vale mai.

Nessuna libreria esterna: le richieste usano urllib e la verifica RSA è fatta qui sotto
(è solo una verifica di firma con la chiave pubblica: niente chiavi private, niente cifratura).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

CALLBACK_PATH = "/accesso/callback"
SCOPES = "openid email profile"
LEEWAY = 120                 # secondi di tolleranza sugli orologi
CACHE_SECONDS = 600          # configurazione e chiavi del provider
MAX_RESPONSE = 1024 * 1024
TIMEOUT = 10
DEFAULT_LABEL = "Authentik"
# Solo per le prove in locale: il provider deve essere in https.
ALLOW_HTTP = os.environ.get("OIDC_ALLOW_HTTP") == "1"

KEYS = ("oidc_enabled", "oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_label", "oidc_key", "oidc_auto_email")

_cache = {}
_lock = threading.Lock()


class OIDCError(Exception):
    """Errore da mostrare all'utente così com'è (mai il contenuto delle risposte del provider)."""


# ---------------------------------------------------------------- impostazioni dell'azienda

def settings(company_id):
    s = {k: db.get_csetting(company_id, k, "") or "" for k in KEYS}
    s["enabled"] = s["oidc_enabled"] == "1"
    s["auto_email"] = s["oidc_auto_email"] == "1"
    s["label"] = s["oidc_label"] or DEFAULT_LABEL
    return s


def ensure_key(company_id):
    key = db.get_csetting(company_id, "oidc_key")
    if not key:
        key = secrets.token_urlsafe(12)
        db.set_csetting(company_id, "oidc_key", key)
    return key


def company_for_key(key):
    """Azienda con questa chiave e il provider attivo (None se la chiave non vale)."""
    if not isinstance(key, str) or not 8 <= len(key) <= 40:
        return None
    row = db.one("SELECT co.* FROM company_settings k JOIN companies co ON co.id = k.company_id "
                 "WHERE k.key = 'oidc_key' AND k.value = ?", key)
    if not row or row["suspended_at"]:
        return None
    s = settings(row["id"])
    if not (s["enabled"] and s["oidc_issuer"] and s["oidc_client_id"]):
        return None
    if not hmac.compare_digest(s["oidc_key"], key):
        return None
    return row


def valid_issuer(url):
    if not isinstance(url, str) or len(url) > 300 or any(ch.isspace() for ch in url):
        return False
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in (("https", "http") if ALLOW_HTTP else ("https",)):
        return False
    return bool(parts.hostname) and not parts.username and not parts.password and not parts.query and not parts.fragment


# ---------------------------------------------------------------- richieste al provider

def _request(url, data=None, headers=None):
    if not url.startswith("https://") and not (ALLOW_HTTP and url.startswith("http://")):
        raise OIDCError("il provider deve usare https")
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json",
                                                          "User-Agent": "NexohmAssist", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as e:
        # dal token endpoint arriva un JSON {error, error_description}: si mostra solo il codice
        code = ""
        try:
            code = str(json.loads(e.read(4096)).get("error", ""))[:60]
        except (ValueError, AttributeError, OSError):
            pass
        raise OIDCError(f"il provider ha risposto {e.code}" + (f" ({code})" if code else ""))
    except (urllib.error.URLError, OSError) as e:
        raise OIDCError(f"provider non raggiungibile ({getattr(e, 'reason', e)})")
    if len(body) > MAX_RESPONSE:
        raise OIDCError("risposta del provider troppo grande")
    try:
        value = json.loads(body)
    except ValueError:
        raise OIDCError("il provider non ha risposto con dati validi (indirizzo giusto?)")
    if not isinstance(value, dict):
        raise OIDCError("il provider non ha risposto con dati validi")
    return value


def _cached(key, loader, refresh=False):
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and hit[0] > time.time():
            return hit[1]
    value = loader()
    with _lock:
        _cache[key] = (time.time() + CACHE_SECONDS, value)
    return value


def discover(issuer, refresh=False):
    """Configurazione del provider (/.well-known/openid-configuration), con i controlli di base."""
    if not valid_issuer(issuer):
        raise OIDCError("indirizzo del provider non valido (deve iniziare con https://)")

    def load():
        conf = _request(issuer.rstrip("/") + "/.well-known/openid-configuration")
        if str(conf.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
            raise OIDCError("l'indirizzo del provider non corrisponde all'«issuer» che dichiara: "
                            "in Authentik copia l'«OpenID Configuration Issuer» dell'applicazione")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            v = conf.get(k)
            if not isinstance(v, str) or not valid_issuer(v.split("?")[0]):
                raise OIDCError(f"configurazione del provider incompleta ({k})")
        return {k: conf.get(k) for k in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri",
                                          "end_session_endpoint")}
    return _cached(("conf", issuer), load, refresh)


def _jwks(uri, refresh=False):
    def load():
        keys = _request(uri).get("keys")
        if not isinstance(keys, list):
            raise OIDCError("chiavi del provider non valide")
        return [k for k in keys if isinstance(k, dict)]
    return _cached(("jwks", uri), load, refresh)


# ---------------------------------------------------------------- PKCE, indirizzo di autorizzazione

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(text) -> bytes:
    if not isinstance(text, str) or len(text) > 20000:
        raise OIDCError("token d'identità non valido")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise OIDCError("token d'identità non valido")


def start(company_id, redirect_uri):
    """Prepara l'accesso: restituisce l'indirizzo del provider e lo stato da tenere in sessione."""
    s = settings(company_id)
    conf = discover(s["oidc_issuer"])
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": s["oidc_client_id"], "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    sep = "&" if "?" in conf["authorization_endpoint"] else "?"
    pending = {"state": state, "nonce": nonce, "verifier": verifier, "co": company_id, "t": int(time.time())}
    return conf["authorization_endpoint"] + sep + query, pending


# ---------------------------------------------------------------- verifica del token d'identità

# Prefisso DigestInfo (PKCS#1 v1.5) per ogni hash
_DIGEST_INFO = {
    "RS256": (hashlib.sha256, bytes.fromhex("3031300d060960864801650304020105000420")),
    "RS384": (hashlib.sha384, bytes.fromhex("3041300d060960864801650304020205000430")),
    "RS512": (hashlib.sha512, bytes.fromhex("3051300d060960864801650304020305000440")),
}
_HMAC = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}


def _rsa_verify(alg, jwk, signed: bytes, signature: bytes) -> bool:
    try:
        n = int.from_bytes(_unb64url(jwk["n"]), "big")
        e = int.from_bytes(_unb64url(jwk["e"]), "big")
    except (KeyError, OIDCError):
        return False
    k = (n.bit_length() + 7) // 8
    if n.bit_length() < 2048 or e < 3 or len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    hash_fn, prefix = _DIGEST_INFO[alg]
    t = prefix + hash_fn(signed).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def _check_signature(token, header, conf, client_secret):
    alg = header.get("alg")
    head, payload, sig = token.split(".")
    signed, signature = f"{head}.{payload}".encode(), _unb64url(sig)
    if alg in _HMAC:
        if not client_secret:
            raise OIDCError("token firmato col client secret, ma il secret non è impostato")
        good = hmac.new(client_secret.encode(), signed, _HMAC[alg]).digest()
        if not hmac.compare_digest(good, signature):
            raise OIDCError("firma del token d'identità non valida")
        return
    if alg not in _DIGEST_INFO:
        raise OIDCError(f"algoritmo di firma non supportato ({str(alg)[:10]}): in Authentik scegli una "
                        "chiave di firma RSA nel provider")
    kid = header.get("kid")
    for refresh in (False, True):   # chiave nuova del provider: si rilegge l'elenco una volta
        keys = [k for k in _jwks(conf["jwks_uri"], refresh) if k.get("kty") == "RSA" and k.get("use", "sig") == "sig"
                and (kid is None or k.get("kid") == kid)]
        if keys:
            break
    if not any(_rsa_verify(alg, k, signed, signature) for k in keys):
        raise OIDCError("firma del token d'identità non valida")


def verify_id_token(token, conf, client_id, client_secret, nonce, now=None):
    if not isinstance(token, str) or token.count(".") != 2:
        raise OIDCError("il provider non ha restituito un token d'identità")
    head, payload, _ = token.split(".")
    try:
        header, claims = json.loads(_unb64url(head)), json.loads(_unb64url(payload))
    except ValueError:
        raise OIDCError("token d'identità non valido")
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise OIDCError("token d'identità non valido")
    _check_signature(token, header, conf, client_secret)
    now = now or time.time()
    if claims.get("iss") != conf["issuer"]:
        raise OIDCError("token d'identità di un altro provider")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if client_id not in auds or (len(auds) > 1 and claims.get("azp") != client_id):
        raise OIDCError("token d'identità destinato a un'altra applicazione")
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, (int, float)) or exp + LEEWAY < now:
        raise OIDCError("token d'identità scaduto: controlla l'orologio del server")
    if isinstance(iat, (int, float)) and iat - LEEWAY > now:
        raise OIDCError("token d'identità emesso nel futuro: controlla l'orologio del server")
    if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce):
        raise OIDCError("token d'identità non legato a questo accesso")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise OIDCError("token d'identità senza identificativo dell'utente")
    return claims


def finish(pending, code, redirect_uri):
    """Scambia il codice con i token e restituisce i dati verificati dell'utente."""
    s = settings(pending["co"])
    if not s["enabled"]:
        raise OIDCError("l'accesso con il provider è stato disattivato")
    conf = discover(s["oidc_issuer"])
    if not isinstance(code, str) or not 1 <= len(code) <= 2000:
        raise OIDCError("risposta del provider senza codice")
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                                   "code_verifier": pending["verifier"]}).encode()
    basic = base64.b64encode(f"{urllib.parse.quote(s['oidc_client_id'], safe='')}:"
                             f"{urllib.parse.quote(s['oidc_client_secret'], safe='')}".encode()).decode()
    tokens = _request(conf["token_endpoint"], data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"})
    return verify_id_token(tokens.get("id_token"), conf, s["oidc_client_id"], s["oidc_client_secret"], pending["nonce"])


# ---------------------------------------------------------------- utente della dashboard

def find_user(company_id, claims):
    """Utente attivo dell'azienda legato a questa identità. (utente, None) oppure (None, motivo)."""
    sub = claims["sub"][:255]
    user = db.one("SELECT * FROM users WHERE company_id = ? AND oidc_sub = ?", company_id, sub)
    if user:
        return (user, None) if user["active"] else (None, "il tuo utente della dashboard è disattivato")
    hint = ("entra una volta con nome utente e password e in «Il mio account» premi "
            f"«Collega {settings(company_id)['label']}»")
    if not settings(company_id)["auto_email"]:
        return None, f"questo account non è ancora collegato a un utente della dashboard: {hint}"
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        return None, f"il provider non ha inviato l'email (in Authentik serve lo scope «email»), oppure {hint}"
    if claims.get("email_verified") is False:
        return None, f"il provider dice che l'email {email[:120]} non è verificata: {hint}"
    matches = db.all_("SELECT * FROM users WHERE company_id = ? AND lower(email) = ? AND oidc_sub IS NULL",
                      company_id, email.strip().lower())
    if not matches:
        return None, f"nessun utente della dashboard da collegare con l'email {email[:120]}: {hint}"
    if len(matches) > 1:
        return None, f"più utenti della dashboard hanno l'email {email[:120]}: {hint}"
    user = matches[0]
    if not user["active"]:
        return None, "il tuo utente della dashboard è disattivato"
    return (user, None) if link(user["id"], company_id, sub) else (None, "collegamento non riuscito, riprova")


def link(user_id, company_id, sub):
    """Lega l'utente all'identità del provider. False se quell'identità è già di un altro utente."""
    sub = sub[:255]
    if db.one("SELECT id FROM users WHERE company_id = ? AND oidc_sub = ? AND id != ?", company_id, sub, user_id):
        return False
    try:
        return bool(db.changed("UPDATE users SET oidc_sub = ? WHERE id = ? AND company_id = ?", sub, user_id, company_id))
    except Exception:   # indice unico: due collegamenti contemporanei della stessa identità
        return False


def unlink_all(company_id):
    """Provider cambiato: i legami con le vecchie identità non valgono più (si ricollega per email)."""
    db.run("UPDATE users SET oidc_sub = NULL WHERE company_id = ?", company_id)
