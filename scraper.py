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

# Ordinea coloanelor (fără Time, care e prima)
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
    # Eliminăm rândurile noi din celulă pentru a nu avea surprize
    return text.replace('\n', ' ').strip()

def is_time_slot(text):
    # Validăm strict formatul orar (ex: 7:20, 12:00)
    if not text: return False
    return bool(re.search(r'^\d{1,2}:\d{2}', str(text).strip()))

def parse_pdf(pdf_path):
    final_schedule = {}

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0] # Lucrăm pe prima pagină
        width = page.width
        
        # 1. GĂSIM HEADER-ELE CLASELOR PENTRU A CALCULA LINIILE VERTICALE
        # Căutăm cuvintele "9A", "9B"... pentru a ști unde sunt coloanele
        words = page.extract_words()
        header_words = [w for w in words if w['text'] in CLASS_HEADERS]
        header_words.sort(key=lambda w: w['x0'])

        # Calculăm liniile verticale (x_cuts)
        # O linie verticală trebuie să fie exact la mijlocul distanței dintre "9A" și "9B"
        vertical_lines = []
        
        # Adăugăm linia de start (înainte de Time)
        if header_words:
            # Estimăm începutul tabelului (stânga de 9A minus o marjă pt Time)
            vertical_lines.append(max(0, header_words[0]['x0'] - 50))
            
            # Adăugăm liniile dintre clase
            for i in range(len(header_words) - 1):
                curr = header_words[i]
                next_w = header_words[i+1]
                # Punctul de mijloc
                mid_point = (curr['x1'] + next_w['x0']) / 2
                vertical_lines.append(mid_point)
            
            # Adăugăm linia de final (dreapta de 12D)
            vertical_lines.append(header_words[-1]['x1'] + 10)
        else:
            print("Nu am găsit header-ele claselor. Folosesc fallback.")
            # Fallback (împărțire egală a paginii)
            vertical_lines = [50 + i*47 for i in range(18)]

        # 2. GĂSIM ZONELE ZILELOR (Slicing Vertical)
        day_zones = []
        for w in words:
            txt_upper = w['text'].upper()
            for de_key, ro_val in DAY_MARKERS.items():
                if de_key in txt_upper:
                    day_zones.append({"day": ro_val, "top": w['top']})
                    break
        day_zones.sort(key=lambda x: x['top'])

        # 3. EXTRAGEM TABELELE FOLOSIND GRID-UL EXPLICIT
        for i, zone in enumerate(day_zones):
            day_name = zone['day']
            y_start = zone['top']
            y_end = day_zones[i+1]['top'] if i+1 < len(day_zones) else page.height

            print(f"--- Procesez {day_name} ---")

            # Decupăm zona zilei
            cropped = page.crop((0, y_start, width, y_end))

            # EXTRAGEM TABELUL CU LINII VERTICALE FORȚATE
            # Asta obligă pdfplumber să taie între "InfLb1-Dr" și "Eng-Su"
            table_settings = {
                "vertical_strategy": "explicit",
                "explicit_vertical_lines": vertical_lines,
                "horizontal_strategy": "text", # Lăsăm textul să definească rândurile
                "snap_tolerance": 3
            }

            try:
                table = cropped.extract_table(table_settings)
            except Exception as e:
                print(f"Eroare la extragere tabel: {e}")
                continue

            if not table: continue

            # Parsăm rândurile tabelului extras
            for row in table:
                # Curățăm celulele
                clean_row = [clean_text(cell) for cell in row]
                
                # Verificăm dacă rândul începe cu o oră validă
                # Uneori prima coloană e goală sau conține gunoi, verificăm primele 2
                time_slot = None
                if clean_row and is_time_slot(clean_row[0]):
                    time_slot = clean_row[0]
                
                if not time_slot: continue

                # Mapăm materiile la clase
                # clean_row[0] = Time
                # clean_row[1] = 9A, clean_row[2] = 9B ...
                
                for col_idx, subject in enumerate(clean_row[1:]):
                    if col_idx >= len(CLASS_HEADERS): break
                    
                    class_name = CLASS_HEADERS[col_idx]
                    
                    # Ignorăm celulele goale sau repetarea numelui clasei
                    if len(subject) < 2: continue
                    if subject in CLASS_HEADERS: continue 

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
        print("Empty.")
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
