import json
import re
from pathlib import Path
import os
from src import extract

def process_archive(file_path):
    print(f"Processing {file_path}")
    content = Path(file_path).read_text(encoding="utf-8")
    
    # Extract the graphify-record JSON at the bottom
    match = re.search(r"<!-- graphify-record\s*```json\s*(.*?)\s*```\s*-->", content, re.DOTALL)
    if not match:
        print("No graphify-record found")
        return
        
    record = json.loads(match.group(1))
    
    transcript = record.get("transcript", "")
    caption = record.get("caption", "")
    onscreen = record.get("onscreen", "")
    meta = {
        "uploader": record.get("uploader"),
        "duration": record.get("duration")
    }
    
    # Turn off verification to speed up this test
    os.environ["VERIFY_NAMES"] = "0"
    
    data = extract.extract(transcript, caption, onscreen, meta)
    
    print("\nExtracted Items:")
    for item in data.get("items", []):
        print(f"- {item.get('name_en')} -> kind: {item.get('kind')}")
    print("-" * 40)

for f in ["Db2ZGglM7x7.md", "Db3syUmtWe0.md", "Db07wnRy9kx.md"]:
    path = rf"D:\claude projects\Promtlarim\instagram\{f}"
    process_archive(path)
