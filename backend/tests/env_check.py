"""环境检查脚本"""
import sys, platform, os, sqlite3

# 系统信息
print("=== 系统环境 ===")
print(f"Python: {sys.version}")
print(f"OS: {platform.system()} {platform.release()}")
print(f"CPU核心: {os.cpu_count()}")

try:
    import psutil
    print(f"内存总量: {round(psutil.virtual_memory().total/1024/1024/1024, 1)} GB")
    print(f"内存使用率: {psutil.virtual_memory().percent}%")
    print(f"CPU使用率: {psutil.cpu_percent(interval=1)}%")
    print(f"磁盘C可用: {round(psutil.disk_usage('C:\\').free/1024/1024/1024, 1)} GB")
except ImportError:
    print("psutil 未安装，跳过详细系统信息")

# 应用配置
print("\n=== 应用配置 ===")
from app.core.config import settings
print(f"APP_NAME: {settings.APP_NAME}")
print(f"APP_VERSION: {settings.APP_VERSION}")
print(f"DEBUG: {settings.DEBUG}")
print(f"DATABASE_URL: {settings.DATABASE_URL}")
print(f"AI_DEFAULT_TIMEOUT: {settings.AI_DEFAULT_TIMEOUT}")
print(f"AI_MAX_RETRIES: {settings.AI_MAX_RETRIES}")
print(f"AI_MAX_TOKENS: {settings.AI_MAX_TOKENS}")

# 数据库状态
print("\n=== 数据库状态 ===")
db_path = settings.DATABASE_URL.replace("sqlite:///", "")
print(f"数据库路径: {db_path}")
print(f"数据库大小: {os.path.getsize(db_path)} bytes")

conn = sqlite3.connect(db_path)
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = c.fetchall()
for t in tables:
    c.execute(f"SELECT COUNT(*) FROM {t[0]}")
    count = c.fetchone()[0]
    print(f"  {t[0]}: {count} records")
conn.close()

# 已安装的包
print("\n=== 关键依赖版本 ===")
for pkg in ['fastapi', 'uvicorn', 'sqlalchemy', 'pydantic', 'openai', 'locust']:
    try:
        mod = __import__(pkg)
        ver = getattr(mod, '__version__', 'unknown')
        print(f"  {pkg}: {ver}")
    except ImportError:
        print(f"  {pkg}: 未安装")

# 检查后端是否运行
print("\n=== 后端服务检查 ===")
try:
    import requests
    r = requests.get('http://localhost:8000/api/projects', timeout=3)
    print(f"  后端运行中: status={r.status_code}")
except Exception as e:
    print(f"  后端未运行或无法连接: {e}")

# 日志目录
print("\n=== 日志目录 ===")
log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
print(f"  日志目录: {os.path.abspath(log_dir)}")
print(f"  日志目录存在: {os.path.exists(os.path.abspath(log_dir))}")

print("\n=== 环境判断 ===")
print("  当前环境: 开发环境 (本地 SQLite, DEBUG=False)")
print("  测试数据库备份: data/ai_factory_backup_20260614.db")
