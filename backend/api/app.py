import os
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext
from psycopg.errors import UniqueViolation
from sanic import Sanic
from sanic.response import json as sanic_json

from db import DEFAULT_DIFF_THRESHOLD, create_pool, ensure_schema, seed_if_empty

SECRET = os.environ.get("JWT_SECRET", "bridge-strain-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

USERS = {
    "surveyor": {"role": "writer", "password_hash": pwd.hash("surv123456")},
    "reviewer": {"role": "reader", "password_hash": pwd.hash("rev123456")},
}

app = Sanic("bridge-strain-shift")


def _auth_header(request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


def _decode_user(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    sub = payload.get("sub")
    if sub not in USERS:
        return None
    return {"username": sub, "role": payload.get("role")}


def _require_user(request) -> dict | None:
    return _decode_user(_auth_header(request))


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _err(detail: str, status: int = 400):
    return sanic_json({"detail": detail}, status=status)


def _pairing_out(r) -> dict:
    return {
        "id": r["id"],
        "span_code": r["span_code"],
        "primary_point": r["primary_point"],
        "secondary_point": r["secondary_point"],
        "frozen": r["frozen"],
        "frozen_at": _iso(r["frozen_at"]),
        "created_by": r["created_by"],
        "created_at": _iso(r["created_at"]),
    }


def _batch_out(r) -> dict:
    return {
        "id": r["id"],
        "pairing_id": r["pairing_id"],
        "span_code": r["span_code"],
        "primary_point": r["primary_point"],
        "secondary_point": r["secondary_point"],
        "primary_value": r["primary_value"],
        "secondary_value": r["secondary_value"],
        "diff_value": r["diff_value"],
        "threshold_value": r["threshold_value"],
        "status": r["status"],
        "reject_reason": r["reject_reason"],
        "created_by": r["created_by"],
        "created_at": _iso(r["created_at"]),
        "processed_at": _iso(r["processed_at"]),
    }


@app.before_server_start
async def setup(_app, _loop):
    pool = await create_pool()
    _app.ctx.pool = pool
    await ensure_schema(pool)
    await seed_if_empty(pool)


@app.after_server_stop
async def teardown(_app, _loop):
    pool = _app.ctx.pool
    if pool:
        await pool.close()


@app.get("/api/health")
async def health(_request):
    return sanic_json({"status": "ok", "service": "bridge-strain-shift"})


@app.post("/api/auth/login")
async def login(request):
    body = request.json or {}
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    user = USERS.get(username)
    if not user or not pwd.verify(password, user["password_hash"]):
        return sanic_json({"detail": "用户名或密码错误"}, status=401)
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": username, "role": user["role"], "exp": exp},
        SECRET,
        algorithm="HS256",
    )
    return sanic_json(
        {"access_token": token, "username": username, "role": user["role"]}
    )


async def _get_threshold(cur) -> float:
    await cur.execute(
        "SELECT value FROM app_settings WHERE key = 'diff_threshold'"
    )
    row = await cur.fetchone()
    return float(row["value"]) if row else DEFAULT_DIFF_THRESHOLD


@app.get("/api/config")
async def get_config(request):
    if not _require_user(request):
        return _err("未登录", 401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            threshold = await _get_threshold(cur)
    return sanic_json({"diff_threshold": threshold})


@app.get("/api/pairings")
async def list_pairings(request):
    user = _require_user(request)
    if not user:
        return _err("未登录", 401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, span_code, primary_point, secondary_point, frozen,
                       frozen_at, created_by, created_at
                FROM pairings
                ORDER BY id
                """
            )
            rows = await cur.fetchall()
    return sanic_json([_pairing_out(r) for r in rows])


@app.post("/api/pairings")
async def create_pairing(request):
    user = _require_user(request)
    if not user:
        return _err("未登录", 401)
    if user["role"] != "writer":
        return _err("仅测量员可维护双测配对", 403)
    body = request.json or {}
    span_code = str(body.get("span_code", "")).strip()
    primary_point = str(body.get("primary_point", "")).strip()
    secondary_point = str(body.get("secondary_point", "")).strip()
    if not span_code:
        return _err("跨段编号不能为空")
    if not primary_point or not secondary_point:
        return _err("主测点与副测点编号均不能为空")
    if primary_point == secondary_point:
        return _err("主测点与副测点编号不能相同")

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO pairings
                        (span_code, primary_point, secondary_point,
                         frozen, created_by, created_at)
                    VALUES (%s, %s, %s, false, %s, now())
                    RETURNING id, span_code, primary_point, secondary_point,
                              frozen, frozen_at, created_by, created_at
                    """,
                    (span_code, primary_point, secondary_point, user["username"]),
                )
                row = await cur.fetchone()
            except UniqueViolation:
                await conn.rollback()
                return _err("该跨段配对已存在", 409)
        await conn.commit()
    return sanic_json(_pairing_out(row), status=201)


@app.put("/api/pairings/<pairing_id:int>")
async def update_pairing(request, pairing_id: int):
    user = _require_user(request)
    if not user:
        return _err("未登录", 401)
    if user["role"] != "writer":
        return _err("仅测量员可维护双测配对", 403)
    body = request.json or {}
    primary_point = str(body.get("primary_point", "")).strip()
    secondary_point = str(body.get("secondary_point", "")).strip()
    if not primary_point or not secondary_point:
        return _err("主测点与副测点编号均不能为空")
    if primary_point == secondary_point:
        return _err("主测点与副测点编号不能相同")

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT frozen FROM pairings WHERE id = %s FOR UPDATE", (pairing_id,))
            existing = await cur.fetchone()
            if not existing:
                return _err("配对不存在", 404)
            if existing["frozen"]:
                return _err("配对已随单冻结，不能修改", 409)
            await cur.execute(
                """
                UPDATE pairings
                SET primary_point = %s, secondary_point = %s
                WHERE id = %s
                RETURNING id, span_code, primary_point, secondary_point,
                          frozen, frozen_at, created_by, created_at
                """,
                (primary_point, secondary_point, pairing_id),
            )
            row = await cur.fetchone()
        await conn.commit()
    return sanic_json(_pairing_out(row))


@app.put("/api/config/threshold")
async def update_threshold(request):
    user = _require_user(request)
    if not user:
        return _err("未登录", 401)
    if user["role"] != "writer":
        return _err("仅测量员可设置差值门槛", 403)
    body = request.json or {}
    try:
        threshold = float(body.get("diff_threshold"))
    except (TypeError, ValueError):
        return _err("差值门槛必须是数字")
    if threshold < 0:
        return _err("差值门槛不能为负数")

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_by, updated_at)
                VALUES ('diff_threshold', %s, %s, now())
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = now()
                """,
                (threshold, user["username"]),
            )
        await conn.commit()
    return sanic_json({"diff_threshold": threshold})


@app.get("/api/batches")
async def list_batches(request):
    if not _require_user(request):
        return _err("未登录", 401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, pairing_id, span_code, primary_point, secondary_point,
                       primary_value, secondary_value, diff_value, threshold_value,
                       status, reject_reason, created_by, created_at, processed_at
                FROM reading_batches
                ORDER BY id DESC
                """
            )
            rows = await cur.fetchall()
    return sanic_json([_batch_out(r) for r in rows])


@app.get("/api/readings")
async def list_readings(request):
    if not _require_user(request):
        return _err("未登录", 401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, batch_id, pairing_id, span_code, point_code, point_role,
                       microstrain, diff_value, threshold_value, verdict, reason,
                       status, created_by, created_at, processed_at
                FROM strain_readings
                ORDER BY id DESC
                """
            )
            rows = await cur.fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "batch_id": r["batch_id"],
                "pairing_id": r["pairing_id"],
                "span_code": r["span_code"],
                "point_code": r["point_code"],
                "point_role": r["point_role"],
                "microstrain": r["microstrain"],
                "diff_value": r["diff_value"],
                "threshold_value": r["threshold_value"],
                "verdict": r["verdict"],
                "reason": r["reason"],
                "status": r["status"],
                "created_by": r["created_by"],
                "created_at": _iso(r["created_at"]),
                "processed_at": _iso(r["processed_at"]),
            }
        )
    return sanic_json(out)


def _parse_value(raw, label: str):
    try:
        return float(raw), None
    except (TypeError, ValueError):
        return None, f"{label}必须是数字"


@app.post("/api/batches")
async def create_batch(request):
    """测量员一次提交同一跨段主、副两个测点读数。

    同一事务内锁住配对行并置 frozen，保证两人并发提交同一对时至多一笔成功；
    主副点号与当前差值门槛随批次快照冻结，事后改配对表不影响旧单。
    差值绝对值超过门槛则整笔退回，写明双测超差。
    """
    user = _require_user(request)
    if not user:
        return _err("未登录", 401)
    if user["role"] != "writer":
        return _err("仅测量员可提交双测读数", 403)

    body = request.json or {}
    span_code = str(body.get("span_code", "")).strip()
    if not span_code:
        return _err("跨段编号不能为空")

    primary_value, err1 = _parse_value(body.get("primary_value"), "主测点微应变")
    if err1:
        return _err(err1)
    secondary_value, err2 = _parse_value(body.get("secondary_value"), "副测点微应变")
    if err2:
        return _err(err2)

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    # 锁住该跨段配对行：并发的第二笔在此等待，提交后只能看到 frozen=true
                    await cur.execute(
                        """
                        SELECT id, primary_point, secondary_point, frozen
                        FROM pairings
                        WHERE span_code = %s
                        FOR UPDATE
                        """,
                        (span_code,),
                    )
                    pairing = await cur.fetchone()
                    if not pairing:
                        return _err("该跨段尚未登记双测配对，请先在双测配对专页登记主副测点", 404)

                    threshold = await _get_threshold(cur)
                    diff_value = abs(primary_value - secondary_value)
                    primary_point = pairing["primary_point"]
                    secondary_point = pairing["secondary_point"]

                    # 先做差值校验：无论配对是否已冻结，超差都整笔退回并留退回样例。
                    # 退回不入队、不改冻结状态。
                    if diff_value > threshold:
                        # 整笔退回：不写读数、不入队，仅留退回样例；不冻结配对
                        reject_reason = (
                            f"双测超差：主测点{primary_point}读数{primary_value:g} με 与"
                            f"副测点{secondary_point}读数{secondary_value:g} με 差值"
                            f"{diff_value:g} με 超过门槛{threshold:g} με，整笔退回"
                        )
                        await cur.execute(
                            """
                            INSERT INTO reading_batches
                                (pairing_id, span_code, primary_point, secondary_point,
                                 primary_value, secondary_value, diff_value, threshold_value,
                                 status, reject_reason, created_by, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                    'rejected', %s, %s, now())
                            RETURNING id, pairing_id, span_code, primary_point,
                                      secondary_point, primary_value, secondary_value,
                                      diff_value, threshold_value, status, reject_reason,
                                      created_by, created_at, processed_at
                            """,
                            (
                                pairing["id"], span_code, primary_point, secondary_point,
                                primary_value, secondary_value, diff_value, threshold,
                                reject_reason, user["username"],
                            ),
                        )
                        rejected_row = await cur.fetchone()
                        return sanic_json(
                            {"detail": reject_reason,
                             "batch": _batch_out(rejected_row),
                             "code": "dual_measure_exceed"},
                            status=422,
                        )

                    # 差值合格：还须配对未被占用——行锁已串行化并发，
                    # 先到者已在同事务置 frozen，后到者在此读到 true，故同对至多一笔成。
                    if pairing["frozen"]:
                        return _err("该跨段配对已有一笔双测在队，同对至多一笔，请勿重复提交", 409)

                    # 冻结配对 + 建候审批次 + 两条读数，一次写完
                    await cur.execute(
                        "UPDATE pairings SET frozen = true, frozen_at = now() WHERE id = %s",
                        (pairing["id"],),
                    )
                    await cur.execute(
                        """
                        INSERT INTO reading_batches
                            (pairing_id, span_code, primary_point, secondary_point,
                             primary_value, secondary_value, diff_value, threshold_value,
                             status, created_by, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                'pending', %s, now())
                        RETURNING id, pairing_id, span_code, primary_point,
                                  secondary_point, primary_value, secondary_value,
                                  diff_value, threshold_value, status, reject_reason,
                                  created_by, created_at, processed_at
                        """,
                        (
                            pairing["id"], span_code, primary_point, secondary_point,
                            primary_value, secondary_value, diff_value, threshold,
                            user["username"],
                        ),
                    )
                    batch_row = await cur.fetchone()

                    for point, value, role in (
                        (primary_point, primary_value, "primary"),
                        (secondary_point, secondary_value, "secondary"),
                    ):
                        await cur.execute(
                            """
                            INSERT INTO strain_readings
                                (batch_id, pairing_id, span_code, point_code, point_role,
                                 microstrain, diff_value, threshold_value,
                                 status, created_by, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                                    'pending', %s, now())
                            """,
                            (
                                batch_row["id"], pairing["id"], span_code, point, role,
                                value, diff_value, threshold,
                                user["username"],
                            ),
                        )
            # 事务提交即"入队与配对冻结一次写完"
        except Exception:
            raise

    return sanic_json(
        {
            "batch": _batch_out(batch_row),
            "message": "双测已入候审队列，配对已冻结",
        },
        status=201,
    )
