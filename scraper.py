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

# The fixed column structure (Time + 16 Classes)
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
    # Checks for 7:20, 8:00, etc.
    if not text: return False
    return bool(re.match(r'^\d{1,2}[:.]\d{2}', text))

def parse_pdf_with_tabula(pdf_path):
    final_schedule = {}
    
    # 1. CONVERT PDF TO "EXCEL" (DataFrames)
    # stream=True forces it to look at whitespace gaps to define columns
    # lattice=False ignores the broken lines in the PDF
    print("Converting PDF to DataFrames (Tabula)...")
    try:
        dfs = tabula.read_pdf(pdf_path, pages='all', stream=True, multiple_tables=False)
    except Exception as e:
        print(f"Tabula Error: {e}")
        return {}

    # German Day Names (Unique to each page)
    day_mapping = {
        "MONTAG": "Luni", "DIENSTAG": "Marti", "MITTWOCH": "Miercuri", 
        "DONNERSTAG": "Joi", "FREITAG": "Vineri"
    }

    for i, df in enumerate(dfs):
        print(f"--- Processing Sheet {i+1} ---")
        
        # 2. FIND DAY
        # We search the entire raw data of the sheet for the day name
        df_str = df.to_string().upper()
        current_day = None
        for german, romanian in day_mapping.items():
            if german in df_str:
                current_day = romanian
                break
        
        if not current_day:
            print("   -> SKIP: No day found in this sheet.")
            continue
        
        print(f"   -> DAY: {current_day}")

        # 3. CLEAN & MAP COLUMNS
        # Tabula might produce headers like "Unnamed: 0". We assume the structure is fixed.
        # We just grab the data row by row.
        
        for index, row in df.iterrows():
            # Convert row to a simple list of strings
            row_data = [normalize_text(x) for x in row.tolist()]
            
            # Filter: We only want rows that start with a Time Slot
            # We check the first few columns in case 'Time' shifted slightly
            time_slot = None
            start_col_idx = 0
            
            # Find which column holds the time (usually col 0 or 1)
            for idx, cell in enumerate(row_data[:3]):
                if is_time_slot(cell):
                    time_slot = cell
                    start_col_idx = idx
                    break
            
            if not time_slot: continue

            # 4. READ CLASSES
            # The columns AFTER the time slot correspond to our classes
            # Time | 9A | 9B | 9C ...
            
            # Start reading subjects from the column after Time
            current_col = start_col_idx + 1
            
            # Loop through our expected classes (9A, 9B...)
            for class_name in EXPECTED_COLUMNS[1:]: # Skip "Time" in list
                if current_col >= len(row_data): break
                
                subject = row_data[current_col]
                current_col += 1
                
                # Cleanup
                if len(subject) < 2 or "nan" in subject.lower(): continue
                
                # Save Data
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
    
    # Process
    new_schedule = parse_pdf_with_tabula("temp.pdf")
    
    if not new_schedule: 
        print("Error: Schedule empty.")
        return

    # Save
    final_json = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "schedule": new_schedule
    }
    with open(OUTPUT_FILE, "w", encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)
    print("Success.")

if __name__ == "__main__":
    main()
