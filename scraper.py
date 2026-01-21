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
    return text.replace("\n", " ").strip() if text else ""

def parse_pdf(pdf_path):
    final_schedule = {}
    day_mapping = {"MONTAG": "Luni", "DIENSTAG": "Marti", "MITTWOCH": "Miercuri", "DONNERSTAG": "Joi", "FREITAG": "Vineri"}

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if not table: continue

            # Detect Day
            header = clean_text(table[0][0]).upper()
            current_day = next((val for key, val in day_mapping.items() if key in header), None)
            if not current_day: continue

            # Detect Classes (Row 0)
            classes_row = table[0]

            # Read Times & Subjects
            for row in table[1:]:
                if len(row) < 2: continue
                time_slot = clean_text(row[0])
                
                for col_index in range(1, len(row)):
                    if col_index >= len(classes_row): break
                    class_name = clean_text(classes_row[col_index])
                    subject = clean_text(row[col_index])
                    
                    if not class_name or not subject: continue
                    
                    if class_name not in final_schedule: final_schedule[class_name] = {}
                    if current_day not in final_schedule[class_name]: final_schedule[class_name][current_day] = []
                    
                    final_schedule[class_name][current_day].append(f"{time_slot} | {subject}")
    return final_schedule

def main():
    pdf_url = get_latest_pdf_url()
    if not pdf_url: return

    pdf_data = requests.get(pdf_url, headers=HEADERS).content
    with open("temp.pdf", "wb") as f: f.write(pdf_data)
        
    new_schedule = parse_pdf("temp.pdf")
    if not new_schedule: return

    # Load old data to compare
    old_data = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r") as f: old_data = json.load(f).get("schedule", {})
        except: pass

    if new_schedule != old_data:
        final_json = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "schedule": new_schedule
        }
        with open(OUTPUT_FILE, "w", encoding='utf-8') as f:
            json.dump(final_json, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
