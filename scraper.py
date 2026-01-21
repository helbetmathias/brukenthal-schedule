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

# HARDCODED DAYS BY PAGE INDEX (Foolproof)
PAGE_TO_DAY = {
    0: "Luni",
    1: "Marti",
    2: "Miercuri",
    3: "Joi",
    4: "Vineri"
}

def get_latest_pdf_url():
    try:
        html = requests.get(URL, headers=HEADERS).text
        match = re.search(r'href="([^"]*orarliceu[^"]*\.pdf)"', html, re.IGNORECASE)
        if match: return match.group(1)
    except Exception as e: print(f"Error finding PDF: {e}")
    return None

def is_time_slot(text):
    # Strict check: Must look like "7:20" or "12:00"
    if not text: return False
    return bool(re.search(r'^\d{1,2}[:.]\d{2}', str(text).strip()))

def parse_pdf(pdf_path):
    final_schedule = {}

    with pdfplumber.open(pdf_path) as pdf:
        # Process first 5 pages only (Mon-Fri)
        for page_idx in range(min(5, len(pdf.pages))):
            page = pdf.pages[page_idx]
            current_day = PAGE_TO_DAY.get(page_idx, "Unknown")
            
            print(f"--- Processing Page {page_idx + 1} -> {current_day} ---")

            # EXTRACT WORDS WITH COORDINATES
            # This ignores broken lines and uses pure geometry
            words = page.extract_words(x_tolerance=2, y_tolerance=2)
            
            # Group words by Y-position (Rows)
            rows = {}
            for w in words:
                # Snap Y to nearest 10px to handle wavy text lines
                y = round(w['top'] / 10) * 10 
                if y not in rows: rows[y] = []
                rows[y].append(w)

            # PROCESS ROWS
            for y in sorted(rows.keys()):
                # Sort words in this row from Left to Right (X position)
                row_words = sorted(rows[y], key=lambda w: w['x0'])
                
                if not row_words: continue

                # CHECK 1: Does row start with a Time?
                first_text = row_words[0]['text']
                if not is_time_slot(first_text):
                    # If not a time (e.g. "LUNI 19.01" or "9A 9B"), SKIP ROW
                    continue
                
                time_slot = first_text

                # CHECK 2: Map remaining words to Classes based on X-Position
                # A4 Landscape Metrics:
                # Time column ends approx at X=55
                # Classes start at X=55 and go across the page
                # Each class column is approx 47.5 pixels wide
                
                COL_START_X = 55
                COL_WIDTH = 47.5 

                for word in row_words[1:]: # Skip the time word
                    text = word['text']
                    x_pos = word['x0']
                    
                    # Calculate bucket: (Word_X - Start_X) / Column_Width
                    col_index = int((x_pos - COL_START_X) / COL_WIDTH)
                    
                    # Safety bounds
                    if col_index < 0: continue
                    if col_index >= len(ORDERED_CLASSES): continue
                    
                    class_name = ORDERED_CLASSES[col_index]
                    
                    # Clean up subject text
                    if len(text) < 2: continue 
                    
                    # SAVE TO SCHEDULE
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
