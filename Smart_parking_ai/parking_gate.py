"""
Smart Parking - ANPR gate controller (one camera per gate)

  Two cameras:  MODE=entry CAM_INDEX=1 python parking_gate.py
                MODE=exit  CAM_INDEX=2 python parking_gate.py
  One camera :  CAM_INDEX=0 python parking_gate.py      then press M in the video window to
                switch between ENTRY and EXIT mode while it is running.

Booking lifecycle (Firebase /bookings/<PLATE>):
  booked -> active -> awaiting_payment -> completed
  entry camera : booked  -> active           (gate/entry opens)
  exit camera  : active  -> awaiting_payment (gate stays CLOSED, bill shown on screen)
  customer pays on the website (or admin presses "Mark paid")  -> paid = true
  exit camera  : sees paid -> completed, slot freed, history saved, gate/exit opens

Gates are opened by writing  gate/<entry|exit>/command = "open"  to Firebase.
The ESP32 performs it, keeps the gate open until the car has passed, then closes.
"""
import os
import re
import time
from collections import deque, Counter
from datetime import datetime, timezone

import cv2
import easyocr
import requests

# ---------------- CONFIG ----------------
FIREBASE_URL = os.getenv("FIREBASE_URL",
    "https://smartparkingsystem-ccb6e-default-rtdb.asia-southeast1.firebasedatabase.app")
FIREBASE_AUTH = os.getenv("FIREBASE_AUTH", "")     # optional: DB secret / token once rules are locked
MODE = os.getenv("MODE", "entry").lower()          # starting mode: entry | exit
state = {"mode": MODE}                             # current mode (press M to switch)
CAM_INDEX = int(os.getenv("CAM_INDEX", "1"))
if MODE not in ("entry", "exit"):
    raise SystemExit("MODE must be 'entry' or 'exit'")

DEFAULT_RATE_PER_HOUR = 100       # overridden by /settings/ratePerHour (dashboard Settings page)
BOOKING_VALID_HOURS = 8           # website stores only "HH:MM", so a booking is valid this long after createdAt
MIN_STAY_SEC = int(os.getenv("MIN_STAY_SEC", "60"))          # ignore an exit read sooner than this after entry (set 5 for a quick demo)
PLATE_LOCKOUT_SEC = int(os.getenv("PLATE_LOCKOUT_SEC", "60"))  # after acting on a plate, ignore it for a while (set 5 for a quick demo)
OCR_EVERY_FRAMES = 10
CONFIRM_READS = 3
WINDOW = 10
PAY_POLL_SEC = 1.5                # how often to check if a waiting plate has paid

PLATE_RE = re.compile(r"^[A-Z]{2,3}\d{4}$|^\d{2,3}\d{4}$")   # adjust to your plate formats
ALLOW = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# ---------------- FIREBASE REST ----------------
def _u(path):
    q = f"?auth={FIREBASE_AUTH}" if FIREBASE_AUTH else ""
    return f"{FIREBASE_URL}/{path}.json{q}"

def fb_get(path):
    try:
        r = requests.get(_u(path), timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("Firebase GET error:", e)
        return None

def fb_patch(path, fields) -> bool:
    try:
        r = requests.patch(_u(path), json=fields, timeout=5)
        r.raise_for_status()
        return True
    except Exception as e:
        print("Firebase PATCH error:", e)
        return False

def fb_put(path, value) -> bool:
    try:
        r = requests.put(_u(path), json=value, timeout=5)
        r.raise_for_status()
        return True
    except Exception as e:
        print("Firebase PUT error:", e)
        return False

def fb_post(path, value):
    try:
        requests.post(_u(path), json=value, timeout=5)
    except Exception:
        pass

def log_event(plate, event, extra=None, gate=None):
    body = {"plate": plate, "event": event, "gate": gate or state["mode"], "ts": {".sv": "timestamp"}}
    body.update(extra or {})
    fb_post("gate_log", body)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

# ---------------- HELPERS ----------------
def clean(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())

def plate_variants(p):
    swaps = {"O": "0", "0": "O", "I": "1", "1": "I", "B": "8", "8": "B", "S": "5", "5": "S"}
    yield p
    for i, ch in enumerate(p):
        if ch in swaps:
            yield p[:i] + swaps[ch] + p[i + 1:]

def find_booking(plate):
    for v in plate_variants(plate):
        d = fb_get(f"bookings/{v}")
        if d:
            return v, d
    return None, None

def booking_age_hours(data):
    try:
        t = datetime.fromisoformat(str(data.get("createdAt")).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return None

def set_slot(slot, status, plate=""):
    """Keep the website's slot grid correct (it reads /slots/<id>)."""
    if slot:
        fb_patch(f"slots/{slot}", {"id": slot, "status": status, "plate": plate, "updatedAt": now_iso()})

def open_gate(gate):
    """ESP32 watches this node; it opens, waits for the car to pass, closes and resets it to 'none'."""
    fb_patch(f"gate/{gate}", {"command": "open"})

def fmt_duration(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}h {(sec % 3600) // 60}m"

def get_rate():
    r = fb_get("settings/ratePerHour")
    try:
        return float(r) if r is not None else DEFAULT_RATE_PER_HOUR
    except (TypeError, ValueError):
        return DEFAULT_RATE_PER_HOUR

# ---------------- ON-SCREEN STATUS ----------------
screen_msg = {"text": "", "color": (255, 255, 255), "until": 0}

def say(text, color=(255, 255, 255), seconds=6):
    screen_msg.update(text=text, color=color, until=time.time() + seconds)
    print(text)

GREEN, RED, AMBER = (80, 220, 100), (60, 60, 255), (0, 190, 255)

# ---------------- ENTRY ----------------
def handle_entry(plate, confidence):
    key, data = find_booking(plate)
    if not data:
        say(f"{plate}: no booking - DENIED", RED)
        log_event(plate, "denied_no_booking", gate="entry")
        return
    status = data.get("status")
    if status == "booked":
        age = booking_age_hours(data)
        if age is not None and age > BOOKING_VALID_HOURS:
            say(f"{key}: booking expired - DENIED", RED)
            log_event(key, "denied_expired", gate="entry")
            return
        now = int(time.time())
        if fb_patch(f"bookings/{key}", {"status": "active", "entered": True, "entry_time": now,
                                        "entryStatus": "entered", "updatedAt": now_iso()}):
            set_slot(data.get("slot"), "occupied", key)
            say(f"{key}: ENTRY GRANTED - slot {data.get('slot', '?')}", GREEN)
            log_event(key, "entry", {"confidence": confidence}, gate="entry")
            open_gate("entry")
        return
    if status in ("active", "awaiting_payment"):
        say(f"{key}: already inside - DENIED", RED)
        log_event(key, "denied_already_inside", gate="entry")
        return
    say(f"{key}: status {status} - DENIED", RED)
    log_event(key, f"denied_status_{status}", gate="entry")

# ---------------- EXIT ----------------
pending = {}   # plate key -> time of last payment check

def handle_exit(plate, confidence):
    key, data = find_booking(plate)
    if not data:
        say(f"{plate}: no booking found", RED)
        log_event(plate, "exit_unknown_plate", gate="exit")
        return
    status = data.get("status")
    now = int(time.time())

    if status == "active":
        entry = int(data.get("entry_time") or 0)
        if entry == 0:
            say(f"{key}: no entry time - see staff", RED)
            log_event(key, "exit_error_no_entry_time", gate="exit")
            return
        if now - entry < MIN_STAY_SEC:
            print(f"[{key}] re-read right after entry, ignored")
            return
        hours = max(1.0, round((now - entry) / 3600, 2))
        bill = round(hours * get_rate(), 2)
        if fb_patch(f"bookings/{key}", {
                "status": "awaiting_payment", "bill": bill, "fee": f"Rs.{bill:g}",
                "duration": fmt_duration(now - entry), "exit_time": now,
                "paid": False, "updatedAt": now_iso()}):
            pending[key] = 0
            say(f"{key}: PAY Rs.{bill:g} on the website", AMBER, seconds=3600)
            log_event(key, "awaiting_payment", {"bill": bill, "confidence": confidence}, gate="exit")
        return

    if status == "awaiting_payment":
        pending.setdefault(key, 0)
        fee = data.get("fee") or f"Rs.{data.get('bill', '?')}"
        say(f"{key}: PAY {fee} on the website", AMBER, seconds=3600)
        return

    if status == "completed":
        print(f"[{key}] already completed")
        return
    say(f"{key}: not inside (status {status})", RED)
    log_event(key, f"exit_denied_status_{status}", gate="exit")

def check_pending():
    """Look for plates that have paid; finish their exit and open the gate."""
    now = time.time()
    for key in list(pending):
        if now - pending[key] < PAY_POLL_SEC:
            continue
        pending[key] = now
        data = fb_get(f"bookings/{key}")
        if not data or data.get("status") != "awaiting_payment":
            pending.pop(key, None)
            continue
        if data.get("paid") is True:
            fields = {"status": "completed", "entered": False, "updatedAt": now_iso()}
            if fb_patch(f"bookings/{key}", fields):
                done = dict(data)
                done.update(fields)
                bid = data.get("bookingId") or f"{key}-{data.get('entry_time', 0)}"
                fb_put(f"history/{bid}", done)
                set_slot(data.get("slot"), "available", "")
                pending.pop(key, None)
                say(f"{key}: PAID - gate opening", GREEN)
                log_event(key, "exit", {"bill": data.get("bill", 0)}, gate="exit")
                open_gate("exit")

# ---------------- MAIN ----------------
def draw_overlay(frame):
    h, w = frame.shape[:2]
    cv2.rectangle(frame, (0, 0), (w, 34), (30, 30, 30), -1)
    m = state["mode"]
    cv2.putText(frame, f"{m.upper()} MODE", (8, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 220, 100) if m == "entry" else (0, 190, 255), 2)
    cv2.putText(frame, "press M to switch, Q to quit", (w - 260, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
    if time.time() < screen_msg["until"] and screen_msg["text"]:
        cv2.rectangle(frame, (0, h - 44), (w, h), (30, 30, 30), -1)
        cv2.putText(frame, screen_msg["text"], (8, h - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.7, screen_msg["color"], 2)

def main():
    reader = easyocr.Reader(["en"], gpu=False)
    cap = cv2.VideoCapture(CAM_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    reads = deque(maxlen=WINDOW)
    best_conf = {}
    locked_until = {}
    n = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Camera lost - retrying in 2s")
                cap.release(); time.sleep(2)
                cap = cv2.VideoCapture(CAM_INDEX)
                continue

            check_pending()          # finishes exits that have been paid, whichever mode is shown

            draw_overlay(frame)
            cv2.imshow("ANPR", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("m"):
                state["mode"] = "exit" if state["mode"] == "entry" else "entry"
                reads.clear(); best_conf.clear(); locked_until.clear()
                say(f"Switched to {state['mode'].upper()} mode", (255, 255, 255), 3)
            n += 1
            if n % OCR_EVERY_FRAMES:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.createCLAHE(2.0, (8, 8)).apply(gray)
            results = reader.readtext(gray, allowlist=ALLOW)

            good = [(b, t, p) for b, t, p in results if p >= 0.4]
            good.sort(key=lambda x: x[0][0][0])
            joined = clean("".join(t for _, t, _ in good))
            joined_conf = min((p for _, _, p in good), default=0)
            candidates = {joined: joined_conf}
            for _, t, p in good:
                candidates[clean(t)] = p

            for c, p in candidates.items():
                if PLATE_RE.match(c):
                    reads.append(c)
                    best_conf[c] = max(best_conf.get(c, 0), p)

            now = time.time()
            for plate, freq in Counter(reads).items():
                if freq >= CONFIRM_READS and now >= locked_until.get(plate, 0):
                    locked_until[plate] = now + PLATE_LOCKOUT_SEC
                    conf = round(best_conf.get(plate, 0) * 100)
                    reads.clear()
                    best_conf.clear()
                    fb_put(f"anpr/{state['mode']}", {"plate": plate, "confidence": conf, "ts": {".sv": "timestamp"}})
                    (handle_entry if state["mode"] == "entry" else handle_exit)(plate, conf)
                    break
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
