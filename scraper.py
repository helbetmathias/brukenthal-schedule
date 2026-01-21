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

# WE HARDCODE THE CLASSES because the PDF headers are broken.
# We trust the column order is always the same.
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

def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', str(text)).strip()

def is_time_slot(text):
    # Looks for "7:20", "8:00", etc.
    if not text: return False
    return bool(re.search(r'\d{1,2}[:.]\d{2}', text))

def parse_pdf(pdf_path):
    final_schedule = {}
    
    day_mapping = {
        "MONTAG": "Luni", "LUNI": "Luni",
        "DIENSTAG": "Marti", "MARTI": "Marti",
        "MITTWOCH": "Miercuri", "MIERCURI": "Miercuri",
        "DONNERSTAG": "Joi", "JOI": "Joi",
        "FREITAG": "Vineri", "VINERI": "Vineri"
    }

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            print(f"--- Page {i+1} ---")

            # 1. FIND DAY (Scan full text)
            page_text = (page.extract_text() or "").upper().replace("\n", " ")
            current_day = None
            for key, val in day_mapping.items():
                if key in page_text:
                    current_day = val
                    break
            
            if not current_day:
                print("   -> SKIP: No day found.")
                continue
            
            print(f"   -> DAY: {current_day}")

            # 2. EXTRACT TABLE WITH "TEXT" STRATEGY
            # This is the magic fix. It ignores broken PDF lines and aligns by text whitespace.
            settings = {
                "vertical_strategy": "text",
                "horizontal_strategy": "text",
                "snap_tolerance": 5,
            }
            table = page.extract_table(settings)

            if not table: 
                print("   -> SKIP: No table structure found.")
                continue

            # 3. PARSE DATA (Blindly map columns to classes)
            for row in table:
                # We need at least a time column + some data
                if len(row) < 2: continue
                
                # Check column 0 for Time
                time_slot = clean_text(row[0])
                if not is_time_slot(time_slot): continue
                
                # If we found a time, assume the next columns are our classes in order
                # Col 0 = Time
                # Col 1 = 9A
                # Col 2 = 9B
                # ...
                for col_index in range(1, len(row)):
                    # Stop if we run out of known classes
                    class_idx = col_index - 1
                    if class_idx >= len(ORDERED_CLASSES): break
                    
                    class_name = ORDERED_CLASSES[class_idx]
                    subject = clean_text(row[col_index])
                    
                    if not subject: continue
                    if len(subject) < 2: continue # Skip artifacts like "-"

                    # Save Data
                    if class_name not in final_schedule: final_schedule[class_name] = {}
                    if current_day not in final_schedule[class_name]: final_schedule[class_name][current_day] = []
                    
                    entry = f"{time_slot} | {subject}"
                    # Remove duplicates
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
    print("Success! JSON updated.")

if __name__ == "__main__":
    main()
