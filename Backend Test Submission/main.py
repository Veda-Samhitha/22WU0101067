from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl
from datetime import datetime, timedelta, timezone
import sqlite3
import string
import ipaddress
import json
import urllib.request

# ---------------------------
# App + Logging Middleware
# ---------------------------
from starlette.middleware.base import BaseHTTPMiddleware

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = datetime.now(timezone.utc)
        client_ip = request.client.host if request.client else "unknown"
        try:
            response = await call_next(request)
            return response
        finally:
            dur = (datetime.now(timezone.utc) - start).total_seconds() * 1000
            code = response.status_code if "response" in locals() else "ERR"
            print(f"[REQ] {request.method} {request.url.path} from {client_ip} -> {code} in {dur:.2f}ms")

app = FastAPI(title="URL Shortener Microservice")
app.add_middleware(LoggingMiddleware)

# ---------------------------
# Database setup (SQLite)
# ---------------------------
DB_PATH = "shortener.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shorturls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_url TEXT NOT NULL,
            shortcode TEXT UNIQUE,
            created_at TEXT NOT NULL,
            expiry TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clicks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shortcode TEXT NOT NULL,
            clicked_at TEXT NOT NULL,
            referrer TEXT,
            location TEXT
        )
    """)
    return conn

# ---------------------------
# Utilities
# ---------------------------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def compute_expiry_dt(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes or 30)

def iso_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def validate_shortcode(code: str):
    allowed = string.ascii_letters + string.digits + "_-"
    if not (4 <= len(code) <= 32) or not all(c in allowed for c in code):
        raise HTTPException(status_code=422, detail={
            "error": "INVALID_SHORTCODE",
            "message": "Shortcode must be 4-32 chars of [0-9A-Za-z_-]."
        })

def to_base62(num: int) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    if num == 0:
        return alphabet[0]
    base, out = len(alphabet), []
    n = num
    while n > 0:
        n, r = divmod(n, base)
        out.append(alphabet[r])
    return "".join(reversed(out))

def get_client_ip(request: Request) -> str:
    # Prefer X-Forwarded-For if behind proxy
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def is_private_or_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback
    except Exception:
        return True

def geolocate_ip(ip: str, timeout_sec: float = 2.0) -> str:
    """
    Best-effort geolocation using public endpoint (no API key).
    Falls back to 'Local' for private/loopback and 'Unknown' on failure.
    """
    if not ip or ip == "unknown":
        return "Unknown"
    if is_private_or_loopback(ip):
        return "Local"
    try:
        # ipapi.co free endpoint
        url = f"https://ipapi.co/{ip}/json/"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.load(resp)
        city = data.get("city")
        country = data.get("country_name") or data.get("country")
        if city and country:
            return f"{city}, {country}"
        if country:
            return str(country)
        return "Unknown"
    except Exception:
        return "Unknown"

# ---------------------------
# Models
# ---------------------------
class CreateShortUrlBody(BaseModel):
    url: HttpUrl
    validity: int = 30
    shortcode: str | None = None

class CreateResp(BaseModel):
    shortLink: str
    expiry: str

# ---------------------------
# Routes (order: health, create, stats, redirect)
# ---------------------------
@app.get("/healthz")
def health_check():
    return {"status": "ok", "time": now_utc_iso()}

@app.post("/shorturls", response_model=CreateResp, status_code=201)
def create_shorturl(body: CreateShortUrlBody, request: Request):
    conn = get_conn()
    cur = conn.cursor()

    created_at = now_utc_iso()
    expiry_dt = compute_expiry_dt(body.validity)
    expiry_str = iso_utc_z(expiry_dt)

    shortcode = body.shortcode
    if shortcode:
        validate_shortcode(shortcode)
        try:
            cur.execute(
                "INSERT INTO shorturls (original_url, shortcode, created_at, expiry) VALUES (?, ?, ?, ?)",
                (str(body.url), shortcode, created_at, expiry_dt.isoformat()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            raise HTTPException(status_code=409, detail={"error": "SHORTCODE_TAKEN", "message": "Provided shortcode is already in use."})
    else:
        cur.execute(
            "INSERT INTO shorturls (original_url, shortcode, created_at, expiry) VALUES (?, NULL, ?, ?)",
            (str(body.url), created_at, expiry_dt.isoformat()),
        )
        new_id = cur.lastrowid
        generated = to_base62(new_id)
        try:
            cur.execute("UPDATE shorturls SET shortcode=? WHERE id=?", (generated, new_id))
            conn.commit()
            shortcode = generated
        except sqlite3.IntegrityError:
            # extremely rare; fallback with timestamp suffix
            fallback = f"{generated}-{int(datetime.now(timezone.utc).timestamp())%1000}"
            cur.execute("UPDATE shorturls SET shortcode=? WHERE id=?", (fallback, new_id))
            conn.commit()
            shortcode = fallback

    conn.close()

    base = str(request.base_url).rstrip("/")
    short_link = f"{base}/{shortcode}"
    return CreateResp(shortLink=short_link, expiry=expiry_str)

@app.get("/shorturls/{shortcode}")
def get_stats(shortcode: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT original_url, created_at, expiry FROM shorturls WHERE shortcode=?", (shortcode,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Shortcode not found."})

    original_url, created_at, expiry = row
    cur.execute("SELECT clicked_at, referrer, location FROM clicks WHERE shortcode=? ORDER BY clicked_at DESC", (shortcode,))
    clicks = cur.fetchall()
    conn.close()

    return {
        "total_clicks": len(clicks),
        "original_url": original_url,
        "created_at": created_at,
        "expiry": expiry,
        "click_logs": [
            {"timestamp": c[0], "referrer": c[1], "location": c[2]} for c in clicks
        ]
    }

@app.get("/{shortcode}")
def redirect_shortcode(shortcode: str, request: Request):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT original_url, expiry FROM shorturls WHERE shortcode=?", (shortcode,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail={"error": "NOT_FOUND", "message": "Shortcode not found."})

    original_url, expiry = row
    if datetime.fromisoformat(expiry) < datetime.now(timezone.utc):
        conn.close()
        raise HTTPException(status_code=410, detail={"error": "EXPIRED", "message": "This link has expired."})

    # Click logging with real(ish) location
    referrer = request.headers.get("referer") or request.headers.get("referrer")
    client_ip = get_client_ip(request)
    location = geolocate_ip(client_ip)

    cur.execute(
        "INSERT INTO clicks (shortcode, clicked_at, referrer, location) VALUES (?, ?, ?, ?)",
        (shortcode, now_utc_iso(), referrer, location),
    )
    conn.commit()
    conn.close()

    return RedirectResponse(url=original_url, status_code=307)
