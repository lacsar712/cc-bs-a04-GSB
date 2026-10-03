import os

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from rules import judge_microstrain

DSN = os.environ.get(
    "DATABASE_URL", "postgresql://app:app@localhost:54398/bridgestrain"
)

# 双测差值门槛（με），默认 10：主副两点读数差绝对值超过门槛即整笔退回。
DEFAULT_DIFF_THRESHOLD = 10.0

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS strain_readings (
    id serial PRIMARY KEY,
    span_code text NOT NULL,
    microstrain double precision NOT NULL,
    verdict text,
    reason text,
    status text NOT NULL DEFAULT 'pending',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz
);

-- 双测配对：同一跨段由主测点 + 副测点成对双测
CREATE TABLE IF NOT EXISTS dual_pairs (
    id serial PRIMARY KEY,
    span_code text NOT NULL,
    main_point_code text NOT NULL,
    sub_point_code text NOT NULL,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_by text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT dual_pairs_span_uk UNIQUE (span_code)
);

-- 应用级设置（差值门槛等单行键值）
CREATE TABLE IF NOT EXISTS app_settings (
    key text PRIMARY KEY,
    value double precision NOT NULL,
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 退回样例：双测超差的报送整笔退回并留痕
CREATE TABLE IF NOT EXISTS rejected_submissions (
    id serial PRIMARY KEY,
    pair_id integer,
    span_code text NOT NULL,
    main_point_code text NOT NULL,
    sub_point_code text NOT NULL,
    main_microstrain double precision NOT NULL,
    sub_microstrain double precision NOT NULL,
    abs_diff double precision NOT NULL,
    threshold double precision NOT NULL,
    reason text NOT NULL,
    rejected_by text NOT NULL,
    rejected_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS pair_id integer;
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS span_code_frozen text;
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS main_point_code text;
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS sub_point_code text;
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS main_microstrain double precision;
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS sub_microstrain double precision;
ALTER TABLE strain_readings ADD COLUMN IF NOT EXISTS abs_diff double precision;

CREATE INDEX IF NOT EXISTS idx_strain_readings_status ON strain_readings (status, id);

-- 在审（pending/processing）同一配对至多一笔：入队与冻结同事务，并发撞车至多一笔成
CREATE UNIQUE INDEX IF NOT EXISTS uq_readings_pair_inflight
    ON strain_readings (pair_id)
    WHERE status IN ('pending', 'processing');
"""


SEED_PAIRS = [
    ("跨中甲", "跨中甲-主", "跨中甲-副"),
    ("支座S2", "支座S2-主", "支座S2-副"),
]

SEED_DONE = [
    ("跨中甲", 150.0, 152.0),
    ("支座S2", 40.0, 43.0),
]


SEED_DONE_SQL = """
INSERT INTO strain_readings
    (span_code, microstrain, verdict, reason, status, created_by,
     processed_at, pair_id, span_code_frozen,
     main_point_code, sub_point_code,
     main_microstrain, sub_microstrain, abs_diff)
SELECT %s, %s, %s, %s, 'done', 'surveyor', now(),
       dp.id, %s, dp.main_point_code, dp.sub_point_code, %s, %s, %s
FROM dual_pairs dp WHERE dp.span_code = %s
"""


def _seed_done_params(span_code, main_val, sub_val):
    verdict, reason = judge_microstrain(main_val)
    diff = abs(main_val - sub_val)
    return (
        span_code, main_val, verdict, reason,
        span_code, main_val, sub_val, diff, span_code,
    )


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
        await conn.commit()


async def seed_if_empty(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) AS n FROM strain_readings")
            row = await cur.fetchone()
            if row["n"] > 0:
                return

            await cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_by)
                VALUES ('diff_threshold', %s, 'system')
                ON CONFLICT (key) DO NOTHING
                """,
                (DEFAULT_DIFF_THRESHOLD,),
            )

            await cur.execute("SELECT COUNT(*) AS n FROM dual_pairs")
            if (await cur.fetchone())["n"] == 0:
                for span, main_pt, sub_pt in SEED_PAIRS:
                    await cur.execute(
                        """
                        INSERT INTO dual_pairs
                            (span_code, main_point_code, sub_point_code, created_by)
                        VALUES (%s, %s, %s, 'system')
                        """,
                        (span, main_pt, sub_pt),
                    )

            for span_code, main_val, sub_val in SEED_DONE:
                await cur.execute(
                    SEED_DONE_SQL,
                    _seed_done_params(span_code, main_val, sub_val),
                )
        await conn.commit()


def connect_sync():
    import psycopg

    return psycopg.connect(DSN, row_factory=dict_row)


def ensure_schema_sync(conn) -> None:
    conn.execute(SCHEMA_SQL)


def seed_if_empty_sync(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS n FROM strain_readings").fetchone()
    if row["n"] > 0:
        return

    conn.execute(
        """
        INSERT INTO app_settings (key, value, updated_by)
        VALUES ('diff_threshold', %s, 'system')
        ON CONFLICT (key) DO NOTHING
        """,
        (DEFAULT_DIFF_THRESHOLD,),
    )

    n = conn.execute("SELECT COUNT(*) AS n FROM dual_pairs").fetchone()["n"]
    if n == 0:
        for span, main_pt, sub_pt in SEED_PAIRS:
            conn.execute(
                """
                INSERT INTO dual_pairs
                    (span_code, main_point_code, sub_point_code, created_by)
                VALUES (%s, %s, %s, 'system')
                """,
                (span, main_pt, sub_pt),
            )

    for span_code, main_val, sub_val in SEED_DONE:
        conn.execute(
            SEED_DONE_SQL,
            _seed_done_params(span_code, main_val, sub_val),
        )
    conn.commit()
