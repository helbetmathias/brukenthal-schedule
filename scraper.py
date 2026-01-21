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
    
    # Mapping for Day Detection
    # We check for both German and Romanian to be safe
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
            
            # 1. EXTRACT FULL TEXT to find the Day Name
            # (We do this BEFORE looking at the table)
            page_text = (page.extract_text() or "").upper()
            
            current_day = None
            for key, val in day_mapping.items():
                if key in page_text:
                    current_day = val
                    break
            
            if not current_day:
                print(f"   -> No day found in page text. Skipping Page {i+1}.")
                continue
                
            print(f"   -> Detected Day: {current_day}")

            # 2. EXTRACT TABLE for the Schedule
            table = page.extract_table()
            if not table: 
                print("   -> No table found on this page.")
                continue

            # Detect Classes (Row 0 usually contains: "", "9A", "9B", etc.)
            classes_row = table[0]

            # Read Times & Subjects (Rows 1 to end)
            for row in table[1:]:
                if len(row) < 2: continue
                
                # Column 0 is the Time Slot
                time_slot = clean_text(row[0])
                # If time slot is empty/weird, skip
                if len(time_slot) < 3: continue 
                
                # Loop through the columns (Classes)
                for col_index in range(1, len(row)):
                    if col_index >= len(classes_row): break
                    
                    class_name = clean_text(classes_row[col_index])
                    subject = clean_text(row[col_index])
                    
                    # Cleanup: Ignore if class name is empty or too long (garbage text)
                    if not class_name or len(class_name) > 6: continue
                    if not subject: continue

                    # Initialize Data Structure
                    if class_name not in final_schedule: final_schedule[class_name] = {}
                    if current_day not in final_schedule[class_name]: final_schedule[class_name][current_day] = []
                    
                    # Create Entry
                    entry = f"{time_slot} | {subject}"
                    
                    # Avoid duplicates
                    if entry not in final_schedule[class_name][current_day]:
                        final_schedule[class_name][current_day].append(entry)
                        
    return final_schedule

def main():
    print("Finding PDF...")
    pdf_url = get_latest_pdf_url()
    if not pdf_url: return

    print(f"Downloading {pdf_url}...")
    pdf_data = requests.get(pdf_url, headers=HEADERS).content
    with open("temp.pdf", "wb") as f: f.write(pdf_data)
        
    print("Parsing PDF...")
    new_schedule = parse_pdf("temp.pdf")
    
    if not new_schedule: 
        print("Parsing result is empty.")
        return

    # Always save the new data
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
