import sys, re

# Fix image_tools.py
with open('app/image_tools.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
skip = False
for i, line in enumerate(lines):
    if 'elif mode.startswith("riso_"):' in line:
        skip = True
        continue
    if skip:
        # Stop skipping when we hit the next elif
        if line.strip().startswith('elif mode == "remove_bg_gemini":'):
            skip = False
        else:
            continue
    new_lines.append(line)

content = ''.join(new_lines)
content = re.sub(r'\s*- \'riso_sticker.*?\n', '', content)
content = re.sub(r'\s*- \'riso_duotone.*?\n', '', content)
content = re.sub(r'\s*- \'riso_tritone.*?\n', '', content)
content = re.sub(r'\s*- \'riso_multiply.*?\n', '', content)
content = re.sub(r'\s*- \'riso_sticker_book.*?\n', '', content)

content = re.sub(r'\s*if mode == "riso_pop":\n\s*mode = random\.choice\(\["riso_sticker", "riso_duotone", "riso_tritone", "riso_multiply", "riso_sticker_book"\]\)\n', '\n', content)
content = content.replace(', riso_sticker, riso_duotone, riso_tritone, riso_multiply, riso_sticker_book', '')
content = content.replace("('riso_sticker', 'riso_duotone', 'riso_tritone', 'riso_multiply', 'riso_sticker_book')", "()")
content = content.replace('if mode.startswith("riso_"):', 'if False:') # handle the message intercept

with open('app/image_tools.py', 'w', encoding='utf-8') as f:
    f.write(content)

# Fix views.py
with open('bot/views.py', 'r', encoding='utf-8') as f:
    vlines = f.readlines()

vnew_lines = []
vskip = False
for i, line in enumerate(vlines):
    if 'def riso_' in line and 'button' in line:
        vskip = True
    if vskip:
        if line.strip() == '' and (i+1 < len(vlines) and not 'def riso_' in vlines[i+1]):
            # if we hit a blank line and next isn't riso
            if i+2 < len(vlines) and not 'def riso_' in vlines[i+2]:
                vskip = False
        continue
    vnew_lines.append(line)

vcontent = ''.join(vnew_lines)
vcontent = re.sub(r'\s*"riso_sticker": "🖨️ Riso Sticker", "riso_duotone": "🖨️ Riso Duotone", "riso_multiply": "🖨️ Riso Multiply",\n', '\n', vcontent)
vcontent = re.sub(r'\s*"riso_tritone": "🖨️ Riso Tritone", "riso_sticker_book": "🖨️ Sticker Book"\n', '\n', vcontent)

with open('bot/views.py', 'w', encoding='utf-8') as f:
    f.write(vcontent)
print("Done!")
