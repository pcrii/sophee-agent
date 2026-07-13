import sys

with open('app/fast_api_app.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Add verify_api_key logic and module-level app
auth_logic = '''from fastapi import FastAPI, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from starlette.status import HTTP_403_FORBIDDEN
import os

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def verify_api_key(api_key_header: str = Security(api_key_header)):
    expected_api_key = os.getenv("SOPHEE_API_KEY")
    if not expected_api_key:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Server API key not configured."
        )
    if api_key_header != expected_api_key:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Invalid API Key"
        )
'''

text = text.replace('from fastapi import FastAPI', auth_logic)

# Add dependencies=[Depends(verify_api_key)] to FastAPI
text = text.replace('app = FastAPI(title="Sophee Agent API")', 'app = FastAPI(title="Sophee Agent API", dependencies=[Depends(verify_api_key)])')

# Add app = create_app() at module level
module_export = '''
app = create_app()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
'''

text = text.split('if __name__ == "__main__":')[0] + module_export

with open('app/fast_api_app.py', 'w', encoding='utf-8') as f:
    f.write(text)
