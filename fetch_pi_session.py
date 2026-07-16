import urllib.request
import json
req = urllib.request.Request(
    'http://192.168.1.225:8000/api/debug/sessions',
    headers={'X-API-Key': '133754337'}
)
try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        # Find the session for the channel id
        session = None
        if isinstance(data, dict):
            # Try to get it directly if it's a dict keyed by session ID
            session = data.get('1115483224676773978')
            if not session:
                for k, v in data.items():
                    if isinstance(v, dict) and v.get('session_id') == '1115483224676773978':
                        session = v
                        break
        else:
            for s in data:
                if isinstance(s, dict) and s.get('session_id') == '1115483224676773978':
                    session = s
                    break
        
        if session:
            print("Session Found!")
            print(json.dumps(session, indent=2))
        else:
            print("Session not found in the debug dump.")
except Exception as e:
    print('Failed:', e)
