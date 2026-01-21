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
    day_mapping = {
        "MONTAG": "Luni", "LUNI": "Luni",
        "DIENSTAG": "Marti", "MARTI": "Marti",
        "MITTWOCH": "Miercuri", "MIERCURI": "Miercuri",
        "DONNERSTAG": "Joi", "JOI": "Joi",
        "FREITAG": "Vineri", "VINERI": "Vineri"
    }
    
    # Valid class names to look for (Anchors)
    valid_classes = ["9A", "9B", "9C", "10A", "11A", "12A"]

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            print(f"--- Processing Page {i+1} ---")
            
            # 1. FIND DAY (Search everywhere: Page text AND Table text)
            page_text = (page.extract_text() or "").upper().replace("\n", " ")
            table = page.extract_table()
            
            # If table exists, add its header text to search scope to be safe
            if table:
                for row in table[:3]: # Check first 3 rows
                    page_text += " " + " ".join([str(c).upper() for c in row if c])

            current_day = None
            for key, val in day_mapping.items():
                if key in page_text:
                    current_day = val
                    break
            
            if not current_day:
                print(f"   -> SKIP: No day found on Page {i+1}")
                continue
            
            print(f"   -> DAY FOUND: {current_day}")

            if not table: continue

            # 2. FIND ANCHOR ROW (The row that has '9A', '9B', etc.)
            header_row_index = -1
            classes_row = []

            for r_idx, row in enumerate(table):
                # Count how many valid class names are in this row
                # We simply check if any cell contains "9A" or "10A" etc.
                matches = 0
                clean_row = [clean_text(str(c)).upper() for c in row]
                
                for cell in clean_row:
                    # Check if cell MATCHES a class name (e.g. "9A" or "9 A")
                    if any(vc in cell for vc in valid_classes):
                        matches += 1
                
                # If we found at least 3 class headers, this is the Header Row!
                if matches >= 3:
                    header_row_index = r_idx
                    classes_row = [clean_text(str(c)) for c in row]
                    print(f"   -> HEADER FOUND at Row {r_idx}: {classes_row[:5]}...")
                    break
            
            if header_row_index == -1:
                print("   -> SKIP: Could not find class headers (9A, 9B...) in table.")
                continue

            # 3. PARSE DATA (Start from the row AFTER the header)
            for row in table[header_row_index + 1:]:
                if len(row) < 2: continue
                
                time_slot = clean_text(row[0])
                if len(time_slot) < 3: continue # Skip empty time slots
                
                # Iterate columns based on the Header Row we found
                for col_index in range(1, len(row)):
                    if col_index >= len(classes_row): break
                    
                    class_name = classes_row[col_index] # Get class from Anchor Row
                    subject = clean_text(row[col_index])
                    
                    # Cleanup
                    class_name = class_name.replace("\n", "").strip()
                    if len(class_name) > 6 or len(class_name) < 2: continue # garbage check
                    if not subject: continue

                    # Save
                    if class_name not in final_schedule: final_schedule[class_name] = {}
                    if current_day not in final_schedule[class_name]: final_schedule[class_name][current_day] = []
                    
                    entry = f"{time_slot} | {subject}"
                    if entry not in final_schedule[class_name][current_day]:
                        final_schedule[class_name][current_day].append(entry)

    return final_schedule

def main():
    print("Starting...")
    pdf_url = get_latest_pdf_url()
    if not pdf_url: return

    print(f"Downloading {pdf_url}...")
    pdf_data = requests.get(pdf_url, headers=HEADERS).content
    with open("temp.pdf", "wb") as f: f.write(pdf_data)
        
    new_schedule = parse_pdf("temp.pdf")
    
    if not new_schedule: 
        print("Error: Empty schedule.")
        return

    print("Saving JSON...")
    final_json = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "schedule": new_schedule
    }
    with open(OUTPUT_FILE, "w", encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)
    print("Success.")

if __name__ == "__main__":
    main()
