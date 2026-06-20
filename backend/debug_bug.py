import sqlite3
conn = sqlite3.connect('data/ai_factory.db')
c = conn.cursor()
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = c.fetchall()
print('Tables:', [t[0] for t in tables])
for t in tables:
    name = t[0]
    c.execute(f"PRAGMA table_info({name})")
    cols = c.fetchall()
    print(f'\n  {name}:')
    for col in cols:
        print(f'    {col[1]} ({col[2]})')
conn.close()

# Also try creating a Bug directly to see the error
print('\n--- Testing direct bug creation ---')
from app.database.engine import SessionLocal, engine
from app.models import Project, Bug
from sqlalchemy import text

db = SessionLocal()

# Check if bug_status_logs table exists
try:
    result = db.execute(text("SELECT count(*) FROM bug_status_logs"))
    print(f'bug_status_logs rows: {result.scalar()}')
except Exception as e:
    print(f'bug_status_logs ERROR: {e}')

try:
    p = db.query(Project).filter(Project.id == 3).first()
    if p:
        bug = Bug(project_id=3, title='test-direct')
        db.add(bug)
        db.commit()
        print(f'Bug created: id={bug.id}')
    else:
        print('Project 3 not found')
except Exception as e:
    print(f'ERROR: {type(e).__name__}: {e}')
finally:
    db.close()
