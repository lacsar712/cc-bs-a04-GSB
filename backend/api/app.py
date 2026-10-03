import os
from datetime import datetime, timedelta, timezone

import jwt
import psycopg
from passlib.context import CryptContext
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


def _require_writer(request):
    """返回 (user, error_response)；复核员/未登录拿到错误响应。"""
    user = _require_user(request)
    if not user:
        return None, sanic_json({"detail": "未登录"}, status=401)
    if user["role"] != "writer":
        return None, sanic_json(
            {"detail": "仅测量员可维护配对与报送读数，复核员只读"}, status=403
        )
    return user, None


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _reading_out(r) -> dict:
    return {
        "id": r["id"],
        "span_code": r["span_code"],
        "microstrain": r["microstrain"],
        "verdict": r["verdict"],
        "reason": r["reason"],
        "status": r["status"],
        "created_by": r["created_by"],
        "created_at": _iso(r["created_at"]),
        "processed_at": _iso(r["processed_at"]),
        "pair_id": r["pair_id"],
        "span_code_frozen": r["span_code_frozen"],
        "main_point_code": r["main_point_code"],
        "sub_point_code": r["sub_point_code"],
        "main_microstrain": r["main_microstrain"],
        "sub_microstrain": r["sub_microstrain"],
        "abs_diff": r["abs_diff"],
    }


def _pair_out(r) -> dict:
    return {
        "id": r["id"],
        "span_code": r["span_code"],
        "main_point_code": r["main_point_code"],
        "sub_point_code": r["sub_point_code"],
        "created_by": r["created_by"],
        "created_at": _iso(r["created_at"]),
        "updated_by": r["updated_by"],
        "updated_at": _iso(r["updated_at"]),
    }


def _rejection_out(r) -> dict:
    return {
        "id": r["id"],
        "pair_id": r["pair_id"],
        "span_code": r["span_code"],
        "main_point_code": r["main_point_code"],
        "sub_point_code": r["sub_point_code"],
        "main_microstrain": r["main_microstrain"],
        "sub_microstrain": r["sub_microstrain"],
        "abs_diff": r["abs_diff"],
        "threshold": r["threshold"],
        "reason": r["reason"],
        "rejected_by": r["rejected_by"],
        "rejected_at": _iso(r["rejected_at"]),
    }


READING_COLS = (
    "id, span_code, microstrain, verdict, reason, status, created_by, "
    "created_at, processed_at, pair_id, span_code_frozen, "
    "main_point_code, sub_point_code, main_microstrain, sub_microstrain, abs_diff"
)


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


@app.get("/api/readings")
async def list_readings(request):
    if not _require_user(request):
        return sanic_json({"detail": "未登录"}, status=401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {READING_COLS} FROM strain_readings ORDER BY id DESC"
            )
            rows = await cur.fetchall()
    return sanic_json([_reading_out(r) for r in rows])


@app.post("/api/readings")
async def create_reading(request):
    """双测报送：同一次提交主、副两个微应变，差超门槛整笔退回。

    入队与配对冻结在同一事务一次写完；先对配对行加 FOR UPDATE 行锁，
    两人并发撞交同一对时后者等锁后撞上在审唯一索引，至多一笔成功。
    """
    user, err = _require_writer(request)
    if err:
        return err

    body = request.json or {}
    try:
        pair_id = int(body.get("pair_id"))
    except (TypeError, ValueError):
        return sanic_json({"detail": "请选择有效的双测配对"}, status=400)

    try:
        main_val = float(body.get("main_microstrain"))
        sub_val = float(body.get("sub_microstrain"))
    except (TypeError, ValueError):
        return sanic_json(
            {"detail": "主、副测点微应变必须同时填写数字"}, status=400
        )

    abs_diff = abs(main_val - sub_val)
    rejection_payload = None
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    # 锁定配对行，串行化同一配对的并发报送
                    await cur.execute(
                        "SELECT * FROM dual_pairs WHERE id = %s FOR UPDATE",
                        (pair_id,),
                    )
                    pair = await cur.fetchone()
                    if not pair:
                        return sanic_json(
                            {"detail": "双测配对不存在，请先在专页维护"},
                            status=404,
                        )

                    await cur.execute(
                        "SELECT value FROM app_settings "
                        "WHERE key = 'diff_threshold'"
                    )
                    srow = await cur.fetchone()
                    threshold = (
                        float(srow["value"]) if srow else DEFAULT_DIFF_THRESHOLD
                    )
                    reject_reason = (
                        f"双测超差：主副差值 {abs_diff:g} με 超过门槛 "
                        f"{threshold:g} με，整笔退回"
                    )

                    if abs_diff > threshold:
                        # 整笔退回：不入队，在同一事务写退回留痕
                        await cur.execute(
                            """
                            INSERT INTO rejected_submissions
                                (pair_id, span_code, main_point_code, sub_point_code,
                                 main_microstrain, sub_microstrain, abs_diff,
                                 threshold, reason, rejected_by)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            (
                                pair["id"],
                                pair["span_code"],
                                pair["main_point_code"],
                                pair["sub_point_code"],
                                main_val,
                                sub_val,
                                abs_diff,
                                threshold,
                                reject_reason,
                                user["username"],
                            ),
                        )
                        rej = await cur.fetchone()
                        rejection_payload = {
                            "rejected": True,
                            "rejection_id": rej["id"],
                            "detail": reject_reason,
                        }
                    else:
                        # 差值合格：冻结配对快照并入队（一次写完）
                        await cur.execute(
                            """
                            INSERT INTO strain_readings
                                (span_code, microstrain, status, created_by, created_at,
                                 pair_id, span_code_frozen,
                                 main_point_code, sub_point_code,
                                 main_microstrain, sub_microstrain, abs_diff)
                            VALUES (%s, %s, 'pending', %s, now(),
                                    %s, %s, %s, %s, %s, %s, %s)
                            RETURNING """ + READING_COLS,
                            (
                                pair["span_code"],
                                main_val,
                                user["username"],
                                pair["id"],
                                pair["span_code"],
                                pair["main_point_code"],
                                pair["sub_point_code"],
                                main_val,
                                sub_val,
                                abs_diff,
                            ),
                        )
                        row = await cur.fetchone()
                        reading_payload = _reading_out(row)
        except psycopg.errors.UniqueViolation:
            return sanic_json(
                {"detail": "该配对已有一笔在审单，候复核后再交（同一对至多一笔在审）"},
                status=409,
            )

    if rejection_payload is not None:
        return sanic_json(rejection_payload, status=422)
    return sanic_json(
        {**reading_payload, "message": "双测已入队候审，配对关系随单冻结"},
        status=201,
    )


@app.get("/api/pairs")
async def list_pairs(request):
    if not _require_user(request):
        return sanic_json({"detail": "未登录"}, status=401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM dual_pairs ORDER BY id")
            rows = await cur.fetchall()
    return sanic_json([_pair_out(r) for r in rows])


@app.post("/api/pairs")
async def create_pair(request):
    user, err = _require_writer(request)
    if err:
        return err
    body = request.json or {}
    span_code = str(body.get("span_code", "")).strip()
    main_point_code = str(body.get("main_point_code", "")).strip()
    sub_point_code = str(body.get("sub_point_code", "")).strip()
    if not span_code or not main_point_code or not sub_point_code:
        return sanic_json(
            {"detail": "跨段、主测点、副测点编号均不能为空"}, status=400
        )
    if main_point_code == sub_point_code:
        return sanic_json(
            {"detail": "主测点与副测点编号不能相同"}, status=400
        )

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO dual_pairs
                            (span_code, main_point_code, sub_point_code, created_by)
                        VALUES (%s, %s, %s, %s)
                        RETURNING *
                        """,
                        (span_code, main_point_code, sub_point_code,
                         user["username"]),
                    )
                    row = await cur.fetchone()
        except psycopg.errors.UniqueViolation:
            return sanic_json(
                {"detail": f"跨段 {span_code} 的配对已存在"}, status=409
            )
    return sanic_json(_pair_out(row), status=201)


@app.patch("/api/pairs/<pair_id:int>")
async def update_pair(request, pair_id: int):
    user, err = _require_writer(request)
    if err:
        return err
    body = request.json or {}
    fields = {}
    for key in ("span_code", "main_point_code", "sub_point_code"):
        if key in body:
            val = str(body.get(key, "")).strip()
            if not val:
                return sanic_json({"detail": f"{key} 不能为空"}, status=400)
            fields[key] = val
    if not fields:
        return sanic_json({"detail": "没有需要更新的字段"}, status=400)
    if (
        fields.get("main_point_code") == fields.get("sub_point_code")
    ):
        return sanic_json(
            {"detail": "主测点与副测点编号不能相同"}, status=400
        )

    # 只改配对表；旧单已冻结的点号存在快照列，改配对动不了旧单点号
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    params = list(fields.values()) + [user["username"], pair_id]
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        try:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"""
                        UPDATE dual_pairs
                        SET {set_clause}, updated_by = %s, updated_at = now()
                        WHERE id = %s
                        RETURNING *
                        """,
                        params,
                    )
                    row = await cur.fetchone()
        except psycopg.errors.UniqueViolation:
            return sanic_json(
                {"detail": "该跨段编号已被其他配对占用"}, status=409
            )
    if not row:
        return sanic_json({"detail": "配对不存在"}, status=404)
    return sanic_json(_pair_out(row))


@app.get("/api/settings")
async def get_settings(request):
    if not _require_user(request):
        return sanic_json({"detail": "未登录"}, status=401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT key, value, updated_by, updated_at
                FROM app_settings WHERE key = 'diff_threshold'
                """
            )
            row = await cur.fetchone()
    if not row:
        return sanic_json(
            {"diff_threshold": DEFAULT_DIFF_THRESHOLD,
             "updated_by": None, "updated_at": None}
        )
    return sanic_json(
        {
            "diff_threshold": float(row["value"]),
            "updated_by": row["updated_by"],
            "updated_at": _iso(row["updated_at"]),
        }
    )


@app.put("/api/settings")
async def update_settings(request):
    user, err = _require_writer(request)
    if err:
        return err
    body = request.json or {}
    try:
        threshold = float(body.get("diff_threshold"))
    except (TypeError, ValueError):
        return sanic_json({"detail": "差值门槛必须是数字"}, status=400)
    if threshold < 0:
        return sanic_json({"detail": "差值门槛不能为负"}, status=400)

    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_by, updated_at)
                    VALUES ('diff_threshold', %s, %s, now())
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = now()
                    RETURNING value, updated_by, updated_at
                    """,
                    (threshold, user["username"]),
                )
                row = await cur.fetchone()
    return sanic_json(
        {
            "diff_threshold": float(row["value"]),
            "updated_by": row["updated_by"],
            "updated_at": _iso(row["updated_at"]),
        }
    )


@app.get("/api/rejections")
async def list_rejections(request):
    if not _require_user(request):
        return sanic_json({"detail": "未登录"}, status=401)
    pool = request.app.ctx.pool
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM rejected_submissions ORDER BY id DESC"
            )
            rows = await cur.fetchall()
    return sanic_json([_rejection_out(r) for r in rows])
