import sys
import re
import os

with open('bot/audio.py', 'r', encoding='utf-8') as f:
    content = f.read()

def extract_func(name, text):
    # simple extraction by finding 'async def name' or 'def name' and getting everything up to the next non-indented line
    # except empty lines or comments
    match = re.search(r'^(?:async\s+)?def\s+' + name + r'\s*\(.*?\):\s*\n.*?(?=\n(?:(?:async\s+)?def\s+[a-zA-Z_]|\Z))', text, re.MULTILINE | re.DOTALL)
    if match:
        return match.group(0)
    return None

funcs = ['persist_radio_state_helper', 'clear_radio_state_helper', 'jit_replenish_queue', 'get_user_favorites']
extracted = []
for f in funcs:
    ext = extract_func(f, content)
    if ext:
        extracted.append(ext)
    else:
        print(f'Failed to extract {f}')

new_file_content = '''"""Radio orchestration and persistence helpers extracted from bot."""

import logging
import asyncio
import os
import json
import random
from app.db import session_service
from app.ytmusic_tools import search_ytmusic_track, generate_ytmusic_radio

logger = logging.getLogger("sophee.app.radio_orchestration")

''' + '\n\n'.join(extracted)

with open('app/radio_orchestration.py', 'w', encoding='utf-8') as out_f:
    out_f.write(new_file_content)

for ext in extracted:
    content = content.replace(ext, '')

with open('bot/audio.py', 'w', encoding='utf-8') as out_f:
    out_f.write(content)
print('Done!')
