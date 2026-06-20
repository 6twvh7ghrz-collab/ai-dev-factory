"""Test infrastructure helpers for V2 regression and e2e runs."""

from __future__ import annotations

import importlib
import os
import json
import sqlite3
import shutil
import socket
import subprocess
import tempfile
import time
import sys
from pathlib import Path

import pytest
import requests


BACKEND_DIR = Path(__file__).resolve().parent.parent
PROD_DB = BACKEND_DIR / "data" / "ai_factory.db"
SANDBOX_WORKSPACE = Path(r"C:\SandboxUser\本机\Desktop\executor-sandbox-v2")
_E2E_REQUESTED = False
_E2E_PROC = None
_E2E_DB = None
_E2E_PORT = None
_E2E_ENV_SNAPSHOT = {}

sys.path.insert(0, str(BACKEND_DIR))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _clear_proxy_env(env: dict[str, str]) -> dict[str, str]:
    for key in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]:
        env.pop(key, None)
    no_proxy = "localhost,127.0.0.1,::1"
    env["NO_PROXY"] = no_proxy
    env["no_proxy"] = no_proxy
    return env


def _ensure_git_workspace(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git_dir = path / ".git"
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True, text=True, check=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=str(path), capture_output=True, text=True, check=True)


def _ensure_public_seed_db() -> None:
    def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def row_exists(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> bool:
        try:
            return conn.execute(sql, params).fetchone() is not None
        except sqlite3.OperationalError:
            return False

    def task_title(conn: sqlite3.Connection, task_id: int) -> str | None:
        try:
            row = conn.execute(
                "SELECT title FROM development_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            return row["title"] if row else None
        except sqlite3.OperationalError:
            return None

    needs_schema = not (PROD_DB.exists() and PROD_DB.stat().st_size > 0)
    needs_seed = True

    if not needs_schema:
        try:
            conn = sqlite3.connect(str(PROD_DB))
            conn.row_factory = sqlite3.Row
            try:
                needs_schema = not table_exists(conn, "projects")
                if not needs_schema:
                    expected_projects = (1, 56, 64, 65)
                    expected_tasks = (1, 26, 27, 31, 51)
                    project_rows = [row_exists(conn, "SELECT 1 FROM projects WHERE id = ?", (pid,)) for pid in expected_projects]
                    task_rows = [row_exists(conn, "SELECT 1 FROM development_tasks WHERE id = ?", (tid,)) for tid in expected_tasks]
                    title_27 = task_title(conn, 27)
                    needs_seed = not all(project_rows + task_rows) or title_27 is None or "拼多多" not in title_27
            finally:
                conn.close()
        except Exception:
            needs_schema = not PROD_DB.exists() or PROD_DB.stat().st_size == 0

    if needs_schema:
        PROD_DB.parent.mkdir(parents=True, exist_ok=True)

        import app.models  # noqa: F401
        from app.database.engine import create_tables

        create_tables()

        migration_dir = BACKEND_DIR / "app" / "migrations"
        migration_modules = sorted(
            p.stem for p in migration_dir.glob("[0-9][0-9][0-9]_*.py") if p.name != "__init__.py"
        )
        for module_name in migration_modules:
            mod = importlib.import_module(f"app.migrations.{module_name}")
            migrate_fn = getattr(mod, "upgrade", None) or getattr(mod, "migrate", None)
            if migrate_fn is not None:
                migrate_fn(str(PROD_DB))
        needs_seed = True

    if needs_seed:
        conn = sqlite3.connect(str(PROD_DB))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            projects = [
                (
                    1,
                    "seed-project-1",
                    "seed",
                    "anchor project for harness migrations",
                    "sandbox",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "developing",
                    "draft",
                ),
                (
                    56,
                    "sandbox-project-56",
                    "sandbox",
                    "disabled execution project",
                    "sandbox",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "developing",
                    "draft",
                ),
                (
                    64,
                    "sandbox-project-64",
                    "sandbox",
                    "disabled execution project",
                    "sandbox",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "developing",
                    "draft",
                ),
                (
                    65,
                    "sandbox-project-65",
                    "sandbox",
                    "seed project for public release tests",
                    "sandbox",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "unknown",
                    "developing",
                    "tasks_complete",
                ),
            ]
            for project in projects:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO projects
                    (id, name, idea, description, platform, need_login, need_database, need_ai,
                     need_third_party, need_upload, need_export, status, current_stage, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                    """,
                    project,
                )

            tasks = [
                (
                    1,
                    1,
                    "seed-task-1",
                    "anchor task for harness migrations",
                    "backend",
                    "medium",
                    "[]",
                    "[]",
                    "[\"seed.py\"]",
                    "",
                    "[\"pytest -q\"]",
                    "[\"seed\"]",
                    "[\"seed\"]",
                    "completed",
                    None,
                    1,
                    "ready",
                    1,
                ),
                (
                    26,
                    56,
                    "seed-task-26",
                    "seed task for planning approval tests",
                    "backend",
                    "medium",
                    "[]",
                    "[]",
                    "[\"seed.py\"]",
                    "",
                    "[\"pytest -q\"]",
                    "[\"seed\"]",
                    "[\"seed\"]",
                    "pending",
                    None,
                    26,
                    "needs_planning",
                    1,
                ),
                (
                    27,
                    56,
                    "拼多多采集商品",
                    "需要与平台交互并采集商品信息的高风险任务",
                    "backend",
                    "medium",
                    "[]",
                    "[]",
                    "[\"seed.py\"]",
                    "",
                    "[\"pytest -q\"]",
                    "[\"seed\"]",
                    "[\"seed\"]",
                    "pending",
                    None,
                    27,
                    "needs_planning",
                    1,
                ),
                (
                    31,
                    56,
                    "图片处理UI",
                    "图片处理相关界面任务",
                    "backend",
                    "medium",
                    "[]",
                    "[]",
                    "[\"seed.py\"]",
                    "",
                    "[\"pytest -q\"]",
                    "[\"seed\"]",
                    "[\"seed\"]",
                    "pending",
                    None,
                    31,
                    "needs_planning",
                    1,
                ),
                (
                    51,
                    65,
                    "normalize_title",
                    "completed seed task",
                    "backend",
                    "medium",
                    "[]",
                    "[]",
                    "[\"module_demo.py\", \"test_module_demo.py\"]",
                    "",
                    "[\"pytest -q\"]",
                    "[\"passes rehearsal\"]",
                    "[\"1. implement normalize_title\"]",
                    "completed",
                    None,
                    1,
                    "ready",
                    1,
                ),
            ]
            for task in tasks:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO development_tasks
                    (id, project_id, title, description, task_type, priority, dependencies, files_to_check,
                     files_to_modify, codex_prompt, test_steps, acceptance_criteria, implementation_steps,
                     status, execution_result, sort_order, created_at, updated_at, readiness_status, state_version, last_state_change)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?, ?, datetime('now'))
                    """,
                    task,
                )

            conn.execute(
                """
                INSERT OR REPLACE INTO project_execution_configs
                (project_id, workspace_path, execution_enabled, execution_mode, max_workers, max_tasks, requires_confirmation)
                VALUES (?, ?, 0, 'sandbox', 1, 10, 1), (?, ?, 0, 'sandbox', 1, 10, 1)
                """,
                (
                    56,
                    str(SANDBOX_WORKSPACE),
                    64,
                    str(SANDBOX_WORKSPACE),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    _ensure_git_workspace(SANDBOX_WORKSPACE)


_ensure_public_seed_db()


def pytest_collection_modifyitems(items):
    global _E2E_REQUESTED
    _E2E_REQUESTED = bool(os.getenv("V2_RUN_E2E_BACKEND")) and any(
        item.get_closest_marker("e2e") for item in items
    )
    if not os.getenv("V2_RUN_LIVE_MODEL"):
        skip_live = pytest.mark.skip(reason="live model tests are disabled unless V2_RUN_LIVE_MODEL=1")
        for item in items:
            if item.get_closest_marker("live_model"):
                item.add_marker(skip_live)


@pytest.fixture(scope="session", autouse=True)
def e2e_backend_session():
    global _E2E_PROC, _E2E_DB, _E2E_PORT, _E2E_ENV_SNAPSHOT
    if not _E2E_REQUESTED:
        yield
        return

    _E2E_PORT = _free_port()
    fd, tmp_path = tempfile.mkstemp(prefix="v2_e2e_", suffix=".db")
    os.close(fd)
    os.unlink(tmp_path)
    shutil.copy2(PROD_DB, tmp_path)
    _E2E_DB = Path(tmp_path)

    _E2E_ENV_SNAPSHOT = {
        key: os.environ.get(key)
        for key in [
            "DATABASE_URL",
            "AI_FACTORY_DB_PATH",
            "V2_CONTROL_PLANE_ENABLED",
            "E2E_BASE_URL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ]
    }

    os.environ["DATABASE_URL"] = f"sqlite:///{str(_E2E_DB).replace(chr(92), '/')}"
    os.environ["AI_FACTORY_DB_PATH"] = str(_E2E_DB)
    os.environ["V2_CONTROL_PLANE_ENABLED"] = "true"
    os.environ["E2E_BASE_URL"] = f"http://127.0.0.1:{_E2E_PORT}"
    _clear_proxy_env(os.environ)

    env = _clear_proxy_env(os.environ.copy())
    env["DATABASE_URL"] = os.environ["DATABASE_URL"]
    env["AI_FACTORY_DB_PATH"] = os.environ["AI_FACTORY_DB_PATH"]
    env["V2_CONTROL_PLANE_ENABLED"] = "true"

    _E2E_PROC = subprocess.Popen(
        [
            os.sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(_E2E_PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    base_url = f"http://127.0.0.1:{_E2E_PORT}/api/health"
    client = requests.Session()
    client.trust_env = False
    deadline = time.time() + 45
    last_error = None
    while time.time() < deadline:
        if _E2E_PROC.poll() is not None:
            stderr = b""
            try:
                stderr = _E2E_PROC.stderr.read(2000) if _E2E_PROC.stderr else b""
            except Exception:
                pass
            raise RuntimeError(f"e2e backend exited early: {stderr.decode('utf-8', errors='replace')}")
        try:
            resp = client.get(base_url, timeout=3)
            if resp.status_code == 200:
                break
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    else:
        stderr = b""
        try:
            stderr = _E2E_PROC.stderr.read(2000) if _E2E_PROC.stderr else b""
        except Exception:
            pass
        raise RuntimeError(
            f"e2e backend did not become healthy: {last_error}; stderr={stderr.decode('utf-8', errors='replace')}"
        )

    try:
        yield
    finally:
        if _E2E_PROC is not None:
            try:
                _E2E_PROC.terminate()
                _E2E_PROC.wait(timeout=10)
            except Exception:
                try:
                    _E2E_PROC.kill()
                    _E2E_PROC.wait(timeout=5)
                except Exception:
                    pass
            try:
                if _E2E_PROC.poll() is None:
                    subprocess.run(
                        ["taskkill", "/PID", str(_E2E_PROC.pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
            except Exception:
                pass
            try:
                if _E2E_PROC.stderr:
                    _E2E_PROC.stderr.close()
            except Exception:
                pass
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(_E2E_DB) + suffix).unlink(missing_ok=True)
            except Exception:
                pass
        for key, value in _E2E_ENV_SNAPSHOT.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
