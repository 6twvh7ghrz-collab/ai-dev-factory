import sqlite3
conn = sqlite3.connect('data/ai_factory.db')
c = conn.cursor()

# Get current columns
c.execute("PRAGMA table_info(bugs)")
existing = [row[1] for row in c.fetchall()]
print(f'Existing ({len(existing)}): {existing}')

# All columns from Bug model
all_model_cols = [
    'id', 'project_id', 'title', 'description', 'error_message',
    'reproduction_steps', 'expected_result', 'actual_result', 'related_code',
    'backend_logs', 'console_errors', 'network_requests', 'bug_type',
    'severity', 'probable_cause', 'affected_module', 'affected_files',
    'fix_plan', 'regression_risks', 'fix_prompt', 'test_steps', 'is_blocking',
    'execution_result', 'files_changed', 'test_result', 'remaining_issues',
    'executed_at', 'status', 'resolved_at', 'created_at', 'updated_at',
]

missing_cols = {
    'is_blocking': "VARCHAR(10) DEFAULT NULL",
    'execution_result': "TEXT DEFAULT NULL",
    'files_changed': "TEXT DEFAULT NULL",
    'test_result': "TEXT DEFAULT NULL",
    'remaining_issues': "TEXT DEFAULT NULL",
    'executed_at': "DATETIME DEFAULT NULL",
    'resolved_at': "DATETIME DEFAULT NULL",
}

for col, dtype in missing_cols.items():
    if col not in existing:
        sql = f"ALTER TABLE bugs ADD COLUMN {col} {dtype}"
        try:
            c.execute(sql)
            print(f'+ Added: {col}')
        except Exception as e:
            print(f'? Error adding {col}: {e}')
    else:
        print(f'. Exists: {col}')

conn.commit()
conn.close()

# Final verification
conn = sqlite3.connect('data/ai_factory.db')
c = conn.cursor()
c.execute("PRAGMA table_info(bugs)")
final = [row[1] for row in c.fetchall()]
print(f'\nFinal ({len(final)}): {final}')
print('Missing:', [c for c in all_model_cols if c not in final])
conn.close()
