"""
AI算力平台 — 后端服务
首页 + 用户认证 + AI视频生成代理
"""
import os, sys, json, uuid, time, logging, sqlite3, re
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, request, jsonify, send_from_directory, session, redirect, Response
from werkzeug.security import generate_password_hash, check_password_hash
from waitress import serve

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("api-b")

# ============================================================
# Flask 初始化
# ============================================================
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.json.ensure_ascii = False  # Flask 3.x 用这个
app.secret_key = os.environ.get("SECRET_KEY", uuid.uuid4().hex)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "video-website")

# CORS — 允许 Netlify 前端跨域访问
@app.after_request
def _cors_headers(response):
    origin = request.headers.get("Origin", "")
    # 允许本地开发 & Netlify 部署的域名
    allowed = ["http://localhost:9000", "http://localhost:3000", "http://localhost:5173",
               "https://yunzhen.netlify.app"]
    if origin in allowed or "netlify.app" in origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response

# ============================================================
# 数据库
# ============================================================
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

def _get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init_db():
    conn = _get_db()
    # 用户表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    UNIQUE NOT NULL,
            email       TEXT    NOT NULL,
            phone       TEXT    NOT NULL DEFAULT '',
            password    TEXT    NOT NULL,
            role        TEXT    NOT NULL DEFAULT 'user',
            status      TEXT    NOT NULL DEFAULT 'pending',
            parent_id   INTEGER,
            created_at  TEXT    NOT NULL,
            FOREIGN KEY (parent_id) REFERENCES users(id)
        )
    """)
    # 兼容已有数据库：尝试加 parent_id 列
    try:
        conn.execute("ALTER TABLE users ADD COLUMN parent_id INTEGER REFERENCES users(id)")
    except:
        pass
    # 销售员表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS salespersons (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            name        TEXT    NOT NULL,
            code        TEXT    UNIQUE NOT NULL,
            phone       TEXT    DEFAULT '',
            created_at  TEXT    NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    # 销售员 ↔ 客户关联表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS salesperson_customer (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            salesperson_id  INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            created_at      TEXT    NOT NULL,
            FOREIGN KEY (salesperson_id) REFERENCES salespersons(id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(user_id)
        )
    """)
    # 渠道表（AI网关渠道，管理员管理）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    UNIQUE NOT NULL,
            base_url    TEXT    NOT NULL,
            api_key     TEXT    NOT NULL DEFAULT '',
            status      TEXT    NOT NULL DEFAULT 'active',
            created_at  TEXT    NOT NULL
        )
    """)
    # 用户设置表（记住用户偏好）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id         INTEGER PRIMARY KEY,
            selected_model  TEXT DEFAULT '',
            rate_per_second REAL DEFAULT 2.30,
            channel_id      INTEGER,
            updated_at      TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (channel_id) REFERENCES channels(id)
        )
    """)
    # 兼容已有数据库：尝试加 channel_id 列
    try:
        conn.execute("ALTER TABLE user_settings ADD COLUMN channel_id INTEGER")
    except:
        pass
    # 账户余额表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_balance (
            user_id             INTEGER PRIMARY KEY,
            balance             REAL    NOT NULL DEFAULT 0,
            total_deposit       REAL    NOT NULL DEFAULT 0,
            total_used          REAL    NOT NULL DEFAULT 0,
            points              INTEGER NOT NULL DEFAULT 0,
            total_points_used   INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    # 兼容已有数据库
    try:
        conn.execute("ALTER TABLE user_balance ADD COLUMN total_points_used INTEGER NOT NULL DEFAULT 0")
    except:
        pass
    # 积分使用记录表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS points_usage_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            points_used     INTEGER NOT NULL,
            balance_after   INTEGER NOT NULL,
            model_name      TEXT    DEFAULT '',
            duration        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    # 客户可用模型表（销售员管理客户能用哪些模型）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_allowed_models (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            model_name      TEXT    NOT NULL,
            channel_id      INTEGER NOT NULL,
            enabled         INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(user_id, model_name)
        )
    """)
    # 交易记录表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            type            TEXT    NOT NULL,
            amount          REAL    NOT NULL,
            balance_after   REAL    NOT NULL,
            description     TEXT    DEFAULT '',
            created_at      TEXT    NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    # 充值记录表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recharge_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            amount          REAL    NOT NULL,
            balance_before  REAL    NOT NULL,
            balance_after   REAL    NOT NULL,
            created_by      INTEGER NOT NULL,
            description     TEXT    DEFAULT '',
            created_at      TEXT    NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)
    # 用量明细表（每次API调用记录）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_records (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            channel_id      INTEGER,
            seconds_used    REAL    NOT NULL DEFAULT 0,
            tokens_used     INTEGER NOT NULL DEFAULT 0,
            request_text    TEXT    DEFAULT '',
            created_at      TEXT    NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (channel_id) REFERENCES channels(id)
        )
    """)
    # 渠道用量汇总表（user + channel 维度，实时累加）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_channel_usage (
            user_id         INTEGER NOT NULL,
            channel_id      INTEGER NOT NULL,
            total_seconds   REAL    NOT NULL DEFAULT 0,
            synced_seconds  REAL    NOT NULL DEFAULT 0,
            last_synced_at  TEXT    DEFAULT '',
            PRIMARY KEY (user_id, channel_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (channel_id) REFERENCES channels(id)
        )
    """)
    # 渠道-模型积分定价表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_model_pricing (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id          INTEGER NOT NULL,
            model_name          TEXT    NOT NULL,
            points_per_second   INTEGER NOT NULL DEFAULT 10,
            FOREIGN KEY (channel_id) REFERENCES channels(id),
            UNIQUE(channel_id, model_name)
        )
    """)

    # 视频库表（持久化，重启不丢失）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id          TEXT    PRIMARY KEY,
            name        TEXT    NOT NULL DEFAULT '',
            url         TEXT    NOT NULL,
            is_remote   INTEGER NOT NULL DEFAULT 0,
            owner_id    INTEGER NOT NULL,
            created_at  TEXT    NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        )
    """)
    # 任务持久化表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id              TEXT    PRIMARY KEY,
            remote_task_id  TEXT    DEFAULT '',
            model           TEXT    DEFAULT '',
            prompt_text     TEXT    DEFAULT '',
            status          TEXT    DEFAULT 'queued',
            video_url       TEXT    DEFAULT '',
            channel_name    TEXT    DEFAULT '',
            query_url_path  TEXT    DEFAULT '',
            owner_id        INTEGER DEFAULT 0,
            username        TEXT    DEFAULT '',
            baidu_status    TEXT    DEFAULT '',
            baidu_progress  INTEGER DEFAULT 0,
            created_at      REAL    NOT NULL,
            FOREIGN KEY (owner_id) REFERENCES users(id)
        )
    """)

    # --- 初始化默认数据 ---

    # 默认管理员
    admin = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()
    if not admin:
        conn.execute(
            "INSERT INTO users (username, email, phone, password, role, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("admin", "admin@aigongchang.com", "13800000000", generate_password_hash("admin123"), "admin", "approved",
             datetime.now().isoformat()[:19])
        )
        admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()["id"]
        # 给管理员也创建余额记录
        conn.execute(
            "INSERT INTO user_balance (user_id, balance, total_deposit, total_used, points) VALUES (?, 0, 0, 0, 0)",
            (admin_id,)
        )
        conn.execute(
            "INSERT INTO user_settings (user_id, selected_model, updated_at) VALUES (?, '', ?)",
            (admin_id, datetime.now().isoformat()[:19])
        )

    # 默认渠道：天翼云
    ch = conn.execute("SELECT * FROM channels WHERE name='天翼云'").fetchone()
    if not ch:
        conn.execute(
            "INSERT INTO channels (name, base_url, api_key, status, created_at) VALUES (?, ?, ?, ?, ?)",
            ("天翼云", "https://ai.ctaigw.cn/v1", CTYUN_API_KEY, "active",
             datetime.now().isoformat()[:19])
        )
    elif CTYUN_API_KEY and not ch["api_key"]:
        # 已有渠道但没有 Key，从环境变量填充
        conn.execute("UPDATE channels SET api_key=? WHERE id=?", (CTYUN_API_KEY, ch["id"]))

    # 默认渠道：百度云（gogogotoken.com）
    ch2 = conn.execute("SELECT * FROM channels WHERE name='百度云'").fetchone()
    if not ch2:
        conn.execute(
            "INSERT INTO channels (name, base_url, api_key, status, created_at) VALUES (?, ?, ?, ?, ?)",
            ("百度云", "https://gogogotoken.com/v1", GOGO_API_KEY, "active",
             datetime.now().isoformat()[:19])
        )
    elif GOGO_API_KEY and not ch2["api_key"]:
        conn.execute("UPDATE channels SET api_key=? WHERE id=?", (GOGO_API_KEY, ch2["id"]))

    # 示例销售员（初始化数据 + 创建对应用户账号）
    sp_data = [
        ("张经理", "abc123", "13900000001"),
        ("李经理", "xyz789", "13900000002"),
    ]
    for sp_name, sp_code, sp_phone in sp_data:
        sp = conn.execute("SELECT id, user_id FROM salespersons WHERE code=?", (sp_code,)).fetchone()
        if not sp:
            # 为销售员创建用户账号
            sp_username = "sales_" + sp_code
            conn.execute(
                "INSERT INTO users (username, email, phone, password, role, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sp_username, f"{sp_code}@aigongchang.com", sp_phone,
                 generate_password_hash("888888"), "salesperson", "approved",
                 datetime.now().isoformat()[:19])
            )
            sp_user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO salespersons (user_id, name, code, phone, created_at) VALUES (?, ?, ?, ?, ?)",
                (sp_user_id, sp_name, sp_code, sp_phone, datetime.now().isoformat()[:19])
            )
            conn.execute(
                "INSERT INTO user_balance (user_id, balance, total_deposit, total_used, points) VALUES (?, 0, 0, 0, 0)",
                (sp_user_id,)
            )
            conn.execute(
                "INSERT INTO user_settings (user_id, selected_model, rate_per_second, updated_at) VALUES (?, '', 2.30, ?)",
                (sp_user_id, datetime.now().isoformat()[:19])
            )

    # --- 默认定价（天翼云渠道） ---
    ch_id = conn.execute("SELECT id FROM channels WHERE name='天翼云'").fetchone()["id"]
    default_pricing = [
        ("GLM-5.0",  10), ("GLM-5.1",    12), ("DeepSeek-V3", 8),
        ("Kimi-K2.5", 15), ("Kimi-K2.6",  18), ("qwen-plus",   6),
    ]
    for mname, pts in default_pricing:
        conn.execute(
            "INSERT OR IGNORE INTO channel_model_pricing (channel_id, model_name, points_per_second) VALUES (?, ?, ?)",
            (ch_id, mname, pts)
        )
    # --- 默认定价（天翼云视频模型） ---
    ty_video = [
        ("seedance-1.0", 20), ("seedance-1.5", 25), ("seedance-2.0", 40),
        ("qwen-video", 15),
    ]
    for mname, pts in ty_video:
        conn.execute(
            "INSERT OR IGNORE INTO channel_model_pricing (channel_id, model_name, points_per_second) VALUES (?, ?, ?)",
            (ch_id, mname, pts)
        )
    # --- 默认定价（百度云渠道） ---
    ch2_id = conn.execute("SELECT id FROM channels WHERE name='百度云'").fetchone()["id"]
    baidu_pricing = [
        ("doubao-seedance-1-5-pro_480p",   15),
        ("doubao-seedance-1-5-pro_720p",   25),
        ("doubao-seedance-1-5-pro_1080p",  40),
        ("doubao-seedance-2-0-480p",       20),
        ("doubao-seedance-2-0-720p",       35),
        ("doubao-seedance-2-0-1080p",      50),
        ("doubao-seedance-2-0-fast-480p",  18),
        ("doubao-seedance-2-0-fast-720p",  30),
        ("doubao-seedream-4-5-251128",     8),
    ]
    for mname, pts in baidu_pricing:
        conn.execute(
            "INSERT OR IGNORE INTO channel_model_pricing (channel_id, model_name, points_per_second) VALUES (?, ?, ?)",
            (ch2_id, mname, pts)
        )

    # --- 固定测试客户 xiaoming ---
    xm = conn.execute("SELECT id FROM users WHERE username='xiaoming'").fetchone()
    if not xm:
        conn.execute(
            "INSERT INTO users (username, email, phone, password, role, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("xiaoming", "xiaoming@qq.com", "13600001111", generate_password_hash("123456"), "user", "approved",
             datetime.now().isoformat()[:19])
        )
        xm_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO user_balance (user_id, balance, total_deposit, total_used, points) VALUES (?, 0, 0, 0, 500)",
            (xm_id,)
        )
        conn.execute(
            "INSERT INTO user_settings (user_id, selected_model, rate_per_second, channel_id, updated_at) VALUES (?, '', 2.30, 1, ?)",
            (xm_id, datetime.now().isoformat()[:19])
        )
        sp1 = conn.execute("SELECT id FROM salespersons WHERE code='abc123'").fetchone()
        if sp1:
            conn.execute(
                "INSERT OR IGNORE INTO salesperson_customer (salesperson_id, user_id, created_at) VALUES (?, ?, ?)",
                (sp1["id"], xm_id, datetime.now().isoformat()[:19])
            )

    # --- 固定测试客户 zhangwei ---
    zw = conn.execute("SELECT id FROM users WHERE username='zhangwei'").fetchone()
    if not zw:
        conn.execute(
            "INSERT INTO users (username, email, phone, password, role, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("zhangwei", "zhangwei@qq.com", "13700000001", generate_password_hash("123456"), "user", "approved",
             datetime.now().isoformat()[:19])
        )
        zw_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO user_balance (user_id, balance, total_deposit, total_used, points) VALUES (?, 0, 0, 0, 500)",
            (zw_id,)
        )
        conn.execute(
            "INSERT INTO user_settings (user_id, selected_model, rate_per_second, channel_id, updated_at) VALUES (?, '', 2.30, 1, ?)",
            (zw_id, datetime.now().isoformat()[:19])
        )
        sp1 = conn.execute("SELECT id FROM salespersons WHERE code='abc123'").fetchone()
        if sp1:
            conn.execute(
                "INSERT OR IGNORE INTO salesperson_customer (salesperson_id, user_id, created_at) VALUES (?, ?, ?)",
                (sp1["id"], zw_id, datetime.now().isoformat()[:19])
            )

    conn.commit()
    conn.close()

# ============================================================
# 天翼云配置（必须在 _init_db 之前定义，因为初始化需要用到）
# ============================================================
CTYUN_BASE_URL = os.environ.get("CTYUN_BASE_URL", "https://ai.ctaigw.cn/v1")
CTYUN_API_KEY  = os.environ.get("CTYUN_API_KEY", "")
GOGO_API_KEY   = os.environ.get("GOGO_API_KEY", "")

_init_db()

def _ctyun_headers():
    return {"Authorization": f"Bearer {CTYUN_API_KEY}", "Content-Type": "application/json"}

# ============================================================
# 视频/任务内存存储
# ============================================================
_video_store = []
_task_store  = {}

# ============================================================ #
#                        认证装饰器                               #
# ============================================================ #

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"code": 401, "message": "请先登录"}), 401
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"code": 401, "message": "请先登录"}), 401
        if session.get("role") != "admin":
            return jsonify({"code": 403, "message": "需要管理员权限"}), 403
        return f(*args, **kwargs)
    return wrapper

def salesperson_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"code": 401, "message": "请先登录"}), 401
        if session.get("role") != "salesperson":
            return jsonify({"code": 403, "message": "需要销售员权限"}), 403
        return f(*args, **kwargs)
    return wrapper

# ============================================================ #
#                    销售员 API                                   #
# ============================================================ #

@app.route("/api/salesperson/dashboard", methods=["GET"])
@salesperson_required
def api_salesperson_dashboard():
    uid = session["user_id"]
    conn = _get_db()

    # 销售员信息
    sp = conn.execute(
        "SELECT id, name, code, phone FROM salespersons WHERE user_id=?", (uid,)
    ).fetchone()
    if not sp:
        conn.close()
        return jsonify({"code": 404, "message": "销售员信息不存在"}), 404

    # 统计：客户数、客户累计充值
    stats = conn.execute("""
        SELECT
            COUNT(DISTINCT sc.user_id) AS customer_count,
            COALESCE(SUM(ub.total_deposit), 0) AS total_deposits
        FROM salesperson_customer sc
        JOIN user_balance ub ON ub.user_id = sc.user_id
        WHERE sc.salesperson_id = ?
    """, (sp["id"],)).fetchone()

    # 客户列表（只列主账号 + 子账号数）
    customers = conn.execute("""
        SELECT
            u.id, u.username, u.email, u.phone, u.status, u.created_at,
            ub.balance, ub.total_deposit, ub.total_used, ub.points,
            us.rate_per_second, us.channel_id,
            COALESCE(ch.name, '') AS channel_name,
            (SELECT COUNT(*) FROM users WHERE parent_id = u.id) AS sub_count
        FROM salesperson_customer sc
        JOIN users u ON u.id = sc.user_id
        LEFT JOIN user_balance ub ON ub.user_id = u.id
        LEFT JOIN user_settings us ON us.user_id = u.id
        LEFT JOIN channels ch ON ch.id = us.channel_id
        WHERE sc.salesperson_id = ? AND u.parent_id IS NULL
        ORDER BY u.created_at DESC
    """, (sp["id"],)).fetchall()

    conn.close()
    return jsonify({
        "code": 0,
        "salesperson": dict(sp),
        "stats": {
            "customer_count": stats["customer_count"] if stats else 0,
            "total_deposits": stats["total_deposits"] if stats else 0.0,
        },
        "customers": [dict(c) for c in customers],
    })


@app.route("/api/salesperson/customer/<int:customer_id>/sub-accounts", methods=["GET"])
@salesperson_required
def api_salesperson_customer_subs(customer_id):
    """获取客户下的子账号列表"""
    conn = _get_db()
    subs = conn.execute("""
        SELECT u.id, u.username, u.email, u.phone, u.status, u.created_at,
               ub.points, ub.balance, ub.total_deposit, ub.total_used, ub.total_points_used,
               us.rate_per_second, us.channel_id,
               COALESCE(ch.name, '') AS channel_name
        FROM users u
        LEFT JOIN user_balance ub ON ub.user_id = u.id
        LEFT JOIN user_settings us ON us.user_id = u.id
        LEFT JOIN channels ch ON ch.id = us.channel_id
        WHERE u.parent_id = ?
        ORDER BY u.created_at DESC
    """, (customer_id,)).fetchall()
    conn.close()
    return jsonify({"code": 0, "sub_accounts": [dict(s) for s in subs]})


@app.route("/api/salesperson/customer/<int:customer_id>/usage", methods=["GET"])
@salesperson_required
def api_salesperson_customer_usage(customer_id):
    """获取客户用量统计：总秒数 + 各渠道秒数"""
    conn = _get_db()

    # 本地统计：总秒数
    total = conn.execute(
        "SELECT COALESCE(SUM(total_seconds), 0) AS s FROM user_channel_usage WHERE user_id=?", (customer_id,)
    ).fetchone()
    total_seconds = total["s"] if total else 0.0

    # 各渠道秒数明细
    channels_usage = conn.execute("""
        SELECT ucu.channel_id, COALESCE(ch.name, '未分配') AS channel_name,
               ucu.total_seconds, ucu.synced_seconds, ucu.last_synced_at
        FROM user_channel_usage ucu
        LEFT JOIN channels ch ON ch.id = ucu.channel_id
        WHERE ucu.user_id = ?
    """, (customer_id,)).fetchall()

    # 当前用户的渠道ID和费率
    settings = conn.execute(
        "SELECT channel_id, rate_per_second FROM user_settings WHERE user_id=?", (customer_id,)
    ).fetchone()
    current_channel_id = settings["channel_id"] if settings else None
    rate = settings["rate_per_second"] if settings else 2.30

    # 余额
    bal = conn.execute("SELECT balance FROM user_balance WHERE user_id=?", (customer_id,)).fetchone()
    balance = bal["balance"] if bal else 0.0

    conn.close()

    return jsonify({
        "code": 0,
        "total_seconds": round(total_seconds, 2),
        "balance": balance,
        "rate_per_second": rate,
        "remaining_seconds": round(balance / rate, 2) if rate > 0 else 0,
        "current_channel_id": current_channel_id,
        "channels": [dict(c) for c in channels_usage],
    })


@app.route("/api/salesperson/customer/<int:customer_id>", methods=["PUT"])
@salesperson_required
def api_salesperson_update_customer(customer_id):
    """销售员编辑客户信息"""
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    changes = data.get("changes", {})  # {field: new_value}

    if not changes:
        return jsonify({"code": 400, "message": "没有修改内容"}), 400

    conn = _get_db()

    # 验证该客户属于当前销售员
    sp = conn.execute("SELECT id FROM salespersons WHERE user_id=?", (uid,)).fetchone()
    if not sp:
        conn.close()
        return jsonify({"code": 403, "message": "无权限"}), 403

    rel = conn.execute(
        "SELECT 1 FROM salesperson_customer WHERE salesperson_id=? AND user_id=?",
        (sp["id"], customer_id)
    ).fetchone()
    if not rel:
        conn.close()
        return jsonify({"code": 403, "message": "该客户不属于您"}), 403

    # 允许编辑的字段
    allowed = {"email", "phone", "status", "rate_per_second", "channel_id"}
    applied = []

    for field, new_val in changes.items():
        if field not in allowed:
            continue
        if field == "email":
            conn.execute("UPDATE users SET email=? WHERE id=?", (str(new_val).strip(), customer_id))
        elif field == "phone":
            conn.execute("UPDATE users SET phone=? WHERE id=?", (str(new_val).strip(), customer_id))
        elif field == "status":
            conn.execute("UPDATE users SET status=? WHERE id=?", (str(new_val).strip(), customer_id))
        elif field == "rate_per_second":
            new_rate = float(new_val)
            conn.execute(
                "UPDATE user_settings SET rate_per_second=?, updated_at=? WHERE user_id=?",
                (new_rate, datetime.now().isoformat()[:19], customer_id)
            )
        elif field == "channel_id":
            conn.execute(
                "UPDATE user_settings SET channel_id=?, updated_at=? WHERE user_id=?",
                (int(new_val) if new_val else None, datetime.now().isoformat()[:19], customer_id)
            )
        applied.append(field)

    conn.commit()
    conn.close()

    logger.info("销售员 %s 修改了客户 %s: %s", session["username"], customer_id, applied)
    return jsonify({"code": 0, "message": f"已修改 {len(applied)} 项", "applied": applied})


@app.route("/api/salesperson/customer/<int:customer_id>/models", methods=["GET"])
@salesperson_required
def api_salesperson_customer_models(customer_id):
    """获取客户当前渠道下所有模型及启用状态"""
    conn = _get_db()
    # 获取客户的渠道
    settings = conn.execute("SELECT channel_id FROM user_settings WHERE user_id=?", (customer_id,)).fetchone()
    ch_id = settings["channel_id"] if settings else None
    if not ch_id:
        conn.close()
        return jsonify({"code": 0, "models": []})

    # 检查是否已有任何手动设置记录（首次初始化用）
    has_any = conn.execute(
        "SELECT 1 FROM customer_allowed_models WHERE user_id=? AND channel_id=?",
        (customer_id, ch_id)
    ).fetchone()

    all_models = conn.execute("""
        SELECT cmp.model_name, cmp.points_per_second
        FROM channel_model_pricing cmp
        WHERE cmp.channel_id = ?
        ORDER BY cmp.points_per_second
    """, (ch_id,)).fetchall()

    result = []
    for m in all_models:
        mname = m["model_name"]
        existing = conn.execute(
            "SELECT enabled FROM customer_allowed_models WHERE user_id=? AND model_name=? AND channel_id=?",
            (customer_id, mname, ch_id)
        ).fetchone()
        if existing:
            result.append({"model_name": mname, "points_per_second": m["points_per_second"], "enabled": existing["enabled"]})
        elif not has_any:
            # 首次：seedance 默认启用，其他默认禁用
            is_seedance = "seedance" in mname.lower()
            enabled = 1 if is_seedance else 0
            conn.execute(
                "INSERT OR IGNORE INTO customer_allowed_models (user_id, model_name, channel_id, enabled) VALUES (?, ?, ?, ?)",
                (customer_id, mname, ch_id, enabled)
            )
            result.append({"model_name": mname, "points_per_second": m["points_per_second"], "enabled": enabled})
        else:
            # 不在手动记录里但有设置记录：默认启用
            result.append({"model_name": mname, "points_per_second": m["points_per_second"], "enabled": 1})

    conn.commit()
    conn.close()
    return jsonify({"code": 0, "models": result})


@app.route("/api/salesperson/customer/<int:customer_id>/models/<model_name>/toggle", methods=["PUT"])
@salesperson_required
def api_salesperson_toggle_model(customer_id, model_name):
    """销售员为客户开关某个模型"""
    uid = session["user_id"]
    conn = _get_db()
    # 权限检查
    sp = conn.execute("SELECT id FROM salespersons WHERE user_id=?", (uid,)).fetchone()
    if not sp:
        conn.close()
        return jsonify({"code": 403, "message": "无权限"}), 403
    rel = conn.execute(
        "SELECT 1 FROM salesperson_customer WHERE salesperson_id=? AND user_id=?",
        (sp["id"], customer_id)
    ).fetchone()
    if not rel:
        conn.close()
        return jsonify({"code": 403, "message": "该客户不属于您"}), 403

    # 获取客户的渠道
    settings = conn.execute("SELECT channel_id FROM user_settings WHERE user_id=?", (customer_id,)).fetchone()
    ch_id = settings["channel_id"] if settings else None

    # 查当前状态
    existing = conn.execute(
        "SELECT id, enabled FROM customer_allowed_models WHERE user_id=? AND model_name=?",
        (customer_id, model_name)
    ).fetchone()
    if existing:
        new_val = 0 if existing["enabled"] else 1
        conn.execute("UPDATE customer_allowed_models SET enabled=? WHERE id=?", (new_val, existing["id"]))
    else:
        # 新建记录，默认禁用→启用
        conn.execute(
            "INSERT INTO customer_allowed_models (user_id, model_name, channel_id, enabled) VALUES (?, ?, ?, 0)",
            (customer_id, model_name, ch_id or 1)
        )
        new_val = 0

    conn.commit()
    conn.close()
    return jsonify({"code": 0, "enabled": bool(new_val), "message": f"模型 {model_name} 已{'启用' if new_val else '禁用'}"})


@app.route("/api/salesperson/customer/<int:customer_id>/recharge", methods=["POST"])
@salesperson_required
def api_salesperson_recharge(customer_id):
    """销售员为客户充值"""
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    amount = float(data.get("amount") or 0)
    desc = (data.get("description") or "").strip()

    if amount <= 0:
        return jsonify({"code": 400, "message": "充值金额必须大于 0"}), 400

    conn = _get_db()

    # 验证客户属于当前销售员
    sp = conn.execute("SELECT id FROM salespersons WHERE user_id=?", (uid,)).fetchone()
    if not sp:
        conn.close()
        return jsonify({"code": 403, "message": "无权限"}), 403
    rel = conn.execute(
        "SELECT 1 FROM salesperson_customer WHERE salesperson_id=? AND user_id=?",
        (sp["id"], customer_id)
    ).fetchone()
    if not rel:
        conn.close()
        return jsonify({"code": 403, "message": "该客户不属于您"}), 403

    # 获取当前余额
    bal = conn.execute(
        "SELECT balance, total_deposit FROM user_balance WHERE user_id=?", (customer_id,)
    ).fetchone()
    if not bal:
        conn.close()
        return jsonify({"code": 404, "message": "客户余额记录不存在"}), 404

    before = bal["balance"]
    after  = before + amount
    total  = bal["total_deposit"] + amount

    conn.execute(
        "UPDATE user_balance SET balance=?, total_deposit=? WHERE user_id=?",
        (after, total, customer_id)
    )
    conn.execute(
        "INSERT INTO recharge_records (user_id, amount, balance_before, balance_after, created_by, description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (customer_id, amount, before, after, uid, desc, datetime.now().isoformat()[:19])
    )

    conn.commit()
    conn.close()

    logger.info("销售员 %s 为客户 %s 充值 ¥%s", session["username"], customer_id, amount)
    return jsonify({
        "code": 0,
        "message": f"充值 ¥{amount:.2f} 成功",
        "balance_before": before,
        "balance_after": after,
    })


@app.route("/api/salesperson/customer/<int:customer_id>/recharges", methods=["GET"])
@salesperson_required
def api_salesperson_recharges(customer_id):
    """获取客户的充值记录"""
    conn = _get_db()
    records = conn.execute("""
        SELECT rr.id, rr.amount, rr.balance_before, rr.balance_after,
               rr.description, rr.created_at, u.username AS created_by_name
        FROM recharge_records rr
        JOIN users u ON u.id = rr.created_by
        WHERE rr.user_id = ?
        ORDER BY rr.created_at DESC
    """, (customer_id,)).fetchall()
    conn.close()
    return jsonify({"code": 0, "recharges": [dict(r) for r in records]})


@app.route("/api/channels/all", methods=["GET"])
def api_channels_all():
    """获取所有渠道（供下拉框使用）"""
    conn = _get_db()
    channels = conn.execute("SELECT id, name, status FROM channels ORDER BY id").fetchall()
    conn.close()
    return jsonify({"code": 0, "channels": [dict(c) for c in channels]})


@app.route("/api/channels/<int:ch_id>", methods=["PUT"])
@admin_required
def api_update_channel(ch_id):
    """管理员更新渠道配置（API Key 等）"""
    data = request.get_json(silent=True) or {}
    conn = _get_db()
    ch = conn.execute("SELECT * FROM channels WHERE id=?", (ch_id,)).fetchone()
    if not ch:
        conn.close()
        return jsonify({"code": 404, "message": "渠道不存在"}), 404

    new_api_key = data.get("api_key", ch["api_key"])
    new_base_url = data.get("base_url", ch["base_url"])
    new_status = data.get("status", ch["status"])

    conn.execute(
        "UPDATE channels SET api_key=?, base_url=?, status=? WHERE id=?",
        (new_api_key, new_base_url, new_status, ch_id)
    )
    conn.commit()
    conn.close()
    logger.info("管理员更新渠道 %s", ch["name"])
    return jsonify({"code": 0, "message": f"渠道 {ch['name']} 已更新"})


@app.route("/api/pricing", methods=["GET"])
def api_pricing():
    """获取渠道-模型积分定价"""
    conn = _get_db()
    rows = conn.execute("""
        SELECT cmp.id, cmp.channel_id, ch.name AS channel_name,
               cmp.model_name, cmp.points_per_second
        FROM channel_model_pricing cmp
        JOIN channels ch ON ch.id = cmp.channel_id
        WHERE ch.status = 'active'
        ORDER BY cmp.channel_id, cmp.points_per_second
    """).fetchall()
    conn.close()
    return jsonify({"code": 0, "pricing": [dict(r) for r in rows]})

@app.route("/api/balance", methods=["GET"])
@login_required
def api_balance():
    uid = session["user_id"]
    conn = _get_db()
    bal = conn.execute(
        "SELECT balance, total_deposit, total_used, points FROM user_balance WHERE user_id=?", (uid,)
    ).fetchone()
    if not bal:
        conn.execute(
            "INSERT INTO user_balance (user_id, balance, total_deposit, total_used, points) VALUES (?, 0, 0, 0, 0)",
            (uid,)
        )
        conn.commit()
        bal = {"balance": 0, "total_deposit": 0, "total_used": 0, "points": 0}
    else:
        bal = dict(bal)
    # 获取计费单价
    set_row = conn.execute(
        "SELECT rate_per_second, channel_id FROM user_settings WHERE user_id=?", (uid,)
    ).fetchone()
    channel_name = ""
    ch_id = None
    rate = 2.30
    if set_row:
        ch_id = set_row["channel_id"]
        rate = set_row["rate_per_second"] or 2.30
        if ch_id:
            ch = conn.execute("SELECT name FROM channels WHERE id=?", (ch_id,)).fetchone()
            if ch:
                channel_name = ch["name"]

    # 用量统计
    total_row = conn.execute(
        "SELECT COALESCE(SUM(total_seconds), 0) AS s FROM user_channel_usage WHERE user_id=?", (uid,)
    ).fetchone()
    total_seconds = round(total_row["s"], 2) if total_row else 0.0

    # 当前渠道用量
    cur_ch_seconds = 0.0
    if ch_id:
        ch_row = conn.execute(
            "SELECT total_seconds FROM user_channel_usage WHERE user_id=? AND channel_id=?", (uid, ch_id)
        ).fetchone()
        cur_ch_seconds = round(ch_row["total_seconds"], 2) if ch_row else 0.0

    # 子账号积分总消耗（主账号视角）
    sub_points_used = 0
    sub_rows = conn.execute(
        "SELECT COALESCE(SUM(total_points_used), 0) AS s FROM user_balance ub JOIN users u ON u.id = ub.user_id WHERE u.parent_id = ?", (uid,)
    ).fetchone()
    sub_points_used = sub_rows["s"] if sub_rows else 0

    conn.close()

    remaining = round(bal["balance"] / rate, 2) if rate > 0 else 0.0

    return jsonify({
        "code": 0,
        "balance": bal["balance"],
        "total_deposit": bal["total_deposit"],
        "total_used": bal["total_used"],
        "points": bal["points"],
        "sub_points_used": sub_points_used,
        "rate_per_second": rate,
        "channel_id": ch_id,
        "channel_name": channel_name,
        "total_seconds": total_seconds,
        "channel_seconds": cur_ch_seconds,
        "remaining_seconds": remaining,
        "unit": "元",
    })


@app.route("/api/channels/active", methods=["GET"])
def api_channels_active():
    """获取当前激活的渠道列表（客户只能看，不能改）"""
    conn = _get_db()
    channels = conn.execute(
        "SELECT id, name, status FROM channels WHERE status='active' ORDER BY id"
    ).fetchall()
    conn.close()
    return jsonify({"code": 0, "channels": [dict(c) for c in channels]})


@app.route("/api/user-settings", methods=["GET"])
@login_required
def api_get_settings():
    """获取当前用户的设置（选中的模型等）"""
    uid = session["user_id"]
    conn = _get_db()
    s = conn.execute("SELECT selected_model FROM user_settings WHERE user_id=?", (uid,)).fetchone()
    if not s:
        conn.execute("INSERT INTO user_settings (user_id, selected_model, rate_per_second, updated_at) VALUES (?, '', 2.30, ?)",
                     (uid, datetime.now().isoformat()[:19]))
        conn.commit()
        selected_model = ""
    else:
        selected_model = s["selected_model"] or ""
    conn.close()
    return jsonify({"code": 0, "selected_model": selected_model})


@app.route("/api/user-settings/model", methods=["PUT"])
@login_required
def api_update_model():
    """保存用户选择的模型"""
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    if not model:
        return jsonify({"code": 400, "message": "请选择模型"}), 400

    conn = _get_db()
    exists = conn.execute("SELECT 1 FROM user_settings WHERE user_id=?", (uid,)).fetchone()
    if exists:
        conn.execute("UPDATE user_settings SET selected_model=?, updated_at=? WHERE user_id=?",
                     (model, datetime.now().isoformat()[:19], uid))
    else:
        conn.execute("INSERT INTO user_settings (user_id, selected_model, rate_per_second, updated_at) VALUES (?, ?, 2.30, ?)",
                     (uid, model, datetime.now().isoformat()[:19]))
    conn.commit()
    conn.close()
    return jsonify({"code": 0, "message": "模型设置已保存", "model": model})


# ============================================================ #
#                        公开页面                                  #
# ============================================================ #

def _serve_html(filename, required=True):
    """返回 HTML 文件，支持从 session 注入用户信息"""
    path = os.path.join(FRONTEND_DIR, filename)
    if not os.path.exists(path):
        return jsonify({"code": 404, "message": f"页面 {filename} 不存在"}), 404
    return send_from_directory(FRONTEND_DIR, filename)

@app.route("/")
def index():
    return _serve_html("index.html")

@app.route("/login")
def login_page():
    return _serve_html("login.html")

@app.route("/register")
def register_page():
    return _serve_html("register.html")

# 保护页面 — 通过 JS 检查登录状态
@app.route("/dashboard")
def dashboard_page():
    return _serve_html("dashboard.html")

@app.route("/admin")
def admin_page():
    return _serve_html("admin.html")

@app.route("/sales")
def sales_page():
    return _serve_html("sales.html")

# ============================================================ #
#                       认证 API                                  #
# ============================================================ #

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    username   = (data.get("username") or "").strip()
    email      = (data.get("email") or "").strip()
    phone      = (data.get("phone") or "").strip()
    password   = (data.get("password") or "").strip()
    sales_code = (data.get("sales_code") or "").strip().lower()

    # 必填校验
    if not username or not email or not phone or not password:
        return jsonify({"code": 400, "message": "用户名、邮箱、手机号、密码为必填项"}), 400
    if len(username) < 3:
        return jsonify({"code": 400, "message": "用户名至少 3 个字符"}), 400
    if len(password) < 6:
        return jsonify({"code": 400, "message": "密码至少 6 个字符"}), 400

    # 手机号校验：中国大陆 11 位，1 开头
    phone_pat = r'^1[3-9]\d{9}$'
    if not re.match(phone_pat, phone):
        return jsonify({"code": 400, "message": "手机号格式不正确（11位中国大陆手机号）"}), 400

    # 邮箱格式校验
    email_pat = r'^[^@\s]+@[^@\s]+\.[^@\s]+$'
    if not re.match(email_pat, email):
        return jsonify({"code": 400, "message": "邮箱格式不正确"}), 400

    conn = _get_db()

    # 查重
    existing = conn.execute(
        "SELECT id FROM users WHERE username=? OR email=? OR phone=?",
        (username, email, phone)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"code": 400, "message": "用户名、邮箱或手机号已被注册"}), 400

    # 销售员码匹配
    salesperson_id = None
    matched_sales_name = ""
    if sales_code:
        sp = conn.execute(
            "SELECT id, name FROM salespersons WHERE code=?", (sales_code,)
        ).fetchone()
        if sp:
            salesperson_id = sp["id"]
            matched_sales_name = sp["name"]
        else:
            conn.close()
            return jsonify({"code": 400, "message": f"销售员码 '{sales_code}' 不存在"}), 400

    # 写入用户（status=pending 待审核）
    conn.execute(
        "INSERT INTO users (username, email, phone, password, role, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, email, phone, generate_password_hash(password), "user", "pending",
         datetime.now().isoformat()[:19])
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # 关联销售员
    if salesperson_id:
        conn.execute(
            "INSERT INTO salesperson_customer (salesperson_id, user_id, created_at) VALUES (?, ?, ?)",
            (salesperson_id, new_id, datetime.now().isoformat()[:19])
        )

    conn.execute("INSERT INTO user_balance (user_id, balance, total_deposit, total_used, points) VALUES (?, 0, 0, 0, 0)", (new_id,))
    conn.execute("INSERT INTO user_settings (user_id, selected_model, rate_per_second, updated_at) VALUES (?, '', 2.30, ?)",
                 (new_id, datetime.now().isoformat()[:19]))
    conn.commit()
    conn.close()

    logger.info("新用户注册: %s (%s) phone=%s status=pending", username, email, phone)

    msg = "注册已成功，等待销售员进行审核通过"
    if sales_code and matched_sales_name:
        msg += f"（销售员：{matched_sales_name}）"

    return jsonify({"code": 0, "message": msg})


# -------- 积分使用明细 --------

@app.route("/api/points-usage", methods=["GET"])
@login_required
def api_points_usage():
    """获取积分使用记录（支持子账号筛选）"""
    uid = session["user_id"]
    sub_id = request.args.get("user_id", "").strip()
    target_id = int(sub_id) if sub_id else uid

    conn = _get_db()
    # 权限检查：只能查自己或自己的子账号
    if target_id != uid:
        sub = conn.execute("SELECT id FROM users WHERE id=? AND parent_id=?", (target_id, uid)).fetchone()
        if not sub:
            conn.close()
            return jsonify({"code": 403, "message": "无权查看"}), 403

    records = conn.execute("""
        SELECT pur.id, pur.points_used, pur.balance_after, pur.model_name,
               pur.duration, pur.created_at
        FROM points_usage_records pur
        WHERE pur.user_id = ?
        ORDER BY pur.created_at DESC
        LIMIT 50
    """, (target_id,)).fetchall()
    total = conn.execute(
        "SELECT COALESCE(SUM(points_used), 0) AS total FROM points_usage_records WHERE user_id=?", (target_id,)
    ).fetchone()

    conn.close()
    return jsonify({"code": 0, "total_points_used": total["total"] if total else 0, "records": [dict(r) for r in records]})


@app.route("/api/sub-accounts", methods=["GET"])
@login_required
def api_sub_accounts():
    """获取当前用户创建的所有子账号"""
    uid = session["user_id"]
    conn = _get_db()
    subs = conn.execute("""
        SELECT u.id, u.username, u.email, u.phone, u.status, u.created_at,
               ub.points, ub.balance, ub.total_points_used
        FROM users u
        LEFT JOIN user_balance ub ON ub.user_id = u.id
        WHERE u.parent_id = ?
        ORDER BY u.created_at DESC
    """, (uid,)).fetchall()
    conn.close()
    return jsonify({"code": 0, "sub_accounts": [dict(s) for s in subs]})


@app.route("/api/sub-accounts", methods=["POST"])
@login_required
def api_create_sub_account():
    """主账号创建子账号（自动审核通过）"""
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email    = (data.get("email") or "").strip()
    phone    = (data.get("phone") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not email or not phone or not password:
        return jsonify({"code": 400, "message": "用户名、邮箱、手机号、密码为必填项"}), 400
    if len(username) < 3:
        return jsonify({"code": 400, "message": "用户名至少 3 个字符"}), 400
    if len(password) < 6:
        return jsonify({"code": 400, "message": "密码至少 6 个字符"}), 400
    if not re.match(r'^1[3-9]\d{9}$', phone):
        return jsonify({"code": 400, "message": "手机号格式不正确"}), 400
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({"code": 400, "message": "邮箱格式不正确"}), 400

    conn = _get_db()
    existing = conn.execute(
        "SELECT id FROM users WHERE username=? OR email=? OR phone=?", (username, email, phone)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"code": 400, "message": "用户名、邮箱或手机号已被注册"}), 400

    # 继承主账号的销售员关联
    main_sp = conn.execute(
        "SELECT salesperson_id FROM salesperson_customer WHERE user_id=?", (uid,)
    ).fetchone()
    # 继承主账号的设置
    main_settings = conn.execute(
        "SELECT selected_model, rate_per_second, channel_id FROM user_settings WHERE user_id=?", (uid,)
    ).fetchone()

    conn.execute(
        "INSERT INTO users (username, email, phone, password, role, status, parent_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (username, email, phone, generate_password_hash(password), "user", "approved", uid,
         datetime.now().isoformat()[:19])
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    if main_sp:
        conn.execute(
            "INSERT INTO salesperson_customer (salesperson_id, user_id, created_at) VALUES (?, ?, ?)",
            (main_sp["salesperson_id"], new_id, datetime.now().isoformat()[:19])
        )
    conn.execute("INSERT INTO user_balance (user_id, balance, total_deposit, total_used, points) VALUES (?, 0, 0, 0, 0)", (new_id,))
    if main_settings:
        conn.execute(
            "INSERT INTO user_settings (user_id, selected_model, rate_per_second, channel_id, updated_at) VALUES (?, ?, ?, ?, ?)",
            (new_id, main_settings["selected_model"] or "", main_settings["rate_per_second"] or 2.30,
             main_settings["channel_id"], datetime.now().isoformat()[:19])
        )
    else:
        conn.execute(
            "INSERT INTO user_settings (user_id, selected_model, rate_per_second, updated_at) VALUES (?, '', 2.30, ?)",
            (new_id, datetime.now().isoformat()[:19])
        )

    conn.commit()
    conn.close()

    logger.info("子账号创建: %s (主账号=%s)", username, session["username"])
    return jsonify({"code": 0, "message": "子账号创建成功，已自动审核通过，可直接登录"})


@app.route("/api/sub-accounts/<int:sub_id>", methods=["DELETE"])
@login_required
def api_delete_sub_account(sub_id):
    """删除子账号（只能删自己的）"""
    uid = session["user_id"]
    conn = _get_db()
    sub = conn.execute("SELECT id FROM users WHERE id=? AND parent_id=?", (sub_id, uid)).fetchone()
    if not sub:
        conn.close()
        return jsonify({"code": 404, "message": "子账号不存在或不属于您"}), 404
    conn.execute("DELETE FROM user_balance WHERE user_id=?", (sub_id,))
    conn.execute("DELETE FROM user_settings WHERE user_id=?", (sub_id,))
    conn.execute("DELETE FROM salesperson_customer WHERE user_id=?", (sub_id,))
    conn.execute("DELETE FROM user_channel_usage WHERE user_id=?", (sub_id,))
    conn.execute("DELETE FROM users WHERE id=?", (sub_id,))
    conn.commit()
    conn.close()
    return jsonify({"code": 0, "message": "子账号已删除"})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"code": 400, "message": "请输入用户名和密码"}), 400

    conn = _get_db()
    user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()

    if not user or not check_password_hash(user["password"], password):
        return jsonify({"code": 401, "message": "用户名或密码错误"}), 401

    if user["status"] == "pending":
        return jsonify({"code": 403, "message": "您的账号正在等待销售员审核，审核通过后方可登录"}), 403

    session["user_id"]  = user["id"]
    session["username"] = user["username"]
    session["role"]     = user["role"]

    logger.info("用户登录: %s (role=%s)", username, user["role"])
    return jsonify({
        "code": 0,
        "message": "登录成功",
        "user": {"id": user["id"], "username": user["username"], "email": user["email"], "role": user["role"]},
    })


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"code": 0, "message": "已退出登录"})


@app.route("/api/me", methods=["GET"])
def api_me():
    if not session.get("user_id"):
        return jsonify({"code": 401, "logged_in": False}), 401
    return jsonify({
        "code": 0,
        "logged_in": True,
        "user": {
            "id":       session["user_id"],
            "username": session["username"],
            "role":     session["role"],
        }
    })


# ============================================================ #
#                      管理员 API                                 #
# ============================================================ #

@app.route("/api/users", methods=["GET"])
@admin_required
def api_users():
    conn = _get_db()
    users = conn.execute("SELECT id, username, email, phone, role, status, created_at FROM users ORDER BY id").fetchall()
    conn.close()
    return jsonify({"code": 0, "users": [dict(u) for u in users]})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@admin_required
def api_delete_user(uid):
    if uid == session["user_id"]:
        return jsonify({"code": 400, "message": "不能删除自己"}), 400
    conn = _get_db()
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return jsonify({"code": 0, "message": "用户已删除"})


@app.route("/api/users", methods=["POST"])
@admin_required
def api_create_user():
    """管理员创建用户（可指定角色：admin / sales / user）"""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email    = (data.get("email") or "").strip()
    phone    = (data.get("phone") or "").strip()
    password = (data.get("password") or "").strip()
    role     = (data.get("role") or "user").strip()

    if not username or not email or not phone or not password:
        return jsonify({"code": 400, "message": "用户名、邮箱、手机号、密码为必填项"}), 400
    if len(username) < 3:
        return jsonify({"code": 400, "message": "用户名至少 3 个字符"}), 400
    if len(password) < 6:
        return jsonify({"code": 400, "message": "密码至少 6 个字符"}), 400
    if role not in ("admin", "sales", "user"):
        return jsonify({"code": 400, "message": "角色必须为 admin、sales 或 user"}), 400
    if not re.match(r'^1[3-9]\d{9}$', phone):
        return jsonify({"code": 400, "message": "手机号格式不正确"}), 400
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({"code": 400, "message": "邮箱格式不正确"}), 400

    conn = _get_db()
    existing = conn.execute(
        "SELECT id FROM users WHERE username=? OR email=? OR phone=?",
        (username, email, phone)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"code": 400, "message": "用户名、邮箱或手机号已被注册"}), 400

    conn.execute(
        "INSERT INTO users (username, email, phone, password, role, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (username, email, phone, generate_password_hash(password), role, "approved",
         datetime.now().isoformat()[:19])
    )
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO user_balance (user_id, balance, total_deposit, total_used, points) VALUES (?, 0, 0, 0, 0)", (new_id,))
    conn.execute("INSERT INTO user_settings (user_id, selected_model, rate_per_second, updated_at) VALUES (?, '', 2.30, ?)",
                 (new_id, datetime.now().isoformat()[:19]))
    conn.commit()
    conn.close()

    logger.info("管理员创建用户: %s role=%s", username, role)
    return jsonify({"code": 0, "message": f"用户 {username} 创建成功", "user_id": new_id})


@app.route("/api/users/<int:uid>", methods=["GET"])
@admin_required
def api_get_user(uid):
    """获取单个用户详情"""
    conn = _get_db()
    u = conn.execute(
        "SELECT id, username, email, phone, role, status, created_at FROM users WHERE id=?", (uid,)
    ).fetchone()
    if not u:
        conn.close()
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    bal = conn.execute("SELECT * FROM user_balance WHERE user_id=?", (uid,)).fetchone()
    sets = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (uid,)).fetchone()
    # 子账号
    subs = conn.execute("SELECT id, username, email, phone, status, created_at FROM users WHERE parent_id=?", (uid,)).fetchall()
    # 积分使用记录
    usage = conn.execute(
        "SELECT * FROM points_usage_records WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (uid,)
    ).fetchall()
    conn.close()

    return jsonify({
        "code": 0,
        "user": dict(u),
        "balance": dict(bal) if bal else None,
        "settings": dict(sets) if sets else None,
        "sub_accounts": [dict(s) for s in subs],
        "points_usage": [dict(r) for r in usage],
    })


@app.route("/api/users/<int:uid>", methods=["PUT"])
@admin_required
def api_update_user(uid):
    """管理员更新用户信息（角色、状态、邮箱、手机号、积分、余额等）"""
    data = request.get_json(silent=True) or {}
    conn = _get_db()
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        conn.close()
        return jsonify({"code": 404, "message": "用户不存在"}), 404

    changes = []

    # --- 用户表字段 ---
    new_email = data.get("email", u["email"]).strip() if data.get("email") is not None else u["email"]
    new_phone = data.get("phone", u["phone"]).strip() if data.get("phone") is not None else u["phone"]

    new_role = data.get("role", u["role"]).strip() if data.get("role") is not None else u["role"]
    new_status = data.get("status", u["status"]).strip() if data.get("status") is not None else u["status"]

    # 不能改自己的角色
    if uid == session["user_id"]:
        new_role = u["role"]

    if new_role not in ("admin", "sales", "user"):
        new_role = u["role"]
    if new_status not in ("pending", "approved", "disabled"):
        new_status = u["status"]

    # 邮箱/手机号查重（排除自己）
    if new_email != u["email"]:
        dup = conn.execute("SELECT id FROM users WHERE email=? AND id!=?", (new_email, uid)).fetchone()
        if dup:
            conn.close()
            return jsonify({"code": 400, "message": "该邮箱已被其他用户使用"}), 400
    if new_phone != u["phone"]:
        dup = conn.execute("SELECT id FROM users WHERE phone=? AND id!=?", (new_phone, uid)).fetchone()
        if dup:
            conn.close()
            return jsonify({"code": 400, "message": "该手机号已被其他用户使用"}), 400

    conn.execute(
        "UPDATE users SET email=?, phone=?, role=?, status=? WHERE id=?",
        (new_email, new_phone, new_role, new_status, uid)
    )
    changes.append(f"email={new_email} phone={new_phone} role={new_role} status={new_status}")

    # --- 余额表字段 ---
    if "points" in data or "balance" in data:
        bal = conn.execute("SELECT * FROM user_balance WHERE user_id=?", (uid,)).fetchone()
        if bal:
            new_points = int(data.get("points", bal["points"])) if data.get("points") is not None else bal["points"]
            new_balance = float(data.get("balance", bal["balance"])) if data.get("balance") is not None else bal["balance"]
            new_total_deposit = float(data.get("total_deposit", bal["total_deposit"])) if data.get("total_deposit") is not None else bal["total_deposit"]
            conn.execute(
                "UPDATE user_balance SET points=?, balance=?, total_deposit=? WHERE user_id=?",
                (new_points, new_balance, new_total_deposit, uid)
            )
            changes.append(f"points={new_points} balance={new_balance}")
        else:
            new_points = int(data.get("points", 0))
            new_balance = float(data.get("balance", 0))
            conn.execute(
                "INSERT INTO user_balance (user_id, balance, total_deposit, total_used, points) VALUES (?, ?, 0, 0, ?)",
                (uid, new_balance, new_points)
            )
            changes.append(f"points={new_points} balance={new_balance} (新建)")

    conn.commit()
    conn.close()
    logger.info("管理员更新用户 %s: %s", u["username"], ", ".join(changes))
    return jsonify({"code": 0, "message": "用户信息已更新"})


@app.route("/api/admin/videos", methods=["GET"])
@admin_required
def api_admin_videos():
    """管理员查看所有视频"""
    return jsonify({"code": 0, "videos": list(_video_store)})


@app.route("/api/admin/tasks", methods=["GET"])
@admin_required
def api_admin_tasks():
    """管理员查看所有任务"""
    tasks = []
    for tid, t in _task_store.items():
        tasks.append({
            "task_id": tid,
            "model": t.get("model", ""),
            "status": t.get("status", ""),
            "prompt": (t.get("prompt", "") or "")[:80],
            "created_at": t.get("created_at", ""),
            "user_id": t.get("user_id", 0),
            "username": t.get("username", ""),
        })
    return jsonify({"code": 0, "tasks": tasks})


@app.route("/api/site-stats", methods=["GET"])
@admin_required
def api_site_stats():
    conn = _get_db()
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
    conn.close()
    return jsonify({
        "code": 0,
        "stats": {
            "total_users": total,
            "admin_users": admin_count,
            "videos_stored": len(_video_store),
            "tasks_processed": len(_task_store),
        }
    })


# ============================================================ #
#                       AI 网关 API                               #
# ============================================================ #

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})


@app.route("/api/models", methods=["GET"])
def list_models():
    """返回当前用户可用的模型列表（主数据源: channel_model_pricing）"""
    conn = _get_db()
    uid = session.get("user_id")
    user_ch_id = None
    disabled_set = set()

    if uid:
        us = conn.execute("SELECT channel_id FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        if us and us["channel_id"]:
            user_ch_id = us["channel_id"]
        role = conn.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        is_regular = role and role["role"] == "user"
        if is_regular:
            rows = conn.execute(
                "SELECT model_name FROM customer_allowed_models WHERE user_id=? AND channel_id=? AND enabled=0",
                (uid, user_ch_id or 0)
            ).fetchall()
            disabled_set = set(r["model_name"] for r in rows)

    # 从定价表加载，保证和销售员端看到的名字完全一致
    if user_ch_id:
        rows = conn.execute("""
            SELECT cmp.model_name, cmp.points_per_second, ch.name AS channel_name, ch.id AS channel_id
            FROM channel_model_pricing cmp
            JOIN channels ch ON ch.id = cmp.channel_id
            WHERE cmp.channel_id = ?
            ORDER BY cmp.points_per_second
        """, (user_ch_id,)).fetchall()
    else:
        # 未分配渠道：展示所有渠道的定价模型
        rows = conn.execute("""
            SELECT cmp.model_name, cmp.points_per_second, ch.name AS channel_name, ch.id AS channel_id
            FROM channel_model_pricing cmp
            JOIN channels ch ON ch.id = cmp.channel_id
            WHERE ch.status = 'active'
            ORDER BY cmp.points_per_second
        """).fetchall()

    conn.close()

    all_models = []
    for r in rows:
        if r["model_name"] in disabled_set:
            continue
        all_models.append({
            "modelName": r["model_name"],
            "channel_id": r["channel_id"],
            "channel_name": r["channel_name"],
            "points_per_second": r["points_per_second"],
        })

    return jsonify({"code": 0, "models": all_models})


@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    model    = (data.get("model") or "").strip()
    messages = data.get("messages", [])
    text     = (data.get("text") or "").strip()
    duration = data.get("duration", 0)
    size     = data.get("size", "16:9")  # 百度云视频比例参数

    if not messages and text:
        messages = [{"role": "user", "content": text}]
    if not model:
        return jsonify({"code": 400, "message": "请选择模型"}), 400
    if not messages:
        return jsonify({"code": 400, "message": "请输入内容"}), 400
    if not duration or int(duration) < 5:
        return jsonify({"code": 400, "message": "请选择视频时长（最少5秒）"}), 400

    duration = int(duration)
    uid = session.get("user_id")
    prompt_text = messages[0].get("content", "")

    # -------- 模型禁用检查 --------
    if uid:
        conn_pre = _get_db()
        role_row = conn_pre.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if role_row and role_row["role"] == "user":
            disabled = conn_pre.execute(
                "SELECT 1 FROM customer_allowed_models WHERE user_id=? AND model_name=? AND enabled=0",
                (uid, model)
            ).fetchone()
            if disabled:
                conn_pre.close()
                return jsonify({"code": 403, "message": f"模型 {model} 已被禁用，请联系销售员开通"}), 403
        conn_pre.close()

    # -------- 积分预检 --------
    cost = 0
    parent_id = None
    bill_user_id = None
    points_remaining = 0

    if uid:
        conn3 = _get_db()
        user_info = conn3.execute("SELECT parent_id FROM users WHERE id=?", (uid,)).fetchone()
        parent_id = user_info["parent_id"] if user_info else None
        bill_user_id = parent_id if parent_id else uid

        settings = conn3.execute("SELECT channel_id FROM user_settings WHERE user_id=?", (uid,)).fetchone()
        ch_id = settings["channel_id"] if settings else None
        if ch_id:
            pricing = conn3.execute(
                "SELECT points_per_second FROM channel_model_pricing WHERE channel_id=? AND model_name=?",
                (ch_id, model)
            ).fetchone()
            pps = pricing["points_per_second"] if pricing else 10
        else:
            pps = 10
        cost = duration * pps

        bal = conn3.execute("SELECT points FROM user_balance WHERE user_id=?", (bill_user_id,)).fetchone()
        current_points = bal["points"] if bal else 0
        if current_points < cost:
            conn3.close()
            who = "主账号" if parent_id else "您"
            return jsonify({"code": 402, "message": f"积分不足！需要 {cost} 积分，{who}当前只有 {current_points} 积分"}), 402

        # 获取渠道信息 — 优先按模型匹配正确的渠道
        ch_row = conn3.execute(
            "SELECT ch.id, ch.name, ch.base_url, ch.api_key FROM channels ch "
            "JOIN user_settings us ON us.channel_id = ch.id WHERE us.user_id=?",
            (uid,)
        ).fetchone() if ch_id else None
        if ch_row:
            # 检查该渠道是否有这个模型
            model_in_ch = conn3.execute(
                "SELECT 1 FROM channel_model_pricing WHERE channel_id=? AND model_name=?",
                (ch_row["id"], model)
            ).fetchone()
            if model_in_ch:
                channel_info = dict(ch_row)
            else:
                ch_row = None  # 用户渠道不支持该模型，走回退逻辑

        if not ch_row:
            # 回退：按模型找正确的渠道
            model_ch = conn3.execute(
                "SELECT ch.id, ch.name, ch.base_url, ch.api_key FROM channels ch "
                "JOIN channel_model_pricing cmp ON cmp.channel_id = ch.id "
                "WHERE cmp.model_name=? AND ch.status='active' LIMIT 1",
                (model,)
            ).fetchone()
            if model_ch:
                channel_info = dict(model_ch)
            else:
                # 最后回退：取第一个活跃渠道
                fb = conn3.execute("SELECT id, name, base_url, api_key FROM channels WHERE status='active' ORDER BY id LIMIT 1").fetchone()
                if fb:
                    channel_info = dict(fb)
                else:
                    channel_info = {"name": "天翼云", "base_url": CTYUN_BASE_URL, "id": ch_id or 1, "api_key": CTYUN_API_KEY}
        conn3.close()
    else:
        conn_fb = _get_db()
        fb = conn_fb.execute("SELECT id, name, base_url, api_key FROM channels WHERE status='active' ORDER BY id LIMIT 1").fetchone()
        if fb:
            channel_info = dict(fb)
        else:
            channel_info = {"name": "天翼云", "base_url": CTYUN_BASE_URL, "id": 1, "api_key": CTYUN_API_KEY}
        conn_fb.close()

    headers = {"Authorization": f"Bearer {channel_info.get('api_key', '')}", "Content-Type": "application/json"}
    is_video = "seedance" in model.lower()

    # -------- 根据模型类型调用不同 API --------
    t1 = time.time()
    video_url = ""
    content = ""
    usage_tokens = 0

    if is_video:
        # --- 视频生成（天翼云/百度云，异步不阻塞） ---
        ch_name = channel_info["name"]
        is_ty = (ch_name == "天翼云")
        try:
            if is_ty:
                # 天翼云: POST /v1/video/generations
                video_params = {
                    "model": model,
                    "prompt": prompt_text,
                    "metadata": {
                        "ratio": size,
                        "duration": duration,
                        "generate_audio": True,
                        "watermark": False,
                    }
                }
                video_url_path = "/video/generations"
                query_url_path = "/video/generations/"
            else:
                # 百度云: POST /videos (base_url 已含 /v1)
                video_params = {
                    "model": model, "prompt": prompt_text,
                    "seconds": str(duration), "size": size,
                }
                video_url_path = "/videos"
                # 百度云统一用 /videos/:id 查询
                query_url_path = "/videos/"

            logger.info("%s提交视频: %s", ch_name, video_params)
            resp = requests.post(
                f"{channel_info['base_url']}{video_url_path}",
                headers=headers, json=video_params, timeout=60
            )
            resp.raise_for_status()
            submit_result = resp.json()
            remote_task_id = submit_result.get("id") or submit_result.get("task_id", "")

            if not remote_task_id:
                return jsonify({"code": 502, "message": f"{ch_name}返回异常: {submit_result}"}), 502

            logger.info("%s视频任务已提交: task_id=%s (异步)", ch_name, remote_task_id)
            elapsed = round(time.time() - t1, 2)

            task_id = str(uuid.uuid4())
            task_data = {
                "model": model, "text": prompt_text[:200],
                "status": "queued",
                "remote_task_id": remote_task_id,
                "channel_name": ch_name,
                "query_url_path": query_url_path,
                "video_url": "",
                "content": "",
                "owner_id": uid,
                "username": session.get("username", ""),
                "created_at": time.time(),
            }
            _task_store[task_id] = task_data
            # 持久化到 SQLite
            _save_task_to_db(task_id, task_data)

            if uid:
                conn2 = _get_db()
                if cost > 0 and bill_user_id:
                    new_points = conn2.execute(
                        "SELECT points FROM user_balance WHERE user_id=?", (bill_user_id,)
                    ).fetchone()["points"]
                    conn2.execute("UPDATE user_balance SET points=points-? WHERE user_id=?", (cost, bill_user_id))
                    conn2.execute("UPDATE user_balance SET total_points_used=total_points_used+? WHERE user_id=?", (cost, uid))
                    conn2.execute(
                        "INSERT INTO points_usage_records (user_id, points_used, balance_after, model_name, duration, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (uid, cost, new_points - cost, model, duration, datetime.now().isoformat()[:19])
                    )
                    points_remaining = new_points - cost
                conn2.execute(
                    "INSERT INTO usage_records (user_id, channel_id, seconds_used, tokens_used, request_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (uid, channel_info["id"], elapsed, 0, prompt_text[:100], datetime.now().isoformat()[:19])
                )
                conn2.commit()
                conn2.close()

            return jsonify({
                "code": 0, "task_id": task_id,
                "remote_task_id": remote_task_id,
                "status": "queued",
                "elapsed_seconds": elapsed,
                "points_used": cost,
                "points_remaining": points_remaining,
                "message": f"{ch_name}视频任务已提交 (ID: {remote_task_id})，请稍后查询结果",
            })

        except requests.exceptions.RequestException as e:
            logger.error("%s调用失败: %s", ch_name, e)
            return jsonify({"code": 502, "message": f"{ch_name} AI 服务调用失败: {e}"}), 502
    else:
        # --- 天翼云：Chat Completions ---
        params = {"model": model, "messages": messages}
        if data.get("max_tokens"):  params["max_tokens"]  = data["max_tokens"]
        if data.get("temperature"): params["temperature"] = data["temperature"]
        if data.get("top_p"):       params["top_p"]       = data["top_p"]

        try:
            resp = requests.post(
                f"{channel_info['base_url']}/chat/completions",
                headers=headers, json=params, timeout=120
            )
            resp.raise_for_status()
            result = resp.json()
        except requests.exceptions.RequestException as e:
            logger.error("AI网关调用失败: %s", e)
            return jsonify({"code": 502, "message": f"AI 服务调用失败: {e}"}), 502
        elapsed = round(time.time() - t1, 2)
        usage_tokens = result.get("usage", {}).get("total_tokens", 0)
        choices = result.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        video_url = _extract_video_url(result)

    # 记录用量 + 积分扣减（API已成功，此时执行）
    uid = session.get("user_id")
    if uid:
        conn2 = _get_db()

        if cost > 0 and bill_user_id:
            new_points = conn2.execute(
                "SELECT points FROM user_balance WHERE user_id=?", (bill_user_id,)
            ).fetchone()["points"]
            conn2.execute("UPDATE user_balance SET points=points-? WHERE user_id=?", (cost, bill_user_id))
            conn2.execute("UPDATE user_balance SET total_points_used=total_points_used+? WHERE user_id=?", (cost, uid))
            conn2.execute(
                "INSERT INTO points_usage_records (user_id, points_used, balance_after, model_name, duration, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, cost, new_points - cost, model, duration, datetime.now().isoformat()[:19])
            )
            points_remaining = new_points - cost

        conn2.execute(
            "INSERT INTO usage_records (user_id, channel_id, seconds_used, tokens_used, request_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, channel_info["id"], elapsed, usage_tokens, prompt_text[:100],
             datetime.now().isoformat()[:19])
        )
        existing = conn2.execute(
            "SELECT 1 FROM user_channel_usage WHERE user_id=? AND channel_id=?", (uid, channel_info["id"])
        ).fetchone()
        if existing:
            conn2.execute(
                "UPDATE user_channel_usage SET total_seconds=total_seconds+? WHERE user_id=? AND channel_id=?",
                (elapsed, uid, channel_info["id"])
            )
        else:
            conn2.execute(
                "INSERT INTO user_channel_usage (user_id, channel_id, total_seconds, synced_seconds) VALUES (?, ?, ?, 0)",
                (uid, channel_info["id"], elapsed)
            )
        conn2.commit()
        conn2.close()
        logger.info("用量记录: user=%s channel=%s elapsed=%.2fs cost=%spts", uid, channel_info["id"], elapsed, cost)

    task_id = str(uuid.uuid4())
    task_data = {
        "model": model, "text": prompt_text[:200],
        "status": "completed" if video_url else "processing",
        "video_url": video_url, "content": content, "owner_id": uid,
        "username": session.get("username", ""),
        "created_at": time.time(),
    }
    _task_store[task_id] = task_data
    _save_task_to_db(task_id, task_data)
    return jsonify({
        "code": 0, "task_id": task_id, "status": _task_store[task_id]["status"],
        "video_url": video_url, "content": content,
        "elapsed_seconds": elapsed,
        "points_used": cost,
        "points_remaining": points_remaining,
        "message": "生成完成" if video_url else "文本已生成",
    })


def _extract_video_url(result):
    choices = result.get("choices", [])
    if not choices: return ""
    content = choices[0].get("message", {}).get("content", "")
    if not content: return ""
    for pat in [r'https?://[^\s"\'<>]+\.(?:mp4|avi|mov|webm|m3u8)',
                r'https?://[^\s"\'<>]+video[^\s"\'<>]*',
                r'https?://[^\s"\'<>]+oss[^\s"\'<>]*']:
        m = re.search(pat, content, re.IGNORECASE)
        if m: return m.group(0)
    return ""


@app.route("/api/task/<task_id>", methods=["GET"])
def query_task(task_id):
    task = _task_store.get(task_id)
    # 内存中没有就从数据库恢复
    if not task:
        task = _load_task_from_db(task_id)
        if task:
            _task_store[task_id] = task  # 回填内存
    if not task: return jsonify({"code": 404, "message": "任务不存在"}), 404

    # 异步视频任务：实时查一下状态
    ch_name = task.get("channel_name", "")
    if ch_name and task.get("remote_task_id"):
        remote_id = task["remote_task_id"]
        db = _get_db()
        ch = db.execute("SELECT base_url, api_key FROM channels WHERE name=?", (ch_name,)).fetchone()
        db.close()
        if ch:
            ch_headers = {"Authorization": f"Bearer {ch['api_key'] or ''}"}
            query_url_path = task.get("query_url_path", "/v1/video/generations/")
            query_url = f"{ch['base_url']}{query_url_path}{remote_id}"
            try:
                qr = requests.get(query_url, headers=ch_headers, timeout=15)
                td = qr.json()
                inner = td.get("data", td)
                remote_status = inner.get("status", td.get("status", ""))
                task["baidu_status"] = remote_status
                task["baidu_progress"] = td.get("progress", inner.get("progress", 0))

                # 各种完成/失败状态
                remote_status_lower = remote_status.lower()
                done = remote_status_lower in ("completed", "success", "done", "succeeded")
                failed = remote_status_lower in ("failed", "error", "failure")
                if done:
                    vurl = inner.get("video_url", td.get("video_url", ""))
                    if not vurl and isinstance(inner, dict):
                        content_inner = inner.get("content", {})
                        if isinstance(content_inner, dict):
                            vurl = content_inner.get("video_url", "")
                        if not vurl:
                            vurl = inner.get("result", {}).get("video_url", "")
                        # 百度云把URL放在 metadata.url 中
                        if not vurl:
                            vurl = inner.get("metadata", {}).get("url", "")
                            if not vurl:
                                vurl = td.get("metadata", {}).get("url", "")
                    if not vurl:
                        vurl = inner.get("result_url", "")
                        task["result_url"] = vurl

                    # 下载到本地，避免CDN链接过期
                    local_url = vurl
                    if vurl:
                        try:
                            r = requests.get(vurl, timeout=60, stream=True,
                                headers={"Referer": "https://gogogotoken.com/",
                                         "User-Agent": "Mozilla/5.0"})
                            if r.status_code == 200:
                                local_fname = f"gen_{remote_id}.mp4"
                                local_path = os.path.join(os.path.dirname(__file__), "uploads", local_fname)
                                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                                with open(local_path, "wb") as f:
                                    for chunk in r.iter_content(8192):
                                        if chunk: f.write(chunk)
                                local_url = f"/api/video-file/{local_fname}"
                                logger.info("视频已下载到本地: %s", local_fname)
                        except Exception as de:
                            logger.warning("视频下载失败,使用远程URL: %s", de)

                    task["status"] = "completed"
                    task["video_url"] = local_url
                    _save_task_to_db(task_id, task)  # 持久化
                    # 自动加入视频库
                    vid = str(uuid.uuid4())[:8]
                    video_entry = {
                        "id": vid, "name": (task.get("text", "") or "AI生成视频")[:40],
                        "url": local_url, "is_remote": False,
                        "owner_id": task.get("owner_id", 0),
                        "created_at": datetime.now().isoformat()[:10],
                    }
                    _video_store.append(video_entry)
                    _save_video_to_db(video_entry)
                elif failed:
                    task["status"] = "failed"
                    _save_task_to_db(task_id, task)
            except:
                pass

    return jsonify({"code": 0, "task_id": task_id, "status": task["status"],
                    "video_url": task.get("video_url", ""), "content": task.get("content", ""),
                    "model": task.get("model", ""),
                    "baidu_status": task.get("baidu_status", ""),
                    "baidu_progress": task.get("baidu_progress", ""),
                    "remote_task_id": task.get("remote_task_id", ""),})


# ============================================================ #
#                       视频管理                                  #
# ============================================================ #

@app.route("/api/videos", methods=["GET"])
def list_videos():
    # 按用户隔离：主账号看自己+所有子账号，子账号看父账号+同级子账号
    uid = session.get("user_id", 0)
    conn = _get_db()
    # 找到所属"主账号组"
    user_row = conn.execute("SELECT parent_id FROM users WHERE id=?", (uid,)).fetchone()
    if user_row and user_row["parent_id"]:
        group_root = user_row["parent_id"]  # 子账号：以主账号为根
    else:
        group_root = uid  # 主账号或无父子关系：以自己为根
    # 收集组内所有用户ID
    group_users = [group_root]
    subs = conn.execute("SELECT id FROM users WHERE parent_id=?", (group_root,)).fetchall()
    group_users.extend([s["id"] for s in subs])
    conn.close()
    filtered = [v for v in _video_store if v.get("owner_id", 0) in group_users]
    return jsonify({"code": 0, "videos": filtered})


@app.route("/api/upload", methods=["POST"])
def upload_video():
    f = request.files.get("file")
    if not f: return jsonify({"code": 400, "message": "请选择文件"}), 400
    name = request.form.get("name", f.filename.rsplit(".", 1)[0])
    fname = f"{uuid.uuid4().hex}_{f.filename}"
    save_path = os.path.join(os.path.dirname(__file__), "uploads", fname)
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    f.save(save_path)

    vid = str(uuid.uuid4())[:8]
    video_entry = {
        "id": vid, "name": name, "filename": fname,
        "url": f"/api/video-file/{fname}", "owner_id": session.get("user_id", 0),
        "created_at": datetime.now().isoformat()[:10],
    }
    _video_store.append(video_entry)
    _save_video_to_db(video_entry)
    return jsonify({"code": 0, "video": _video_store[-1], "message": "上传成功"})


@app.route("/api/video-file/<fname>", methods=["GET"])
def serve_video(fname):
    return send_from_directory(os.path.join(os.path.dirname(__file__), "uploads"), fname)


@app.route("/api/proxy-video", methods=["GET"])
def proxy_video():
    """后端代理获取远程视频（绕过CDN防盗链/CORS）"""
    from flask import Response
    target_url = request.args.get("url", "").strip()
    if not target_url:
        return jsonify({"code": 400, "message": "缺少url参数"}), 400
    try:
        r = requests.get(target_url, headers={"Referer": "https://gogogotoken.com/"}, timeout=120, stream=True)
        r.raise_for_status()
        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        return Response(generate(), content_type=r.headers.get("Content-Type", "video/mp4"))
    except requests.exceptions.RequestException as e:
        return jsonify({"code": 502, "message": f"视频获取失败: {e}"}), 502


@app.route("/api/videos/save-url", methods=["POST"])
def save_video_url():
    """将生成的视频URL保存到视频库"""
    data = request.get_json(silent=True) or {}
    video_url = (data.get("video_url") or "").strip()
    name = (data.get("name") or "AI生成视频").strip()
    if not video_url:
        return jsonify({"code": 400, "message": "缺少video_url"}), 400
    vid = str(uuid.uuid4())[:8]
    video_entry = {
        "id": vid, "name": name, "filename": "",
        "url": video_url, "is_remote": True,
        "owner_id": session.get("user_id", 0),
        "created_at": datetime.now().isoformat()[:10],
    }
    _video_store.append(video_entry)
    _save_video_to_db(video_entry)
    return jsonify({"code": 0, "video": _video_store[-1], "message": "已保存"})


@app.route("/api/videos/<vid>", methods=["DELETE"])
def delete_video(vid):
    global _video_store
    _video_store = [v for v in _video_store if v["id"] != vid]
    return jsonify({"code": 0, "message": "已删除"})


# ============================================================ #
#                       静态文件                                  #
# ============================================================ #

@app.route("/<path:filename>")
def serve_static(filename):
    return send_from_directory(FRONTEND_DIR, filename)


# ============================================================ #
#                       启动入口                                  #
# ============================================================ #

def handler(environ, start_response):
    return app(environ, start_response)


# 任务持久化
def _save_task_to_db(task_id, task_data):
    try:
        conn = _get_db()
        conn.execute("""
            INSERT OR REPLACE INTO tasks (id, remote_task_id, model, prompt_text, status, video_url,
                channel_name, query_url_path, owner_id, username, baidu_status, baidu_progress, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_id,
            task_data.get("remote_task_id", ""),
            task_data.get("model", ""),
            task_data.get("text", "")[:200],
            task_data.get("status", "queued"),
            task_data.get("video_url", ""),
            task_data.get("channel_name", ""),
            task_data.get("query_url_path", ""),
            task_data.get("owner_id", 0),
            task_data.get("username", ""),
            task_data.get("baidu_status", ""),
            task_data.get("baidu_progress", 0),
            task_data.get("created_at", time.time()),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("保存任务到DB失败: %s", e)


def _load_task_from_db(task_id):
    try:
        conn = _get_db()
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.close()
        if row:
            return {
                "model": row["model"], "text": row["prompt_text"],
                "status": row["status"], "remote_task_id": row["remote_task_id"],
                "channel_name": row["channel_name"], "query_url_path": row["query_url_path"],
                "video_url": row["video_url"], "content": "", "owner_id": row["owner_id"],
                "username": row["username"], "baidu_status": row["baidu_status"],
                "baidu_progress": row["baidu_progress"], "created_at": row["created_at"],
            }
    except:
        pass
    return None


# 将视频保存到数据库
def _save_video_to_db(video):
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO videos (id, name, url, is_remote, owner_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (video["id"], video["name"], video["url"], 1 if video.get("is_remote") else 0,
         video.get("owner_id", 1), video.get("created_at", datetime.now().isoformat()[:10]))
    )
    conn.commit()
    conn.close()

# 启动时恢复视频库（从 DB + uploads 目录）
def _restore_video_store():
    global _video_store
    conn = _get_db()
    db_rows = conn.execute("SELECT * FROM videos ORDER BY created_at DESC").fetchall()
    conn.close()
    seen_urls = set()
    for r in db_rows:
        seen_urls.add(r["url"])
        _video_store.append({
            "id": r["id"], "name": r["name"], "url": r["url"],
            "is_remote": bool(r["is_remote"]), "owner_id": r["owner_id"],
            "created_at": r["created_at"][:10],
        })
    # 补充：uploads 目录中有但 DB 中没有的文件
    upload_dir = os.path.join(os.path.dirname(__file__), "uploads")
    if os.path.isdir(upload_dir):
        for fname in sorted(os.listdir(upload_dir)):
            if fname.startswith("gen_") and fname.endswith(".mp4"):
                url = f"/api/video-file/{fname}"
                if url in seen_urls:
                    continue
                vid = str(uuid.uuid4())[:8]
                video = {
                    "id": vid, "name": fname.replace("gen_", "").replace(".mp4", "")[:40],
                    "url": url, "is_remote": False, "owner_id": 1,
                    "created_at": datetime.now().isoformat()[:10],
                }
                _video_store.append(video)
                _save_video_to_db(video)

_restore_video_store()


if __name__ == "__main__":
    port = int(os.environ.get("PORT") or os.environ.get("FC_SERVER_PORT", 9000))
    logger.info("服务启动, 端口 %s, 静态目录 %s", port, FRONTEND_DIR)
    serve(app, host="0.0.0.0", port=port)
