import requests
import tabula
import json
import re
import pandas as pd
import os
from datetime import datetime

# CONFIG
URL = "https://brukenthal.ro/"
HEADERS = {'User-Agent': 'Mozilla/5.0'}
OUTPUT_FILE = "timetable.json"

# Fixed Column Structure (Time + 16 Classes)
EXPECTED_COLUMNS = [
    "Time", 
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

def normalize_text(text):
    if pd.isna(text): return ""
    return str(text).replace("\r", " ").replace("\n", " ").strip()

def is_time_slot(text):
    if not text: return False
    # Looks for "7:20" or "8:00" start
    return bool(re.match(r'^\d{1,2}[:.]\d{2}', text))

def parse_pdf_with_tabula(pdf_path):
    final_schedule = {}
    
    # 1. READ PDF (Stream mode to handle broken lines)
    print("Reading PDF with Tabula...")
    try:
        # pages='all' returns a list of DataFrames (one per page usually)
        dfs = tabula.read_pdf(pdf_path, pages='all', stream=True, guess=False)
    except Exception as e:
        print(f"Tabula Error: {e}")
        return {}

    # STRICT DAY MAPPING (GERMAN KEYS ONLY)
    # We removed "LUNI", "MARTI" etc. to prevent false matches from the page footer.
    day_mapping = {
        "MONTAG": "Luni", 
        "DIENSTAG": "Marti", 
        "MITTWOCH": "Miercuri", 
        "DONNERSTAG": "Joi", 
        "FREITAG": "Vineri"
    }

    for i, df in enumerate(dfs):
        print(f"--- Processing Table {i+1} ---")
        
        # 2. DETECT DAY (Search full text of the table)
        # We convert the whole table to a string to find the header "DIENSTAG" etc.
        table_text = df.to_string().upper()
        
        current_day = None
        for german_key, romanian_val in day_mapping.items():
            if german_key in table_text:
                current_day = romanian_val
                break
        
        if not current_day:
            print("   -> SKIP: No German day name found in this table.")
            continue
            
        print(f"   -> DETECTED DAY: {current_day}")

        # 3. PARSE ROWS
        # We iterate row by row. If we find a time, we map the rest to classes.
        for index, row in df.iterrows():
            # Convert row to list of strings
            row_data = [normalize_text(x) for x in row.tolist()]
            
            # Find the Time Column
            time_slot = None
            start_col_idx = -1
            
            # Check the first 3 columns for a time
            for idx, cell in enumerate(row_data[:3]):
                if is_time_slot(cell):
                    time_slot = cell
                    start_col_idx = idx
                    break
            
            if not time_slot: continue

            # Map Columns to Classes
            # The column AFTER the time slot is 9A, then 9B, etc.
            current_data_idx = start_col_idx + 1
            
            # Loop through 9A...12D
            for class_name in EXPECTED_COLUMNS[1:]: 
                if current_data_idx >= len(row_data): break
                
                subject = row_data[current_data_idx]
                current_data_idx += 1
                
                # Cleanup
                if len(subject) < 2 or "nan" in subject.lower(): continue
                
                # Save
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
    
    new_schedule = parse_pdf_with_tabula("temp.pdf")
    
    if not new_schedule: 
        print("Error: Schedule empty.")
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
