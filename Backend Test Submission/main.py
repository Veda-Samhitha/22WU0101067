from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl
from datetime import datetime, timedelta, timezone
import sqlite3
import string

app = FastAPI()

# ---------------------------
# Database setup
# ---------------------------
def get_conn():
    conn = sqlite3.connect("shortener.db")
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
def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()

def compute_expiry_dt(minutes: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)

def iso_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def validate_shortcode(code: str):
    allowed = string.ascii_letters + string.digits
    if not all(c in allowed for c in code):
        raise HTTPException(status_code=400, detail={"error": "INVALID_SHORTCODE", "message": "Shortcode must be alphanumeric."})

def to_base62(num: int) -> str:
    chars = string.ascii_letters + string.digits
    base = len(chars)
    out = ""
    while num > 0:
        num, rem = divmod(num, base)
        out = chars[rem] + out
    return out or "0"

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
# Routes
# ---------------------------
@app.get("/healthz")
def health_check():
    return {"status": "ok"}

@app.post("/shorturls", response_model=CreateResp, status_code=201)
def create_shorturl(body: CreateShortUrlBody, request: Request):
    conn = get_conn()
    cur = conn.cursor()

    created_at = now_utc_iso()
    expiry_dt = compute_expiry_dt(body.validity or 30)
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
            fallback = f"{generated}-{int(datetime.now(timezone.utc).timestamp())%1000}"
            cur.execute("UPDATE shorturls SET shortcode=? WHERE id=?", (fallback, new_id))
            conn.commit()
            shortcode = fallback

    conn.close()

    base = str(request.base_url).rstrip("/")
    short_link = f"{base}/{shortcode}"

    return CreateResp(shortLink=short_link, expiry=expiry_str)

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

    referrer = request.headers.get("referer", "")
    location = "Unknown"  # placeholder
    cur.execute(
        "INSERT INTO clicks (shortcode, clicked_at, referrer, location) VALUES (?, ?, ?, ?)",
        (shortcode, now_utc_iso(), referrer, location),
    )
    conn.commit()
    conn.close()

    # Actual HTTP redirect
    return RedirectResponse(url=original_url, status_code=307)

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
    cur.execute("SELECT clicked_at, referrer, location FROM clicks WHERE shortcode=?", (shortcode,))
    clicks = cur.fetchall()
    conn.close()

    return {
        "total_clicks": len(clicks),
        "original_url": original_url,
        "created_at": created_at,
        "expiry": expiry,
        "click_logs": [
            {"timestamp": c[0], "referrer": c[1], "location": c[2]}
            for c in clicks
        ]
    }
