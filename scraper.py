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
    # Aggressive cleanup: remove newlines, multiple spaces
    if not text: return ""
    return re.sub(r'\s+', ' ', str(text)).strip()

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
            
            # 1. FIND DAY
            # Scan the raw text of the entire page
            page_text = (page.extract_text() or "").upper().replace("\n", " ")
            current_day = None
            for key, val in day_mapping.items():
                if key in page_text:
                    current_day = val
                    break
            
            if not current_day:
                print(f"   -> SKIP: No day found in text.")
                continue
            
            print(f"   -> Day: {current_day}")

            table = page.extract_table()
            if not table: continue

            # 2. FIND HEADER ROW (The "9A, 9B..." row)
            # Strategy: Look for a row that contains "10A" and "11A" 
            # (We skip 9A/9B because the PDF messed them up with newlines)
            header_row_index = -1
            classes_row = []

            for r_idx, row in enumerate(table):
                # Flatten the row to a single string to search
                row_str = " ".join([clean_text(c).upper() for c in row])
                
                # If this row mentions multiple classes, it's the header
                if "10A" in row_str and "11A" in row_str:
                    header_row_index = r_idx
                    classes_row = [clean_text(c) for c in row]
                    print(f"   -> Header Found at Row {r_idx}")
                    break
            
            if header_row_index == -1:
                print("   -> SKIP: Header row (10A, 11A...) not found.")
                continue

            # 3. PARSE DATA
            for row in table[header_row_index + 1:]:
                if len(row) < 2: continue
                
                time_slot = clean_text(row[0])
                if len(time_slot) < 3: continue 
                
                for col_index in range(1, len(row)):
                    if col_index >= len(classes_row): break
                    
                    class_name = classes_row[col_index]
                    subject = clean_text(row[col_index])
                    
                    # Cleanup class name (remove garbage like "9A\n9B")
                    class_name = class_name.split(" ")[0] # Take first word if split
                    if len(class_name) > 5 or len(class_name) < 2: continue
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
    
    if not new_schedule: return

    # Always save
    final_json = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "schedule": new_schedule
    }
    with open(OUTPUT_FILE, "w", encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)
    print("Success")

if __name__ == "__main__":
    main()
