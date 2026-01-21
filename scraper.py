import requests
import pdfplumber
import json
import re
import os
from datetime import datetime

# CONFIG
URL = "https://brukenthal.ro/"
HEADERS = {'User-Agent': 'Mozilla/5.0'}
OUTPUT_FILE = "timetable.json"

# Fixed Class Order (Columns 1 to 16)
ORDERED_CLASSES = [
    "9A", "9B", "9C", "9D", 
    "10A", "10B", "10C", "10D", 
    "11A", "11B", "11C", "11D", 
    "12A", "12B", "12C", "12D"
]

# We look for these SPECIFIC keys to define the start of a new day zone
DAY_MARKERS = {
    "MONTAG": "Luni",
    "DIENSTAG": "Marti",
    "MITTWOCH": "Miercuri",
    "DONNERSTAG": "Joi",
    "FREITAG": "Vineri"
}

def get_latest_pdf_url():
    try:
        html = requests.get(URL, headers=HEADERS).text
        match = re.search(r'href="([^"]*orarliceu[^"]*\.pdf)"', html, re.IGNORECASE)
        if match: return match.group(1)
    except Exception as e: print(f"Error finding PDF: {e}")
    return None

def is_time_slot(text):
    # Matches "7:20", "8:00", "14:00" etc.
    if not text: return False
    return bool(re.search(r'^\d{1,2}[:.]\d{2}', str(text).strip()))

def parse_pdf(pdf_path):
    final_schedule = {}

    with pdfplumber.open(pdf_path) as pdf:
        # We assume everything is on Page 1 (or we process all pages just in case)
        for page in pdf.pages:
            print(f"--- Scanning Page {page.page_number} ---")

            # 1. EXTRACT WORDS WITH COORDINATES
            words = page.extract_words(x_tolerance=2, y_tolerance=2)
            
            # 2. FIND THE VERTICAL POSITIONS (Y) OF DAY HEADERS
            # We look for "MONTAG", "DIENSTAG", etc.
            day_zones = []
            
            for w in words:
                txt = w['text'].upper()
                for marker, day_name in DAY_MARKERS.items():
                    if marker in txt:
                        # Found a marker! Save its Y position (top)
                        day_zones.append({"day": day_name, "top": w['top']})
            
            # Sort zones from top to bottom
            day_zones.sort(key=lambda x: x['top'])
            
            if not day_zones:
                print("   -> No day headers found. Skipping page.")
                continue

            # 3. GROUP WORDS INTO ROWS
            rows = {}
            for w in words:
                y = round(w['top'] / 10) * 10  # Snap to nearest 10px row
                if y not in rows: rows[y] = []
                rows[y].append(w)

            # 4. PROCESS EACH ROW
            for y in sorted(rows.keys()):
                # Determine which Day Zone this row belongs to
                current_day = None
                
                # Check which zone we are currently "inside"
                for i in range(len(day_zones)):
                    zone = day_zones[i]
                    next_zone_top = day_zones[i+1]['top'] if i+1 < len(day_zones) else 99999
                    
                    if y >= zone['top'] and y < next_zone_top:
                        current_day = zone['day']
                        break
                
                if not current_day: continue # Text above the first header? Skip.

                # Sort words in row Left -> Right
                row_words = sorted(rows[y], key=lambda w: w['x0'])
                if not row_words: continue

                # CHECK: Is this a valid schedule row? (Must start with Time)
                first_text = row_words[0]['text']
                if not is_time_slot(first_text):
                    continue # Skip header rows like "9A 9B" or Day Titles
                
                time_slot = first_text

                # MAP COLUMNS (X-Position)
                # A4 Landscape approximation
                COL_START_X = 55
                COL_WIDTH = 47.5 

                for word in row_words[1:]: # Skip time word
                    text = word['text']
                    x_pos = word['x0']
                    
                    # Calculate Column Bucket
                    col_index = int((x_pos - COL_START_X) / COL_WIDTH)
                    
                    if col_index < 0 or col_index >= len(ORDERED_CLASSES): continue
                    
                    class_name = ORDERED_CLASSES[col_index]
                    
                    # Cleanup
                    if len(text) < 2: continue 
                    if "9A" in text: continue # Skip if header text leaked in

                    # SAVE
                    if class_name not in final_schedule: final_schedule[class_name] = {}
                    if current_day not in final_schedule[class_name]: final_schedule[class_name][current_day] = []
                    
                    entry = f"{time_slot} | {text}"
                    
                    if entry not in final_schedule[class_name][current_day]:
                        final_schedule[class_name][current_day].append(entry)

    return final_schedule

def main():
    pdf_url = get_latest_pdf_url()
    if not pdf_url: return
    
    print(f"Downloading {pdf_url}")
    pdf_data = requests.get(pdf_url, headers=HEADERS).content
    with open("temp.pdf", "wb") as f: f.write(pdf_data)
        
    new_schedule = parse_pdf("temp.pdf")
    
    if not new_schedule: 
        print("Error: Empty schedule.")
        return

    final_json = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "schedule": new_schedule
    }
    with open(OUTPUT_FILE, "w", encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)
    print("Success.")

if __name__ == "__main__":
    main()
