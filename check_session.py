import sqlite3
import json
conn = sqlite3.connect('data/adk_sessions.db')
try:
    c = conn.cursor()
    c.execute("SELECT state FROM sessions WHERE session_id='1115483224676773978'")
    row = c.fetchone()
    if row:
        state = json.loads(row[0])
        print("State Keys:", state.keys())
        # The internal ADK state is usually stored inside the session.state under _adk_state or something
        if 'previous_interaction_id' in state:
            print("previous_interaction_id:", state['previous_interaction_id'])
        for k, v in state.items():
            if isinstance(v, dict) and 'previous_interaction_id' in v:
                print(f"Found in {k}:", v['previous_interaction_id'])
    else:
        print("Session not found")
except Exception as e:
    print(e)
