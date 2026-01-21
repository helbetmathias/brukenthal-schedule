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

def get_latest_pdf_url():
    try:
        html = requests.get(URL, headers=HEADERS).text
        match = re.search(r'href="([^"]*orarliceu[^"]*\.pdf)"', html, re.IGNORECASE)
        if match: return match.group(1)
    except Exception as e: print(f"Error finding PDF: {e}")
    return None

def clean_text(text):
    if not text: return ""
    # Remove newlines and weird spaces
    return re.sub(r'\s+', ' ', str(text)).strip()

def is_time_slot(text):
    # Checks if text looks like "7:20" or "8:00" or "12:00"
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
            print(f"--- Processing Page {i+1} ---")
            
            # 1. FIND DAY
            page_text = (page.extract_text() or "").upper().replace("\n", " ")
            current_day = None
            for key, val in day_mapping.items():
                if key in page_text:
                    current_day = val
                    break
            
            if not current_day:
                print(f"   -> SKIP: No day found.")
                continue
            
            print(f"   -> DAY: {current_day}")

            table = page.extract_table()
            if not table: continue

            # 2. FIND HEADER ROW based on TIME SLOTS
            # We look for the first row that starts with a Time (e.g. 7:20-8:00)
            # The row BEFORE that one is the Class Header.
            header_row = []
            start_row_index = 0
            
            for r_idx, row in enumerate(table):
                first_cell = clean_text(row[0])
                if is_time_slot(first_cell):
                    # Found the first time slot! 
                    # The headers must be in the previous row (r_idx - 1)
                    if r_idx > 0:
                        header_row = [clean_text(c) for c in table[r_idx-1]]
                        start_row_index = r_idx
                        print(f"   -> Header found above row {r_idx}")
                        break
            
            if not header_row:
                print("   -> SKIP: Could not find time slots to locate header.")
                continue

            # 3. PARSE DATA
            for row in table[start_row_index:]:
                if len(row) < 2: continue
                
                time_slot = clean_text(row[0])
                if not is_time_slot(time_slot): continue 
                
                for col_index in range(1, len(row)):
                    if col_index >= len(header_row): break
                    
                    # Get Class Name from the discovered header
                    class_name = header_row[col_index]
                    
                    # Fix corrupted class names (e.g. "9A 9B" merged)
                    # We take the first part if it looks like a class
                    class_name = class_name.split(" ")[0]
                    
                    subject = clean_text(row[col_index])
                    
                    # Garbage checks
                    if len(class_name) > 6 or len(class_name) < 2: continue
                    if not subject: continue

                    if class_name not in final_schedule: final_schedule[class_name] = {}
                    if current_day not in final_schedule[class_name]: final_schedule[class_name][current_day] = []
                    
                    entry = f"{time_slot} | {subject}"
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
        print("Empty schedule parsed.")
        return

    final_json = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "schedule": new_schedule
    }
    with open(OUTPUT_FILE, "w", encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)
    print("Success")

if __name__ == "__main__":
    main()
