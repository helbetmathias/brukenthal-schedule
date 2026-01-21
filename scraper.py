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

def get_latest_pdf_url():
    try:
        html = requests.get(URL, headers=HEADERS).text
        match = re.search(r'href="([^"]*orarliceu[^"]*\.pdf)"', html, re.IGNORECASE)
        if match: return match.group(1)
    except Exception as e: print(f"Error finding PDF: {e}")
    return None

def is_time_slot(text):
    # Strict check: Must start with a digit and have a colon/dot (e.g., 7:20, 12:00)
    if not text: return False
    return bool(re.search(r'^\d{1,2}[:.]\d{2}', str(text).strip()))

def parse_pdf(pdf_path):
    final_schedule = {}
    
    # STRICT DAY MAPPING (Look for German names first to avoid "Week of Monday" errors)
    day_mapping = {
        "MONTAG": "Luni",
        "DIENSTAG": "Marti", 
        "MITTWOCH": "Miercuri", 
        "DONNERSTAG": "Joi", 
        "FREITAG": "Vineri"
    }

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            print(f"--- Processing Page {i+1} ---")
            
            # 1. STRICT DAY DETECTION
            page_text = (page.extract_text() or "").upper()
            current_day = None
            
            # Only match the explicit German header unique to the page
            for german, romanian in day_mapping.items():
                if german in page_text:
                    current_day = romanian
                    break
            
            if not current_day:
                print(f"   -> SKIP: No valid day header found (checked German names).")
                continue
            
            print(f"   -> LOCKED DAY: {current_day}")

            # 2. EXTRACT WORDS WITH COORDINATES
            words = page.extract_words(x_tolerance=2, y_tolerance=2)
            
            # Group words by Y-position (Rows)
            rows = {}
            for w in words:
                # Snap Y to nearest 10px to handle wavy lines
                y = round(w['top'] / 10) * 10 
                if y not in rows: rows[y] = []
                rows[y].append(w)

            # 3. PROCESS ROWS
            for y in sorted(rows.keys()):
                # Sort words in this row from Left to Right (X position)
                row_words = sorted(rows[y], key=lambda w: w['x0'])
                
                if not row_words: continue

                # CHECK 1: Does row start with a Time?
                first_text = row_words[0]['text']
                if not is_time_slot(first_text):
                    # This removes headers like "9A 9B" or "LUNI 19.01"
                    continue
                
                time_slot = first_text

                # CHECK 2: Map remaining words to Classes based on X-Position
                # Page layout assumptions (A4 Landscape):
                # Time column ends approx at X=50
                # Classes start at X=50 and go to X=800
                # 16 Classes space ~47px each
                
                COL_START_X = 55
                COL_WIDTH = 47.5 

                for word in row_words[1:]: # Skip the time word
                    text = word['text']
                    x_pos = word['x0']
                    
                    # Calculate which column bucket this word falls into
                    # (x - start) / width
                    col_index = int((x_pos - COL_START_X) / COL_WIDTH)
                    
                    # Safety bounds
                    if col_index < 0: continue
                    if col_index >= len(ORDERED_CLASSES): continue
                    
                    class_name = ORDERED_CLASSES[col_index]
                    
                    # Clean up subject text
                    if len(text) < 2: continue # skip garbage
                    
                    # 4. SAVE TO SCHEDULE
                    if class_name not in final_schedule: final_schedule[class_name] = {}
                    if current_day not in final_schedule[class_name]: final_schedule[class_name][current_day] = []
                    
                    entry = f"{time_slot} | {text}"
                    
                    # Deduplicate
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
        print("Error: Parsed schedule is empty.")
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
