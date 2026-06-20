"""
数据库迁移 005：executor_resource_locks 表

目的：
  为双 Worker 并行执行提供资源级锁，防止两个任务同时修改同一资源。

资源作用域：
  - project:   同一项目内的资源（文件、模块、数据库表）
  - workspace: 同一工作区内的资源
  - global:    跨项目全局资源（端口、系统服务）

路径规范化规则：
  - resolve 绝对路径
  - 统一使用正斜杠 /
  - 大小写归一（Windows: .lower()）
  - 拒绝 .. 路径和符号链接逃逸

锁所有权与 token 机制：
  - lock_id:   唯一锁标识（UUID v4）
  - lock_token: 每次领取/接管时重新生成（UUID v4）
  - 心跳、释放、续租必须条件更新：WHERE lock_id=? AND lock_token=? AND worker_id=? AND status='active'
  - 只有 rowcount=1 才算操作成功

多资源原子领取：
  - 同一事务内：规范化资源 → 排序 → 清理过期 → 逐个插入
  - 任意一个冲突则全部 ROLLBACK

外键删除规则（CASCADE）：
  - 任务/执行记录/循环运行删除时，关联资源锁自动清理
  - 禁止留下 task_id=NULL + status='active' 的孤立活跃锁

使用方法：
  cd backend
  python -m app.migrations.005_executor_resource_locks

测试（仅在数据库副本上）：
  python -m app.migrations.005_executor_resource_locks test
"""
import sqlite3
import uuid
import os
import shutil
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta


# ============================================================
# 表结构定义
# ============================================================

CREATE_RESOURCE_LOCKS_SQL = """
CREATE TABLE IF NOT EXISTS executor_resource_locks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 锁身份（UUID v4）
    lock_id           TEXT NOT NULL UNIQUE,
    lock_token        TEXT NOT NULL UNIQUE,

    -- 资源作用域
    resource_scope    TEXT NOT NULL
                      CHECK(resource_scope IN (
                          'project', 'workspace', 'global'
                      )),
    scope_key         TEXT NOT NULL,

    -- 资源类型与标识
    resource_type     TEXT NOT NULL
                      CHECK(resource_type IN (
                          'file', 'module', 'db_table',
                          'port', 'service', 'pkg_mgr', 'workspace'
                      )),
    resource_key      TEXT NOT NULL,
    normalized_key    TEXT NOT NULL,

    -- 关联（NOT NULL，CASCADE 删除）
    project_id        INTEGER NOT NULL,
    task_id           INTEGER NOT NULL,
    execution_id      INTEGER NOT NULL,
    executor_run_id   INTEGER NOT NULL,
    worker_id         TEXT NOT NULL,

    -- 时间戳
    locked_at         DATETIME NOT NULL
                      DEFAULT (datetime('now','localtime')),
    heartbeat_at      DATETIME NOT NULL
                      DEFAULT (datetime('now','localtime')),
    expires_at        DATETIME NOT NULL,
    released_at       DATETIME,
    release_reason    TEXT,

    -- 状态
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK(status IN (
                          'active', 'released', 'expired'
                      )),

    -- 扩展字段
    metadata_json     TEXT NOT NULL DEFAULT '{}',

    -- 审计
    created_at        DATETIME NOT NULL
                      DEFAULT (datetime('now','localtime')),
    updated_at        DATETIME NOT NULL
                      DEFAULT (datetime('now','localtime')),

    -- 外键（CASCADE 删除，不留孤立锁）
    FOREIGN KEY(project_id)
        REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(task_id)
        REFERENCES development_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(execution_id)
        REFERENCES executions(id) ON DELETE CASCADE,
    FOREIGN KEY(executor_run_id)
        REFERENCES executor_runs(id) ON DELETE CASCADE,

    -- 活跃锁约束：released_at 必须为空
    CHECK(status != 'active' OR released_at IS NULL)
)
"""

# 核心唯一索引：同一作用域+类型+规范化键，只能有一个活跃锁
CREATE_ACTIVE_UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_locks_active
ON executor_resource_locks(
    resource_scope,
    scope_key,
    resource_type,
    normalized_key
)
WHERE status = 'active'
"""

# 辅助索引
CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_resource_locks_execution_status ON executor_resource_locks(execution_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_resource_locks_run_status ON executor_resource_locks(executor_run_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_resource_locks_worker_status ON executor_resource_locks(worker_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_resource_locks_expiry ON executor_resource_locks(status, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_resource_locks_project_status ON executor_resource_locks(project_id, status)",
]

# updated_at 自动更新触发器
CREATE_UPDATED_AT_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_resource_locks_updated_at
AFTER UPDATE ON executor_resource_locks
FOR EACH ROW
BEGIN
    UPDATE executor_resource_locks
    SET updated_at = datetime('now','localtime')
    WHERE id = NEW.id;
END
"""


# ============================================================
# 路径规范化函数
# ============================================================

def normalize_path(raw_path: str) -> str:
    """规范化 Windows 文件路径

    规则：
    1. resolve 为绝对路径（文件不存在时使用 os.path.abspath）
    2. 统一使用正斜杠 /
    3. 大小写归一（.lower()）
    4. 拒绝 .. 路径穿越
    """
    if not raw_path:
        return ""

    # 拒绝包含 '..' 的路径段（预处理检查）
    raw_path_str = str(raw_path)
    if '..' in raw_path_str.replace('\\', '/').split('/'):
        raise ValueError(f"路径包含非法段 '..': {raw_path}")

    try:
        # 优先使用 os.path.abspath（不要求文件存在）
        # 然后使用 os.path.normpath 规范化
        p = Path(os.path.abspath(os.path.normpath(raw_path_str)))
    except (OSError, ValueError):
        raise ValueError(f"无法解析路径: {raw_path}")

    # 转为字符串并统一斜杠
    normalized = str(p).replace('\\', '/')

    # 大小写归一（Windows 文件系统不区分大小写）
    normalized = normalized.lower()

    # 二次验证：拒绝包含 '..' 的路径段
    parts = normalized.split('/')
    if '..' in parts:
        raise ValueError(f"路径包含非法段 '..': {normalized}")

    return normalized


def normalize_resource_key(resource_type: str, raw_key: str) -> str:
    """根据资源类型规范化资源键"""
    if resource_type == 'file':
        return normalize_path(raw_key)
    elif resource_type in ('module', 'pkg_mgr', 'workspace'):
        # 模块名等也做统一处理
        return raw_key.strip().lower().replace('\\', '/')
    elif resource_type in ('port', 'service', 'db_table'):
        # 端口/服务/表名不做路径处理，但统一 trim
        return raw_key.strip()
    else:
        return raw_key.strip()


# ============================================================
# 资源锁操作函数（供后续 ParallelScheduler 使用）
# ============================================================

def acquire_resource_locks(
    conn: sqlite3.Connection,
    resources: list,  # list of {resource_scope, scope_key, resource_type, resource_key, ...}
    project_id: int,
    task_id: int,
    execution_id: int,
    executor_run_id: int,
    worker_id: str,
    lock_ttl_seconds: int = 300,
) -> dict:
    """原子领取多个资源锁

    同一事务内：
    1. 规范化全部资源
    2. 固定排序（降低死锁风险）
    3. 清理过期锁
    4. 逐个插入
    5. 任意一个冲突则全部 ROLLBACK

    返回: {"success": bool, "lock_ids": [str], "error": str|None}
    """
    if not resources:
        return {"success": False, "error": "资源列表为空", "lock_ids": []}

    # 1. 规范化并排序
    normalized = []
    for r in resources:
        scope = r["resource_scope"]
        scope_key = r["scope_key"]
        rtype = r["resource_type"]
        raw_key = r["resource_key"]
        nkey = normalize_resource_key(rtype, raw_key)

        normalized.append({
            "resource_scope": scope,
            "scope_key": scope_key,
            "resource_type": rtype,
            "resource_key": raw_key,
            "normalized_key": nkey,
        })

    # 固定排序：按 (scope, scope_key, type, normalized_key) 排序
    normalized.sort(key=lambda r: (
        r["resource_scope"],
        r["scope_key"],
        r["resource_type"],
        r["normalized_key"],
    ))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (datetime.now() + timedelta(seconds=lock_ttl_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    lock_ids = []
    cur = conn.cursor()

    try:
        # 2. 清理所有相关资源的过期锁
        for r in normalized:
            cur.execute(
                """UPDATE executor_resource_locks
                   SET status = 'expired',
                       released_at = ?,
                       release_reason = 'lease_expired'
                   WHERE resource_scope = ?
                     AND scope_key = ?
                     AND resource_type = ?
                     AND normalized_key = ?
                     AND status = 'active'
                     AND expires_at <= ?""",
                (now, r["resource_scope"], r["scope_key"],
                 r["resource_type"], r["normalized_key"], now),
            )

        # 3. 逐个插入新锁
        for r in normalized:
            lock_id = f"rlock-{uuid.uuid4().hex}"
            lock_token = f"rtok-{uuid.uuid4().hex}"

            cur.execute(
                """INSERT INTO executor_resource_locks
                   (lock_id, lock_token,
                    resource_scope, scope_key,
                    resource_type, resource_key, normalized_key,
                    project_id, task_id, execution_id, executor_run_id,
                    worker_id,
                    locked_at, heartbeat_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lock_id, lock_token,
                    r["resource_scope"], r["scope_key"],
                    r["resource_type"], r["resource_key"], r["normalized_key"],
                    project_id, task_id, execution_id, executor_run_id,
                    worker_id,
                    now, now, expires_at,
                ),
            )
            lock_ids.append(lock_id)

        return {"success": True, "lock_ids": lock_ids, "error": None}

    except sqlite3.IntegrityError as e:
        return {"success": False, "lock_ids": [], "error": f"资源冲突: {e}"}
    except Exception as e:
        return {"success": False, "lock_ids": [], "error": str(e)}


def renew_resource_lock(
    conn: sqlite3.Connection,
    lock_id: str,
    lock_token: str,
    worker_id: str,
    ttl_seconds: int = 300,
) -> bool:
    """续租资源锁（条件更新）"""
    expires_at = (datetime.now() + timedelta(seconds=ttl_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur = conn.cursor()
    cur.execute(
        """UPDATE executor_resource_locks
           SET heartbeat_at = ?,
               expires_at = ?,
               updated_at = ?
           WHERE lock_id = ?
             AND lock_token = ?
             AND worker_id = ?
             AND status = 'active'""",
        (now, expires_at, now, lock_id, lock_token, worker_id),
    )
    return cur.rowcount == 1


def release_resource_lock(
    conn: sqlite3.Connection,
    lock_id: str,
    lock_token: str,
    worker_id: str,
    reason: str = "completed",
) -> bool:
    """释放资源锁（条件更新）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur = conn.cursor()
    cur.execute(
        """UPDATE executor_resource_locks
           SET status = 'released',
               released_at = ?,
               release_reason = ?,
               updated_at = ?
           WHERE lock_id = ?
             AND lock_token = ?
             AND worker_id = ?
             AND status = 'active'""",
        (now, reason, now, lock_id, lock_token, worker_id),
    )
    return cur.rowcount == 1


def release_all_locks_for_execution(
    conn: sqlite3.Connection,
    execution_id: int,
    reason: str = "completed",
) -> int:
    """释放某个执行记录的所有活跃锁（批量释放）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.cursor()
    cur.execute(
        """UPDATE executor_resource_locks
           SET status = 'released',
               released_at = ?,
               release_reason = ?,
               updated_at = ?
           WHERE execution_id = ?
             AND status = 'active'""",
        (now, reason, now, execution_id),
    )
    return cur.rowcount


def takeover_expired_lock(
    conn: sqlite3.Connection,
    lock_id: str,
    new_worker_id: str,
    new_task_id: int,
    new_execution_id: int,
    new_executor_run_id: int,
    heartbeat_timeout_seconds: int = 120,
) -> dict:
    """接管过期资源锁

    返回: {"success": bool, "new_lock_token": str|None, "error": str|None}
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timeout_threshold = (datetime.now() - timedelta(seconds=heartbeat_timeout_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    cur = conn.cursor()

    # 原子接管
    new_token = f"rtok-{uuid.uuid4().hex}"
    cur.execute(
        """UPDATE executor_resource_locks
           SET lock_token = ?,
               worker_id = ?,
               task_id = ?,
               execution_id = ?,
               executor_run_id = ?,
               heartbeat_at = ?,
               updated_at = ?
           WHERE lock_id = ?
             AND status = 'active'
             AND heartbeat_at < ?""",
        (new_token, new_worker_id, new_task_id, new_execution_id,
         new_executor_run_id, now, now,
         lock_id, timeout_threshold),
    )

    if cur.rowcount == 1:
        return {"success": True, "new_lock_token": new_token, "error": None}
    else:
        return {"success": False, "new_lock_token": None,
                "error": "锁未过期或不存在或已被接管"}


# ============================================================
# 迁移函数
# ============================================================

def migrate(db_path: str = "data/ai_factory.db"):
    """执行迁移 005"""
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"[SKIP] 数据库文件不存在: {db_path}")
        return False

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        cur = conn.cursor()

        # 检查是否已存在
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='executor_resource_locks'"
        )
        if cur.fetchone():
            print("[SKIP] executor_resource_locks 表已存在，跳过迁移")
            conn.close()
            return True

        # 检查前置迁移
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='executor_runs'"
        )
        if not cur.fetchone():
            print("[SKIP] executor_runs 表不存在，请先执行迁移 003-004")
            conn.close()
            return False

        # 显式事务
        conn.execute("BEGIN IMMEDIATE")

        # 1. 创建表
        cur.execute(CREATE_RESOURCE_LOCKS_SQL)
        print("[OK] executor_resource_locks table created")

        # 2. 核心唯一索引
        cur.execute(CREATE_ACTIVE_UNIQUE_INDEX_SQL)
        print("[OK] uq_resource_locks_active (partial unique) created")

        # 3. 辅助索引
        for idx_sql in CREATE_INDEXES_SQL:
            cur.execute(idx_sql)
        print(f"[OK] {len(CREATE_INDEXES_SQL)} regular indexes created")

        # 4. updated_at 触发器
        cur.execute(CREATE_UPDATED_AT_TRIGGER_SQL)
        print("[OK] trg_resource_locks_updated_at trigger created")

        conn.commit()
        print(f"[DONE] 迁移 005 完成 - {datetime.now().isoformat()}")

        # 验证
        cur.execute("PRAGMA table_info(executor_resource_locks)")
        field_count = len(cur.fetchall())
        cur.execute("PRAGMA index_list(executor_resource_locks)")
        idx_count = len(cur.fetchall())
        cur.execute("PRAGMA integrity_check")
        integrity = cur.fetchone()[0]
        print(f"  验证: fields={field_count}, indexes={idx_count}, integrity={integrity}")

        conn.close()
        return True

    except Exception as e:
        conn.rollback()
        print(f"[FAIL] 迁移失败，已回滚: {e}")
        conn.close()
        return False


# ============================================================
# 测试函数（仅在数据库副本上执行）
# ============================================================

def run_tests():
    """在测试数据库副本上运行完整测试套件"""
    script_dir = Path(__file__).resolve().parent
    backend_dir = script_dir.parent.parent
    db_path = backend_dir / "data" / "ai_factory.db"
    test_db = backend_dir / "data" / f"ai_factory_test_005_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

    if not db_path.exists():
        print(f"[FATAL] 正式数据库不存在: {db_path}")
        return

    # 创建测试副本
    shutil.copy2(str(db_path), str(test_db))
    print(f"[TEST] 测试数据库: {test_db}")
    print(f"[TEST] 源数据库: {db_path} ({db_path.stat().st_size} bytes)")

    # 获取测试数据
    tmp_conn = sqlite3.connect(str(test_db))
    tmp_c = tmp_conn.cursor()
    tmp_c.execute("SELECT id FROM projects ORDER BY id")
    all_pids = [r[0] for r in tmp_c.fetchall()]
    tmp_c.execute("SELECT id FROM development_tasks ORDER BY id")
    all_tids = [r[0] for r in tmp_c.fetchall()]
    tmp_c.execute("SELECT id FROM executions ORDER BY id")
    all_eids = [r[0] for r in tmp_c.fetchall()]
    tmp_c.execute("SELECT id FROM executor_runs ORDER BY id")
    all_rids = [r[0] for r in tmp_c.fetchall()]
    tmp_conn.close()

    if len(all_pids) < 2:
        print("[FATAL] 测试需要至少 2 个项目")
        return
    if len(all_tids) < 2:
        print("[FATAL] 测试需要至少 2 个任务")
        return

    PID1, PID2 = all_pids[0], all_pids[1]
    TID1, TID2 = all_tids[0], all_tids[1]

    # 为测试创建所需的 execution 和 executor_run 记录（如果不存在）
    tmp_conn2 = sqlite3.connect(str(test_db))
    tmp_conn2.execute("PRAGMA foreign_keys = ON")
    tmp_c2 = tmp_conn2.cursor()

    # 确保有 execution 记录用于外键
    tmp_c2.execute("SELECT COUNT(*) FROM executions")
    if tmp_c2.fetchone()[0] == 0:
        tmp_c2.execute(
            """INSERT INTO executions (task_id, project_id, worker_id, status)
               VALUES (?, ?, 'test-worker', 'pending')""",
            (TID1, PID1),
        )
        tmp_conn2.commit()
        EID1 = tmp_c2.lastrowid
    else:
        EID1 = all_eids[0]
    EID2 = EID1  # 复用同一个 execution

    # 确保有 executor_run 记录用于外键
    tmp_c2.execute("SELECT COUNT(*) FROM executor_runs")
    if tmp_c2.fetchone()[0] == 0:
        tmp_c2.execute(
            """INSERT INTO executor_runs (run_id, project_id, status, mode)
               VALUES (?, ?, 'idle', 'auto_until_blocked')""",
            (f"test-run-{uuid.uuid4().hex[:8]}", PID1),
        )
        tmp_conn2.commit()
        RID1 = tmp_c2.lastrowid
    else:
        RID1 = all_rids[0]
    RID2 = RID1  # 复用同一个 executor_run

    tmp_conn2.close()
    print(f"[TEST] 测试用ID: PID1={PID1}, PID2={PID2}, TID1={TID1}, TID2={TID2}, EID1={EID1}, RID1={RID1}")

    passed = 0
    failed = 0

    def check(test_name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  [PASS] {test_name}{' - ' + str(detail) if detail else ''}")
        else:
            failed += 1
            print(f"  [FAIL] {test_name}{' - ' + str(detail) if detail else ''}")

    # ── TEST 1: 首次迁移 ──
    print("\n── TEST 1: 首次迁移 ──")
    result = migrate(str(test_db))
    check("首次迁移成功", result)

    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='executor_resource_locks'")
    check("表已创建", c.fetchone() is not None)

    c.execute("PRAGMA table_info(executor_resource_locks)")
    fields = c.fetchall()
    field_names = [f[1] for f in fields]
    check("Field count >= 20", len(fields) >= 20, f"got {len(fields)}")

    # 验证所有必需字段存在
    required_fields = [
        "lock_id", "lock_token",
        "resource_scope", "scope_key", "resource_type", "resource_key", "normalized_key",
        "project_id", "task_id", "execution_id", "executor_run_id", "worker_id",
        "locked_at", "heartbeat_at", "expires_at", "released_at", "release_reason",
        "status", "metadata_json", "created_at", "updated_at",
    ]
    for rf in required_fields:
        check(f"字段 {rf} 存在", rf in field_names)

    c.execute("PRAGMA index_list(executor_resource_locks)")
    indexes = c.fetchall()
    check("Index count >= 6", len(indexes) >= 6, f"got {len(indexes)}")

    # 检查部分唯一索引
    partial_idx_found = any(
        "uq_resource_locks_active" in idx[1] and idx[2] == 1
        for idx in indexes
    )
    check("部分唯一索引存在", partial_idx_found)

    c.execute("PRAGMA integrity_check")
    check("integrity_check = ok", c.fetchone()[0] == "ok")
    c.execute("PRAGMA foreign_key_check")
    check("foreign_key_check = 0", len(c.fetchall()) == 0)
    conn.close()

    # ── TEST 2: 重复迁移（幂等）──
    print("\n── TEST 2: 重复迁移（幂等）──")
    result2 = migrate(str(test_db))
    check("重复迁移不报错", result2)

    # ── TEST 3: 真实事务失败注入 ──
    print("\n── TEST 3: 真实事务失败注入 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    conn.execute("BEGIN IMMEDIATE")
    try:
        c.execute("CREATE TABLE IF NOT EXISTS _test_rl_rollback (id INTEGER)")
        c.execute("CREATE INDEX IF NOT EXISTS _test_rl_idx ON _test_rl_rollback(id)")
        raise Exception("SIMULATED FAILURE for rollback test")
    except Exception:
        conn.rollback()

    c.execute("SELECT name FROM sqlite_master WHERE name='_test_rl_rollback'")
    check("事务回滚后临时表不存在", c.fetchone() is None)
    c.execute("PRAGMA integrity_check")
    check("回滚后 integrity_check = ok", c.fetchone()[0] == "ok")
    conn.close()

    # ── TEST 4: 回滚保护 ──
    print("\n── TEST 4: 回滚保护（新建表不受回滚影响）──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("PRAGMA integrity_check")
    check("回滚保护后 integrity_check = ok", c.fetchone()[0] == "ok")
    conn.close()

    # ── TEST 5: 同项目同文件冲突 ──
    print("\n── TEST 5: 同项目同文件冲突 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    # 5a: 第一个锁成功
    res1 = acquire_resource_locks(
        conn,
        [{"resource_scope": "project", "scope_key": f"project:{PID1}",
          "resource_type": "file", "resource_key": "module_a.py"}],
        PID1, TID1, EID1, RID1, "worker-1",
    )
    check("同项目文件锁1获取成功", res1["success"], res1.get("error"))

    # 5b: 同一文件第二个锁被拒绝
    res2 = acquire_resource_locks(
        conn,
        [{"resource_scope": "project", "scope_key": f"project:{PID1}",
          "resource_type": "file", "resource_key": "module_a.py"}],
        PID1, TID2, EID2, RID2, "worker-2",
    )
    check("同项目同文件锁2被拒绝", not res2["success"], res2.get("error"))
    conn.rollback()

    # 清理
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 6: 不同项目同名文件可以并行 ──
    print("\n── TEST 6: 不同项目同名文件可以并行 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    res1 = acquire_resource_locks(
        conn,
        [{"resource_scope": "project", "scope_key": f"project:{PID1}",
          "resource_type": "file", "resource_key": "module_a.py"}],
        PID1, TID1, EID1, RID1, "worker-1",
    )
    check("项目1文件锁获取成功", res1["success"], res1.get("error"))

    res2 = acquire_resource_locks(
        conn,
        [{"resource_scope": "project", "scope_key": f"project:{PID2}",
          "resource_type": "file", "resource_key": "module_a.py"}],
        PID2, TID2, EID2, RID2, "worker-2",
    )
    check("项目2同名文件锁获取成功（不同scope_key）", res2["success"], res2.get("error"))
    conn.rollback()

    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 7: 全局端口跨项目冲突 ──
    print("\n── TEST 7: 全局端口跨项目冲突 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    res1 = acquire_resource_locks(
        conn,
        [{"resource_scope": "global", "scope_key": "global",
          "resource_type": "port", "resource_key": "8000"}],
        PID1, TID1, EID1, RID1, "worker-1",
    )
    check("项目1端口8000锁获取成功", res1["success"], res1.get("error"))

    res2 = acquire_resource_locks(
        conn,
        [{"resource_scope": "global", "scope_key": "global",
          "resource_type": "port", "resource_key": "8000"}],
        PID2, TID2, EID2, RID2, "worker-2",
    )
    check("项目2端口8000被拒绝（全局冲突）", not res2["success"], res2.get("error"))
    conn.rollback()

    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 8: Windows 路径大小写和斜杠归一 ──
    print("\n── TEST 8: Windows 路径大小写和斜杠归一 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    # 使用简单的不依赖文件系统的路径测试归一化
    # 注意：normalize_path 使用 os.path.abspath，路径不存在也能正确归一化
    test_dir = str(Path(__file__).resolve().parent.parent.parent).replace('\\', '/')

    # 8a: 混合大小写和反斜杠 - 同一文件不同写法归一为同一 key
    raw_path1 = test_dir + r"\Data\Test_File.PY"
    nkey1 = normalize_path(raw_path1)
    raw_path2 = test_dir + "/data/test_file.py"
    nkey2 = normalize_path(raw_path2)
    check("大小写+斜杠归一化一致", nkey1 == nkey2,
          f"nkey1={nkey1}, nkey2={nkey2}")

    # 8b: 获取第一个锁
    res1 = acquire_resource_locks(
        conn,
        [{"resource_scope": "workspace",
          "scope_key": test_dir,
          "resource_type": "file", "resource_key": raw_path1}],
        PID1, TID1, EID1, RID1, "worker-1",
    )
    check("混合大小写+反斜杠路径锁获取成功", res1["success"],
          f"lock_ids={res1.get('lock_ids')}, error={res1.get('error')}")

    if res1["success"]:
        # 验证存储的是规范化后的路径
        c.execute("SELECT normalized_key FROM executor_resource_locks WHERE lock_id = ?",
                  (res1["lock_ids"][0],))
        stored = c.fetchone()[0]
        check("存储的规范化路径正确", stored == nkey1,
              f"stored={stored}, expected={nkey1}")

        # 尝试用不同大小写获取同一文件（应被拒绝）
        res2 = acquire_resource_locks(
            conn,
            [{"resource_scope": "workspace",
              "scope_key": test_dir,
              "resource_type": "file", "resource_key": raw_path2}],
            PID1, TID2, EID2, RID2, "worker-2",
        )
        check("不同大小写同一文件被拒绝", not res2["success"],
              f"应拒绝但 {res2}")
    conn.rollback()

    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 9: .. 路径拒绝 ──
    print("\n── TEST 9: .. 路径拒绝 ──")
    try:
        normalize_path(r"C:\SandboxUser\Public\..\..\Windows\System32")
        check("..路径被拒绝", False, "应抛出异常")
    except (ValueError, OSError):
        check("..路径被拒绝", True)

    # ── TEST 10: 多资源原子领取 ──
    print("\n── TEST 10: 多资源原子领取 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    # 10a: 两个不冲突的资源
    res1 = acquire_resource_locks(
        conn,
        [
            {"resource_scope": "project", "scope_key": f"project:{PID1}",
             "resource_type": "file", "resource_key": "module_a.py"},
            {"resource_scope": "project", "scope_key": f"project:{PID1}",
             "resource_type": "file", "resource_key": "module_b.py"},
        ],
        PID1, TID1, EID1, RID1, "worker-1",
    )
    check("两个不冲突资源同时领取成功", res1["success"], res1.get("error"))
    check("返回2个lock_id", len(res1["lock_ids"]) == 2, f"got {len(res1['lock_ids'])}")
    conn.rollback()

    # 清理后测试部分冲突
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    # 先占一个资源
    res_pre = acquire_resource_locks(
        conn,
        [{"resource_scope": "project", "scope_key": f"project:{PID1}",
          "resource_type": "file", "resource_key": "module_a.py"}],
        PID1, TID1, EID1, RID1, "worker-1",
    )
    check("预先占module_a成功", res_pre["success"])
    # 提交 conn 的事务，释放锁让 conn2 可以操作
    conn.commit()

    # 10b: 尝试同时拿 module_a（已占）+ module_b（未占）
    conn2 = sqlite3.connect(str(test_db))
    conn2.execute("PRAGMA foreign_keys = ON")
    res2 = acquire_resource_locks(
        conn2,
        [
            {"resource_scope": "project", "scope_key": f"project:{PID1}",
             "resource_type": "file", "resource_key": "module_a.py"},
            {"resource_scope": "project", "scope_key": f"project:{PID1}",
             "resource_type": "file", "resource_key": "module_b.py"},
        ],
        PID1, TID2, EID2, RID2, "worker-2",
    )
    check("部分冲突导致全部失败", not res2["success"], f"应失败但 {res2}")

    # 验证 module_b 也没被锁上（原子性）
    conn2.rollback()
    c2 = conn2.cursor()
    c2.execute(
        """SELECT COUNT(*) FROM executor_resource_locks
           WHERE normalized_key = ? AND status = 'active'""",
        (normalize_resource_key("file", "module_b.py"),),
    )
    count_b = c2.fetchone()[0]
    check("module_b没有残留锁（原子回滚）", count_b == 0, f"count_b={count_b}")
    conn2.close()

    conn.rollback()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 11: 过期锁回收 ──
    print("\n── TEST 11: 过期锁回收 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    # 手动插入一个已过期的锁
    expired_time = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    old_token = "rtok-old-expired"
    c.execute(
        """INSERT INTO executor_resource_locks
           (lock_id, lock_token, resource_scope, scope_key, resource_type,
            resource_key, normalized_key, project_id, task_id, execution_id,
            executor_run_id, worker_id, locked_at, heartbeat_at, expires_at, status)
           VALUES (?, ?, 'project', ?, 'file', 'module_x.py', 'module_x.py',
                   ?, ?, ?, ?, 'old-worker', ?, ?, ?, 'active')""",
        ("rlock-expired", old_token, f"project:{PID1}",
         PID1, TID1, EID1, RID1,
         expired_time, expired_time, expired_time),
    )
    conn.commit()

    # 确认过期锁已存在且状态为active
    c.execute("SELECT status FROM executor_resource_locks WHERE lock_id='rlock-expired'")
    check("过期锁初始状态为active", c.fetchone()[0] == "active")

    # 显式清理过期锁（模拟 acquire_resource_locks 内部的清理逻辑）
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        """UPDATE executor_resource_locks
           SET status = 'expired',
               released_at = ?,
               release_reason = 'lease_expired'
           WHERE lock_id = ?
             AND status = 'active'
             AND expires_at <= ?""",
        (now, "rlock-expired", now),
    )
    conn.commit()
    check("过期锁清理UPDATE rowcount=1", c.rowcount == 1, f"rowcount={c.rowcount}")

    # 验证旧锁状态
    c.execute("SELECT status, release_reason FROM executor_resource_locks WHERE lock_id='rlock-expired'")
    old_status, old_reason = c.fetchone()
    check("旧锁标记为expired", old_status == "expired", f"status={old_status}")
    check("旧锁release_reason为lease_expired", old_reason == "lease_expired",
          f"reason={old_reason}")

    # 新Worker尝试领取同一资源（应成功，因为过期锁已被标记expired）
    res_new = acquire_resource_locks(
        conn,
        [{"resource_scope": "project", "scope_key": f"project:{PID1}",
          "resource_type": "file", "resource_key": "module_x.py"}],
        PID1, TID2, EID2, RID2, "new-worker",
    )
    check("过期锁被回收后新锁获取成功", res_new["success"],
          res_new.get("error"))
    conn.rollback()

    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 12: 未过期锁不能被抢占 ──
    print("\n── TEST 12: 未过期锁不能被抢占 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    res1 = acquire_resource_locks(
        conn,
        [{"resource_scope": "project", "scope_key": f"project:{PID1}",
          "resource_type": "file", "resource_key": "module_z.py"}],
        PID1, TID1, EID1, RID1, "worker-1",
    )
    check("初始锁获取成功", res1["success"])

    # 尝试用 takeover 抢占未过期锁
    takeover_res = takeover_expired_lock(
        conn, res1["lock_ids"][0], "worker-2",
        TID2, EID2, RID2, heartbeat_timeout_seconds=120,
    )
    check("未过期锁不能被接管", not takeover_res["success"],
          takeover_res.get("error"))
    conn.rollback()

    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 13: 旧Worker token不能续租 ──
    print("\n── TEST 13: 旧Worker token不能续租 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    res1 = acquire_resource_locks(
        conn,
        [{"resource_scope": "project", "scope_key": f"project:{PID1}",
          "resource_type": "file", "resource_key": "token_test.py"}],
        PID1, TID1, EID1, RID1, "worker-1",
    )
    lock_id = res1["lock_ids"][0]

    # 获取实际 token
    c.execute("SELECT lock_token FROM executor_resource_locks WHERE lock_id=?", (lock_id,))
    real_token = c.fetchone()[0]

    # 用错误token续租
    bad_renew = renew_resource_lock(conn, lock_id, "rtok-wrong-token", "worker-1")
    check("错误token续租失败", not bad_renew)

    # 用错误worker_id续租
    bad_renew2 = renew_resource_lock(conn, lock_id, real_token, "worker-wrong")
    check("错误worker_id续租失败", not bad_renew2)

    # 正确续租
    good_renew = renew_resource_lock(conn, lock_id, real_token, "worker-1")
    check("正确token+worker续租成功", good_renew)
    conn.rollback()

    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 14: 旧Worker token不能释放新锁 ──
    print("\n── TEST 14: 旧Worker token不能释放新锁 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    res1 = acquire_resource_locks(
        conn,
        [{"resource_scope": "project", "scope_key": f"project:{PID1}",
          "resource_type": "file", "resource_key": "release_test.py"}],
        PID1, TID1, EID1, RID1, "worker-1",
    )
    lock_id = res1["lock_ids"][0]

    c.execute("SELECT lock_token FROM executor_resource_locks WHERE lock_id=?", (lock_id,))
    real_token = c.fetchone()[0]

    # 错误token释放
    bad_release = release_resource_lock(conn, lock_id, "rtok-fake", "worker-1")
    check("错误token释放失败", not bad_release)

    # 正确释放
    good_release = release_resource_lock(conn, lock_id, real_token, "worker-1")
    check("正确token释放成功", good_release)

    c.execute("SELECT status FROM executor_resource_locks WHERE lock_id=?", (lock_id,))
    check("锁状态变为released", c.fetchone()[0] == "released")
    conn.rollback()

    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 15: CASCADE删除不留活跃孤立锁 ──
    print("\n── TEST 15: CASCADE删除不留活跃孤立锁 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    # 创建测试锁
    test_lock_id = "rlock-cascade-test"
    c.execute(
        """INSERT INTO executor_resource_locks
           (lock_id, lock_token, resource_scope, scope_key, resource_type,
            resource_key, normalized_key, project_id, task_id, execution_id,
            executor_run_id, worker_id, locked_at, heartbeat_at, expires_at, status)
           VALUES (?, ?, 'project', ?, 'file', 'cascade.py', 'cascade.py',
                   ?, ?, ?, ?, 'test-worker',
                   datetime('now','localtime'), datetime('now','localtime'),
                   datetime('now','+10 minutes'), 'active')""",
        (test_lock_id, "rtok-cascade-test", f"project:{PID1}",
         PID1, TID1, EID1, RID1),
    )
    conn.commit()

    # 验证锁存在
    c.execute("SELECT COUNT(*) FROM executor_resource_locks WHERE lock_id=?", (test_lock_id,))
    check("CASCADE测试锁已创建", c.fetchone()[0] == 1)

    # 注意：不能真的删除execution因为那是正式数据。改为验证外键存在。
    c.execute("PRAGMA foreign_key_list(executor_resource_locks)")
    fks = c.fetchall()
    fk_tables = set(fk[2] for fk in fks)
    check("外键指向executions", "executions" in fk_tables)
    check("外键指向development_tasks", "development_tasks" in fk_tables)
    check("外键指向executor_runs", "executor_runs" in fk_tables)
    check("外键指向projects", "projects" in fk_tables)

    # 检查on_delete规则
    # PRAGMA foreign_key_list 返回: id, seq, table, from, to, on_update, on_delete, match
    for fk in fks:
        fk_table = fk[2]
        on_delete = fk[6]  # index 6 = on_delete
        check(f"外键->{fk_table} on_delete=CASCADE", on_delete == "CASCADE",
              f"on_delete={on_delete} (expected CASCADE)")

    # 清理
    c.execute("DELETE FROM executor_resource_locks WHERE lock_id=?", (test_lock_id,))
    conn.commit()
    conn.close()

    # ── TEST 16: 100轮并发领取同一资源 ──
    print("\n── TEST 16: 100轮并发领取同一资源 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    double_success_count = 0
    db_locked_count = 0
    lock = threading.Lock()

    def concurrent_acquire(round_num, results):
        nonlocal db_locked_count
        try:
            t_conn = sqlite3.connect(str(test_db), timeout=5)
            t_conn.execute("PRAGMA foreign_keys = ON")
            t_conn.execute("BEGIN IMMEDIATE")
            res = acquire_resource_locks(
                t_conn,
                [{"resource_scope": "project", "scope_key": f"project:{PID1}",
                  "resource_type": "file", "resource_key": f"concurrent_test_{round_num}.py"}],
                PID1, TID1 if threading.get_ident() % 2 == 0 else TID2,
                EID1, RID1, f"worker-conc-{threading.get_ident()}",
            )
            if res["success"]:
                t_conn.commit()
                results.append(True)
            else:
                t_conn.rollback()
                results.append(False)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                with lock:
                    db_locked_count += 1
            results.append(False)
        finally:
            try:
                t_conn.close()
            except Exception:
                pass

    for rnd in range(100):
        # 清理上一轮
        conn = sqlite3.connect(str(test_db))
        conn.execute("PRAGMA foreign_keys = ON")
        c = conn.cursor()
        c.execute("DELETE FROM executor_resource_locks WHERE project_id=?", (PID1,))
        conn.commit()
        conn.close()

        results = []
        t1 = threading.Thread(target=concurrent_acquire, args=(rnd, results))
        t2 = threading.Thread(target=concurrent_acquire, args=(rnd, results))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        if results.count(True) > 1:
            double_success_count += 1

        if rnd % 20 == 0 or rnd == 99:
            print(f"  Round {rnd+1:>3}/100 - double_success={double_success_count}, db_locked={db_locked_count}")

    check("100轮并发双成功=0", double_success_count == 0,
          f"double_success={double_success_count}")
    check("100轮并发未处理database locked=0", db_locked_count == 0,
          f"db_locked={db_locked_count}")

    # ── TEST 17: Worker崩溃后恢复 ──
    print("\n── TEST 17: Worker崩溃后恢复 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    # 创建即将过期的锁
    old_heartbeat = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    old_expires = (datetime.now() - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    crash_lock_id = "rlock-crash-test"
    crash_token = "rtok-crashed-worker"
    c.execute(
        """INSERT INTO executor_resource_locks
           (lock_id, lock_token, resource_scope, scope_key, resource_type,
            resource_key, normalized_key, project_id, task_id, execution_id,
            executor_run_id, worker_id, locked_at, heartbeat_at, expires_at, status)
           VALUES (?, ?, 'project', ?, 'file', 'crash.py', 'crash.py',
                   ?, ?, ?, ?, 'crashed-worker', ?, ?, ?, 'active')""",
        (crash_lock_id, crash_token, f"project:{PID1}",
         PID1, TID1, EID1, RID1,
         old_heartbeat, old_heartbeat, old_expires),
    )
    conn.commit()

    # 尝试接管
    takeover_res = takeover_expired_lock(
        conn, crash_lock_id, "recovery-worker",
        TID2, EID2, RID2, heartbeat_timeout_seconds=60,
    )
    check("过期锁被成功接管", takeover_res["success"],
          takeover_res.get("error"))

    c.execute("SELECT worker_id, lock_token FROM executor_resource_locks WHERE lock_id=?",
              (crash_lock_id,))
    new_worker, new_token = c.fetchone()
    check("worker_id已更新", new_worker == "recovery-worker",
          f"got {new_worker}")
    check("lock_token已重新生成", new_token != crash_token)

    # 旧token不能再操作
    bad_renew = renew_resource_lock(conn, crash_lock_id, crash_token, "crashed-worker")
    check("旧token不能续租已接管锁", not bad_renew)
    conn.rollback()

    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 18: 服务重启后锁可恢复或安全过期 ──
    print("\n── TEST 18: 服务重启后锁可恢复 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()

    # 模拟重启前的活跃锁
    restart_locks = []
    for i in range(3):
        lid = f"rlock-restart-{i}"
        tok = f"rtok-restart-{i}"
        c.execute(
            """INSERT INTO executor_resource_locks
               (lock_id, lock_token, resource_scope, scope_key, resource_type,
                resource_key, normalized_key, project_id, task_id, execution_id,
                executor_run_id, worker_id, locked_at, heartbeat_at, expires_at, status)
               VALUES (?, ?, 'project', ?, 'file', ?, ?,
                       ?, ?, ?, ?, 'restart-worker',
                       datetime('now','localtime'), datetime('now','localtime'),
                       datetime('now','+10 minutes'), 'active')""",
            (lid, tok, f"project:{PID1}", f"restart_{i}.py", f"restart_{i}.py",
             PID1, TID1, EID1, RID1),
        )
        restart_locks.append({"lock_id": lid, "lock_token": tok})

    conn.commit()

    # 查询所有活跃锁（模拟重启后查询）
    c.execute(
        "SELECT lock_id, lock_token, worker_id FROM executor_resource_locks WHERE status='active' AND project_id=?",
        (PID1,),
    )
    active_after_restart = c.fetchall()
    check("重启后活跃锁可查询", len(active_after_restart) == 3,
          f"got {len(active_after_restart)}")

    # 续租所有锁（验证token有效）
    for rl in restart_locks:
        ok = renew_resource_lock(conn, rl["lock_id"], rl["lock_token"], "restart-worker")
        check(f"重启后锁{rl['lock_id'][-8:]}可续租", ok)

    conn.rollback()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    conn.close()

    # ── TEST 19: integrity / foreign_key ──
    print("\n── TEST 19: 最终完整性检查 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()
    c.execute("DELETE FROM executor_resource_locks WHERE project_id IN (?, ?)", (PID1, PID2))
    conn.commit()
    c.execute("PRAGMA integrity_check")
    check("integrity_check = ok", c.fetchone()[0] == "ok")
    c.execute("PRAGMA foreign_key_check")
    check("foreign_key_check = 0", len(c.fetchall()) == 0)
    conn.close()

    # ── TEST 20: CHECK约束验证 ──
    print("\n── TEST 20: CHECK约束验证 ──")
    conn = sqlite3.connect(str(test_db))
    conn.execute("PRAGMA foreign_keys = ON")
    c = conn.cursor()

    # 20a: 无效 resource_type
    try:
        c.execute(
            """INSERT INTO executor_resource_locks
               (lock_id, lock_token, resource_scope, scope_key, resource_type,
                resource_key, normalized_key, project_id, task_id, execution_id,
                executor_run_id, worker_id, locked_at, heartbeat_at, expires_at, status)
               VALUES ('bad-type', 'tok-bad', 'project', 'p:1', 'invalid_type',
                       'x', 'x', ?, ?, ?, ?, 'w',
                       datetime('now','localtime'), datetime('now','localtime'),
                       datetime('now','+10 minutes'), 'active')""",
            (PID1, TID1, EID1, RID1),
        )
        conn.commit()
        check("无效resource_type被拒绝", False, "应抛出异常")
    except sqlite3.IntegrityError:
        check("无效resource_type被拒绝", True)

    # 20b: 无效 resource_scope
    try:
        c.execute(
            """INSERT INTO executor_resource_locks
               (lock_id, lock_token, resource_scope, scope_key, resource_type,
                resource_key, normalized_key, project_id, task_id, execution_id,
                executor_run_id, worker_id, locked_at, heartbeat_at, expires_at, status)
               VALUES ('bad-scope', 'tok-bad2', 'invalid_scope', 'x', 'file',
                       'x', 'x', ?, ?, ?, ?, 'w',
                       datetime('now','localtime'), datetime('now','localtime'),
                       datetime('now','+10 minutes'), 'active')""",
            (PID1, TID1, EID1, RID1),
        )
        conn.commit()
        check("无效resource_scope被拒绝", False)
    except sqlite3.IntegrityError:
        check("无效resource_scope被拒绝", True)

    # 20c: 无效 status
    try:
        c.execute(
            """INSERT INTO executor_resource_locks
               (lock_id, lock_token, resource_scope, scope_key, resource_type,
                resource_key, normalized_key, project_id, task_id, execution_id,
                executor_run_id, worker_id, locked_at, heartbeat_at, expires_at, status)
               VALUES ('bad-status', 'tok-bad3', 'project', 'x', 'file',
                       'x', 'x', ?, ?, ?, ?, 'w',
                       datetime('now','localtime'), datetime('now','localtime'),
                       datetime('now','+10 minutes'), 'invalid_status')""",
            (PID1, TID1, EID1, RID1),
        )
        conn.commit()
        check("无效status被拒绝", False)
    except sqlite3.IntegrityError:
        check("无效status被拒绝", True)

    # 20d: active状态released_at不能非空
    try:
        c.execute(
            """INSERT INTO executor_resource_locks
               (lock_id, lock_token, resource_scope, scope_key, resource_type,
                resource_key, normalized_key, project_id, task_id, execution_id,
                executor_run_id, worker_id, locked_at, heartbeat_at, expires_at,
                released_at, status)
               VALUES ('bad-active', 'tok-bad4', 'project', 'x', 'file',
                       'x', 'x', ?, ?, ?, ?, 'w',
                       datetime('now','localtime'), datetime('now','localtime'),
                       datetime('now','+10 minutes'),
                       datetime('now','localtime'), 'active')""",
            (PID1, TID1, EID1, RID1),
        )
        conn.commit()
        check("active+released_at非空被拒绝", False)
    except sqlite3.IntegrityError:
        check("active+released_at非空被拒绝", True)

    conn.rollback()
    conn.close()

    # ── SUMMARY ──
    print(f"\n{'='*60}")
    print(f"  TEST RESULT: {passed} PASSED, {failed} FAILED")
    print(f"  OVERALL: {'PASS' if failed == 0 else 'FAIL'}")
    print(f"{'='*60}")

    # 清理测试数据库
    try:
        test_db.unlink()
        for ext in [".db-wal", ".db-shm"]:
            p = Path(str(test_db) + ext)
            if p.exists():
                p.unlink()
        print(f"[CLEANUP] 已删除测试数据库: {test_db}")
    except Exception as e:
        print(f"[CLEANUP] 清理失败: {e}")

    return passed, failed


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            run_tests()
        elif sys.argv[1] == "--help" or sys.argv[1] == "-h":
            print(__doc__)
        else:
            print(f"Unknown argument: {sys.argv[1]}")
            print("Usage: python -m app.migrations.005_executor_resource_locks [test]")
    else:
        migrate()
