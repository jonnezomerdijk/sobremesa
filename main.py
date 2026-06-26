from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from typing import Optional, Literal
import sqlite3
import json
import os
import random
import hashlib
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from pathlib import Path
from jose import jwt, JWTError
from passlib.context import CryptContext
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from matching import run_matching

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 24 * 30  # 30 days

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)

# In-memory WebSocket registry: group_id -> set of (WebSocket, user_id)
ws_connections: dict[int, set] = {}

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "hello@sobremesa.app")


def _send_email(to: str, subject: str, body: str) -> None:
    if not SMTP_HOST or not SMTP_USER:
        print(f"[EMAIL] To: {to}\nSubject: {subject}\n{body}\n---")
        return
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(FROM_EMAIL, [to], msg.as_string())


app = FastAPI(title="Sobremesa")
app.mount("/static", StaticFiles(directory="static"), name="static")

DB_PATH = "sobremesa.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                neighbourhood TEXT NOT NULL,
                dietary TEXT NOT NULL,
                availability TEXT NOT NULL,
                dinner_format TEXT DEFAULT 'any',
                dinner_format_is_must INTEGER DEFAULT 0,
                group_size_pref TEXT DEFAULT 'medium',
                age INTEGER,
                age_range_pref INTEGER DEFAULT 10,
                age_range_is_must INTEGER DEFAULT 0,
                city TEXT,
                lat REAL,
                lng REAL,
                max_travel_km INTEGER DEFAULT 10,
                link_code TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                matched INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_ids TEXT NOT NULL,
                dinner_format TEXT,
                group_size INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate existing DB — add columns if missing
        existing = {row[1] for row in conn.execute("PRAGMA table_info(signups)")}
        new_cols = {
            "dinner_format": "TEXT DEFAULT 'any'",
            "dinner_format_is_must": "INTEGER DEFAULT 0",
            "group_size_pref": "TEXT DEFAULT 'medium'",
            "age": "INTEGER",
            "age_range_pref": "INTEGER DEFAULT 10",
            "age_range_is_must": "INTEGER DEFAULT 0",
            "city": "TEXT",
            "lat": "REAL",
            "lng": "REAL",
            "max_travel_km": "INTEGER DEFAULT 10",
            "link_code": "TEXT",
            "can_host": "INTEGER DEFAULT 0",
            "languages": "TEXT DEFAULT '[]'",
            "gender_pref": "TEXT DEFAULT 'any'",
        }
        for col, typedef in new_cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE signups ADD COLUMN {col} {typedef}")

        existing_g = {row[1] for row in conn.execute("PRAGMA table_info(groups)")}
        group_cols = {
            "dinner_format": "TEXT",
            "group_size": "INTEGER",
            "host_id": "INTEGER",
            "needs_host": "INTEGER DEFAULT 0",
        }
        for col, typedef in group_cols.items():
            if col not in existing_g:
                conn.execute(f"ALTER TABLE groups ADD COLUMN {col} {typedef}")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS past_groups (
                person_a INTEGER NOT NULL,
                person_b INTEGER NOT NULL,
                PRIMARY KEY (person_a, person_b)
            )
        """)


def _extend_init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS otp_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dinners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL UNIQUE,
                venue_suggestion TEXT,
                venue_confirmed INTEGER DEFAULT 0,
                confirmed_slot TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dinner_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dinner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                slot TEXT NOT NULL,
                UNIQUE(dinner_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rsvps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dinner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                UNIQUE(dinner_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dishes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dinner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        existing_s = {row[1] for row in conn.execute("PRAGMA table_info(signups)")}
        for col in ("bio", "avatar_url", "group_id", "push_token"):
            if col not in existing_s:
                conn.execute(f"ALTER TABLE signups ADD COLUMN {col} TEXT")


init_db()
_extend_init_db()


# ── Auth helpers ──

def _make_jwt(user_id: int) -> str:
    exp = datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": str(user_id), "exp": exp}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _verify_jwt(token: str) -> int:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")


def get_current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    user_id = _verify_jwt(creds.credentials)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM signups WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found.")
    return dict(row)


# ── Auth models ──

class OTPRequest(BaseModel):
    email: EmailStr

class OTPVerify(BaseModel):
    email: EmailStr
    code: str

class PatchMe(BaseModel):
    name: Optional[str] = None
    bio: Optional[str] = None
    neighbourhood: Optional[str] = None
    dietary: Optional[list[str]] = None
    availability: Optional[list[str]] = None

class PushTokenBody(BaseModel):
    token: str

class DishBody(BaseModel):
    description: str
    category: str = "other"

class VoteBody(BaseModel):
    slot: str

class RSVPBody(BaseModel):
    status: Literal["yes", "no", "maybe"]


class Signup(BaseModel):
    name: str
    email: EmailStr
    neighbourhood: str
    dietary: list[str]
    availability: list[str]
    dinner_format: Literal["hosted", "potluck", "restaurant", "any"] = "any"
    dinner_format_is_must: bool = False
    group_size_pref: Literal["small", "medium", "large"] = "medium"
    age: Optional[int] = None
    age_range_pref: Optional[int] = 10
    age_range_is_must: bool = False
    city: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    max_travel_km: int = 10
    link_code: Optional[str] = None
    can_host: bool = False
    languages: list[str] = []
    gender_pref: Literal["any", "women", "men"] = "any"


scheduler = AsyncIOScheduler()


@scheduler.scheduled_job("cron", hour=9, minute=0)
def _scheduled_match():
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM signups WHERE matched = 0").fetchone()[0]
    if count < 4:
        print(f"[scheduler] Skipping match - only {count} unmatched signups.")
        return
    try:
        result = _do_match()
        print(f"[scheduler] Matched {result['people_matched']} people into {result['groups_formed']} groups.")
    except Exception as e:
        print(f"[scheduler] Match failed: {e}")


@app.on_event("startup")
async def startup():
    scheduler.start()


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()


_HTML = None

def _render(lang: str) -> str:
    global _HTML
    if _HTML is None:
        _HTML = Path("index.html").read_text()
    # Inject the lang into the <html> tag and set the active flag button via a small inline script
    html = _HTML.replace('<html lang="en">', f'<html lang="{lang}">')
    # Inject init lang so JS picks it up without localStorage
    html = html.replace(
        "let currentLang = localStorage.getItem('lang') || 'es';",
        f"let currentLang = '{lang}';"
    )
    return html


@app.get("/", response_class=RedirectResponse)
def index():
    return RedirectResponse(url="/es", status_code=302)


@app.get("/{lang}", response_class=HTMLResponse)
def index_lang(lang: str):
    if lang not in ("en", "es", "ca"):
        raise HTTPException(status_code=404)
    return _render(lang)


@app.post("/signup")
def signup(data: Signup):
    with get_db() as conn:
        try:
            conn.execute(
                """INSERT INTO signups
                   (name, email, neighbourhood, dietary, availability,
                    dinner_format, dinner_format_is_must, group_size_pref,
                    age, age_range_pref, age_range_is_must,
                    city, lat, lng, max_travel_km, link_code, can_host,
                    languages, gender_pref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data.name, data.email, data.neighbourhood,
                    json.dumps(data.dietary), json.dumps(data.availability),
                    data.dinner_format, int(data.dinner_format_is_must), data.group_size_pref,
                    data.age, data.age_range_pref, int(data.age_range_is_must),
                    data.city, data.lat, data.lng, data.max_travel_km,
                    data.link_code, int(data.can_host),
                    json.dumps(data.languages), data.gender_pref,
                )
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Email already registered.")
    return {"message": "You're on the list! We'll be in touch when your group is ready."}


@app.get("/admin/signups")
def list_signups():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM signups ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def _do_match() -> dict:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM signups WHERE matched = 0").fetchall()
        people = [dict(r) for r in rows]
        past_rows = conn.execute("SELECT person_a, person_b FROM past_groups").fetchall()

    past_pairs: set[tuple] = {(r["person_a"], r["person_b"]) for r in past_rows}

    if len(people) < 4:
        raise HTTPException(
            status_code=400,
            detail=f"Not enough unmatched signups ({len(people)}). Need at least 4."
        )

    groups = run_matching(people, past_pairs=past_pairs)

    with get_db() as conn:
        for group_info in groups:
            members = group_info["members"]
            host = group_info["host"]
            needs_host = group_info["needs_host"]
            dinner_format = group_info["dinner_format"]
            ids = [str(m["id"]) for m in members]
            host_id = host["id"] if host else None

            cursor = conn.execute(
                "INSERT INTO groups (member_ids, dinner_format, group_size, host_id, needs_host) VALUES (?,?,?,?,?)",
                (json.dumps(ids), dinner_format, len(members), host_id, int(needs_host))
            )
            group_db_id = cursor.lastrowid

            conn.execute(
                f"UPDATE signups SET matched = 1, group_id = {group_db_id} WHERE id IN ({','.join(ids)})"
            )

            # Write all pairs to past_groups
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    pa, pb = min(a["id"], b["id"]), max(a["id"], b["id"])
                    conn.execute(
                        "INSERT OR IGNORE INTO past_groups (person_a, person_b) VALUES (?,?)",
                        (pa, pb)
                    )

        # Send notification emails after DB commit
        for group_info in groups:
            _notify_group(group_info)

    return {
        "groups_formed": len(groups),
        "people_matched": sum(len(g["members"]) for g in groups),
        "groups": [
            {
                "size": len(g["members"]),
                "dinner_format": g["dinner_format"],
                "needs_host": g["needs_host"],
                "host": g["host"]["name"] if g["host"] else None,
                "members": [
                    {"name": p["name"], "email": p["email"], "city": p.get("city")}
                    for p in g["members"]
                ],
            }
            for g in groups
        ],
    }


def _dietary_label(dietary: list) -> str:
    if not dietary:
        return "No restrictions"
    return ", ".join(dietary)


def _notify_group(group_info: dict) -> None:
    members = group_info["members"]
    host = group_info["host"]
    dinner_format = group_info["dinner_format"]

    for person in members:
        dietary = person.get("dietary") or []
        if isinstance(dietary, str):
            try:
                dietary = json.loads(dietary)
            except Exception:
                dietary = []

        is_host = host and person["id"] == host["id"]
        others = [m for m in members if m["id"] != person["id"]]

        if is_host:
            dietary_lines = "\n".join(
                f"  - {m['name']}: {_dietary_label(json.loads(m['dietary']) if isinstance(m.get('dietary'), str) else (m.get('dietary') or []))}"
                for m in others
            )
            body = (
                f"Hi {person['name']},\n\n"
                f"Great news - you've been matched with a Sobremesa group and you're the host!\n\n"
                f"Dinner format: {dinner_format}\n\n"
                f"Your group members and their dietary needs:\n{dietary_lines}\n\n"
                f"Your groupmates:\n"
                + "\n".join(f"  - {m['name']} ({m['email']})" for m in others)
                + "\n\nHave a wonderful dinner!\n\nThe Sobremesa team"
            )
            subject = "You've been matched - and you're the host!"
        else:
            body = (
                f"Hi {person['name']},\n\n"
                f"You've been matched with a Sobremesa group!\n\n"
                f"Dinner format: {dinner_format}\n\n"
                f"Your groupmates:\n"
                + "\n".join(f"  - {m['name']} ({m['email']})" for m in others)
                + "\n\nReach out to each other to arrange the details.\nHave a wonderful dinner!\n\nThe Sobremesa team"
            )
            subject = "You've been matched - dinner time!"

        _send_email(person["email"], subject, body)


@app.post("/admin/match")
def match():
    return _do_match()


@app.get("/admin/groups")
def list_groups():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM groups ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            ids = json.loads(row["member_ids"])
            members = conn.execute(
                f"SELECT name, email, neighbourhood, city FROM signups WHERE id IN ({','.join(ids)})"
            ).fetchall()
            result.append({
                "id": row["id"],
                "created_at": row["created_at"],
                "dinner_format": row["dinner_format"],
                "group_size": row["group_size"],
                "members": [dict(m) for m in members],
            })
    return result


# ── Auth endpoints ──

@app.post("/auth/request-otp")
def request_otp(body: OTPRequest):
    code = str(random.randint(100000, 999999))
    code_hash = pwd_ctx.hash(code)
    expires_at = (datetime.utcnow() + timedelta(minutes=10)).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO otp_tokens (email, code_hash, expires_at) VALUES (?,?,?)",
            (body.email, code_hash, expires_at)
        )
    # In production: send email via Resend/SendGrid. For now, print to console.
    print(f"[OTP] {body.email} → {code}")
    return {"message": "Code sent"}


@app.post("/auth/verify-otp")
def verify_otp(body: OTPVerify):
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM otp_tokens WHERE email = ? AND used = 0 AND expires_at > ? ORDER BY id DESC LIMIT 1",
            (body.email, now)
        ).fetchone()
        if not row or not pwd_ctx.verify(body.code, row["code_hash"]):
            raise HTTPException(status_code=400, detail="Invalid or expired code.")
        conn.execute("UPDATE otp_tokens SET used = 1 WHERE id = ?", (row["id"],))

        user = conn.execute("SELECT * FROM signups WHERE email = ?", (body.email,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="No signup found for this email.")

    token = _make_jwt(user["id"])
    return {"access_token": token, "user": _user_dict(dict(user))}


def _user_dict(u: dict) -> dict:
    for field in ("dietary", "availability"):
        if isinstance(u.get(field), str):
            try:
                u[field] = json.loads(u[field])
            except Exception:
                u[field] = []
    u.pop("auth_token_hash", None)
    return u


@app.get("/me")
def me(user=Depends(get_current_user)):
    return _user_dict(user)


@app.patch("/me")
def update_me(body: PatchMe, user=Depends(get_current_user)):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "dietary" in updates:
        updates["dietary"] = json.dumps(updates["dietary"])
    if "availability" in updates:
        updates["availability"] = json.dumps(updates["availability"])
    if not updates:
        return _user_dict(user)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with get_db() as conn:
        conn.execute(f"UPDATE signups SET {set_clause} WHERE id = ?", (*updates.values(), user["id"]))
        row = conn.execute("SELECT * FROM signups WHERE id = ?", (user["id"],)).fetchone()
    return _user_dict(dict(row))


@app.post("/push-token")
def register_push_token(body: PushTokenBody, user=Depends(get_current_user)):
    with get_db() as conn:
        conn.execute("UPDATE signups SET push_token = ? WHERE id = ?", (body.token, user["id"]))
    return {"ok": True}


@app.get("/group")
def get_group(user=Depends(get_current_user)):
    gid = user.get("group_id")
    if not gid:
        raise HTTPException(status_code=404, detail="Not matched yet.")
    with get_db() as conn:
        group = conn.execute("SELECT * FROM groups WHERE id = ?", (gid,)).fetchone()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found.")
        ids = json.loads(group["member_ids"])
        members = conn.execute(
            f"SELECT id, name, neighbourhood, city, avatar_url, bio FROM signups WHERE id IN ({','.join(ids)})"
        ).fetchall()
    return {
        "id": group["id"],
        "created_at": group["created_at"],
        "dinner_format": group["dinner_format"],
        "members": [dict(m) for m in members],
    }


@app.get("/group/messages")
def get_messages(before: Optional[int] = None, limit: int = 40, user=Depends(get_current_user)):
    gid = user.get("group_id")
    if not gid:
        raise HTTPException(status_code=404, detail="Not matched yet.")
    with get_db() as conn:
        if before:
            rows = conn.execute(
                "SELECT m.*, s.name as sender_name FROM messages m JOIN signups s ON m.sender_id = s.id WHERE m.group_id = ? AND m.id < ? ORDER BY m.sent_at DESC LIMIT ?",
                (gid, before, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT m.*, s.name as sender_name FROM messages m JOIN signups s ON m.sender_id = s.id WHERE m.group_id = ? ORDER BY m.sent_at DESC LIMIT ?",
                (gid, limit)
            ).fetchall()
    return {"messages": [dict(r) for r in reversed(rows)]}


@app.websocket("/ws/group/{group_id}")
async def ws_group(websocket: WebSocket, group_id: int, token: str):
    try:
        user_id = _verify_jwt(token)
    except HTTPException:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    if group_id not in ws_connections:
        ws_connections[group_id] = set()
    ws_connections[group_id].add((websocket, user_id))

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "send" and data.get("body"):
                body = str(data["body"])[:2000]
                with get_db() as conn:
                    cursor = conn.execute(
                        "INSERT INTO messages (group_id, sender_id, body) VALUES (?,?,?)",
                        (group_id, user_id, body)
                    )
                    msg_id = cursor.lastrowid
                    sender = conn.execute("SELECT name FROM signups WHERE id = ?", (user_id,)).fetchone()
                msg = {
                    "type": "message",
                    "id": msg_id,
                    "sender_id": user_id,
                    "sender_name": sender["name"] if sender else "Unknown",
                    "body": body,
                    "sent_at": datetime.utcnow().isoformat(),
                }
                dead = set()
                for (ws, _) in ws_connections.get(group_id, set()):
                    try:
                        await ws.send_json(msg)
                    except Exception:
                        dead.add((ws, _))
                ws_connections[group_id] -= dead
    except WebSocketDisconnect:
        ws_connections[group_id].discard((websocket, user_id))


# ── Dinner endpoints ──

def _get_or_create_dinner(conn, group_id: int) -> dict:
    row = conn.execute("SELECT * FROM dinners WHERE group_id = ?", (group_id,)).fetchone()
    if not row:
        conn.execute("INSERT INTO dinners (group_id) VALUES (?)", (group_id,))
        row = conn.execute("SELECT * FROM dinners WHERE group_id = ?", (group_id,)).fetchone()
    return dict(row)


@app.get("/group/dinner")
def get_dinner(user=Depends(get_current_user)):
    gid = user.get("group_id")
    if not gid:
        raise HTTPException(status_code=404, detail="Not matched yet.")
    with get_db() as conn:
        dinner = _get_or_create_dinner(conn, gid)
        did = dinner["id"]
        votes_raw = conn.execute(
            "SELECT slot, COUNT(*) as votes FROM dinner_votes WHERE dinner_id = ? GROUP BY slot",
            (did,)
        ).fetchall()
        rsvps_raw = conn.execute(
            "SELECT r.user_id, s.name, r.status FROM rsvps r JOIN signups s ON r.user_id = s.id WHERE r.dinner_id = ?",
            (did,)
        ).fetchall()
        dishes_raw = conn.execute(
            "SELECT d.*, s.name FROM dishes d JOIN signups s ON d.user_id = s.id WHERE d.dinner_id = ?",
            (did,)
        ).fetchall()
    return {
        "id": did,
        "group_id": gid,
        "venue_suggestion": dinner["venue_suggestion"],
        "venue_confirmed": bool(dinner["venue_confirmed"]),
        "confirmed_slot": dinner["confirmed_slot"],
        "date_votes": [{"slot": r["slot"], "votes": r["votes"]} for r in votes_raw],
        "rsvps": [{"user_id": r["user_id"], "name": r["name"], "status": r["status"]} for r in rsvps_raw],
        "dishes": [{"id": r["id"], "user_id": r["user_id"], "name": r["name"], "description": r["description"], "category": r["category"]} for r in dishes_raw],
    }


@app.post("/group/dinner/vote")
def dinner_vote(body: VoteBody, user=Depends(get_current_user)):
    gid = user.get("group_id")
    if not gid:
        raise HTTPException(status_code=404, detail="Not matched yet.")
    with get_db() as conn:
        dinner = _get_or_create_dinner(conn, gid)
        conn.execute(
            "INSERT OR REPLACE INTO dinner_votes (dinner_id, user_id, slot) VALUES (?,?,?)",
            (dinner["id"], user["id"], body.slot)
        )
    return {"ok": True}


@app.post("/group/dinner/rsvp")
def dinner_rsvp(body: RSVPBody, user=Depends(get_current_user)):
    gid = user.get("group_id")
    if not gid:
        raise HTTPException(status_code=404, detail="Not matched yet.")
    with get_db() as conn:
        dinner = _get_or_create_dinner(conn, gid)
        conn.execute(
            "INSERT OR REPLACE INTO rsvps (dinner_id, user_id, status) VALUES (?,?,?)",
            (dinner["id"], user["id"], body.status)
        )
    return {"ok": True}


@app.post("/group/dinner/dish")
def add_dish(body: DishBody, user=Depends(get_current_user)):
    gid = user.get("group_id")
    if not gid:
        raise HTTPException(status_code=404, detail="Not matched yet.")
    with get_db() as conn:
        dinner = _get_or_create_dinner(conn, gid)
        conn.execute(
            "INSERT INTO dishes (dinner_id, user_id, description, category) VALUES (?,?,?,?)",
            (dinner["id"], user["id"], body.description, body.category)
        )
    return {"ok": True}


@app.delete("/group/dinner/dish/{dish_id}")
def delete_dish(dish_id: int, user=Depends(get_current_user)):
    with get_db() as conn:
        dish = conn.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone()
        if not dish:
            raise HTTPException(status_code=404, detail="Dish not found.")
        if dish["user_id"] != user["id"]:
            raise HTTPException(status_code=403, detail="Not your dish.")
        conn.execute("DELETE FROM dishes WHERE id = ?", (dish_id,))
    return {"ok": True}
