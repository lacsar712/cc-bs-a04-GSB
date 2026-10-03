import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from rules import judge_microstrain

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://app:app@localhost:54398/bridgestrain"
)

DEFAULT_DIFF_THRESHOLD = 20.0

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pairings (
    id serial PRIMARY KEY,
    span_code text NOT NULL UNIQUE,
    primary_point text NOT NULL,
    secondary_point text NOT NULL,
    frozen boolean NOT NULL DEFAULT false,
    frozen_at timestamptz,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reading_batches (
    id serial PRIMARY KEY,
    pairing_id integer REFERENCES pairings(id),
    span_code text NOT NULL,
    primary_point text NOT NULL,
    secondary_point text NOT NULL,
    primary_value double precision NOT NULL,
    secondary_value double precision NOT NULL,
    diff_value double precision NOT NULL,
    threshold_value double precision NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    reject_reason text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_reading_batches_status
    ON reading_batches (status, id);

CREATE TABLE IF NOT EXISTS strain_readings (
    id serial PRIMARY KEY,
    batch_id integer REFERENCES reading_batches(id),
    pairing_id integer REFERENCES pairings(id),
    span_code text NOT NULL,
    point_code text NOT NULL DEFAULT '',
    point_role text NOT NULL DEFAULT 'legacy',
    microstrain double precision NOT NULL,
    diff_value double precision,
    threshold_value double precision,
    verdict text,
    reason text,
    status text NOT NULL DEFAULT 'pending',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_strain_readings_status ON strain_readings (status, id);
CREATE INDEX IF NOT EXISTS idx_strain_readings_batch ON strain_readings (batch_id);

ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS batch_id integer;
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS pairing_id integer;
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS point_code text NOT NULL DEFAULT '';
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS point_role text NOT NULL DEFAULT 'legacy';
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS diff_value double precision;
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS threshold_value double precision;

CREATE TABLE IF NOT EXISTS app_settings (
    key text PRIMARY KEY,
    value double precision NOT NULL,
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""

SETTINGS_SEED_SQL = """
INSERT INTO app_settings (key, value)
VALUES ('diff_threshold', %s)
ON CONFLICT (key) DO NOTHING;
"""

LEGACY_SAMPLES = [
    ("跨中S1", 150.0),
    ("支座S2", 40.0),
]


async def create_pool() -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        conninfo=DSN,
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    await pool.open()
    return pool


async def ensure_schema(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        await conn.execute(SCHEMA_SQL)
        await conn.execute(SETTINGS_SEED_SQL, (DEFAULT_DIFF_THRESHOLD,))
        await conn.commit()


async def seed_if_empty(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) AS n FROM strain_readings")
            row = await cur.fetchone()
            if row["n"] > 0:
                return
            for span_code, microstrain in LEGACY_SAMPLES:
                verdict, reason = judge_microstrain(microstrain)
                await cur.execute(
                    """
                    INSERT INTO strain_readings
                        (span_code, point_code, point_role, microstrain,
                         verdict, reason, status, created_by, processed_at)
                    VALUES (%s, %s, 'legacy', %s, %s, %s, 'done', 'surveyor', now())
                    """,
                    (span_code, span_code, microstrain, verdict, reason),
                )
        await conn.commit()


def connect_sync():
    import psycopg

    return psycopg.connect(DSN, row_factory=dict_row)


def ensure_schema_sync(conn) -> None:
    conn.execute(SCHEMA_SQL)
    conn.execute(SETTINGS_SEED_SQL, (DEFAULT_DIFF_THRESHOLD,))


def seed_if_empty_sync(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS n FROM strain_readings").fetchone()
    if row["n"] > 0:
        return
    for span_code, microstrain in LEGACY_SAMPLES:
        verdict, reason = judge_microstrain(microstrain)
        conn.execute(
            """
            INSERT INTO strain_readings
                (span_code, point_code, point_role, microstrain,
                 verdict, reason, status, created_by, processed_at)
            VALUES (%s, %s, 'legacy', %s, %s, %s, 'done', 'surveyor', now())
            """,
            (span_code, span_code, microstrain, verdict, reason),
        )
    conn.commit()
