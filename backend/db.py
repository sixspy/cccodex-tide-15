import os
import time
from datetime import date, datetime, timezone

import psycopg
from psycopg.rows import dict_row

from dedup import (
    FLAG,
    OPTION_PARTS,
    identity_label,
    key_from_parts,
    name_for_parts,
    normalize_parts,
    parts_label,
    today_local,
)
from parser import parse_telegram_line

# 原因代码。SQL 里一律按代码筛选，展示文案单独放中文。
REASON_FORMAT = "format"
REASON_CONFLICT = "conflict"
REASON_DUPLICATE = "duplicate"
REASON_DUP_IN_BATCH = "dup_in_batch"
REASON_KEY_CLASH = "key_clash"
REASON_KEY_CLASH_PARTS = "key_clash_parts"
REASON_MISMATCH = "key_mismatch"

BLOCKING_CODES = (
    REASON_DUPLICATE,
    REASON_DUP_IN_BATCH,
    REASON_KEY_CLASH,
    REASON_KEY_CLASH_PARTS,
)
BLOCKING_CODE_SQL = ", ".join("'%s'" % code for code in BLOCKING_CODES)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dedup_key_settings (
    id BIGSERIAL PRIMARY KEY,
    key_date DATE NOT NULL UNIQUE,
    parts TEXT[] NOT NULL,
    key_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pinned',
    pinned_by TEXT NOT NULL,
    pinned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS intake_batches (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    status TEXT NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    dedup_parts TEXT[] NOT NULL,
    dedup_key_name TEXT NOT NULL,
    key_date DATE NOT NULL,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS landed_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL,
    tide_cm INTEGER NOT NULL,
    quality_flag TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    dedup_key_name TEXT NOT NULL,
    dedup_parts TEXT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reject_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    raw_line TEXT NOT NULL,
    reason TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT 'format',
    station_code TEXT,
    observed_at TIMESTAMP,
    quality_flag TEXT,
    dedup_key TEXT
);

CREATE TABLE IF NOT EXISTS conflict_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    raw_line TEXT NOT NULL,
    reason TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT 'conflict',
    station_code TEXT,
    observed_at TIMESTAMP,
    quality_flag TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS intake_audit (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    reason TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT 'key_mismatch',
    carried_parts TEXT[],
    pinned_parts TEXT[],
    key_date DATE,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# 旧版本只有“站码 + 时刻”这一个去重约束，也没有键设置、冲突队列和键不符留痕。
MIGRATIONS = """
ALTER TABLE intake_batches ADD COLUMN IF NOT EXISTS conflict_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE intake_batches ADD COLUMN IF NOT EXISTS dedup_parts TEXT[];
ALTER TABLE intake_batches ADD COLUMN IF NOT EXISTS dedup_key_name TEXT;
ALTER TABLE intake_batches ADD COLUMN IF NOT EXISTS key_date DATE;
UPDATE intake_batches
SET dedup_parts = '{station_code,observed_at}' WHERE dedup_parts IS NULL;
UPDATE intake_batches
SET dedup_key_name = '站时键' WHERE dedup_key_name IS NULL;
UPDATE intake_batches
SET key_date = created_at::date WHERE key_date IS NULL;
ALTER TABLE intake_batches ALTER COLUMN dedup_parts SET NOT NULL;
ALTER TABLE intake_batches ALTER COLUMN dedup_key_name SET NOT NULL;
ALTER TABLE intake_batches ALTER COLUMN key_date SET NOT NULL;

ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS dedup_key TEXT;
ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS dedup_key_name TEXT;
ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS dedup_parts TEXT[];
ALTER TABLE landed_rows ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
UPDATE landed_rows
SET dedup_key = 'station_code:observed_at=' || station_code || '|' || to_char(observed_at, 'YYYYMMDDHH24MI')
WHERE dedup_key IS NULL;
UPDATE landed_rows SET dedup_key_name = '站时键' WHERE dedup_key_name IS NULL;
UPDATE landed_rows SET dedup_parts = '{station_code,observed_at}' WHERE dedup_parts IS NULL;
ALTER TABLE landed_rows ALTER COLUMN dedup_key SET NOT NULL;
ALTER TABLE landed_rows ALTER COLUMN dedup_key_name SET NOT NULL;
ALTER TABLE landed_rows ALTER COLUMN dedup_parts SET NOT NULL;
ALTER TABLE landed_rows DROP CONSTRAINT IF EXISTS landed_rows_station_code_observed_at_key;
CREATE UNIQUE INDEX IF NOT EXISTS landed_rows_dedup_key_idx ON landed_rows (dedup_key);
CREATE INDEX IF NOT EXISTS landed_rows_identity_idx ON landed_rows (station_code, observed_at);

ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS reason_code TEXT;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS station_code TEXT;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS observed_at TIMESTAMP;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS quality_flag TEXT;
ALTER TABLE reject_rows ADD COLUMN IF NOT EXISTS dedup_key TEXT;
UPDATE reject_rows SET reason_code = 'duplicate'
WHERE reason_code IS NULL AND position('已经落过库' in reason) > 0;
UPDATE reject_rows SET reason_code = 'format' WHERE reason_code IS NULL;
ALTER TABLE reject_rows ALTER COLUMN reason_code SET DEFAULT 'format';
ALTER TABLE reject_rows ALTER COLUMN reason_code SET NOT NULL;

CREATE TABLE IF NOT EXISTS conflict_rows (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES intake_batches(id),
    line_no INTEGER NOT NULL,
    raw_line TEXT NOT NULL,
    reason TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT 'conflict',
    station_code TEXT,
    observed_at TIMESTAMP,
    quality_flag TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS intake_audit (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    reason TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT 'key_mismatch',
    carried_parts TEXT[],
    pinned_parts TEXT[],
    key_date DATE,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE conflict_rows ADD COLUMN IF NOT EXISTS reason_code TEXT;
UPDATE conflict_rows SET reason_code = 'conflict' WHERE reason_code IS NULL;
ALTER TABLE conflict_rows ALTER COLUMN reason_code SET DEFAULT 'conflict';
ALTER TABLE conflict_rows ALTER COLUMN reason_code SET NOT NULL;

ALTER TABLE intake_audit ADD COLUMN IF NOT EXISTS reason_code TEXT;
UPDATE intake_audit SET reason_code = 'key_mismatch' WHERE reason_code IS NULL;
ALTER TABLE intake_audit ALTER COLUMN reason_code SET DEFAULT 'key_mismatch';
ALTER TABLE intake_audit ALTER COLUMN reason_code SET NOT NULL;

INSERT INTO dedup_key_settings (key_date, parts, key_name, status, pinned_by)
SELECT DISTINCT ON (b.key_date)
       b.key_date, b.dedup_parts, b.dedup_key_name, 'pinned', 'admin'
FROM intake_batches b
WHERE NOT EXISTS (
    SELECT 1 FROM dedup_key_settings s WHERE s.key_date = b.key_date
)
ORDER BY b.key_date, b.id;
"""

SEED_CLEAN = """吴淞口 11-12 早班
WG01|202611120830|186|A
WG01|202611120900|191|A
BG02|202611120830|142|M
"""

SEED_BROKEN = """外高桥 11-12 错报
LG04|202611121000|205|A
WG01|202611120830|186|A
WG0X|202611121000|10|A
"""

SEED_KEY_DATE = date(2026, 11, 12)


class KeySettingMissing(Exception):
    """当天还没有保存键设置。"""


class KeyMismatch(Exception):
    """归集携带的键组成与当天键设置不一致。"""

    def __init__(self, carried, pinned, reason="去重键不符，这批不落库"):
        super().__init__(reason)
        self.carried = tuple(carried) if carried else ()
        self.pinned = tuple(pinned) if pinned else ()
        self.reason = reason


class KeyPinned(Exception):
    """当天键组成已经钉住，不能再换。"""


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def _lock_day(conn, key_date, *, shared=False):
    """键设置的读写锁：保存键设置持排他锁，归集持共享锁。"""
    fn = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    conn.execute(f"SELECT {fn}(hashtextextended(%s, 0))", (f"dedup-key:{key_date}",))


def _lock_identity(conn, station_code, observed_at):
    """同一站同一时刻共用一把行锁；不同键组成撞同一行时在这里排队。"""
    token = f"dedup-row:{station_code}:{observed_at:%Y%m%d%H%M}"
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (token,))


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                conn.execute(MIGRATIONS)
                count = conn.execute("SELECT COUNT(*) AS n FROM intake_batches").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _setting_payload(row):
    if not row:
        return None
    parts = tuple(row["parts"])
    return {
        "id": row["id"],
        "key_date": row["key_date"].isoformat(),
        "parts": list(parts),
        "label": parts_label(parts),
        "name": row["key_name"],
        "status": row["status"],
        "pinned_by": row["pinned_by"],
        "pinned_at": row["pinned_at"].isoformat() if row["pinned_at"] else None,
    }


def get_setting(conn, key_date, *, share=False):
    suffix = " FOR SHARE" if share else ""
    return conn.execute(
        f"SELECT id, key_date, parts, key_name, status, pinned_by, pinned_at "
        f"FROM dedup_key_settings WHERE key_date = %s{suffix}",
        (key_date,),
    ).fetchone()


def save_setting(conn, key_date, parts, username):
    """保存当天的键设置；第一次保存即钉住，之后只能沿用同一组成。"""
    parts = normalize_parts(parts)
    if parts is None:
        raise ValueError("键组成只能是站码 + 观测时刻，或站码 + 观测时刻 + 质量旗")
    _lock_day(conn, key_date)
    existing = get_setting(conn, key_date)
    if existing:
        if tuple(existing["parts"]) != parts:
            raise KeyPinned()
        return existing, False
    row = conn.execute(
        """
        INSERT INTO dedup_key_settings (key_date, parts, key_name, status, pinned_by)
        VALUES (%s, %s, %s, 'pinned', %s)
        ON CONFLICT (key_date) DO NOTHING
        RETURNING id, key_date, parts, key_name, status, pinned_by, pinned_at
        """,
        (key_date, list(parts), name_for_parts(parts), username),
    ).fetchone()
    if row is None:
        row = get_setting(conn, key_date)
        if row is None or tuple(row["parts"]) != parts:
            raise KeyPinned()
        return row, False
    return row, True


def list_settings(conn):
    rows = conn.execute(
        f"""
        SELECT s.id, s.key_date, s.parts, s.key_name, s.status, s.pinned_by, s.pinned_at,
               COALESCE((
                   SELECT COUNT(*) FROM conflict_rows c
                   JOIN intake_batches b ON b.id = c.batch_id
                   WHERE b.key_date = s.key_date
               ), 0) AS conflict_count,
               COALESCE((
                   SELECT COUNT(*) FROM reject_rows r
                   JOIN intake_batches b ON b.id = r.batch_id
                   WHERE b.key_date = s.key_date
                     AND r.reason_code IN ({BLOCKING_CODE_SQL})
               ), 0) AS duplicate_count,
               COALESCE((
                   SELECT COUNT(*) FROM intake_audit a
                   WHERE a.key_date = s.key_date
               ), 0) AS mismatch_count,
               COALESCE((
                   SELECT COUNT(*) FROM intake_audit a
                   WHERE a.key_date = s.key_date AND a.reason_code = '{REASON_MISMATCH}'
               ), 0) AS key_mismatch_count,
               COALESCE((
                   SELECT COUNT(*) FROM intake_audit a
                   WHERE a.key_date = s.key_date AND a.reason_code = '{REASON_KEY_CLASH}'
               ), 0) AS key_clash_count,
               COALESCE((
                   SELECT COUNT(*) FROM intake_batches b WHERE b.key_date = s.key_date
               ), 0) AS batch_count,
               COALESCE((
                   SELECT COUNT(*) FROM landed_rows l
                   JOIN intake_batches b ON b.id = l.batch_id
                   WHERE b.key_date = s.key_date
               ), 0) AS landed_count
        FROM dedup_key_settings s
        ORDER BY s.key_date DESC
        """
    ).fetchall()
    result = []
    for row in rows:
        item = _setting_payload(row)
        item["conflict_count"] = row["conflict_count"]
        item["duplicate_count"] = row["duplicate_count"]
        item["blocked_count"] = row["conflict_count"] + row["duplicate_count"]
        item["mismatch_count"] = row["mismatch_count"]
        item["key_mismatch_count"] = row["key_mismatch_count"]
        item["key_clash_count"] = row["key_clash_count"]
        item["batch_count"] = row["batch_count"]
        item["landed_count"] = row["landed_count"]
        result.append(item)
    return result


def _summary_for_date(conn, key_date):
    row = conn.execute(
        f"""
        SELECT COALESCE((
                   SELECT COUNT(*) FROM conflict_rows c
                   JOIN intake_batches b ON b.id = c.batch_id
                   WHERE b.key_date = %s
               ), 0) AS conflict_count,
               COALESCE((
                   SELECT COUNT(*) FROM reject_rows r
                   JOIN intake_batches b ON b.id = r.batch_id
                   WHERE b.key_date = %s AND r.reason_code IN ({BLOCKING_CODE_SQL})
               ), 0) AS duplicate_count,
               COALESCE((
                   SELECT COUNT(*) FROM intake_audit a WHERE a.key_date = %s
               ), 0) AS mismatch_count,
               COALESCE((
                   SELECT COUNT(*) FROM intake_audit a
                   WHERE a.key_date = %s AND a.reason_code = '{REASON_MISMATCH}'
               ), 0) AS key_mismatch_count,
               COALESCE((
                   SELECT COUNT(*) FROM intake_audit a
                   WHERE a.key_date = %s AND a.reason_code = '{REASON_KEY_CLASH}'
               ), 0) AS key_clash_count
        """,
        (key_date, key_date, key_date, key_date, key_date),
    ).fetchone()
    return row


def current_setting(conn, key_date=None):
    key_date = key_date or today_local()
    row = get_setting(conn, key_date)
    setting = _setting_payload(row)
    if setting:
        summary = _summary_for_date(conn, key_date)
        setting.update(
            {
                "conflict_count": summary["conflict_count"],
                "duplicate_count": summary["duplicate_count"],
                "blocked_count": summary["conflict_count"] + summary["duplicate_count"],
                "mismatch_count": summary["mismatch_count"],
                "key_mismatch_count": summary["key_mismatch_count"],
                "key_clash_count": summary["key_clash_count"],
            }
        )
    return setting


def _format_observed(value):
    return value.isoformat(sep=" ", timespec="minutes") if value else None


def setting_detail(conn, key_date):
    raw = get_setting(conn, key_date)
    if not raw:
        return None
    setting = _setting_payload(raw)
    parts = tuple(raw["parts"])
    conflicts = conn.execute(
        """
        SELECT c.station_code, c.observed_at, c.quality_flag, c.reason,
               COUNT(*) AS hits,
               ARRAY_AGG(c.line_no ORDER BY c.line_no) AS line_nos
        FROM conflict_rows c
        JOIN intake_batches b ON b.id = c.batch_id
        WHERE b.key_date = %s
        GROUP BY c.station_code, c.observed_at, c.quality_flag, c.reason
        ORDER BY c.station_code NULLS LAST, c.observed_at NULLS LAST, c.reason
        """,
        (key_date,),
    ).fetchall()
    duplicates = conn.execute(
        f"""
        SELECT r.station_code, r.observed_at, r.quality_flag, r.reason, r.reason_code,
               COUNT(*) AS hits,
               ARRAY_AGG(r.line_no ORDER BY r.line_no) AS line_nos
        FROM reject_rows r
        JOIN intake_batches b ON b.id = r.batch_id
        WHERE b.key_date = %s
          AND r.reason_code IN ({BLOCKING_CODE_SQL})
        GROUP BY r.station_code, r.observed_at, r.quality_flag, r.reason, r.reason_code
        ORDER BY r.station_code NULLS LAST, r.observed_at NULLS LAST, r.reason
        """,
        (key_date,),
    ).fetchall()
    audits = conn.execute(
        """
        SELECT id, title, raw_text, reason, reason_code, carried_parts, pinned_parts,
               created_by, created_at
        FROM intake_audit
        WHERE key_date = %s
        ORDER BY id DESC
        """,
        (key_date,),
    ).fetchall()

    blocked = []
    for row in conflicts:
        identity = {
            "station_code": row["station_code"],
            "observed_at": row["observed_at"],
            "quality_flag": row["quality_flag"],
        }
        blocked.append(
            {
                "kind": "冲突",
                "reason_code": REASON_CONFLICT,
                "identity": identity_label(parts, identity),
                "reason": row["reason"],
                "hits": row["hits"],
                "line_nos": row["line_nos"],
            }
        )
    for row in duplicates:
        identity = {
            "station_code": row["station_code"],
            "observed_at": row["observed_at"],
            "quality_flag": row["quality_flag"],
        }
        if row["reason_code"] in (REASON_KEY_CLASH, REASON_KEY_CLASH_PARTS):
            kind = "键争用"
        else:
            kind = "重复"
        blocked.append(
            {
                "kind": kind,
                "reason_code": row["reason_code"],
                "identity": identity_label(parts, identity),
                "reason": row["reason"],
                "hits": row["hits"],
                "line_nos": row["line_nos"],
            }
        )
    for row in audits:
        blocked.append(
            {
                "kind": "键不符",
                "reason_code": row["reason_code"],
                "identity": "整批携带的键与键设置不一致",
                "reason": row["reason"],
                "hits": 1,
                "line_nos": [],
                "title": row["title"],
                "raw_text": row["raw_text"],
                "carried_parts": list(row["carried_parts"]) if row["carried_parts"] else None,
                "pinned_parts": list(row["pinned_parts"]) if row["pinned_parts"] else None,
            }
        )
    setting["blocked"] = blocked
    summary = _summary_for_date(conn, key_date)
    setting.update(
        {
            "conflict_count": summary["conflict_count"],
            "duplicate_count": summary["duplicate_count"],
            "blocked_count": summary["conflict_count"] + summary["duplicate_count"],
            "mismatch_count": summary["mismatch_count"],
            "key_mismatch_count": summary["key_mismatch_count"],
        }
    )
    return setting


def list_conflicts(conn):
    rows = conn.execute(
        """
        SELECT c.id, c.batch_id, b.title AS batch_title, b.key_date,
               c.line_no, c.raw_line, c.reason, c.reason_code,
               c.station_code, c.observed_at, c.quality_flag, c.created_at
        FROM conflict_rows c
        JOIN intake_batches b ON b.id = c.batch_id
        ORDER BY c.id DESC
        LIMIT 300
        """
    ).fetchall()
    for row in rows:
        row["observed_at"] = _format_observed(row["observed_at"])
        row["key_date"] = row["key_date"].isoformat() if row["key_date"] else None
        row["created_at"] = row["created_at"].isoformat(sep=" ", timespec="minutes")
    return rows


def _duplicate_reason(parts):
    if FLAG in tuple(parts):
        return "这个站这个时刻这个质量旗已经落过库"
    return "这个站这个时刻已经落过库"


def _insert_batch(conn, title, raw_text, created_by, parts, key_date):
    """解析并归集一批；调用方已持当天的共享键锁并核对过键组成。"""
    started_at = datetime.now(timezone.utc)
    batch = conn.execute(
        """
        INSERT INTO intake_batches
            (title, raw_text, status, accepted_count, rejected_count, conflict_count,
             dedup_parts, dedup_key_name, key_date, created_by)
        VALUES (%s, %s, '处理中', 0, 0, 0, %s, %s, %s, %s)
        RETURNING id
        """,
        (title, raw_text, list(parts), name_for_parts(parts), key_date, created_by),
    ).fetchone()
    batch_id = batch["id"]
    accepted = 0
    rejected = 0
    conflicted = 0
    seen = set()

    def add_reject(line_no, parsed, reason, code, dedup_key=None):
        conn.execute(
            """
            INSERT INTO reject_rows
                (batch_id, line_no, raw_line, reason, reason_code,
                 station_code, observed_at, quality_flag, dedup_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                batch_id,
                line_no,
                parsed["raw_line"],
                reason,
                code,
                parsed["station_code"],
                parsed["observed_at"],
                parsed["quality_flag"],
                dedup_key,
            ),
        )

    rows = []
    for line_no, raw_line in enumerate(raw_text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        rows.append((line_no, parse_telegram_line(raw_line, parts)))

    # 先把这一批要碰的站与时刻按固定顺序全部加锁，避免两批行序相反时互相死锁。
    identities = sorted(
        {
            (parsed["station_code"], parsed["observed_at"])
            for _, parsed in rows
            if parsed["status"] == "ok"
        }
    )
    for station_code, observed_at in identities:
        _lock_identity(conn, station_code, observed_at)

    for line_no, parsed in rows:
        if parsed["status"] == "reject":
            add_reject(line_no, parsed, parsed["reason"], REASON_FORMAT)
            rejected += 1
            continue
        if parsed["status"] == "conflict":
            conn.execute(
                """
                INSERT INTO conflict_rows
                    (batch_id, line_no, raw_line, reason, reason_code,
                     station_code, observed_at, quality_flag)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    batch_id,
                    line_no,
                    parsed["raw_line"],
                    parsed["reason"],
                    parsed.get("code") or REASON_CONFLICT,
                    parsed["station_code"],
                    parsed["observed_at"],
                    parsed["quality_flag"],
                ),
            )
            conflicted += 1
            continue

        dedup_key = key_from_parts(parts, parsed)
        existing = conn.execute(
            "SELECT id, batch_id, dedup_parts, created_at FROM landed_rows "
            "WHERE dedup_key = %s",
            (dedup_key,),
        ).fetchone()
        rival = None
        if existing is None:
            # 同一天里，同一个站与时刻若已被另一套键组成占住，后来者记键争用。
            rival = conn.execute(
                """
                SELECT l.id, l.dedup_parts
                FROM landed_rows l
                JOIN intake_batches b ON b.id = l.batch_id
                WHERE b.key_date = %s AND l.station_code = %s AND l.observed_at = %s
                  AND l.dedup_parts <> %s
                LIMIT 1
                """,
                (key_date, parsed["station_code"], parsed["observed_at"], list(parts)),
            ).fetchone()
        if dedup_key in seen:
            reason, code = "同一批里键重复", REASON_DUP_IN_BATCH
        elif rival:
            reason, code = "键争用：这一行已按另一套键组成落库", REASON_KEY_CLASH_PARTS
        elif existing:
            if existing["batch_id"] != batch_id and existing["created_at"] >= started_at:
                reason, code = "键争用：另一批同时落了这个键", REASON_KEY_CLASH
            else:
                reason, code = _duplicate_reason(parts), REASON_DUPLICATE
        else:
            inserted = conn.execute(
                """
                INSERT INTO landed_rows
                    (batch_id, line_no, station_code, observed_at, tide_cm, quality_flag,
                     dedup_key, dedup_key_name, dedup_parts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (dedup_key) DO NOTHING
                RETURNING id
                """,
                (
                    batch_id,
                    line_no,
                    parsed["station_code"],
                    parsed["observed_at"],
                    parsed["tide_cm"],
                    parsed["quality_flag"],
                    dedup_key,
                    name_for_parts(parts),
                    list(parts),
                ),
            ).fetchone()
            if inserted:
                reason, code = None, None
                _mark_contested(conn, key_date, parsed["station_code"], parsed["observed_at"])
            else:
                reason, code = "键争用：另一批同时落了这个键", REASON_KEY_CLASH
        if reason:
            add_reject(line_no, parsed, reason, code, dedup_key)
            rejected += 1
            continue
        seen.add(dedup_key)
        accepted += 1

    if accepted and not rejected and not conflicted:
        status = "已归集"
    elif accepted:
        status = "部分接纳"
    else:
        status = "未接纳"
    conn.execute(
        """
        UPDATE intake_batches
        SET status = %s, accepted_count = %s, rejected_count = %s, conflict_count = %s
        WHERE id = %s
        """,
        (status, accepted, rejected, conflicted, batch_id),
    )
    return batch_id


def submit_batch(title, raw_text, username, carried_parts=None):
    """归集一批。

    键设置在解析之前核对：当天没保存设置不许归集；携带的键与设置不一致时
    整批不落库，只写一条键不符留痕（若同键已被另一套组成占住，同时记键争用）。
    """
    key_date = today_local()
    with connect() as conn:
        _lock_day(conn, key_date, shared=True)
        raw_setting = get_setting(conn, key_date)
        if not raw_setting or raw_setting["status"] != "pinned":
            raise KeySettingMissing()
        pinned = tuple(raw_setting["parts"])
        if carried_parts is None:
            carried = pinned
        else:
            carried = normalize_parts(carried_parts)
            if carried is None:
                raise ValueError("键组成不合法")
        if carried != pinned:
            clash = _carried_key_clashes(conn, raw_text, carried)
            reason = "去重键不符，这批不落库"
            code = REASON_MISMATCH
            if clash:
                reason += "；键争用：同一行已被当天键设置的那一次占住，只采纳与键设置一致的那次"
                code = REASON_KEY_CLASH
            conn.execute(
                """
                INSERT INTO intake_audit
                    (title, raw_text, reason, reason_code, carried_parts, pinned_parts,
                     key_date, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    title,
                    raw_text,
                    reason,
                    code,
                    list(carried),
                    list(pinned),
                    key_date,
                    username,
                ),
            )
            conn.commit()
            raise KeyMismatch(carried, pinned, reason)
        batch_id = _insert_batch(conn, title, raw_text, username, pinned, key_date)
        conn.commit()
    return batch_id


def _carried_key_clashes(conn, raw_text, carried):
    """携带的键是否撞上按当天键设置落库的同一行。

    撞行时先按站与时刻加锁，和按当天键设置归集的那一批排到同一次里；
    谁先拿到锁谁先判，另一次随后由 _mark_contested 补记键争用。
    """
    parsed_lines = [
        parsed
        for parsed in (parse_telegram_line(raw_line, carried) for raw_line in raw_text.splitlines())
        if parsed["status"] == "ok"
    ]
    identities = sorted(
        (parsed["station_code"], parsed["observed_at"]) for parsed in parsed_lines
    )
    for station_code, observed_at in identities:
        _lock_identity(conn, station_code, observed_at)
    for parsed in parsed_lines:
        row = conn.execute(
            "SELECT dedup_parts FROM landed_rows "
            "WHERE station_code = %s AND observed_at = %s LIMIT 1",
            (parsed["station_code"], parsed["observed_at"]),
        ).fetchone()
        if row is None:
            continue
        if tuple(row["dedup_parts"] or ()) != tuple(carried):
            return True
    return False


def _mark_contested(conn, key_date, station_code, observed_at):
    """谁先撞上同一行就把先前的键不符改成键争用。

    两个归集员同时拿不同键撞同一行时，排在后面的那次可能先写下了键不符；
    与键设置一致的那次落库后，在这里把另一批的留痕补成键争用。
    """
    audits = conn.execute(
        """
        SELECT id, raw_text, carried_parts, reason
        FROM intake_audit
        WHERE key_date = %s AND reason_code = %s
        """,
        (key_date, REASON_MISMATCH),
    ).fetchall()
    contest_note = "；键争用：只采纳与键设置一致的那一次"
    for audit in audits:
        carried = tuple(audit["carried_parts"] or ())
        for raw_line in (audit["raw_text"] or "").splitlines():
            parsed = parse_telegram_line(raw_line, carried)
            if parsed["status"] != "ok":
                continue
            if parsed["station_code"] == station_code and parsed["observed_at"] == observed_at:
                if contest_note not in (audit["reason"] or ""):
                    conn.execute(
                        "UPDATE intake_audit SET reason = %s, reason_code = %s WHERE id = %s",
                        (audit["reason"] + contest_note, REASON_KEY_CLASH, audit["id"]),
                    )
                break


def _seed(conn):
    save_setting(conn, SEED_KEY_DATE, OPTION_PARTS[0], "admin")
    title, _, body = SEED_CLEAN.partition("\n")
    _insert_batch(conn, title, body.strip(), "admin", OPTION_PARTS[0], SEED_KEY_DATE)
    title, _, body = SEED_BROKEN.partition("\n")
    _insert_batch(conn, title, body.strip(), "admin", OPTION_PARTS[0], SEED_KEY_DATE)
