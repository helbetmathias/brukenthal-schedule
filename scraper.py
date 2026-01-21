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
    
    # German/Romanian mapping
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
            table = page.extract_table()
            
            if not table: 
                print("   -> No table found. Skipping.")
                continue

            # FIX: Scan the entire first row for the day name
            # We join all text in the first row to ensure we find the day even if cells are merged
            first_row_text = " ".join([clean_text(cell).upper() for cell in table[0] if cell])
            
            current_day = None
            for de_key, ro_val in day_mapping.items():
                if de_key in first_row_text:
                    current_day = ro_val
                    break
            
            if not current_day:
                print(f"   -> Could not detect a valid day in header: '{first_row_text[:50]}...' Skipping.")
                continue

            print(f"   -> Detected Day: {current_day}")

            # Detect Classes (Row 0 contains class names like 9A, 9B...)
            classes_row = table[0]

            # Read Times & Subjects (Rows 1 to end)
            for row in table[1:]:
                if len(row) < 2: continue
                
                # Time is usually in the first column
                time_slot = clean_text(row[0])
                
                # Loop through columns to find subjects
                for col_index in range(1, len(row)):
                    if col_index >= len(classes_row): break
                    
                    class_name = clean_text(classes_row[col_index])
                    subject = clean_text(row[col_index])
                    
                    # specific cleanup for your PDF format
                    if not class_name or not subject: continue
                    if len(class_name) > 5: continue # Ignore long text that isn't a class name

                    if class_name not in final_schedule: final_schedule[class_name] = {}
                    if current_day not in final_schedule[class_name]: final_schedule[class_name][current_day] = []
                    
                    # Create entry
                    entry = f"{time_slot} | {subject}"
                    
                    # Avoid duplicates
                    if entry not in final_schedule[class_name][current_day]:
                        final_schedule[class_name][current_day].append(entry)
                        
    return final_schedule

def main():
    print("Finding PDF...")
    pdf_url = get_latest_pdf_url()
    if not pdf_url: 
        print("No PDF URL found.")
        return

    print(f"Downloading {pdf_url}...")
    pdf_data = requests.get(pdf_url, headers=HEADERS).content
    with open("temp.pdf", "wb") as f: f.write(pdf_data)
        
    print("Parsing PDF...")
    new_schedule = parse_pdf("temp.pdf")
    
    if not new_schedule: 
        print("Parsing result is empty.")
        return

    # Load old data to compare
    old_data = {}
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r") as f: old_data = json.load(f).get("schedule", {})
        except: pass

    # Save if data exists (We force save now to fix your JSON)
    if new_schedule:
        print("Saving new data...")
        final_json = {
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "schedule": new_schedule
        }
        with open(OUTPUT_FILE, "w", encoding='utf-8') as f:
            json.dump(final_json, f, ensure_ascii=False, indent=2)
        print("Success.")

if __name__ == "__main__":
    main()
