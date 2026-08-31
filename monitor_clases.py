#!/usr/bin/env python3
"""
Monitorea paginas de reserva de citas de Google Calendar (calendar.app.google)
en busca de nuevos horarios de clase disponibles, y notifica por ntfy.sh.

Uso:
    python3 monitor_clases.py            # corre un chequeo y termina
    python3 monitor_clases.py --force    # ignora el estado guardado (fuerza notificacion inicial)
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib import request as urlreq
from urllib.parse import quote

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "monitor.log"

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
HEADER_RE = re.compile(r"^(%s) \d{4}$" % "|".join(MONTHS))
DAY_LABEL_RE = re.compile(r"^(\d{1,2}), \w+(, today)?(, no available times)?$")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)", re.I)


def log(msg: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    if os.environ.get("NTFY_TOPIC"):
        config["ntfy_topic"] = os.environ["NTFY_TOPIC"]
    return config


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen_slots": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_ntfy(topic: str, title: str, message: str, priority: str = "default") -> None:
    url = f"https://ntfy.sh/{topic}?title={quote(title)}&priority={quote(priority)}"
    req = urlreq.Request(url, data=message.encode("utf-8"), method="POST")
    try:
        with urlreq.urlopen(req, timeout=15) as resp:
            resp.read()
    except Exception as e:
        log(f"ERROR enviando notificacion ntfy: {e}")


def get_header(page) -> str | None:
    best = None
    for e in page.query_selector_all("div"):
        t = e.inner_text()
        if not t:
            continue
        first_line = t.split("\n")[0]
        if HEADER_RE.match(first_line):
            if best is None or len(t) < len(best):
                best = t
    return best.split("\n")[0] if best else None


def parse_time_to_datetime(d: date, time_str: str):
    if not time_str:
        return None
    m = TIME_RE.match(time_str.strip())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return datetime(d.year, d.month, d.day, h, mi)


def read_columns(page):
    cols = page.query_selector_all("div.wUU3V")
    result = []
    for c in cols:
        header_txt = c.evaluate("el => el.innerText.split('\\n').slice(0,2).join(' ')")
        parts = header_txt.split()
        col_day = int(parts[-1]) if parts and parts[-1].isdigit() else None
        times = tuple(sorted(s.get_attribute("aria-label") for s in c.query_selector_all("button.AeBiU-LgbsSe")))
        result.append((col_day, times))
    return result


def read_columns_stable(page, max_attempts: int = 5, settle_ms: int = 600):
    """Google's widget fetches each day's slots asynchronously after a click,
    so the first read can be incomplete. Keep re-reading until two consecutive
    reads agree (or give up after max_attempts) to avoid false "new slot" alerts."""
    previous = read_columns(page)
    for _ in range(max_attempts):
        page.wait_for_timeout(settle_ms)
        current = read_columns(page)
        if current == previous:
            return [(day, list(times)) for day, times in current]
        previous = current
    return [(day, list(times)) for day, times in previous]


def scan_link(page, url: str, min_months_ahead: int, max_months_ahead: int, min_slots_target: int):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    label = (page.title() or url).strip()

    slots = []
    processed_dates = set()
    scanned_months = set()
    today = date.today()
    months_ahead = min_months_ahead

    while True:
        header = get_header(page)
        if not header:
            break
        month_name, year_s = header.rsplit(" ", 1)
        month_num = MONTHS.index(month_name) + 1
        year = int(year_s)

        if (year, month_num) not in scanned_months:
            scanned_months.add((year, month_num))

            in_month_days = []
            for b in page.query_selector_all("button[aria-label]"):
                al = b.get_attribute("aria-label") or ""
                m = DAY_LABEL_RE.match(al)
                if not m:
                    continue
                day_num = int(m.group(1))
                available = "no available times" not in al
                if available:
                    in_month_days.append(day_num)

            for day_num in sorted(in_month_days):
                d = date(year, month_num, day_num)
                if d in processed_dates or d < today:
                    continue

                target = None
                for b in page.query_selector_all("button[aria-label]"):
                    al = b.get_attribute("aria-label") or ""
                    if al.startswith(f"{day_num}, ") and "no available times" not in al:
                        target = b
                        break
                if target is None:
                    continue

                target.click()
                page.wait_for_timeout(1200)
                col_infos = read_columns_stable(page)
                anchor_idx = None
                for i, (col_day, _times) in enumerate(col_infos):
                    if col_day == day_num:
                        anchor_idx = i
                        break

                if anchor_idx is not None:
                    for i, (col_day, times) in enumerate(col_infos):
                        col_date = d + timedelta(days=(i - anchor_idx))
                        processed_dates.add(col_date)
                        for t in times:
                            dt = parse_time_to_datetime(col_date, t)
                            if dt and dt >= datetime.now():
                                slots.append((dt, label, url))
                else:
                    processed_dates.add(d)

        if len(scanned_months) >= months_ahead:
            if len(slots) >= min_slots_target or months_ahead >= max_months_ahead:
                break
            months_ahead += 1

        next_btn = page.query_selector('button[aria-label="Next month"]')
        if not next_btn:
            break
        next_btn.click()
        page.wait_for_timeout(1500)

    return sorted(set(slots)), label


def format_slot(dt: datetime, label: str) -> str:
    return f"{dt.strftime('%a %d %b, %I:%M %p')} - {label}"


def main():
    force = "--force" in sys.argv
    config = load_config()
    state = load_state()
    is_first_run = not state.get("seen_slots") and not force

    all_slots = []
    for url in config["links"]:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                link_slots, label = scan_link(
                    page, url,
                    config["min_months_ahead"],
                    config["max_months_ahead"],
                    config["min_slots_target"],
                )
                log(f"{label}: {len(link_slots)} horarios encontrados")
                all_slots.extend(link_slots)
            except Exception as e:
                log(f"ERROR escaneando {url}: {e}")
            finally:
                browser.close()

    all_slots.sort(key=lambda s: s[0])
    current_keys = {f"{dt.isoformat()}|{url}" for dt, _, url in all_slots}
    seen_keys = set(state.get("seen_slots", []))

    now = datetime.now()
    seen_keys = {k for k in seen_keys if k.split("|", 1)[0] >= now.isoformat()}

    new_keys = current_keys - seen_keys

    if is_first_run or force:
        top5 = all_slots[: config["top_n_nearest"]]
        if top5:
            body = "\n".join(format_slot(dt, label) for dt, label, _ in top5)
        else:
            body = "No se encontraron horarios disponibles por ahora."
        send_ntfy(config["ntfy_topic"], "Chequeo inicial: proximas clases", body)
        log("Notificacion inicial enviada")
    elif new_keys:
        new_slots = [s for s in all_slots if f"{s[0].isoformat()}|{s[2]}" in new_keys]
        body = "\n".join(format_slot(dt, label) for dt, label, _ in new_slots)
        send_ntfy(config["ntfy_topic"], "Nuevas clases disponibles", body, priority="high")
        log(f"Notificacion de {len(new_slots)} nuevo(s) horario(s) enviada")
    else:
        log("Sin cambios")

    state["seen_slots"] = sorted(seen_keys | current_keys)
    state["last_checked"] = now.isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
