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

# Cloudflare Worker notify endpoint
WORKER_NOTIFY_URL = "https://shrill-tooth-d37a.ronzigamespro2007.workers.dev/notify"
WORKER_AUTH_KEY = os.getenv("WORKER_AUTH_KEY", "")

DAY_MARKERS = {
    "MONTAG": "Luni",
    "DIENSTAG": "Marti",
    "MITTWOCH": "Miercuri",
    "DONNERSTAG": "Joi",
    "FREITAG": "Vineri",
}

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$")
CLASS_RE = re.compile(r"^(?:[5-9]|1[0-2])[A-D]$", re.IGNORECASE)


def score_url_numbers(url: str):
    nums = re.findall(r"\d+", url)
    return [int(n) for n in nums] if nums else [0]


def get_latest_pdf_urls():
    """
    Returnează dict cu chei: 'liceu' / 'gimnaziu' (dacă găsește).
    Alege "cel mai nou" după numerele din URL.
    """
    html = requests.get(URL, headers=HEADERS, timeout=30).text
    pdfs = re.findall(r'href="([^"]+\.pdf)"', html, flags=re.IGNORECASE)
    if not pdfs:
        return {}

    groups = {"liceu": [], "gimnaziu": []}

    for href in pdfs:
        low = href.lower()
        full = urljoin(URL, href)

        if "liceu" in low:
            groups["liceu"].append(full)

        # prinde și "gimnaziu" / "gimnaziu-" / "gimnaz"
        if "gimnaziu" in low or "gimnaz" in low:
            groups["gimnaziu"].append(full)

    latest = {}
    for k, lst in groups.items():
        if not lst:
            continue
        lst.sort(key=score_url_numbers, reverse=True)
        latest[k] = lst[0]
    return latest


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def cluster_positions(values, tol=1.5):
    values = sorted(values)
    clusters = []
    for v in values:
        if not clusters or abs(v - clusters[-1][-1]) > tol:
            clusters.append([v])
        else:
            clusters[-1].append(v)
    return [sum(c) / len(c) for c in clusters]


def is_time_slot(s):
    s = (s or "").strip()
    return bool(TIME_RE.match(s))


def find_day_zones(page):
    words = page.extract_words(x_tolerance=2, y_tolerance=2)
    zones = []
    for w in words:
        t = w["text"].upper()
        if t in DAY_MARKERS:
            zones.append({"day": DAY_MARKERS[t], "top": w["top"], "bottom": w["bottom"]})
    zones.sort(key=lambda z: z["top"])
    return zones


def get_global_x_bounds(page):
    verts = [e for e in page.edges if e["orientation"] == "v"]
    xs = [e["x0"] for e in verts]
    x_bounds = sorted(cluster_positions(xs, tol=1.5))

    # în mod normal tabelul are 17 coloane => 18 bounds
    # dacă detectează prea multe linii, ia "fereastra" cea mai lată de 18 bounds
    if len(x_bounds) > 18:
        best = None
        for i in range(0, len(x_bounds) - 17):
            cand = x_bounds[i:i + 18]
            width = cand[-1] - cand[0]
            if best is None or width > best[0]:
                best = (width, cand)
        x_bounds = best[1]

    return x_bounds


def get_y_bounds_for_crop(page_crop):
    horiz = [e for e in page_crop.edges if e["orientation"] == "h"]
    ys = [e["top"] for e in horiz]
    y_bounds = sorted(cluster_positions(ys, tol=1.5))

    cleaned = []
    for y in y_bounds:
        if not cleaned or abs(y - cleaned[-1]) > 1.0:
            cleaned.append(y)
    return cleaned


def normalize_subject(subj: str) -> str:
    subj = (subj or "").strip()
    subj = re.sub(r"\s+", " ", subj)

    # un singur caracter e gunoi
    if re.fullmatch(r"[a-zA-Z]", subj):
        return ""

    # mai conservator: taie doar "litera mica + spatiu" (artefact frecvent)
    subj = re.sub(r"^[a-z]\s+(?=[A-Z0-9ĂÂÎȘȚ])", "", subj).strip()

    # elimină clasele lipite în capăt (ex: AJ-Ka9A)
    subj = re.sub(r"(?<=[A-Za-zĂÂÎȘȚăâîșț])((?:[5-9]|1[0-2])[A-D])$", "", subj, flags=re.IGNORECASE).strip()

    # elimină clase apărute separate (ex: 9B/Rev-J)
    subj = re.sub(r"(?:^|[\s,/])((?:[5-9]|1[0-2])[A-D])(?:$|[\s,/])", " ", subj, flags=re.IGNORECASE)
    subj = re.sub(r"\s+", " ", subj).strip()

    if len(subj) < 2:
        return ""
    return subj


def cell_text_from_chars(
    chars,
    x0, x1, y0, y1,
    y_tol=1.2,
    x_gap=1.0,
    x_pad_left=None,
    x_pad_right=None,
    y_pad=0.2
):
    # padding DINAMIC ca să nu taie prima literă la coloane înguste (gimnaziu)
    w = (x1 - x0)
    if x_pad_left is None:
        x_pad_left = max(0.15, min(0.6, w * 0.03))
    if x_pad_right is None:
        x_pad_right = max(0.10, min(0.45, w * 0.025))

    sx0 = x0 + x_pad_left
    sx1 = x1 - x_pad_right
    sy0 = y0 + y_pad
    sy1 = y1 - y_pad

    if sx1 <= sx0:
        sx0, sx1 = x0, x1
    if sy1 <= sy0:
        sy0, sy1 = y0, y1

    sel = []
    for ch in chars:
        cx = (ch["x0"] + ch["x1"]) / 2
        cy = (ch["top"] + ch["bottom"]) / 2
        if (sx0 < cx < sx1) and (sy0 < cy < sy1):
            sel.append(ch)

    if not sel:
        return ""

    sel.sort(key=lambda c: (c["top"], c["x0"]))

    lines = []
    cur = []
    cur_top = None
    for ch in sel:
        if cur_top is None or abs(ch["top"] - cur_top) <= y_tol:
            cur.append(ch)
            cur_top = ch["top"] if cur_top is None else (cur_top * 0.7 + ch["top"] * 0.3)
        else:
            lines.append(cur)
            cur = [ch]
            cur_top = ch["top"]
    if cur:
        lines.append(cur)

    out_lines = []
    for line in lines:
        line.sort(key=lambda c: c["x0"])
        s = ""
        prev = None
        for ch in line:
            if prev is not None and (ch["x0"] - prev["x1"]) > x_gap:
                s += " "
            s += ch["text"]
            prev = ch
        out_lines.append(s.strip())

    return re.sub(r"\s+", " ", " ".join([l for l in out_lines if l]).strip())


def detect_header_row_and_cols(grid, n_rows, n_cols):
    """
    Găsește rândul header și mapează coloane -> clasă pe baza label-urilor (5A..12D).
    """
    header_r = None
    best = -1
    best_cols = None

    for r in range(min(12, n_rows)):
        colmap = {}
        score = 0
        for c in range(1, n_cols):  # sar peste coloana de timp
            lab = (grid[r][c] or "").strip().upper()
            if CLASS_RE.match(lab):
                if lab not in colmap.values():  # evită dubluri
                    colmap[c] = lab
                    score += 1

        if score > best:
            best = score
            header_r = r
            best_cols = colmap

    # ca să fie valid, trebuie să detecteze măcar câteva clase
    if header_r is None or best < 6:
        return None, None

    return header_r, best_cols


def parse_day_block(day_crop, x_bounds):
    y_bounds = get_y_bounds_for_crop(day_crop)
    if len(y_bounds) < 5:
        return {}

    chars = day_crop.chars
    n_rows = len(y_bounds) - 1
    n_cols = len(x_bounds) - 1

    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for r in range(n_rows):
        ry0, ry1 = y_bounds[r], y_bounds[r + 1]
        for c in range(n_cols):
            cx0, cx1 = x_bounds[c], x_bounds[c + 1]
            grid[r][c] = cell_text_from_chars(chars, cx0, cx1, ry0, ry1)

    header_r, col_to_class = detect_header_row_and_cols(grid, n_rows, n_cols)
    if header_r is None:
        return {}

    day_schedule = {cls: [] for cls in col_to_class.values()}

    for r in range(header_r + 1, n_rows):
        time_txt = (grid[r][0] or "").strip()
        if not is_time_slot(time_txt):
            continue

        for c, cls in col_to_class.items():
            subj = normalize_subject(grid[r][c])
            if not subj:
                continue
            # FIX: nu acceptăm "materia" dacă de fapt e timp (bleed din coloana Time)
            if is_time_slot(subj):
                continue
            day_schedule[cls].append(f"{time_txt} | {subj}")

    return {k: v for k, v in day_schedule.items() if v}


def parse_pdf(pdf_path):
    final = {}

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        x_bounds = get_global_x_bounds(page)

        zones = find_day_zones(page)
        if not zones:
            raise RuntimeError("Could not find day headers (MONTAG/DIENSTAG/...).")

        for i, z in enumerate(zones):
            day_name = z["day"]
            y_start = max(0, z["top"] - 8)
            y_end = zones[i + 1]["top"] - 6 if i + 1 < len(zones) else page.height

            crop = page.crop((0, y_start, page.width, y_end))
            day_block = parse_day_block(crop, x_bounds)

            for cls, entries in day_block.items():
                final.setdefault(cls, {})
                final[cls].setdefault(day_name, [])
                for e in entries:
                    if e not in final[cls][day_name]:
                        final[cls][day_name].append(e)

    return final


def notify_worker(title, body, data):
    if not WORKER_AUTH_KEY:
        print("No WORKER_AUTH_KEY set, skipping notification.")
        return

    try:
        resp = requests.post(
            f"{WORKER_NOTIFY_URL}?key={WORKER_AUTH_KEY}",
            json={"title": title, "body": body, "data": data},
            timeout=30,
        )
        print("Worker notify:", resp.status_code, resp.text[:200])
    except Exception as e:
        print("Worker notify failed:", repr(e))


def main():
    pdf_urls = get_latest_pdf_urls()
    if not pdf_urls:
        print("No liceu/gimnaziu PDF links found on site.")
        return

    # citește vechiul json pentru comparație
    old_hashes = {}
    old_schedule = None
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old.get("sources"), dict):
                for k, v in old["sources"].items():
                    if isinstance(v, dict) and v.get("pdf_hash"):
                        old_hashes[k] = v["pdf_hash"]
            # compatibilitate cu format vechi (doar liceu)
            elif old.get("pdf_hash"):
                old_hashes["liceu"] = old.get("pdf_hash")
            old_schedule = old.get("schedule")
        except Exception:
            pass

    # descarcă + hash pentru fiecare pdf
    tmp_files = {}
    new_hashes = {}
    changed = []

    for kind, pdf_url in pdf_urls.items():
        resp = requests.get(pdf_url, headers=HEADERS, timeout=60)
        resp.raise_for_status()

        tmp = f"temp_{kind}.pdf"
        with open(tmp, "wb") as f:
            f.write(resp.content)

        h = file_hash(tmp)
        tmp_files[kind] = tmp
        new_hashes[kind] = h

        if old_hashes.get(kind) != h:
            changed.append(kind)

    # dacă nimic nu s-a schimbat, ieșim
    if not changed:
        print("PDFs unchanged, skipping update.")
        for p in tmp_files.values():
            try:
                os.remove(p)
            except OSError:
                pass
        return

    # parsează toate PDF-urile pe care le avem și unește în același schedule
    merged_schedule = {}

    for kind, tmp in tmp_files.items():
        try:
            sched = parse_pdf(tmp)
        except Exception as e:
            print(f"Parse failed for {kind}: {e!r}")
            sched = {}

        # merge la nivel de clasă
        for cls, days in sched.items():
            merged_schedule.setdefault(cls, {})
            for day, entries in days.items():
                merged_schedule[cls].setdefault(day, [])
                for e in entries:
                    if e not in merged_schedule[cls][day]:
                        merged_schedule[cls][day].append(e)

    out = {
        "updated_at": datetime.now(RO_TZ).strftime("%d.%m.%Y %H:%M"),
        "sources": {
            kind: {"source_pdf": pdf_urls[kind], "pdf_hash": new_hashes[kind]}
            for kind in pdf_urls.keys()
        },
        "schedule": merged_schedule,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    for p in tmp_files.values():
        try:
            os.remove(p)
        except OSError:
            pass

    print("Updated timetable.json | classes:", len(merged_schedule), "| changed:", changed)

    notify_worker(
        title="Schedule updated",
        body=f"Updated PDFs: {', '.join(changed)}. Open the app to refresh.",
        data={"updated_at": out["updated_at"], "changed": changed, "sources": out["sources"]},
    )


if __name__ == "__main__":
    main()
