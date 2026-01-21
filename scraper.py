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

# Clasele pe care le căutăm în antet pentru a stabili coloanele
TARGET_CLASSES = [
    "9A", "9B", "9C", "9D", 
    "10A", "10B", "10C", "10D", 
    "11A", "11B", "11C", "11D", 
    "12A", "12B", "12C", "12D"
]

# Mapare Zile (Germana -> Romana)
DAY_HEADERS = {
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

def is_time_slot(text):
    if not text: return False
    return bool(re.search(r'^\d{1,2}[:.]\d{2}', str(text).strip()))

def parse_pdf(pdf_path):
    final_schedule = {}

    with pdfplumber.open(pdf_path) as pdf:
        # Procesăm fiecare pagină (chiar dacă e una singură)
        for page_idx, page in enumerate(pdf.pages):
            print(f"--- Scanning Page {page_idx + 1} ---")

            words = page.extract_words(x_tolerance=2, y_tolerance=2)
            
            # 1. GĂSIRE ZONE ZILE (Vertical)
            day_zones = []
            for w in words:
                text_upper = w['text'].upper()
                for de_key, ro_val in DAY_HEADERS.items():
                    if de_key in text_upper:
                        day_zones.append({"day": ro_val, "top": w['top']})
                        break
            day_zones.sort(key=lambda x: x['top'])
            
            if not day_zones:
                print("   -> Nu am găsit zile pe această pagină.")
                continue

            # 2. GĂSIRE COLOANE CLASE (Orizontal - Dinamic)
            # Căutăm rândul care conține "9A", "9B", etc.
            # Facem o medie a coordonatelor X pentru a stabili granițele
            
            column_map = [] # Va reține {class: "9A", x_start: 50, x_end: 90}
            
            # Căutăm cuvintele care sunt nume de clase
            header_words = [w for w in words if w['text'] in TARGET_CLASSES]
            header_words.sort(key=lambda w: w['x0']) # Sortăm de la stânga la dreapta

            # Dacă nu găsim antetul, folosim valorile hardcodate (fallback)
            if len(header_words) < 5:
                print("   -> Antetul claselor nu e clar. Folosesc estimare.")
                current_x = 55
                width = 47.5
                for cls in TARGET_CLASSES:
                    column_map.append({
                        "class": cls,
                        "x_start": current_x,
                        "x_end": current_x + width
                    })
                    current_x += width
            else:
                # Construim harta exactă bazată pe poziția textului "9A", "9B"...
                for i in range(len(header_words)):
                    current_header = header_words[i]
                    cls_name = current_header['text']
                    
                    # Începutul coloanei este la jumătatea distanței față de coloana anterioară
                    if i == 0:
                        x_start = current_header['x0'] - 10 # Marjă stânga
                    else:
                        prev_header = header_words[i-1]
                        # Punctul de mijloc între sfârșitul clasei anterioare și începutul clasei curente
                        x_start = (prev_header['x1'] + current_header['x0']) / 2
                    
                    # Sfârșitul coloanei
                    if i == len(header_words) - 1:
                        x_end = current_header['x1'] + 50 # Marjă dreapta
                    else:
                        next_header = header_words[i+1]
                        x_end = (current_header['x1'] + next_header['x0']) / 2
                    
                    column_map.append({
                        "class": cls_name,
                        "x_start": x_start,
                        "x_end": x_end
                    })

            # 3. PROCESARE RÂNDURI ORARE
            rows = {}
            for w in words:
                y = round(w['top'] / 10) * 10 
                if y not in rows: rows[y] = []
                rows[y].append(w)

            for y in sorted(rows.keys()):
                # Aflăm ziua curentă
                current_day = None
                for i in range(len(day_zones)):
                    zone = day_zones[i]
                    # Dacă suntem sub header-ul zilei curente și deasupra următoarei
                    next_zone_top = day_zones[i+1]['top'] if i+1 < len(day_zones) else 99999
                    if y >= zone['top'] and y < next_zone_top:
                        current_day = zone['day']
                        break
                
                if not current_day: continue

                # Sortăm cuvintele
                row_words = sorted(rows[y], key=lambda w: w['x0'])
                if not row_words: continue

                # Verificăm dacă e rând de orar (începe cu oră)
                first_text = row_words[0]['text']
                if not is_time_slot(first_text): continue
                
                time_slot = first_text

                # Alocăm materiile în funcție de harta coloanelor calculată mai sus
                for word in row_words[1:]: # Sărim peste oră
                    text = word['text']
                    x_center = (word['x0'] + word['x1']) / 2 # Folosim centrul cuvântului
                    
                    found_class = None
                    for col in column_map:
                        if col['x_start'] <= x_center < col['x_end']:
                            found_class = col['class']
                            break
                    
                    if not found_class: continue
                    if len(text) < 2: continue
                    if text in TARGET_CLASSES: continue # Ignorăm antetele repetate

                    # Salvare
                    if found_class not in final_schedule: final_schedule[found_class] = {}
                    if current_day not in final_schedule[found_class]: final_schedule[found_class][current_day] = []
                    
                    entry = f"{time_slot} | {text}"
                    
                    if entry not in final_schedule[found_class][current_day]:
                        final_schedule[found_class][current_day].append(entry)

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
    print("Success.")

if __name__ == "__main__":
    main()
