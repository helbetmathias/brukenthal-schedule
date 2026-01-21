import pdfplumber
import requests
import json
import re
from datetime import datetime
from urllib.parse import urljoin

URL = "https://brukenthal.ro/"
HEADERS = {"User-Agent": "Mozilla/5.0"}
OUTPUT_FILE = "timetable.json"

COLUMNS_ORDER = [
    "Time",
    "9A", "9B", "9C", "9D",
    "10A", "10B", "10C", "10D",
    "11A", "11B", "11C", "11D",
    "12A", "12B", "12C", "12D",
]

DAY_MARKERS = {
    "MONTAG": "Luni",
    "DIENSTAG": "Marti",
    "MITTWOCH": "Miercuri",
    "DONNERSTAG": "Joi",
    "FREITAG": "Vineri",
}

TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$")


def get_latest_pdf_url():
    html = requests.get(URL, headers=HEADERS, timeout=30).text
    m = re.search(r'href="([^"]*orarliceu[^"]*\.pdf)"', html, re.IGNORECASE)
    if not m:
        return None
    return urljoin(URL, m.group(1))


def cluster_positions(values, tol=1.5):
    """Cluster numeric positions that are within `tol`."""
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
    # Grid vertical lines are in edges
    verts = [e for e in page.edges if e["orientation"] == "v"]
    xs = [e["x0"] for e in verts]
    x_bounds = sorted(cluster_positions(xs, tol=1.5))

    # Expect 18 boundaries (17 columns => 18 borders)
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
    """
    IMPORTANT: Use edge["top"] (same coordinate system as words/chars top/bottom),
    NOT y0/y1 (PDF bottom-origin space).
    """
    horiz = [e for e in page_crop.edges if e["orientation"] == "h"]
    ys = [e["top"] for e in horiz]  # ✅ FIX
    y_bounds = sorted(cluster_positions(ys, tol=1.5))

    cleaned = []
    for y in y_bounds:
        if not cleaned or abs(y - cleaned[-1]) > 1.0:
            cleaned.append(y)
    return cleaned


def cell_text_from_chars(chars, x0, x1, y0, y1, y_tol=1.2, x_gap=1.0):
    """
    Build text for a single cell by selecting PDF 'chars' whose center lies inside the cell bbox.
    This avoids cross-column word merging that extract_words can do.
    """
    sel = []
    for ch in chars:
        cx = (ch["x0"] + ch["x1"]) / 2
        cy = (ch["top"] + ch["bottom"]) / 2
        if (x0 <= cx <= x1) and (y0 <= cy <= y1):
            sel.append(ch)

    if not sel:
        return ""

    # Sort roughly by line then by x
    sel.sort(key=lambda c: (c["top"], c["x0"]))

    # Group into lines by similar 'top'
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

    # Join each line left-to-right; add spaces if there's a gap
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


def parse_day_block(day_crop, x_bounds):
    y_bounds = get_y_bounds_for_crop(day_crop)
    if len(y_bounds) < 5:
        return {}

    chars = day_crop.chars
    n_rows = len(y_bounds) - 1
    n_cols = len(x_bounds) - 1

    # Build grid by reading chars inside each cell bbox
    grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    for r in range(n_rows):
        ry0, ry1 = y_bounds[r], y_bounds[r + 1]
        for c in range(n_cols):
            cx0, cx1 = x_bounds[c], x_bounds[c + 1]
            grid[r][c] = cell_text_from_chars(chars, cx0, cx1, ry0, ry1)

    # Find header row: row that contains the most class labels
    header_r = None
    best_score = -1
    for r in range(min(10, n_rows)):
        score = sum(1 for lab in COLUMNS_ORDER[1:] if lab in grid[r])
        if score > best_score:
            best_score = score
            header_r = r

    if header_r is None or best_score < 5:
        return {}

    # Map col index -> class name (col 0 is Time)
    col_to_class = {c: COLUMNS_ORDER[c] for c in range(1, min(17, n_cols))}

    # Parse rows below header
    day_schedule = {cls: [] for cls in COLUMNS_ORDER[1:]}
    for r in range(header_r + 1, n_rows):
        time_txt = (grid[r][0] or "").strip()
        if not is_time_slot(time_txt):
            continue

        for c, cls in col_to_class.items():
            subj = (grid[r][c] or "").strip()
            if not subj:
                continue
            if subj in COLUMNS_ORDER:
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


def main():
    pdf_url = get_latest_pdf_url()
    if not pdf_url:
        print("No PDF link found on site.")
        return

    pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=60)
    pdf_resp.raise_for_status()
    pdf_data = pdf_resp.content

    tmp = "temp.pdf"
    with open(tmp, "wb") as f:
        f.write(pdf_data)

    schedule = parse_pdf(tmp)
    out = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "schedule": schedule
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # Helpful debug
    print("OK:", OUTPUT_FILE, "| classes:", len(schedule))


if __name__ == "__main__":
    main()
