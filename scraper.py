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

# The exact order of classes in the PDF columns
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
    # Matches "7:20", "8:00", etc.
    return bool(re.search(r'\d{1,2}[:.]\d{2}', str(text)))

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

            # 2. EXTRACT WORDS (The "Atomic" Method)
            # Instead of trusting table lines, we get every single word and its X-position.
            words = page.extract_words()
            
            # We group words by their Y-position (Vertical Row)
            # Tolerance=5 means words within 5 pixels height difference are on the "same line"
            rows = {} 
            for w in words:
                y = round(w['top'] / 5) * 5 # Round to nearest 5 to group roughly aligned text
                if y not in rows: rows[y] = []
                rows[y].append(w)

            # 3. ANALYZE EACH ROW
            sorted_y_keys = sorted(rows.keys())
            
            for y in sorted_y_keys:
                row_words = sorted(rows[y], key=lambda w: w['x0']) # Sort by Left-to-Right
                
                # Check if the first word is a Time Slot
                first_word_text = row_words[0]['text']
                if not is_time_slot(first_word_text):
                    continue

                time_slot = first_word_text
                
                # Now we need to figure out which column (Class) each subsequent word belongs to.
                # The page width is roughly 840px (A4 Landscape).
                # 16 Classes + 1 Time column = 17 columns.
                # Width per column approx 50px.
                
                # We define approximate X-coordinates for each class column.
                # NOTE: These values are estimated based on standard PDF layouts.
                # If columns are misaligned, we might need to tweak 'start_x'
                
                # Start searching for subjects AFTER the time slot (approx x=50)
                for word in row_words[1:]:
                    text = word['text']
                    x_pos = word['x0']
                    
                    # Estimate column index based on X position
                    # Time is at 0-50.
                    # 9A starts around 50.
                    # 12D ends around 800.
                    # Total width ~750px for 16 classes -> ~47px per class.
                    
                    offset_x = x_pos - 60 # Subtract Time column width
                    if offset_x < 0: continue 

                    col_index = int(offset_x // 47) # 47 is the "magic number" for column width
                    
                    if col_index >= len(ORDERED_CLASSES): continue
                    
                    class_name = ORDERED_CLASSES[col_index]
                    
                    # Basic cleanup
                    if len(text) < 2: continue

                    # SAVE DATA
                    if class_name not in final_schedule: final_schedule[class_name] = {}
                    if current_day not in final_schedule[class_name]: final_schedule[class_name][current_day] = []
                    
                    # We might catch fragments. We append them.
                    # Later, the app just shows the list.
                    entry = f"{time_slot} | {text}"
                    
                    # Check if we already added this subject (to avoid duplicate fragments)
                    already_exists = False
                    for existing in final_schedule[class_name][current_day]:
                        if entry in existing: 
                            already_exists = True
                            break
                    
                    if not already_exists:
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
