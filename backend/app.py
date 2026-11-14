from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from auth import authenticate, issue_token, read_user
from db import (
    KeyMismatch,
    KeyPinned,
    KeySettingMissing,
    connect,
    current_setting,
    init_db,
    list_conflicts,
    list_settings,
    save_setting,
    setting_detail,
    submit_batch,
)
from dedup import normalize_parts, option_payload, parts_label, today_local
from parser import FLAGS


class LoginBody(BaseModel):
    username: str
    password: str


class KeyBody(BaseModel):
    parts: list[str]
    key_date: date | None = None


class BatchBody(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    raw_text: str = Field(min_length=1)
    key_parts: list[str] | None = None


def _stamp(value):
    return value.isoformat(sep=" ", timespec="minutes") if value else None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(lifespan=lifespan)


def _user_or_401(authorization: str | None):
    user = read_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _writer_or_403(authorization: str | None):
    user = _user_or_401(authorization)
    if user["role"] != "writer":
        raise HTTPException(status_code=403, detail="档案员不能归集报文")
    return user


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "service": "tide-telegram-intake"}


@app.post("/api/login")
def login(body: LoginBody):
    user = authenticate(body.username.strip(), body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {
        "access_token": issue_token(user),
        "username": user["username"],
        "role": user["role"],
        "title": user["title"],
    }


@app.get("/api/dedup-key/options")
def dedup_key_options(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    return {"options": option_payload(), "today": today_local().isoformat()}


@app.get("/api/dedup-key")
def current_dedup_key(authorization: str | None = Header(default=None)):
    """页面用它读出当前用的是哪一种去重键。"""
    _user_or_401(authorization)
    with connect() as conn:
        setting = current_setting(conn)
    return {
        "today": today_local().isoformat(),
        "setting": setting,
        "options": option_payload(),
        "ready": bool(setting),
    }


@app.put("/api/dedup-key")
def put_dedup_key(body: KeyBody, authorization: str | None = Header(default=None)):
    user = _writer_or_403(authorization)
    parts = normalize_parts(body.parts)
    if parts is None:
        raise HTTPException(
            status_code=400,
            detail="键组成只能是站码 + 观测时刻，或站码 + 观测时刻 + 质量旗",
        )
    key_date = body.key_date or today_local()
    with connect() as conn:
        try:
            row, created = save_setting(conn, key_date, parts, user["username"])
            conn.commit()
        except KeyPinned:
            existing = conn.execute(
                "SELECT parts FROM dedup_key_settings WHERE key_date = %s", (key_date,)
            ).fetchone()
            pinned = list(existing["parts"]) if existing else []
            raise HTTPException(
                status_code=409,
                detail=f"当天去重键已钉住为 {parts_label(pinned)}，不能再更换",
            )
    setting = {
        "id": row["id"],
        "key_date": row["key_date"].isoformat(),
        "parts": list(row["parts"]),
        "label": parts_label(row["parts"]),
        "name": row["key_name"],
        "status": row["status"],
        "pinned_by": row["pinned_by"],
        "pinned_at": _stamp(row["pinned_at"]),
    }
    return {"setting": setting, "created": created}


@app.get("/api/dedup-keys")
def dedup_key_list(authorization: str | None = Header(default=None)):
    """键列表：键组成、冲突行数、键名。"""
    _user_or_401(authorization)
    with connect() as conn:
        return {"keys": list_settings(conn), "today": today_local().isoformat()}


@app.get("/api/dedup-keys/{key_date}")
def dedup_key_detail(key_date: date, authorization: str | None = Header(default=None)):
    """键详情：展开被挡住的组合。"""
    _user_or_401(authorization)
    with connect() as conn:
        detail = setting_detail(conn, key_date)
        if not detail:
            raise HTTPException(status_code=404, detail="这一天还没有键设置")
        detail["options"] = option_payload()
    return detail


@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = list_conflicts(conn)
        setting = current_setting(conn)
    return {"conflicts": rows, "setting": setting}


@app.get("/api/batches")
def list_batches(authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, status, accepted_count, rejected_count, conflict_count,
                   dedup_parts, dedup_key_name, key_date, created_by, created_at
            FROM intake_batches
            ORDER BY id DESC
            """
        ).fetchall()
    for row in rows:
        row["created_at"] = _stamp(row["created_at"])
        row["key_date"] = row["key_date"].isoformat() if row["key_date"] else None
        row["dedup_label"] = parts_label(row["dedup_parts"])
    return rows


@app.get("/api/batches/{batch_id}")
def batch_detail(batch_id: int, authorization: str | None = Header(default=None)):
    _user_or_401(authorization)
    with connect() as conn:
        batch = conn.execute(
            """
            SELECT id, title, raw_text, status, accepted_count, rejected_count, conflict_count,
                   dedup_parts, dedup_key_name, key_date, created_by, created_at
            FROM intake_batches WHERE id = %s
            """,
            (batch_id,),
        ).fetchone()
        if not batch:
            raise HTTPException(status_code=404, detail="没有这一批报文")
        landed = conn.execute(
            """
            SELECT line_no, station_code, observed_at, tide_cm, quality_flag,
                   dedup_key, dedup_key_name, dedup_parts
            FROM landed_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        rejected = conn.execute(
            """
            SELECT line_no, raw_line, reason
            FROM reject_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
        conflicted = conn.execute(
            """
            SELECT line_no, raw_line, reason, station_code, observed_at, quality_flag
            FROM conflict_rows WHERE batch_id = %s ORDER BY line_no
            """,
            (batch_id,),
        ).fetchall()
    batch["created_at"] = _stamp(batch["created_at"])
    batch["key_date"] = batch["key_date"].isoformat() if batch["key_date"] else None
    batch["dedup_label"] = parts_label(batch["dedup_parts"])
    for row in landed:
        row["observed_at"] = _stamp(row["observed_at"])
        row["flag_label"] = FLAGS.get(row["quality_flag"], row["quality_flag"])
    for row in conflicted:
        row["observed_at"] = _stamp(row["observed_at"])
    return {"batch": batch, "landed": landed, "rejected": rejected, "conflicts": conflicted}


@app.post("/api/batches")
def create_batch(body: BatchBody, authorization: str | None = Header(default=None)):
    user = _writer_or_403(authorization)
    title = body.title.strip()
    raw_text = body.raw_text.strip()
    if not title or not raw_text:
        raise HTTPException(status_code=400, detail="标题和报文都不能空")
    carried = None
    if body.key_parts is not None:
        carried = normalize_parts(body.key_parts)
        if carried is None:
            raise HTTPException(
                status_code=400,
                detail="键组成只能是站码 + 观测时刻，或站码 + 观测时刻 + 质量旗",
            )
    try:
        batch_id = submit_batch(title, raw_text, user["username"], carried)
    except KeySettingMissing:
        raise HTTPException(
            status_code=409,
            detail="当天去重键还没有保存，先定好键组成再归集",
        )
    except KeyMismatch as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": exc.reason,
                "carried_parts": list(exc.carried),
                "pinned_parts": list(exc.pinned),
                "reason_code": "key_mismatch"
                if "键争用" not in exc.reason
                else "key_clash",
            },
        )
    return batch_detail(batch_id, authorization)


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
