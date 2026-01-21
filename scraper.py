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

# The column order is FIXED in the PDF
EXPECTED_COLUMNS = [
    "Time", 
    "9A", "9B", "9C", "9D", 
    "10A", "10B", "10C", "10D", 
    "11A", "11B", "11C", "11D", 
    "12A", "12B", "12C", "12D"
]

# HARDCODED DAYS (Index 0 = Page 1 = Luni)
# This prevents the script from reading "Luni" on the Tuesday page footer.
PAGE_TO_DAY = ["Luni", "Marti", "Miercuri", "Joi", "Vineri"]

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
    # Checks for "7:20", "8:00" start
    if not text: return False
    return bool(re.match(r'^\d{1,2}[:.]\d{2}', text))

def parse_pdf_with_tabula(pdf_path):
    final_schedule = {}
    
    print("Reading PDF pages...")
    try:
        # stream=True is ESSENTIAL for this PDF to split the columns correctly
        dfs = tabula.read_pdf(pdf_path, pages='all', stream=True, guess=False, pandas_options={'header': None})
    except Exception as e:
        print(f"Tabula Error: {e}")
        return {}

    # Iterate through pages (DataFrames)
    for i, df in enumerate(dfs):
        if i >= 5: break # Only process first 5 pages (Mon-Fri)
        
        # 1. FORCE THE DAY BASED ON PAGE NUMBER
        current_day = PAGE_TO_DAY[i]
        print(f"--- Processing Page {i+1} -> Forcing Day: {current_day} ---")

        # 2. PARSE ROWS
        for index, row in df.iterrows():
            # Convert row to simple list of strings
            row_data = [normalize_text(x) for x in row.tolist()]
            
            # Find which column holds the Time (usually col 0, but sometimes shifts to 1)
            time_slot = None
            start_col_idx = -1
            
            for idx, cell in enumerate(row_data[:3]): # Check first 3 cols
                if is_time_slot(cell):
                    time_slot = cell
                    start_col_idx = idx
                    break
            
            if not time_slot: continue

            # 3. MAP COLUMNS TO CLASSES
            # The columns AFTER the time slot correspond to 9A, 9B, etc.
            current_data_idx = start_col_idx + 1
            
            for class_name in EXPECTED_COLUMNS[1:]: # Skip "Time"
                if current_data_idx >= len(row_data): break
                
                subject = row_data[current_data_idx]
                current_data_idx += 1
                
                # Filter out garbage
                if len(subject) < 2 or "nan" in subject.lower(): continue
                # Filter out headers that look like "9A 9B"
                if "9A" in subject and "9B" in subject: continue 

                # Initialize keys
                if class_name not in final_schedule: final_schedule[class_name] = {}
                if current_day not in final_schedule[class_name]: final_schedule[class_name][current_day] = []
                
                entry = f"{time_slot} | {subject}"
                
                # Avoid duplicates
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

    # SAVE
    final_json = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "schedule": new_schedule
    }
    with open(OUTPUT_FILE, "w", encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)
    print("Success. JSON updated.")

if __name__ == "__main__":
    main()
