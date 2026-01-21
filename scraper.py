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

def is_time_slot(text):
    # Caută formate de genul 7:20, 08:00 (strict cu :)
    if not text: return False
    return bool(re.search(r'^\d{1,2}:\d{2}', str(text).strip()))

def parse_pdf(pdf_path):
    final_schedule = {}

    with pdfplumber.open(pdf_path) as pdf:
        # Scanăm toate paginile (de obicei e una singură lungă)
        for page in pdf.pages:
            # 1. EXTRACT WORDS CU TOLERANȚĂ ZERO
            # x_tolerance=0 este CRITIC. Împiedică lipirea cuvintelor din coloane vecine.
            # Dacă între "Mat" și "Bio" e un spațiu mic, le va vedea separat.
            words = page.extract_words(x_tolerance=0, y_tolerance=3)
            
            # 2. IDENTIFICĂ ZONELE ZILELOR (Slicing Vertical)
            day_zones = []
            for w in words:
                text_upper = w['text'].upper()
                for de_key, ro_val in DAY_MARKERS.items():
                    if de_key in text_upper:
                        day_zones.append({"day": ro_val, "top": w['top']})
                        break
            day_zones.sort(key=lambda x: x['top'])
            
            if not day_zones: continue

            # Procesăm fiecare zi
            for i, zone in enumerate(day_zones):
                current_day = zone['day']
                zone_top = zone['top']
                # Zona se termină la următoarea zi sau la finalul paginii
                zone_bottom = day_zones[i+1]['top'] if i+1 < len(day_zones) else page.height
                
                # Luăm doar cuvintele din această zi
                day_words = [w for w in words if zone_top <= w['top'] < zone_bottom]
                if not day_words: continue

                # 3. CONSTRUIM GRILA DE COLOANE (X-Axis)
                # Căutăm header-ul claselor SPECIFIC acestei zile (ex: rândul cu 9A, 9B...)
                header_words = [w for w in day_words if w['text'] in CLASS_HEADERS]
                header_words.sort(key=lambda w: w['x0'])
                
                col_map = []
                
                # Dacă găsim header-ul, calculăm granițele precise
                if len(header_words) >= 5:
                    for k in range(len(header_words)):
                        curr = header_words[k]
                        cls_name = curr['text']
                        
                        # Limita stângă: mijlocul distanței față de clasa anterioară
                        if k == 0:
                            start_x = curr['x0'] - 15
                        else:
                            prev = header_words[k-1]
                            start_x = (prev['x1'] + curr['x0']) / 2
                        
                        # Limita dreaptă: mijlocul distanței față de clasa următoare
                        if k == len(header_words) - 1:
                            end_x = curr['x1'] + 15
                        else:
                            nxt = header_words[k+1]
                            end_x = (curr['x1'] + nxt['x0']) / 2
                            
                        col_map.append({"class": cls_name, "min": start_x, "max": end_x})
                else:
                    # Fallback (dacă nu găsește header-ul, deși ar trebui)
                    # Estimare bazată pe lățimea paginii A4 Landscape
                    curr_x = 55
                    width = 47.5
                    for cls in CLASS_HEADERS:
                        col_map.append({"class": cls, "min": curr_x, "max": curr_x + width})
                        curr_x += width

                # 4. IDENTIFICĂM RÂNDURILE DE ORE (Y-Axis)
                time_rows = []
                for w in day_words:
                    if is_time_slot(w['text']):
                        # Verificăm duplicatele (același rând vizual)
                        is_dup = False
                        for tr in time_rows:
                            if abs(tr['top'] - w['top']) < 8: # Dacă e la +/- 8px
                                is_dup = True; break
                        if not is_dup:
                            time_rows.append(w)
                time_rows.sort(key=lambda x: x['top'])

                # 5. ALOCARE (Match Words to Cells)
                for w in day_words:
                    text = w['text']
                    
                    # Filtre: ignorăm orele, headerele, datele calendaristice, gunoiul
                    if is_time_slot(text) or text in CLASS_HEADERS or len(text) < 2: continue
                    if any(x in text.upper() for x in ["MONTAG", "DIENSTAG", "MITTWOCH", "DONNERSTAG", "FREITAG", "LUNI", "MARTI"]): continue
                    
                    # Găsim ORA (Rândul)
                    found_time = None
                    word_mid_y = (w['top'] + w['bottom']) / 2
                    
                    for tr in time_rows:
                        row_mid_y = (tr['top'] + tr['bottom']) / 2
                        # Toleranță verticală generoasă (12px) pentru a prinde textele ușor decalate
                        if abs(word_mid_y - row_mid_y) < 12:
                            found_time = tr['text']
                            break
                    
                    if not found_time: continue

                    # Găsim CLASA (Coloana)
                    # Folosim centrul orizontal al cuvântului pentru precizie
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
                    
                    # Evităm duplicatele
                    if entry not in final_schedule[found_class][current_day]:
                        final_schedule[found_class][current_day].append(entry)

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
