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

# Header-ele pe care le căutăm pentru a stabili grila
CLASS_HEADERS = [
    "9A", "9B", "9C", "9D", 
    "10A", "10B", "10C", "10D", 
    "11A", "11B", "11C", "11D", 
    "12A", "12B", "12C", "12D"
]

DAY_MARKERS = {
    "MONTAG": "Luni",
    "DIENSTAG": "Marti",
    "MITTWOCH": "Miercuri",
    "DONNERSTAG": "Joi",
    "FREITAG": "Vineri"
}

def get_latest_pdf_url():
    try:
        html = requests.get(URL, headers=HEADERS).text
        match = re.search(r'href="([^"]*orarliceu[^"]*\.pdf)"', html, re.IGNORECASE)
        if match: return match.group(1)
    except Exception as e: print(f"Error finding PDF: {e}")
    return None

def clean_text(text):
    if not text: return ""
    return text.replace('\n', ' ').strip()

def is_time_slot(text):
    # Caută format strict de oră (7:20, 8:00)
    # Exclude datele (19.01)
    if not text: return False
    return bool(re.search(r'^\d{1,2}:\d{2}', str(text).strip()))

def parse_pdf(pdf_path):
    final_schedule = {}

    with pdfplumber.open(pdf_path) as pdf:
        # Scanăm pagina 1 (unde este tot orarul)
        page = pdf.pages[0]
        width = page.width
        
        # 1. IDENTIFICĂM ZONELE ZILELOR (Vertical)
        # Folosim cuvintele pentru a găsi Y-ul titlurilor (MONTAG, DIENSTAG...)
        words = page.extract_words(x_tolerance=2, y_tolerance=2)
        day_zones = []
        
        for w in words:
            text_upper = w['text'].upper()
            for de_key, ro_val in DAY_MARKERS.items():
                if de_key in text_upper:
                    day_zones.append({"day": ro_val, "top": w['top']})
                    break
        day_zones.sort(key=lambda x: x['top'])
        
        if not day_zones:
            print("Nu am găsit zilele.")
            return {}

        # 2. PROCESĂM FIECARE ZI INDIVIDUAL
        for i, zone in enumerate(day_zones):
            day_name = zone['day']
            y_start = zone['top']
            # Ziua se termină la următoarea zi sau la finalul paginii
            y_end = day_zones[i+1]['top'] if i+1 < len(day_zones) else page.height
            
            print(f"--- Procesez {day_name} ---")

            # Decupăm zona zilei curente
            # Adăugăm un mic buffer la y_end (-5) ca să nu prindem headerul zilei următoare
            cropped = page.crop((0, y_start, width, y_end - 5))

            # 3. CALCULĂM LINIILE VERTICALE (SPECIFIC PENTRU ACEASTĂ ZI)
            # Căutăm header-ele claselor ("9A", "9B"...) DOAR în această secțiune
            crop_words = cropped.extract_words()
            header_words = [w for w in crop_words if w['text'] in CLASS_HEADERS]
            header_words.sort(key=lambda w: w['x0'])

            vertical_lines = []
            
            if len(header_words) >= 5:
                # 3a. Avem headere, calculăm liniile de mijloc
                # Linia de start (stânga de 9A)
                vertical_lines.append(max(0, header_words[0]['x0'] - 50))
                
                # Liniile dintre clase
                for k in range(len(header_words) - 1):
                    curr = header_words[k]
                    nxt = header_words[k+1]
                    mid = (curr['x1'] + nxt['x0']) / 2
                    vertical_lines.append(mid)
                
                # Linia de final (dreapta de 12D)
                vertical_lines.append(header_words[-1]['x1'] + 10)
            else:
                # 3b. Fallback (dacă PDF-ul e ciudat în zona aia)
                print(f"   Warning: Nu am găsit headerele claselor pentru {day_name}. Folosesc estimare.")
                vertical_lines = [55 + k*47.5 for k in range(18)]

            # 4. EXTRAGEM TABELUL CU LINII FORȚATE
            # Aceasta este cheia: 'explicit_vertical_lines' taie textul lipit
            table_settings = {
                "vertical_strategy": "explicit",
                "explicit_vertical_lines": vertical_lines,
                "horizontal_strategy": "text", # Lăsăm rândurile să fie detectate după text
                "snap_tolerance": 3
            }

            try:
                # extract_table returnează o listă de rânduri
                table = cropped.extract_table(table_settings)
            except Exception as e:
                print(f"   Eroare tabel: {e}")
                continue

            if not table: continue

            # 5. PARSĂM DATELE
            for row in table:
                # Curățăm
                clean_row = [clean_text(cell) for cell in row]
                
                # Validăm că e rând de oră (prima coloană trebuie să fie oră)
                if not clean_row or not is_time_slot(clean_row[0]):
                    continue
                
                time_slot = clean_row[0]

                # Iterăm prin materii
                # clean_row[0] = Time, clean_row[1] = 9A, ...
                for col_idx, subject in enumerate(clean_row[1:]):
                    if col_idx >= len(CLASS_HEADERS): break
                    
                    class_name = CLASS_HEADERS[col_idx]
                    
                    # Filtre
                    if len(subject) < 2: continue
                    if subject in CLASS_HEADERS: continue # Ignorăm dacă scrie "9A" în celulă

                    # Salvare
                    if class_name not in final_schedule: final_schedule[class_name] = {}
                    if day_name not in final_schedule[class_name]: final_schedule[class_name][day_name] = []
                    
                    entry = f"{time_slot} | {subject}"
                    
                    if entry not in final_schedule[class_name][day_name]:
                        final_schedule[class_name][day_name].append(entry)

    return final_schedule

def main():
    print("Starting...")
    pdf_url = get_latest_pdf_url()
    if not pdf_url: return
    
    pdf_data = requests.get(pdf_url, headers=HEADERS).content
    with open("temp.pdf", "wb") as f: f.write(pdf_data)
        
    new_schedule = parse_pdf("temp.pdf")
    
    if not new_schedule: 
        print("Empty schedule.")
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
