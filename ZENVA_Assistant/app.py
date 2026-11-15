from __future__ import annotations

import re
from typing import Any

import requests
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

app = Flask(__name__)
WEB_DIR = Path(__file__).parent / "web"

FIREBASE_URL = (
    "https://smartparkingsystem-ccb6e-default-rtdb."
    "asia-southeast1.firebasedatabase.app"
).rstrip("/")

REQUEST_TIMEOUT = 8

SLOT_SENSOR_MAP = {
    "IR1": "A-01",
    "IR2": "A-02",
    "IR3": "A-03",
    "IR4": "A-04",
}

ACTIVE_BOOKING_STATUSES = {
    "booked",
    "reserved",
    "active",
    "confirmed",
    "pending",
}

INSIDE_STATUSES = {
    "awaiting_payment",
    "inside",
    "entered",
    "parked",
}

FINISHED_STATUSES = {
    "paid",
    "exited",
    "completed",
    "cancelled",
    "canceled",
    "denied",
    "invalid",
    "expired",
}


# ============================================================
# BASIC HELPERS
# ============================================================

def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def clean_plate(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def normalize_slot(value: Any) -> str:
    """
    Converts A1, A01, A-1, A_01 and A-01 to A-01.
    Other slot names are returned in uppercase.
    """
    raw = str(value or "").strip().upper()
    compact = re.sub(r"[^A-Z0-9]", "", raw)

    match = re.fullmatch(r"([A-Z]+)0*(\d+)", compact)
    if match:
        prefix = match.group(1)
        number = int(match.group(2))
        return f"{prefix}-{number:02d}"

    return raw


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value != 0

    return normalize_text(value) in {
        "true",
        "1",
        "yes",
        "on",
        "entered",
        "inside",
        "paid",
    }


def natural_join(items: list[str]) -> str:
    values = [str(item) for item in items if str(item).strip()]

    if not values:
        return ""

    if len(values) == 1:
        return values[0]

    if len(values) == 2:
        return f"{values[0]} and {values[1]}"

    return ", ".join(values[:-1]) + f", and {values[-1]}"


def get_booking_slot(booking: dict) -> str:
    return normalize_slot(
        booking.get("assignedSlot")
        or booking.get("assigned_slot")
        or booking.get("slot")
        or booking.get("parkingSlot")
        or booking.get("parking_slot")
    )


def get_booking_status(booking: dict) -> str:
    return normalize_text(booking.get("status"))


def booking_is_finished(booking: dict) -> bool:
    status = get_booking_status(booking)
    paid = to_bool(booking.get("paid", False))
    return paid or status in FINISHED_STATUSES


def booking_is_inside(booking: dict) -> bool:
    if booking_is_finished(booking):
        return False

    status = get_booking_status(booking)
    entered = to_bool(booking.get("entered", False))

    return entered or status in INSIDE_STATUSES


def booking_is_active_reservation(booking: dict) -> bool:
    if booking_is_finished(booking) or booking_is_inside(booking):
        return False

    return get_booking_status(booking) in ACTIVE_BOOKING_STATUSES


# ============================================================
# FIREBASE
# ============================================================

def firebase_get(path: str = "") -> Any:
    clean_path = path.strip("/")
    url = (
        f"{FIREBASE_URL}/{clean_path}.json"
        if clean_path
        else f"{FIREBASE_URL}/.json"
    )

    try:
        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"Cache-Control": "no-cache"},
        )
        response.raise_for_status()
        return response.json()

    except requests.RequestException as error:
        print(f"Firebase read error [{clean_path or '/'}]: {error}")
        return None


def get_firebase_root() -> dict:
    """
    Reads Firebase once so slots, sensors and bookings all come from
    the same database moment.
    """
    data = firebase_get("")
    return data if isinstance(data, dict) else {}


def get_node(root: dict, name: str) -> dict:
    value = root.get(name)
    return value if isinstance(value, dict) else {}


# ============================================================
# BOOKING LOOKUP
# ============================================================

def find_booking_by_plate(bookings: dict, requested_plate: str) -> tuple[str, dict] | tuple[None, None]:
    """
    Matches Firebase keys even when one version contains spaces or hyphens.
    Example: WP-CAB-1234, WPCAB1234 and WP CAB 1234.
    """
    wanted = clean_plate(requested_plate)

    if not wanted:
        return None, None

    for firebase_key, booking in bookings.items():
        if not isinstance(booking, dict):
            continue

        possible_plates = [
            firebase_key,
            booking.get("plate"),
            booking.get("plateNumber"),
            booking.get("plate_number"),
            booking.get("vehicleNumber"),
            booking.get("vehicle_number"),
            booking.get("numberPlate"),
        ]

        if any(clean_plate(value) == wanted for value in possible_plates):
            return clean_plate(firebase_key) or wanted, booking

    return None, None


def extract_plate_from_question(question: str, bookings: dict) -> str:
    """
    First checks known Firebase booking keys. This is more reliable than
    guessing a number-plate format from speech.
    """
    clean_question = clean_plate(question)

    known_plates: list[str] = []

    for key, booking in bookings.items():
        if not isinstance(booking, dict):
            continue

        values = [
            key,
            booking.get("plate"),
            booking.get("plateNumber"),
            booking.get("plate_number"),
            booking.get("vehicleNumber"),
            booking.get("vehicle_number"),
            booking.get("numberPlate"),
        ]

        for value in values:
            plate = clean_plate(value)
            if plate and plate not in known_plates:
                known_plates.append(plate)

    # Longest first prevents partial matches.
    for plate in sorted(known_plates, key=len, reverse=True):
        if plate in clean_question:
            return plate

    # General fallback for formats such as ABC1234 or WP-CAB-1234.
    candidates = re.findall(
        r"\b[A-Z]{1,3}(?:[\s-]?[A-Z]{1,3})?[\s-]?\d{2,4}\b",
        question.upper(),
    )

    for candidate in candidates:
        plate = clean_plate(candidate)
        if 4 <= len(plate) <= 12:
            return plate

    return ""


# ============================================================
# CORRECT SLOT CALCULATION
# ============================================================

def read_sensor_state(value: Any) -> str:
    text = normalize_text(value)

    occupied_values = {
        "detected",
        "occupied",
        "car detected",
        "vehicle detected",
        "blocked",
        "1",
        "true",
        "on",
    }

    available_values = {
        "not detected",
        "available",
        "free",
        "empty",
        "clear",
        "0",
        "false",
        "off",
    }

    if text in occupied_values:
        return "occupied"

    if text in available_values:
        return "available"

    return "unknown"


def read_slots_node_state(slot_data: Any) -> str:
    if isinstance(slot_data, dict):
        raw = (
            slot_data.get("status")
            or slot_data.get("state")
            or slot_data.get("availability")
            or slot_data.get("occupied")
        )
    else:
        raw = slot_data

    if isinstance(raw, bool):
        return "occupied" if raw else "available"

    text = normalize_text(raw)

    if text in {"occupied", "detected", "inside", "parked"}:
        return "occupied"

    if text in {"available", "free", "empty", "not detected"}:
        return "available"

    if text in {"reserved", "booked", "active", "confirmed"}:
        return "reserved"

    return "unknown"


def build_slot_statuses(root: dict) -> dict[str, str]:
    """
    Final priority:
    1. A physical IR 'detected' reading always means occupied.
    2. An unfinished entered booking also means occupied.
    3. An active unfinished booking means reserved.
    4. A physical IR 'not detected' reading means available only when
       no active booking reserves the slot.
    5. /slots is used only as a fallback when an IR reading is unknown.
    """
    sensors = get_node(root, "sensors")
    slots_node = get_node(root, "slots")
    bookings = get_node(root, "bookings")

    statuses: dict[str, str] = {}
    sensor_states: dict[str, str] = {}

    # Start with all four real hardware slots.
    for sensor_key, slot_name in SLOT_SENSOR_MAP.items():
        state = read_sensor_state(sensors.get(sensor_key))
        sensor_states[slot_name] = state
        statuses[slot_name] = state

    # Add any extra slots found in Firebase /slots.
    for raw_slot_name, slot_data in slots_node.items():
        slot_name = normalize_slot(raw_slot_name)

        if not slot_name:
            continue

        node_state = read_slots_node_state(slot_data)

        if slot_name not in statuses:
            statuses[slot_name] = node_state
            sensor_states[slot_name] = "unknown"
        elif statuses[slot_name] == "unknown" and node_state != "unknown":
            statuses[slot_name] = node_state

    # Determine occupied/reserved slots from bookings.
    booked_occupied: set[str] = set()
    booked_reserved: set[str] = set()

    for booking in bookings.values():
        if not isinstance(booking, dict):
            continue

        slot = get_booking_slot(booking)

        if not slot:
            continue

        if slot not in statuses:
            statuses[slot] = "unknown"
            sensor_states[slot] = "unknown"

        if booking_is_inside(booking):
            booked_occupied.add(slot)
        elif booking_is_active_reservation(booking):
            booked_reserved.add(slot)

    # Apply reliable priority.
    for slot in list(statuses):
        physical_state = sensor_states.get(slot, "unknown")

        if physical_state == "occupied":
            statuses[slot] = "occupied"
        elif slot in booked_occupied:
            statuses[slot] = "occupied"
        elif slot in booked_reserved:
            statuses[slot] = "reserved"
        elif physical_state == "available":
            statuses[slot] = "available"
        elif statuses.get(slot) not in {"available", "occupied", "reserved"}:
            statuses[slot] = "unknown"

    return dict(sorted(statuses.items()))


# ============================================================
# LIVE DATA SUMMARIES
# ============================================================

def booking_counts(bookings: dict) -> dict[str, int]:
    counts = {
        "total": 0,
        "active": 0,
        "inside": 0,
        "finished": 0,
    }

    for booking in bookings.values():
        if not isinstance(booking, dict):
            continue

        counts["total"] += 1

        if booking_is_finished(booking):
            counts["finished"] += 1
        elif booking_is_inside(booking):
            counts["inside"] += 1
        elif booking_is_active_reservation(booking):
            counts["active"] += 1

    return counts


def active_booking_rows(bookings: dict) -> list[dict]:
    rows = []

    for key, booking in bookings.items():
        if not isinstance(booking, dict):
            continue

        if not (
            booking_is_active_reservation(booking)
            or booking_is_inside(booking)
        ):
            continue

        plate = clean_plate(
            booking.get("plate")
            or booking.get("plateNumber")
            or booking.get("vehicleNumber")
            or key
        )

        rows.append(
            {
                "plate": plate or "UNKNOWN",
                "slot": get_booking_slot(booking) or "not assigned",
                "status": (
                    "inside"
                    if booking_is_inside(booking)
                    else get_booking_status(booking) or "booked"
                ),
            }
        )

    return rows


def summarize_slots(statuses: dict[str, str]) -> str:
    available = [slot for slot, state in statuses.items() if state == "available"]
    reserved = [slot for slot, state in statuses.items() if state == "reserved"]
    occupied = [slot for slot, state in statuses.items() if state == "occupied"]
    unknown = [slot for slot, state in statuses.items() if state == "unknown"]

    parts = []

    if available:
        parts.append(f"Available slots are {natural_join(available)}")
    else:
        parts.append("There are no available slots right now")

    if reserved:
        parts.append(f"Booked or reserved slots are {natural_join(reserved)}")

    if occupied:
        parts.append(f"Occupied slots are {natural_join(occupied)}")

    if unknown:
        parts.append(f"Slots without a reliable reading are {natural_join(unknown)}")

    return ". ".join(parts) + "."


def summarize_available_slots(statuses: dict[str, str]) -> str:
    available = [slot for slot, state in statuses.items() if state == "available"]

    if available:
        return f"Available slots are {natural_join(available)}."

    return "There are no available parking slots right now."


def summarize_booked_slots(statuses: dict[str, str], bookings: dict) -> str:
    reserved_slots = [
        slot for slot, state in statuses.items() if state == "reserved"
    ]

    active_rows = active_booking_rows(bookings)

    if not active_rows:
        return "There are no active bookings right now."

    descriptions = []

    for row in active_rows:
        descriptions.append(
            f"vehicle {row['plate']} has slot {row['slot']} with status {row['status']}"
        )

    answer = "Active bookings: " + "; ".join(descriptions) + "."

    if reserved_slots:
        answer += f" Currently reserved slots are {natural_join(reserved_slots)}."

    return answer


def summarize_specific_booking(plate: str, bookings: dict) -> str:
    found_plate, booking = find_booking_by_plate(bookings, plate)

    if not booking:
        return f"No booking was found for vehicle {plate}."

    slot = get_booking_slot(booking) or "not assigned"
    status = get_booking_status(booking) or "unknown"
    entered = booking_is_inside(booking)
    paid = to_bool(booking.get("paid", False))
    duration = booking.get("duration")
    fee = booking.get("fee")
    bill = booking.get("bill")
    amount = fee if fee not in (None, "") else bill

    parts = [
        f"Booking found for vehicle {found_plate}",
        f"the assigned slot is {slot}",
        f"the booking status is {status}",
    ]

    if entered:
        parts.append("the vehicle is currently marked as inside")
    elif booking_is_finished(booking):
        parts.append("the parking session is finished")
    else:
        parts.append("the vehicle is not marked as inside")

    if duration not in (None, ""):
        parts.append(f"the recorded duration is {duration}")

    if amount not in (None, ""):
        parts.append(f"the recorded fee is {amount} rupees")

    if paid:
        parts.append("payment is completed")

    return ". ".join(part[:1].upper() + part[1:] for part in parts) + "."


def get_latest_booking(bookings: dict) -> dict:
    latest: dict | None = None

    for key, booking in bookings.items():
        if not isinstance(booking, dict):
            continue

        timestamp = 0.0

        for field in (
            "exit_time",
            "entry_time",
            "updatedAt",
            "updated_at",
            "createdAt",
            "created_at",
            "timestamp",
        ):
            try:
                timestamp = max(timestamp, float(booking.get(field) or 0))
            except (TypeError, ValueError):
                pass

        row = {
            "plate": clean_plate(
                booking.get("plate")
                or booking.get("plateNumber")
                or booking.get("vehicleNumber")
                or key
            )
            or "—",
            "status": get_booking_status(booking) or "unknown",
            "slot": get_booking_slot(booking) or "—",
            "entered": booking_is_inside(booking),
            "paid": to_bool(booking.get("paid", False)),
            "fee": booking.get("fee") or booking.get("bill"),
            "duration": booking.get("duration"),
            "time": timestamp,
        }

        if latest is None or row["time"] >= latest["time"]:
            latest = row

    return latest or {
        "plate": "—",
        "status": "none",
        "slot": "—",
        "entered": False,
        "paid": False,
        "fee": None,
        "duration": None,
        "time": 0,
    }


def build_snapshot(root: dict) -> dict:
    sensors = get_node(root, "sensors")
    gate = get_node(root, "gate")
    bookings = get_node(root, "bookings")
    statuses = build_slot_statuses(root)
    counts = booking_counts(bookings)

    return {
        "online": bool(root),
        "slots": statuses,
        "slot_counts": {
            "available": sum(state == "available" for state in statuses.values()),
            "occupied": sum(state == "occupied" for state in statuses.values()),
            "reserved": sum(state == "reserved" for state in statuses.values()),
            "unknown": sum(state == "unknown" for state in statuses.values()),
        },
        "bookings": {
            "total": counts["total"],
            "active": counts["active"],
            "inside": counts["inside"],
            "exited": counts["finished"],
        },
        "gate": {
            "open": gate.get("open"),
            "command": gate.get("command", "unknown"),
        },
        "sensors": {
            "IR1": sensors.get("IR1", "unknown"),
            "IR2": sensors.get("IR2", "unknown"),
            "IR3": sensors.get("IR3", "unknown"),
            "IR4": sensors.get("IR4", "unknown"),
            "temperature": sensors.get("temperature"),
            "humidity": sensors.get("humidity"),
            "gasStatus": sensors.get("gasStatus", "unknown"),
            "relay": sensors.get("relay", "unknown"),
            "fire": sensors.get("fire", "unknown"),
            "pump": sensors.get("pump", "unknown"),
        },
        "event": get_latest_booking(bookings),
    }


# ============================================================
# QUESTION ANSWERING
# ============================================================

def answer_question(question: str) -> str:
    text = normalize_text(question)

    if not text:
        return "Please ask a question."

    # Small talk works without touching Firebase.
    words = set(re.findall(r"[a-z']+", text))

    if words & {"hello", "hi", "hey"} or text.startswith(("good morning", "good afternoon", "good evening")):
        return "Hello! Welcome to ParkEase. I am ZENVA, your smart parking assistant. How can I help you today?"

    if "how are you" in text:
        return "I am doing great, thank you for asking. How can I help you with parking today?"

    if "your name" in text or "who are you" in text:
        return "I am ZENVA, the ParkEase smart parking assistant."

    if words & {"thanks", "thank"}:
        return "You are very welcome. Have a wonderful day and drive safely."

    if words & {"bye", "goodbye"}:
        return "Goodbye! Drive safely and see you again at ParkEase."

    if "help" in words or "what can you do" in text:
        return (
            "You can ask me which slots are available, which slots are booked, "
            "how many bookings exist, the booking for a number plate, gate status, "
            "sensor readings, temperature, humidity, fire sensor, water pump, or system status."
        )

    root = get_firebase_root()

    if not root:
        return (
            "I cannot read Firebase right now. Please check the internet connection "
            "and Firebase Realtime Database rules."
        )

    sensors = get_node(root, "sensors")
    gate = get_node(root, "gate")
    bookings = get_node(root, "bookings")
    statuses = build_slot_statuses(root)
    plate = extract_plate_from_question(question, bookings)

    # Greetings
    if any(word in text.split() for word in ("hello", "hi", "hey")):
        return "Hello. I am ZENVA, your live smart parking assistant."

    # Help
    if "help" in text or "what can you do" in text:
        return (
            "You can ask me which slots are available, which slots are booked, "
            "how many bookings exist, the booking for a number plate, gate status, "
            "sensor readings, temperature, humidity, fire sensor, water pump, or system status."
        )

    # Specific vehicle booking must be checked before general slot questions.
    if plate and any(
        word in text
        for word in (
            "booking",
            "booked",
            "vehicle",
            "plate",
            "slot",
            "status",
            "fee",
            "bill",
            "payment",
            "duration",
            "where",
        )
    ):
        return summarize_specific_booking(plate, bookings)

    # General booking questions without a plate.
    if any(
        phrase in text
        for phrase in (
            "booked slot",
            "booking slot",
            "reserved slot",
            "active booking",
            "show booking",
            "list booking",
            "what are the bookings",
            "which vehicles are booked",
        )
    ):
        return summarize_booked_slots(statuses, bookings)

    if (
        "booking" in text
        and any(word in text for word in ("count", "many", "total", "number"))
    ):
        counts = booking_counts(bookings)
        return (
            f"There are {counts['total']} booking records. "
            f"{counts['active']} are active reservations, "
            f"{counts['inside']} vehicles are inside, "
            f"and {counts['finished']} sessions are finished."
        )

    # Available-only question.
    if any(
        phrase in text
        for phrase in (
            "available slot",
            "available parking",
            "free slot",
            "free space",
            "empty slot",
            "where can i park",
        )
    ):
        return summarize_available_slots(statuses)

    # Full slot summary.
    if "slot" in text or "parking space" in text:
        return summarize_slots(statuses)

    # Gate.
    if "gate" in text or "barrier" in text:
        parts = []
        for name in ("entry", "exit"):
            node = gate.get(name)
            if isinstance(node, dict):
                state = normalize_text(node.get("status")) or "unknown"
                parts.append(f"The {name} gate is {state}")
        if parts:
            return ". ".join(parts) + "."
        return "The gate status is not available right now."

    # Individual sensors.
    if "temperature" in text or "temp" in text:
        value = sensors.get("temperature")
        return (
            f"The current temperature is {value} degrees Celsius."
            if value is not None
            else "Temperature data is not available."
        )

    if "humidity" in text:
        value = sensors.get("humidity")
        return (
            f"The current humidity is {value} percent."
            if value is not None
            else "Humidity data is not available."
        )

    if "fire" in text or "smoke" in text or "flame" in text or "gas" in text:
        value = normalize_text(sensors.get("fire", "unknown"))
        if value == "detected":
            return "Warning. Fire is detected. The water pump should be running automatically."
        if value == "safe":
            return "The fire sensor reports safe. No fire is detected."
        return "The fire sensor reading is not available right now."

    if "pump" in text or "water" in text or "sprinkler" in text:
        value = sensors.get("pump", sensors.get("relay", "unknown"))
        return f"The water pump is {value}."

    if "relay" in text:
        return f"The relay is {sensors.get('relay', 'unknown')}."

    if "sensor" in text or "ir" in text:
        return (
            f"IR1 is {sensors.get('IR1', 'unknown')}, "
            f"IR2 is {sensors.get('IR2', 'unknown')}, "
            f"IR3 is {sensors.get('IR3', 'unknown')}, "
            f"and IR4 is {sensors.get('IR4', 'unknown')}."
        )

    # Complete system summary.
    if any(
        phrase in text
        for phrase in (
            "system status",
            "parking status",
            "full status",
            "system summary",
            "parking summary",
        )
    ):
        counts = booking_counts(bookings)
        entry_node = gate.get("entry") if isinstance(gate.get("entry"), dict) else {}
        gate_word = normalize_text(entry_node.get("status")) or "unknown"

        return (
            f"{summarize_slots(statuses)} "
            f"The entry gate is {gate_word}. "
            f"There are {counts['active']} active bookings and "
            f"{counts['inside']} vehicles inside."
        )

    return (
        "I did not understand that question. Ask me about available slots, "
        "booked slots, a vehicle booking, booking count, gate status, sensors, "
        "temperature, humidity, fire sensor, water pump, or system status."
    )


# ============================================================
# FLASK ROUTES
# ============================================================

@app.get("/")
def home():
    if (WEB_DIR / "index.html").exists():
        return send_from_directory(WEB_DIR, "index.html")
    return render_template("index.html")


@app.get("/classic")
def classic():
    return render_template("index.html")


@app.get("/assets/<path:filename>")
def web_assets(filename):
    return send_from_directory(WEB_DIR / "assets", filename)


@app.get("/images/<path:filename>")
def web_images(filename):
    return send_from_directory(WEB_DIR / "images", filename)


@app.get("/api/status")
def api_status():
    root = get_firebase_root()

    if not root:
        return jsonify(
            {
                "online": False,
                "slots": {
                    "A-01": "unknown",
                    "A-02": "unknown",
                    "A-03": "unknown",
                    "A-04": "unknown",
                },
                "slot_counts": {
                    "available": 0,
                    "occupied": 0,
                    "reserved": 0,
                    "unknown": 4,
                },
                "bookings": {
                    "total": 0,
                    "active": 0,
                    "inside": 0,
                    "exited": 0,
                },
                "gate": {
                    "open": None,
                    "command": "unknown",
                },
                "sensors": {
                    "IR1": "unknown",
                    "IR2": "unknown",
                    "IR3": "unknown",
                    "IR4": "unknown",
                    "temperature": None,
                    "humidity": None,
                    "gasStatus": "unknown",
                    "relay": "unknown",
                },
                "event": {
                    "plate": "—",
                    "status": "none",
                    "slot": "—",
                    "entered": False,
                    "paid": False,
                    "fee": None,
                    "duration": None,
                    "time": 0,
                },
            }
        )

    return jsonify(build_snapshot(root))


@app.post("/api/ask")
def api_ask():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()

    if not question:
        return jsonify(
            {
                "ok": False,
                "answer": "Please ask a question.",
            }
        ), 400

    return jsonify(
        {
            "ok": True,
            "answer": answer_question(question),
        }
    )


@app.get("/api/debug")
def api_debug():
    """
    Open http://127.0.0.1:5000/api/debug to see exactly what the
    assistant calculates from Firebase.
    """
    root = get_firebase_root()

    return jsonify(
        {
            "raw_sensors": get_node(root, "sensors"),
            "raw_slots": get_node(root, "slots"),
            "raw_bookings": get_node(root, "bookings"),
            "calculated_slots": build_slot_statuses(root),
            "snapshot": build_snapshot(root) if root else {},
        }
    )


if __name__ == "__main__":
    print("ZENVA web assistant is running.")
    print("Open: http://127.0.0.1:5000")
    print("Debug Firebase values: http://127.0.0.1:5000/api/debug")

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        threaded=True,
    )