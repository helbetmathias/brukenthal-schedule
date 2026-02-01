import pdfplumber
import requests
import json
import re
import hashlib
import os
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

RO_TZ = ZoneInfo("Europe/Bucharest")
URL = "https://brukenthal.ro/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
OUTPUT_FILE = "timetable.json"

WORKER_NOTIFY_URL = "https://shrill-tooth-d37a.ronzigamespro2007.workers.dev/notify"
WORKER_AUTH_KEY = os.getenv("WORKER_AUTH_KEY", "")

# Configurații specifice pentru coloane
COLS_LICEU = [
    "Time",
    "9A", "9B", "9C", "9D",
    "10A", "10B", "10C", "10D",
    "11A", "11B", "11C", "11D",
    "12A", "12B", "12C", "12D",
]

COLS_GIMNAZIU = [
    "Time",
    "5A", "5B", "5C", "5D",
    "6A", "6B", "6C", "6D",
    "7A", "7B", "7C", "7D",
    "8A", "8B", "8C", "8D",
]

DAY_MARKERS = {
    "MONTAG": "Luni",
    "DIENSTAG": "Marti",
    "MITTWOCH": "Miercuri",
    "DONNERSTAG": "Joi",
    "FREITAG": "Vineri",
}

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$")

def get_latest_pdf_urls():
    """Găsește URL-urile pentru Liceu și Gimnaziu."""
    try:
        html = requests.get(URL, headers=HEADERS, timeout=30).text
    except Exception:
        return {}

    pdfs = re.findall(r'href="([^"]+\.pdf)"', html, flags=re.IGNORECASE)
    found = {"liceu": [], "gimnaziu": []}
    
    # Funcție de sortare (după cifrele din URL - dată/versiune)
    def score(url):
        nums = re.findall(r"\d+", url)
        return [int(n) for n in nums] if nums else [0]

    for href in pdfs:
        full_url = urljoin(URL, href)
        lower = href.lower()
        if "liceu" in lower:
            found["liceu"].append(full_url)
        elif "gimnaziu" in lower:
            found["gimnaziu"].append(full_url)

    result = {}
    if found["liceu"]:
        found["liceu"].sort(key=score, reverse=True)
        result["liceu"] = found["liceu"][0]
    
    if found["gimnaziu"]:
        found["gimnaziu"].sort(key=score, reverse=True)
        result["gimnaziu"] = found["gimnaziu"][0]

    return result

def cluster_positions(values, tol=2.0):
    """Grupează liniile verticale care sunt foarte apropiate."""
    values = sorted(values)
    clusters = []
    for v in values:
        if not clusters or abs(v - clusters[-1][-1]) > tol:
            clusters.append([v])
        else:
            clusters[-1].append(v)
    return [sum(c) / len(c) for c in clusters]

def get_global_x_bounds(page, expected_cols_count):
    """
    Detectează limitele coloanelor (X).
    FIX: Dacă lipsește ultima linie din dreapta, o prezice.
    """
    verts = [e for e in page.edges if e["orientation"] == "v"]
    xs = [e["x0"] for e in verts]
    x_bounds = sorted(cluster_positions(xs, tol=2.0))

    # Avem nevoie de N+1 linii pentru N coloane
    needed = expected_cols_count + 1 
    
    # 1. Căutăm exact setul de linii necesar
    best = None
    if len(x_bounds) >= needed:
        for i in range(0, len(x_bounds) - (needed - 1)):
            cand = x_bounds[i : i + needed]
            width = cand[-1] - cand[0]
            # Alegem setul cel mai lat (care acoperă probabil toată pagina)
            if best is None or width > best[0]:
                best = (width, cand)
    
    if best:
        return best[1]
    
    # 2. FALLBACK: Dacă avem N linii (lipsește ultima bordură din dreapta)
    # Asta rezolvă problema cu 12D/8D dacă PDF-ul e tăiat prost
    if len(x_bounds) >= needed - 1:
        # Încercăm să găsim un set de N linii care par a fi coloane
        candidate = x_bounds[-(needed-1):] # Luăm ultimele N linii disponibile
        
        # Calculăm lățimea medie a coloanelor existente
        avg_width = (candidate[-1] - candidate[0]) / (len(candidate) - 1)
        
        # Dacă lățimea pare ok (>20px), adăugăm manual ultima linie
        if avg_width > 20:
            candidate.append(candidate[-1] + avg_width)
            print(f"⚠️ Warning: Added virtual right border at {candidate[-1]}")
            return candidate

    return x_bounds

def get_y_bounds_for_crop(page_crop):
    horiz = [e for e in page_crop.edges if e["orientation"] == "h"]
    ys = [e["top"] for e in horiz]
    return sorted(cluster_positions(ys, tol=1.5))

def normalize_subject(subj: str) -> str:
    subj = (subj or "").strip()
    subj = re.sub(r"\s+", " ", subj)
    if re.fullmatch(r"[a-z]", subj): return ""
    subj = re.sub(r"^[a-z](?=[A-Z0-9ĂÂÎȘȚ])", "", subj).strip()
    return subj if len(subj) >= 2 else ""

def cell_text_from_chars(chars, x0, x1, y0, y1):
    # Marje mici pentru a evita suprapunerile
    sx0, sx1 = x0 + 1.5, x1 - 0.5
    sy0, sy1 = y0 + 0.5, y1 - 0.5

    sel = [c for c in chars if sx0 < (c["x0"]+c["x1"])/2 < sx1 and sy0 < (c["top"]+c["bottom"])/2 < sy1]
    if not sel: return ""

    sel.sort(key=lambda c: (c["top"], c["x0"]))
    
    # Reconstruim textul linie cu linie
    lines = []
    cur = []
    cur_top = None
    for ch in sel:
        if cur_top is None or abs(ch["top"] - cur_top) <= 2.0:
            cur.append(ch)
            cur_top = ch["top"] if cur_top is None else (cur_top * 0.7 + ch["top"] * 0.3)
        else:
            lines.append(cur)
            cur = [ch]
            cur_top = ch["top"]
    if cur: lines.append(cur)

    out_lines = []
    for line in lines:
        line.sort(key=lambda c: c["x0"])
        s = "".join([c["text"] for c in line]) # Simplificat, uneori spațiile sunt tricky
        out_lines.append(s.strip())

    return " ".join(out_lines).strip()

def parse_day_block(day_crop, x_bounds, columns_cfg):
    y_bounds = get_y_bounds_for_crop(day_crop)
    if len(y_bounds) < 3: return {}

    chars = day_crop.chars
    n_rows = len(y_bounds) - 1
    
    # Construim grila
    grid = []
    for r in range(n_rows):
        row_data = []
        ry0, ry1 = y_bounds[r], y_bounds[r+1]
        for c in range(len(x_bounds) - 1):
            cx0, cx1 = x_bounds[c], x_bounds[c+1]
            row_data.append(cell_text_from_chars(chars, cx0, cx1, ry0, ry1))
        grid.append(row_data)

    # Identificăm rândul cu clasele (Header)
    header_r = -1
    best_score = 0
    for r in range(min(5, len(grid))):
        # Numărăm câte clase cunoscute apar în rândul acesta
        score = sum(1 for txt in grid[r] if normalize_subject(txt) in columns_cfg)
        if score > best_score:
            best_score = score
            header_r = r
    
    if header_r == -1 or best_score < 2:
        return {}

    # Mapăm indexul coloanei la numele Clasei
    col_map = {}
    for c_idx, text in enumerate(grid[header_r]):
        clean = normalize_subject(text)
        if clean in columns_cfg:
            col_map[c_idx] = clean
        # Fallback: dacă textul e gol dar poziția corespunde cu config-ul
        elif c_idx + 1 < len(columns_cfg): 
             # Presupunem că ordinea e păstrată (Time, 9A, 9B...)
             col_map[c_idx] = columns_cfg[c_idx + 1]

    schedule = {cls: [] for cls in columns_cfg[1:]}
    
    for r in range(header_r + 1, n_rows):
        row_items = grid[r]
        time_txt = row_items[0] if row_items else ""
        
        if not TIME_RE.match(time_txt):
            continue

        for c_idx, subj_raw in enumerate(row_items):
            if c_idx not in col_map: continue
            cls_name = col_map[c_idx]
            
            subj = normalize_subject(subj_raw)
            if subj and subj not in columns_cfg:
                # Format: "Ora | Materie"
                schedule[cls_name].append(f"{time_txt} | {subj}")

    return {k: v for k, v in schedule.items() if v}

def parse_pdf(pdf_path, columns_cfg):
    final = {}
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        
        # Detectăm coloanele (cu fallback pentru ultima coloană)
        x_bounds = get_global_x_bounds(page, len(columns_cfg) - 1)
        
        # Găsim zonele pentru zile (LUNI, MARTI...)
        words = page.extract_words()
        zones = []
        for w in words:
            if w["text"].upper() in DAY_MARKERS:
                zones.append({"name": DAY_MARKERS[w["text"].upper()], "top": w["top"]})
        zones.sort(key=lambda z: z["top"])

        for i, z in enumerate(zones):
            day_name = z["name"]
            y_start = z["top"] - 5
            y_end = zones[i+1]["top"] - 10 if i+1 < len(zones) else page.height
            
            crop = page.crop((0, y_start, page.width, y_end))
            day_data = parse_day_block(crop, x_bounds, columns_cfg)
            
            for cls, lessons in day_data.items():
                if cls not in final: final[cls] = {}
                if day_name not in final[cls]: final[cls][day_name] = []
                final[cls][day_name].extend(lessons)
                
                # Eliminăm duplicatele
                final[cls][day_name] = list(dict.fromkeys(final[cls][day_name]))

    return final

def main():
    urls = get_latest_pdf_urls()
    if not urls:
        print("Nu am găsit PDF-uri.")
        return

    full_schedule = {}
    
    # Descărcăm și parsăm LICEUL
    if "liceu" in urls:
        print(f"Descarc Liceu: {urls['liceu']}")
        with open("temp_liceu.pdf", "wb") as f:
            f.write(requests.get(urls["liceu"], headers=HEADERS).content)
        try:
            data = parse_pdf("temp_liceu.pdf", COLS_LICEU)
            full_schedule.update(data)
            print(f"Liceu parsat: {len(data)} clase.")
        except Exception as e:
            print(f"Eroare Liceu: {e}")
            
    # Descărcăm și parsăm GIMNAZIUL
    if "gimnaziu" in urls:
        print(f"Descarc Gimnaziu: {urls['gimnaziu']}")
        with open("temp_gimnaziu.pdf", "wb") as f:
            f.write(requests.get(urls["gimnaziu"], headers=HEADERS).content)
        try:
            data = parse_pdf("temp_gimnaziu.pdf", COLS_GIMNAZIU)
            full_schedule.update(data)
            print(f"Gimnaziu parsat: {len(data)} clase.")
        except Exception as e:
            print(f"Eroare Gimnaziu: {e}")

    # Curățenie
    for f in ["temp_liceu.pdf", "temp_gimnaziu.pdf"]:
        if os.path.exists(f): os.remove(f)

    # Salvare JSON
    output = {
        "updated_at": datetime.now(RO_TZ).strftime("%d.%m.%Y %H:%M"),
        "source_pdf": list(urls.values()),
        "schedule": full_schedule
    }
    
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print("timetable.json generat cu succes!")

if __name__ == "__main__":
    main()
