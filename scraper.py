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

# Column Headers to look for to establish X-coordinates
CLASS_HEADERS = [
    "9A", "9B", "9C", "9D", 
    "10A", "10B", "10C", "10D", 
    "11A", "11B", "11C", "11D", 
    "12A", "12B", "12C", "12D"
]

# Day Headers to look for to establish Y-coordinates
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
    # STRICT: Matches "7:20", "8:00". 
    # Does NOT match "19.01" (dot instead of colon).
    if not text: return False
    return bool(re.search(r'^\d{1,2}:\d{2}', str(text).strip()))

def get_column_ranges(words):
    # Find the X-coordinates of "9A", "9B", etc. to build a dynamic grid
    header_words = [w for w in words if w['text'] in CLASS_HEADERS]
    header_words.sort(key=lambda w: w['x0'])
    
    if len(header_words) < 5:
        # Fallback if headers aren't found
        ranges = []
        curr_x = 55
        width = 47.5
        for cls in CLASS_HEADERS:
            ranges.append({"class": cls, "min": curr_x, "max": curr_x + width})
            curr_x += width
        return ranges

    # Build ranges based on midpoints between headers
    ranges = []
    for i in range(len(header_words)):
        curr = header_words[i]
        cls_name = curr['text']
        
        # Start X is the midpoint between previous header and this one
        if i == 0:
            start_x = curr['x0'] - 20
        else:
            prev = header_words[i-1]
            start_x = (prev['x1'] + curr['x0']) / 2
            
        # End X is the midpoint between this header and next one
        if i == len(header_words) - 1:
            end_x = curr['x1'] + 20
        else:
            nxt = header_words[i+1]
            end_x = (curr['x1'] + nxt['x0']) / 2
            
        ranges.append({"class": cls_name, "min": start_x, "max": end_x})
        
    return ranges

def parse_pdf(pdf_path):
    final_schedule = {}

    with pdfplumber.open(pdf_path) as pdf:
        # Assume Page 1 (index 0) contains the data
        page = pdf.pages[0]
        words = page.extract_words(x_tolerance=2, y_tolerance=2)
        
        # 1. MAP VERTICAL ZONES (DAYS)
        day_zones = []
        for w in words:
            txt_upper = w['text'].upper()
            for de_key, ro_val in DAY_MARKERS.items():
                if de_key in txt_upper:
                    day_zones.append({"day": ro_val, "top": w['top']})
                    break
        day_zones.sort(key=lambda x: x['top'])
        
        if not day_zones:
            print("ERROR: No days found.")
            return {}

        # 2. MAP HORIZONTAL ZONES (CLASSES)
        col_ranges = get_column_ranges(words)

        # 3. PROCESS EACH DAY ZONE
        for i, zone in enumerate(day_zones):
            current_day = zone['day']
            zone_top = zone['top']
            zone_bottom = day_zones[i+1]['top'] if i+1 < len(day_zones) else page.height
            
            # Filter words belonging to this day
            day_words = [w for w in words if zone_top <= w['top'] < zone_bottom]
            
            # Identify TIME ROWS in this zone
            time_anchors = []
            for w in day_words:
                if is_time_slot(w['text']):
                    # Deduplicate: if close to existing time row, skip
                    is_dup = False
                    for t in time_anchors:
                        if abs(t['top'] - w['top']) < 10:
                            is_dup = True; break
                    if not is_dup:
                        time_anchors.append(w)
            
            time_anchors.sort(key=lambda x: x['top'])

            # 4. ASSIGN SUBJECTS
            for w in day_words:
                text = w['text']
                # Skip garbage
                if is_time_slot(text) or text in CLASS_HEADERS or len(text) < 2: continue
                if "MONTAG" in text.upper() or "LUNI" in text.upper(): continue

                # Find which Time Row this word aligns with vertically
                found_time = None
                word_mid_y = (w['top'] + w['bottom']) / 2
                
                for t in time_anchors:
                    row_mid_y = (t['top'] + t['bottom']) / 2
                    # Tolerance 15px up/down
                    if abs(word_mid_y - row_mid_y) < 15:
                        found_time = t['text']
                        break
                
                if not found_time: continue

                # Find which Class Column this word aligns with horizontally
                found_class = None
                word_mid_x = (w['x0'] + w['x1']) / 2
                
                for col in col_ranges:
                    if col['min'] <= word_mid_x < col['max']:
                        found_class = col['class']
                        break
                
                if not found_class: continue

                # Save
                if found_class not in final_schedule: final_schedule[found_class] = {}
                if current_day not in final_schedule[found_class]: final_schedule[found_class][current_day] = []
                
                entry = f"{found_time} | {text}"
                if entry not in final_schedule[found_class][current_day]:
                    final_schedule[found_class][current_day].append(entry)

    return final_schedule

def main():
    print("Starting...")
    pdf_url = get_latest_pdf_url()
    if not pdf_url: return
    
    print(f"Downloading {pdf_url}")
    pdf_data = requests.get(pdf_url, headers=HEADERS).content
    with open("temp.pdf", "wb") as f: f.write(pdf_data)
        
    new_schedule = parse_pdf("temp.pdf")
    
    # SAFETY: Even if empty, verify why. But here we save it to trigger the update.
    if not new_schedule:
        print("WARNING: Schedule parsed is empty. Check logic.")
    
    print(f"Saving {len(new_schedule)} classes to JSON...")
    final_json = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "schedule": new_schedule
    }
    with open(OUTPUT_FILE, "w", encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)
    print("Success.")

if __name__ == "__main__":
    main()
