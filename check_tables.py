import sqlite3
conn = sqlite3.connect('data/adk_sessions.db')
print([row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()])
