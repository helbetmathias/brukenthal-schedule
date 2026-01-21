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

# Ordinea claselor
ORDERED_CLASSES = [
    "9A", "9B", "9C", "9D", 
    "10A", "10B", "10C", "10D", 
    "11A", "11B", "11C", "11D", 
    "12A", "12B", "12C", "12D"
]

# Mapare Zile
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
    # STRICT: Doar formatul 7:20 sau 12:00 (cu două puncte). 
    # Respinge 19.01 (cu punct).
    if not text: return False
    return bool(re.search(r'^\d{1,2}:\d{2}', str(text).strip()))

def parse_pdf(pdf_path):
    final_schedule = {}

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            # Extragem cuvintele
            words = page.extract_words(x_tolerance=2, y_tolerance=2)
            
            # 1. GĂSIRE ZONE ZILE (Slicing Vertical)
            day_zones = []
            for w in words:
                text_upper = w['text'].upper()
                for de_key, ro_val in DAY_HEADERS.items():
                    if de_key in text_upper:
                        day_zones.append({"day": ro_val, "top": w['top']})
                        break
            day_zones.sort(key=lambda x: x['top'])
            
            if not day_zones: continue

            # Procesăm fiecare zonă de zi
            for i, zone in enumerate(day_zones):
                current_day = zone['day']
                zone_top = zone['top']
                zone_bottom = day_zones[i+1]['top'] if i+1 < len(day_zones) else 9999
                
                # Filtrăm cuvintele pentru ziua curentă
                day_words = [w for w in words if zone_top <= w['top'] < zone_bottom]
                if not day_words: continue

                # 2. DETECTARE RÂNDURI (ORE)
                time_rows = []
                for w in day_words:
                    if is_time_slot(w['text']):
                        # Verificăm să nu fie duplicat (același rând vizual)
                        is_duplicate = False
                        for tr in time_rows:
                            if abs(tr['top'] - w['top']) < 10:
                                is_duplicate = True
                                break
                        if not is_duplicate:
                            time_rows.append(w)
                
                time_rows.sort(key=lambda x: x['top'])

                # 3. DETECTARE COLOANE (CLASE)
                # Căutăm headerele (9A, 9B...) pentru a stabili grila X
                header_words = [w for w in day_words if w['text'] in ORDERED_CLASSES]
                header_words.sort(key=lambda w: w['x0'])
                
                col_map = []
                if len(header_words) >= 5:
                    for k in range(len(header_words)):
                        curr = header_words[k]
                        cls_name = curr['text']
                        
                        # Calculăm granițele (mijlocul distanței dintre coloane)
                        # Start: mijlocul dintre clasa anterioară și cea curentă
                        if k == 0:
                            start_x = curr['x0'] - 20
                        else:
                            start_x = (header_words[k-1]['x1'] + curr['x0']) / 2
                        
                        # End: mijlocul dintre clasa curentă și cea următoare
                        if k == len(header_words) - 1:
                            end_x = curr['x1'] + 20
                        else:
                            end_x = (curr['x1'] + header_words[k+1]['x0']) / 2
                        
                        col_map.append({"class": cls_name, "min": start_x, "max": end_x})
                else:
                    # Fallback A4 Landscape
                    curr_x = 55
                    width = 47.5
                    for cls in ORDERED_CLASSES:
                        col_map.append({"class": cls, "min": curr_x, "max": curr_x + width})
                        curr_x += width

                # 4. ALOCARE (GRID MATCHING)
                for w in day_words:
                    text = w['text']
                    if is_time_slot(text) or text in ORDERED_CLASSES or len(text) < 2: continue
                    if "MONTAG" in text.upper() or "LUNI" in text.upper(): continue

                    # Găsim rândul (Ora) - Verificăm dacă cuvântul e pe aceeași linie cu ora
                    found_time = None
                    word_mid_y = (w['top'] + w['bottom']) / 2
                    
                    for tr in time_rows:
                        row_mid_y = (tr['top'] + tr['bottom']) / 2
                        # Toleranță verticală mai mare (15px) pentru a prinde și textele ușor decalate
                        if abs(word_mid_y - row_mid_y) < 15:
                            found_time = tr['text']
                            break
                    
                    if not found_time: continue

                    # Găsim coloana (Clasa)
                    word_mid_x = (w['x0'] + w['x1']) / 2
                    found_class = None
                    for col in col_map:
                        if col['min'] <= word_mid_x < col['max']:
                            found_class = col['class']
                            break
                    
                    if not found_class: continue

                    # Salvare
                    if found_class not in final_schedule: final_schedule[found_class] = {}
                    if current_day not in final_schedule[found_class]: final_schedule[found_class][current_day] = []
                    
                    entry = f"{found_time} | {text}"
                    if entry not in final_schedule[found_class][current_day]:
                        final_schedule[found_class][current_day].append(entry)

    return final_schedule

def main():
    print("Starting Scraper...")
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
    print("Done.")

if __name__ == "__main__":
    main()
