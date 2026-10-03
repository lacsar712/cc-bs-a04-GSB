"""后台工人：用 SKIP LOCKED 认领待审双测批次，成对判定两条读数的合格/越界。"""

import os
import time

from db import connect_sync, ensure_schema_sync, seed_if_empty_sync
from rules import judge_microstrain

POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "1.0"))


def run_once(conn) -> bool:
    """认领一个候审批次并在同一事务内成对判定。

    行锁用 FOR UPDATE SKIP LOCKED：多工人并发时同批次只被一人认领；
    任一步失败则整笔回滚，批次与其两条读数都回到 pending 等待重新认领。
    """
    with conn.transaction():
        batch = conn.execute(
            """
            SELECT id
            FROM reading_batches
            WHERE status = 'pending'
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()
        if not batch:
            return False

        batch_id = batch["id"]
        conn.execute(
            "UPDATE reading_batches SET status = 'processing' WHERE id = %s",
            (batch_id,),
        )

        readings = conn.execute(
            """
            SELECT id, microstrain
            FROM strain_readings
            WHERE batch_id = %s
            ORDER BY id
            FOR UPDATE
            """,
            (batch_id,),
        ).fetchall()

        for reading in readings:
            verdict, reason = judge_microstrain(float(reading["microstrain"]))
            conn.execute(
                """
                UPDATE strain_readings
                SET status = 'done', verdict = %s, reason = %s, processed_at = now()
                WHERE id = %s
                """,
                (verdict, reason, reading["id"]),
            )

        conn.execute(
            """
            UPDATE reading_batches
            SET status = 'done', processed_at = now()
            WHERE id = %s
            """,
            (batch_id,),
        )
    return True


def main() -> None:
    with connect_sync() as conn:
        ensure_schema_sync(conn)
        seed_if_empty_sync(conn)
        conn.commit()

    while True:
        try:
            with connect_sync() as conn:
                processed = run_once(conn)
        except Exception as exc:
            print(f"worker error: {exc}", flush=True)
            processed = False
        if not processed:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
