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
    if not text: return False
    # FIX: Căutăm DOAR formatul cu două puncte (7:20), nu cu punct (19.01)
    # Asta previne detectarea datei ca fiind o oră.
    return bool(re.search(r'^\d{1,2}:\d{2}', str(text).strip()))

def get_vertical_overlap(r1, r2):
    # Calculează cât de mult se suprapun două elemente pe verticală
    # r1, r2 sunt dict-uri cu 'top' și 'bottom'
    return max(0, min(r1['bottom'], r2['bottom']) - max(r1['top'], r2['top']))

def parse_pdf(pdf_path):
    final_schedule = {}

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            # Toleranță mică pentru a nu lipi coloanele
            words = page.extract_words(x_tolerance=1, y_tolerance=1)
            
            # 1. IDENTIFICĂ ZONELE ZILELOR
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
                
                # Cuvintele din această zi
                day_words = [w for w in words if zone_top <= w['top'] < zone_bottom]
                if not day_words: continue

                # 2. GĂSIRE RÂNDURI (ORE)
                # Căutăm doar elementele care sunt ore valide (ex: 7:20)
                time_rows = []
                for w in day_words:
                    if is_time_slot(w['text']):
                        # Verificăm duplicatele (aceeași oră detectată de mai multe ori)
                        is_duplicate = False
                        for tr in time_rows:
                            # Dacă se suprapun semnificativ pe verticală, e același rând
                            overlap = get_vertical_overlap(tr, w)
                            if overlap > 5: 
                                is_duplicate = True
                                break
                        if not is_duplicate:
                            time_rows.append(w)
                
                time_rows.sort(key=lambda x: x['top'])

                # 3. GĂSIRE COLOANE (CLASE) - HARTA DINAMICĂ
                # Căutăm header-ul cu clasele (9A, 9B...) din această zonă
                header_words = [w for w in day_words if w['text'] in ORDERED_CLASSES]
                header_words.sort(key=lambda w: w['x0'])
                
                col_map = []
                if len(header_words) >= 5:
                    for k in range(len(header_words)):
                        curr = header_words[k]
                        cls_name = curr['text']
                        
                        start_x = (header_words[k-1]['x1'] + curr['x0']) / 2 if k > 0 else curr['x0'] - 10
                        end_x = (curr['x1'] + header_words[k+1]['x0']) / 2 if k < len(header_words)-1 else curr['x1'] + 50
                        
                        col_map.append({"class": cls_name, "min": start_x, "max": end_x})
                else:
                    # Fallback
                    curr_x = 55
                    width = 47.5
                    for cls in ORDERED_CLASSES:
                        col_map.append({"class": cls, "min": curr_x, "max": curr_x + width})
                        curr_x += width

                # 4. ASIGNARE MATERII
                for w in day_words:
                    text = w['text']
                    if is_time_slot(text) or text in ORDERED_CLASSES or len(text) < 2: continue
                    if "MONTAG" in text.upper() or "LUNI" in text.upper(): continue

                    # Găsim Ora (Rândul) prin SUPRAPUNERE
                    found_time = None
                    best_overlap = 0
                    
                    for tr in time_rows:
                        overlap = get_vertical_overlap(tr, w)
                        # Trebuie să se suprapună măcar 50% din înălțime
                        if overlap > (w['bottom'] - w['top']) * 0.5:
                            if overlap > best_overlap:
                                best_overlap = overlap
                                found_time = tr['text']
                    
                    if not found_time: continue

                    # Găsim Clasa (Coloana)
                    word_center_x = (w['x0'] + w['x1']) / 2
                    found_class = None
                    for col in col_map:
                        if col['min'] <= word_center_x < col['max']:
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
    print("Done.")

if __name__ == "__main__":
    main()
