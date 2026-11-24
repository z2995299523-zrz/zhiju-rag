"""Single-workspace access policy and expiring server-side sessions."""
import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .accounts import normalize_username, password_matches


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def _local_demo(request):
    peer = request.client.host if request.client else ""
    forwarded = any(k.startswith("x-forwarded-") or k == "forwarded" for k in request.headers)
    return _loopback(peer) and _loopback(request.url.hostname) and not forwarded


def account(request: Request):
    with request.app.state.store.db() as conn:
        return account_in_transaction(request, conn)


def account_in_transaction(request, conn):
    admin_password = os.environ.get("RAG_ADMIN_PASSWORD")
    token = request.cookies.get("rag_session", "")
    if not admin_password and not conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
        if _local_demo(request):
            return {"role": "admin", "owner": "local", "auth_required": False, "username": None}
    if len(token) == 48:
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = conn.execute("""SELECT s.role,s.principal,a.role AS account_role,a.enabled
            FROM sessions s LEFT JOIN accounts a ON a.username=s.principal
            WHERE s.digest=? AND s.expires>?""", (digest, int(time.time()))).fetchone()
        if row and row["principal"] is not None and row["enabled"]:
            return {"role": row["account_role"], "owner": "user/" + row["principal"],
                    "auth_required": True, "username": row["principal"]}
        if row and row["principal"] is None and admin_password:
            if row["role"] == "admin" or os.environ.get("RAG_READER_PASSWORD"):
                return {"role": row["role"], "owner": digest, "auth_required": True, "username": None}
    return {"role": None, "owner": None, "auth_required": True, "username": None}


def audit_actor(identity):
    return identity["owner"] if identity.get("username") else identity["owner"][:12]


def reader(request: Request):
    identity = account(request)
    if identity["role"] is None:
        raise HTTPException(401, "请先登录工作台")
    return identity


def admin(request: Request):
    identity = reader(request)
    if identity["role"] != "admin":
        raise HTTPException(403, "此操作需要管理员权限")
    return identity


def _reserve_attempt(conn, peer, current):
    conn.execute("DELETE FROM attempts WHERE since<=?", (current - 300,))
    row = conn.execute("SELECT failures FROM attempts WHERE peer=?", (peer,)).fetchone()
    if row and row["failures"] >= 10:
        raise HTTPException(429, "登录尝试过于频繁，请稍后再试")
    conn.execute("""INSERT INTO attempts(peer,failures,since) VALUES (?,1,?)
        ON CONFLICT(peer) DO UPDATE SET failures=failures+1""", (peer, current))


def login(request, password, username=None):
    admin_password = os.environ.get("RAG_ADMIN_PASSWORD", "")
    store = request.app.state.store
    peer = request.client.host if request.client else ""
    has_username = bool(username and username.strip())
    try:
        principal = normalize_username(username) if has_username else None
    except ValueError:
        principal = None
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HTTPException(422, "密码含无效字符") from exc
    with store.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not admin_password and not conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
            if _local_demo(request):
                return {"authenticated": True, "role": "admin", "auth_required": False, "username": None}
            raise HTTPException(403, "请先在服务器配置个人账号或管理员密码以开放远程访问")
        # Reserve a slot before password derivation so concurrent attempts are also limited.
        _reserve_attempt(conn, peer, int(time.time()))
        expected = None
        if has_username:
            expected = conn.execute("SELECT * FROM accounts WHERE username=?", (principal,)).fetchone()
    if has_username:
        role = expected["role"] if password_matches(password, expected) else None
    else:
        reader_password = os.environ.get("RAG_READER_PASSWORD", "")
        is_admin = hmac.compare_digest(admin_password.encode(), encoded) and bool(admin_password)
        is_reader = hmac.compare_digest(reader_password.encode(), encoded) and bool(reader_password and admin_password)
        role = "admin" if is_admin else "reader" if is_reader else None
    if not role:
        return JSONResponse({"detail": "账号或密码错误"}, status_code=401)
    with store.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = int(time.time())
        if principal:
            # A concurrent password rotation or account disable must win over an in-flight login.
            latest = conn.execute("SELECT role FROM accounts WHERE username=? AND enabled=1 AND password_hash=?",
                                  (principal, expected["password_hash"])).fetchone()
            if latest is None:
                return JSONResponse({"detail": "账号或密码错误"}, status_code=401)
            role = latest["role"]
        conn.execute("DELETE FROM attempts WHERE peer=?", (peer,))
        conn.execute("DELETE FROM sessions WHERE expires<=?", (current,))
        token = secrets.token_hex(24)
        digest = hashlib.sha256(token.encode()).hexdigest()
        conn.execute("INSERT INTO sessions(digest,role,expires,principal) VALUES (?,?,?,?)",
                     (digest, role, current + 28800, principal))
        store.audit(conn, "user/" + principal if principal else digest[:12], "session.login", role)
    response = JSONResponse({"authenticated": True, "role": role, "auth_required": True, "username": principal})
    response.set_cookie("rag_session", token, httponly=True, secure=os.getenv("RAG_COOKIE_SECURE") == "1",
                        samesite="strict", max_age=28800, path="/")
    return response


async def guard(request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        try:
            source = urlsplit(origin) if origin else None
            # TLS termination is also signalled explicitly by the secure-cookie setting.
            site_scheme = "https" if os.getenv("RAG_COOKIE_SECURE") == "1" else request.url.scheme
            different_origin = source and (source.netloc.lower() != request.url.netloc.lower()
                                            or source.scheme != site_scheme or source.path or source.query or source.fragment)
        except ValueError:
            different_origin = True
        if request.headers.get("sec-fetch-site") == "cross-site" or different_origin:
            return JSONResponse({"detail": "请在同一站点发起操作"}, status_code=403)
        if request.method != "DELETE":
            length = request.headers.get("content-length", "")
            if not length.isascii() or not length.isdigit() or len(length) > 12:
                return JSONResponse({"detail": "请求需要有效的 Content-Length"}, status_code=411)
            maximum = 48 * 1024 * 1024 if request.url.path in {"/api/documents", "/api/documents/inspect"} else 64 * 1024
            if int(length) > maximum:
                return JSONResponse({"detail": "请求超过大小限制"}, status_code=413)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    pdf_preview = getattr(request.state, "pdf_preview", False)
    html_preview = getattr(request.state, "html_preview", False)
    reader_page = request.url.path == "/reader.html"
    response.headers["X-Frame-Options"] = "SAMEORIGIN" if pdf_preview or html_preview or reader_page else "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:" + (" blob:" if reader_page else "") + "; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors "
        + ("'self'" if reader_page else "'none'"))
    if pdf_preview:
        response.headers["Content-Security-Policy"] = ("default-src 'none'; object-src 'self'; "
            "base-uri 'none'; frame-ancestors 'self'; form-action 'none'")
    if html_preview:
        response.headers["Content-Security-Policy"] = ("sandbox; default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data:; font-src data:; media-src data:; base-uri 'none'; frame-ancestors 'self'; form-action 'none'")
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response
