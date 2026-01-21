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

# Ordinea exactă a coloanelor din tabel
COLUMNS_ORDER = [
    "Time", 
    "9A", "9B", "9C", "9D", 
    "10A", "10B", "10C", "10D", 
    "11A", "11B", "11C", "11D", 
    "12A", "12B", "12C", "12D"
]

# Mapare Zile
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
    # Căutăm doar formatul de oră valid (ex: 7:20, 8:00)
    # Excludem datele calendaristice (19.01) care au punct
    if not text: return False
    return bool(re.search(r'^\d{1,2}:\d{2}', str(text).strip()))

def parse_pdf(pdf_path):
    final_schedule = {}

    with pdfplumber.open(pdf_path) as pdf:
        # Presupunem că totul este pe Pagina 1 (conform imaginii tale)
        # Dacă sunt mai multe, le iterăm, dar ne concentrăm pe structura verticală
        page = pdf.pages[0]
        width = page.width
        height = page.height

        # 1. GĂSIM POZIȚIILE ZILELOR (Y-coordinates)
        words = page.extract_words(x_tolerance=2, y_tolerance=2)
        day_zones = []
        
        for w in words:
            txt_upper = w['text'].upper()
            for de_key, ro_val in DAY_MARKERS.items():
                if de_key in txt_upper:
                    # Am găsit un titlu de zi. Salvăm poziția de sus (top)
                    day_zones.append({"day": ro_val, "top": w['top']})
                    break
        
        # Le sortăm de sus în jos
        day_zones.sort(key=lambda x: x['top'])

        if not day_zones:
            print("Eroare: Nu am găsit zilele pe pagină.")
            return {}

        # 2. PROCESĂM FIECARE ZI PRIN DECUPARE (CROP)
        for i, zone in enumerate(day_zones):
            day_name = zone['day']
            y_start = zone['top']
            # Ziua se termină unde începe următoarea, sau la finalul paginii
            y_end = day_zones[i+1]['top'] if i+1 < len(day_zones) else height

            print(f"--- Procesez {day_name} (Y: {y_start:.0f} - {y_end:.0f}) ---")

            # Decupăm zona respectivă din pagină
            # Bounding box: (x0, top, x1, bottom)
            try:
                cropped_page = page.crop((0, y_start, width, y_end))
            except ValueError:
                continue # Skip if dimensions invalid

            # 3. EXTRAGEM TABELUL DIN ZONA DECUPATĂ
            # Folosim setări care favorizează liniile vizibile
            table_settings = {
                "vertical_strategy": "lines", 
                "horizontal_strategy": "lines",
                "intersection_y_tolerance": 5,
                "intersection_x_tolerance": 5,
                "snap_tolerance": 3
            }
            
            tables = cropped_page.extract_tables(table_settings)
            
            # Dacă nu găsește tabele pe bază de linii, încercăm fallback pe text
            if not tables:
                table_settings["vertical_strategy"] = "text"
                table_settings["horizontal_strategy"] = "text"
                tables = cropped_page.extract_tables(table_settings)

            for table in tables:
                for row in table:
                    # Curățăm rândul
                    clean_row = [clean_text(cell) for cell in row]
                    
                    # Verificăm dacă rândul e valid (începe cu oră)
                    # Uneori extract_table returnează rânduri goale sau headere
                    if not clean_row or len(clean_row) < 2: continue
                    
                    time_slot = clean_row[0]
                    if not is_time_slot(time_slot): continue

                    # 4. MAPĂM DATELE
                    # Coloana 0 e Ora. Coloanele 1..16 sunt Clasele.
                    for col_idx, subject in enumerate(clean_row[1:], start=1):
                        if col_idx >= len(COLUMNS_ORDER): break
                        
                        class_name = COLUMNS_ORDER[col_idx]
                        
                        if len(subject) < 2: continue # Ignorăm celule goale
                        if "9A" in subject: continue # Ignorăm rândul cu numele claselor

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
