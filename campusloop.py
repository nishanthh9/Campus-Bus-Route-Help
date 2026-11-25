"""
Campusloop — Malla Reddy Vishwavidyapeeth College Bus Demo

Run the browser interface:
    python3 -m pip install streamlit
    python3 -m streamlit run campus_demo.py

Run the terminal interface:
    python3 campus_demo.py

Run the automated tests:
    python3 campus_demo.py --test

The routes, schedules, and travel times are illustrative classroom data.
"""

import argparse
import csv
import io
import json
import logging
import os
import re
import tempfile
import unittest

from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, time
from heapq import heappop, heappush
from logging.handlers import RotatingFileHandler
from pathlib import Path


# --------------------------- Project settings ---------------------------

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("CAMPUSLOOP_DATA_DIR", ROOT / "data"))
COLLEGE = "Malla Reddy Vishwavidyapeeth Main Gate"

STATUSES = ("In service", "Limited", "Suspended")
FIELDS = ("code", "name", "status", "minutes", "stops")

FIRST_BUS = 6 * 60
LAST_BUS = 20 * 60
TRANSFER_MINUTES = 5

# Travel time between adjacent stops on the sample college shuttle.
SHUTTLE_TIMES = {
    "MRV1": (14, 12, 10, 6),
    "MRV2": (6, 10, 12, 14),
}

# These links identify area landmarks. College pickup instructions
# are recorded separately for each route and direction.
LANDMARKS = {
    "ameerpet": (
        "Ameerpet Metro Station",
        "https://ltmetro.com/stations/ameerpet/",
    ),
    "balanagar": (
        "Balanagar Metro Station",
        "https://ltmetro.com/stations/balanagar/",
    ),
    COLLEGE.casefold(): (
        "Malla Reddy Vishwavidyapeeth, Suraram",
        "https://mrvv.edu.in/contact-us/",
    ),
}

LOG = logging.getLogger("campusloop")


# --------------------------- Route model ---------------------------

@dataclass(frozen=True)
class Route:
    code: str
    name: str
    status: str
    minutes: int
    stops: tuple

    def record(self):
        return {**asdict(self), "stops": list(self.stops)}

    @classmethod
    def parse(cls, row):
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValueError("Required fields: " + ", ".join(FIELDS))

        def clean(value, field, limit=60):
            if not isinstance(value, str) or not value.isprintable():
                raise ValueError(f"{field} must be printable text.")
            value = " ".join(value.split())
            if not 1 <= len(value) <= limit:
                raise ValueError(f"{field} must contain 1–{limit} characters.")
            return value

        code = clean(row["code"], "Route code", 8).upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,7}", code):
            raise ValueError("Use letters, digits, or hyphens for the route code.")

        name = clean(row["name"], "Route name")
        if row["status"] not in STATUSES:
            raise ValueError("Choose a valid service status.")

        interval = str(row["minutes"]).strip()
        if not re.fullmatch(r"[0-9]{1,3}", interval):
            raise ValueError("The interval must be a whole number.")
        if not 1 <= int(interval) <= 240:
            raise ValueError("The interval must be between 1 and 240 minutes.")

        raw_stops = row["stops"]
        if not isinstance(raw_stops, (list, tuple)) or not 2 <= len(raw_stops) <= 60:
            raise ValueError("Enter between 2 and 60 ordered stops.")
        stops = tuple(clean(stop, "Stop name") for stop in raw_stops)

        if any("|" in stop for stop in stops):
            raise ValueError("A stop name cannot contain a vertical bar.")
        if any(a.casefold() == b.casefold() for a, b in zip(stops, stops[1:])):
            raise ValueError("Consecutive stops must have different names.")

        return cls(code, name, row["status"], int(interval), stops)


def sample_routes():
    college_stops = ["Ameerpet", "Balanagar", "Jeedimetla", "Suraram", COLLEGE]
    inbound = Route("MRV1", "Ameerpet to MRV campus", "In service", 30, tuple(college_stops))
    outbound = Route("MRV2", "MRV campus to Ameerpet", "In service", 30, tuple(reversed(college_stops)))
    return {inbound.code: inbound, outbound.code: outbound}


# --------------------------- Local storage ---------------------------

def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="",
            dir=path.parent, delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def validate_records(rows):
    if not isinstance(rows, list):
        raise ValueError("Expected a list of routes.")
    routes, errors = {}, []
    for number, row in enumerate(rows, 1):
        try:
            route = Route.parse(row)
            if route.code in routes:
                raise ValueError(f"Duplicate code {route.code}.")
            routes[route.code] = route
        except ValueError as error:
            errors.append(f"Record {number}: {error}")
    if errors:
        raise ValueError("\n".join(errors))
    return routes


def to_json(routes):
    rows = [route.record() for route in routes.values()]
    return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"


class Directory:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "routes.json"
        self.backup = self.folder / "routes.previous.json"
        self.routes = self.load()

    def load(self):
        for source in (self.path, self.backup):
            try:
                rows = json.loads(source.read_text(encoding="utf-8-sig"))
                routes = validate_records(rows)
            except FileNotFoundError:
                continue
            except (ValueError, RecursionError) as error:
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                damaged = source.with_name(f"{source.stem}.damaged-{stamp}.json")
                source.rename(damaged)
                LOG.warning("Preserved damaged data in %s: %s", damaged, error)
                continue
            if source == self.backup:
                atomic_write(self.path, to_json(routes))
                LOG.warning("Restored routes from backup")
            return routes

        routes = sample_routes()
        atomic_write(self.path, to_json(routes))
        return routes

    def save(self, updated, action):
        atomic_write(self.backup, to_json(self.routes))
        atomic_write(self.path, to_json(updated))
        self.routes = updated
        LOG.info("%s | %d routes", action, len(updated))

    def add(self, route):
        if route.code in self.routes:
            raise ValueError("That route code already exists.")
        self.save({**self.routes, route.code: route}, f"Added {route.code}")

    def update(self, original_code, route):
        if original_code not in self.routes:
            raise ValueError("Unknown route.")
        if route.code != original_code and route.code in self.routes:
            raise ValueError("That route code already exists.")
        updated = self.routes.copy()
        del updated[original_code]
        updated[route.code] = route
        self.save(updated, f"Edited {original_code} to {route.code}")

    def delete(self, code):
        if code not in self.routes:
            raise ValueError("Unknown route.")
        updated = self.routes.copy()
        del updated[code]
        self.save(updated, f"Deleted {code}")


def configure_logging():
    DATA.mkdir(parents=True, exist_ok=True)
    if LOG.handlers:
        return
    handler = RotatingFileHandler(
        DATA / "activity.log", maxBytes=256_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


# --------------------------- Route search ---------------------------

def search_routes(routes, query="", status=""):
    query = query.strip().casefold()
    return sorted(
        (
            route for route in routes
            if (not status or route.status == status)
            and any(query in text.casefold() for text in (route.code, route.name, *route.stops))
        ),
        key=lambda route: route.code,
    )


def stop_names(routes):
    names = {}
    for route in routes:
        for stop in route.stops:
            names.setdefault(stop.casefold(), stop)
    return sorted(names.values(), key=str.casefold)


@dataclass(frozen=True)
class Leg:
    code: str
    board: str
    leave: str
    departure: int | None = None
    arrival: int | None = None


def fewest_transfers(routes, origin, destination):
    """Each search step represents one uninterrupted bus ride."""
    origin, destination = origin.strip().casefold(), destination.strip().casefold()
    boarding = defaultdict(list)
    for route in routes:
        if route.status != "Suspended":
            for index, stop in enumerate(route.stops):
                boarding[stop.casefold()].append((route, index))

    queue = deque([origin])
    previous = {origin: None}

    while queue and destination not in previous:
        current = queue.popleft()
        for route, index in boarding[current]:
            for arrival in route.stops[index + 1:]:
                key = arrival.casefold()
                if key in previous:
                    continue
                previous[key] = (current, Leg(route.code, route.stops[index], arrival))
                queue.append(key)

    if destination not in previous:
        return None

    legs = []
    current = destination
    while current != origin:
        current, leg = previous[current]
        legs.append(leg)
    return legs[::-1]


def latest_trip(routes, origin, destination, deadline):
    """Search backwards to find the latest boarding time meeting the deadline."""
    origin, destination = origin.strip().casefold(), destination.strip().casefold()
    inbound = defaultdict(list)

    for route in routes:
        if route.status == "Suspended":
            continue
        offsets = [0]
        segment_times = SHUTTLE_TIMES.get(route.code, ())
        for index in range(len(route.stops) - 1):
            minutes = segment_times[index] if index < len(segment_times) else 8
            offsets.append(offsets[-1] + minutes)
        for index in range(1, len(route.stops)):
            inbound[route.stops[index].casefold()].append((route, offsets, index))

    queue = [(-deadline, destination)]
    best = {destination: deadline}
    onward = {}

    while queue:
        negative_time, stop = heappop(queue)
        allowed = -negative_time
        if allowed != best[stop]:
            continue

        if stop == origin:
            legs = []
            while stop != destination:
                leg = onward[stop]
                legs.append(leg)
                stop = leg.leave.casefold()
            return legs

        cutoff = allowed if stop == destination else allowed - TRANSFER_MINUTES

        for route, offsets, arrival_index in inbound[stop]:
            latest_start = min(LAST_BUS, cutoff - offsets[arrival_index])
            if latest_start < FIRST_BUS:
                continue

            run_start = FIRST_BUS + (
                (latest_start - FIRST_BUS) // route.minutes
            ) * route.minutes

            for board_index in range(arrival_index):
                board = route.stops[board_index]
                key = board.casefold()
                departure = run_start + offsets[board_index]

                if departure <= best.get(key, -1):
                    continue

                best[key] = departure
                onward[key] = Leg(
                    route.code, board, route.stops[arrival_index],
                    departure, run_start + offsets[arrival_index],
                )
                heappush(queue, (-departure, key))

    return None


def clock(minutes):
    return time(minutes // 60, minutes % 60).strftime("%I:%M %p").lstrip("0")


# --------------------------- Pickup information ---------------------------

def pickup_path(folder, code, stop):
    from hashlib import sha256

    stop_id = sha256(stop.casefold().encode("utf-8")).hexdigest()
    return Path(folder) / "pickup" / f"{code}-{stop_id}.json"


def read_pickup(folder, code, stop):
    path = pickup_path(folder, code, stop)
    if not path.exists():
        return {}
    note = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(note, dict) or not all(
        isinstance(note.get(key), str) for key in ("landmark", "side")
    ):
        raise ValueError("The saved pickup note is invalid.")
    return note


def save_pickup(folder, code, stop, landmark, side):
    landmark, side = landmark.strip(), side.strip()
    if not all(value.isprintable() and 1 <= len(value) <= 120 for value in (landmark, side)):
        raise ValueError("Enter a landmark and boarding side of 1–120 characters each.")
    note = {"landmark": landmark, "side": side}
    atomic_write(pickup_path(folder, code, stop), json.dumps(note, ensure_ascii=False))
    LOG.info("Updated pickup instructions for %s at %s", code, stop)


# --------------------------- Reports and file exchange ---------------------------

def network_report(routes):
    routes = list(routes)
    running = [route.minutes for route in routes if route.status != "Suspended"]
    coverage = Counter()
    names = {}
    for route in routes:
        for stop in route.stops:
            names.setdefault(stop.casefold(), stop)
        coverage.update({stop.casefold() for stop in route.stops})

    average = f"{sum(running) / len(running):.1f} min" if running else "N/A"
    lines = [
        f"Routes: {len(routes)}",
        f"Unique stops: {len(coverage)}",
        f"Average running interval: {average}",
    ]
    for status in STATUSES:
        lines.append(f"{status}: {sum(route.status == status for route in routes)}")

    lines.append("\nMost connected stops")
    for stop, count in sorted(coverage.items(), key=lambda pair: (-pair[1], pair[0]))[:5]:
        lines.append(f"  {names[stop]} — {count} routes")

    lines.append("\nService alerts")
    alerts = [r for r in routes if r.status != "In service" or r.minutes >= 30]
    lines.extend(f"  {r.code} {r.name}: {r.status}, every {r.minutes} min" for r in alerts)
    if not alerts:
        lines.append("  No service alerts.")
    return "\n".join(lines)


def export_data(routes, fmt):
    if fmt == "json":
        return to_json(routes)
    if fmt == "csv":
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        for route in routes.values():
            writer.writerow({**route.record(), "stops": "|".join(route.stops)})
        return output.getvalue()
    report = network_report(routes.values())
    if fmt == "txt":
        return "Campusloop network report\n\n" + report + "\n"
    if fmt == "md":
        return "# Campusloop network report\n\n```text\n" + report + "\n```\n"
    raise ValueError("Choose CSV, JSON, Markdown, or plain text.")


def read_import(path):
    path = Path(path)
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, strict=True)
            if sorted(reader.fieldnames or []) != sorted(FIELDS):
                raise ValueError("Expected CSV headers: " + ", ".join(FIELDS))
            rows = []
            try:
                for row in reader:
                    if isinstance(row.get("stops"), str):
                        row["stops"] = row["stops"].split("|")
                    rows.append(row)
            except csv.Error as error:
                raise ValueError(f"CSV line {reader.line_num}: {error}") from error
    else:
        raise ValueError("Choose a CSV or JSON file.")
    incoming = validate_records(rows)
    if not incoming:
        raise ValueError("The import file is empty.")
    return incoming


# --------------------------- Streamlit browser interface ---------------------------

def browser_app():
    import streamlit as st

    st.set_page_config(page_title="Campusloop | MRV College Bus", page_icon="🚌")
    configure_logging()

    try:
        directory = Directory(DATA)
    except OSError as error:
        st.error(f"Could not open the route directory: {error}")
        return

    routes = directory.routes
    st.sidebar.title("🚌 Campusloop")
    st.sidebar.caption("Malla Reddy Vishwavidyapeeth · Suraram, Hyderabad")
    page = st.sidebar.radio(
        "College bus service",
        ["Routes", "Journey planner", "Add route", "Network report"],
    )
    st.sidebar.caption("Classroom demo · Illustrative routes and timings")

    if page == "Routes":
        st.title("Find a college bus")
        left, right = st.columns([2, 1])
        query = left.text_input("Search by area, route name, or code", placeholder="Try Ameerpet")
        status = right.selectbox("Service", ["All services", *STATUSES])
        matches = search_routes(routes.values(), query, "" if status == "All services" else status)

        if not matches:
            st.info("No matching routes. Try another area or service.")
            return

        st.caption(f"{len(matches)} matching routes")
        st.dataframe([
            {
                "Code": r.code,
                "Route": r.name,
                "From": r.stops[0],
                "To": r.stops[-1],
                "Service": r.status,
                "Interval": "Paused" if r.status == "Suspended" else f"{r.minutes} min",
            }
            for r in matches
        ], hide_index=True)

        code = st.selectbox(
            "See stops for a route",
            [r.code for r in matches],
            format_func=lambda value: f"{value} · {routes[value].name}",
        )
        st.table([
            {"Order": i, "Stop": stop}
            for i, stop in enumerate(routes[code].stops, 1)
        ])

    elif page == "Journey planner":
        st.title("Plan your college commute")
        stops = stop_names(routes.values())
        if not stops:
            st.info("Add a route to start planning.")
            return

        state = st.session_state
        for key, default in (("boarding", "Ameerpet"), ("destination", COLLEGE)):
            if state.get(key) not in stops:
                state[key] = default if default in stops else stops[0]

        def swap_stops():
            state.boarding, state.destination = state.destination, state.boarding
            if "deadline" in state:
                state.deadline = time(18) if state.boarding == COLLEGE else time(9)

        left, right = st.columns(2)
        origin = left.selectbox("From (boarding stop)", stops, key="boarding")
        destination = right.selectbox("To (destination stop)", stops, key="destination")
        st.button("Plan my return", on_click=swap_stops)

        mode = st.radio("Plan by", ["Fewest transfers", "Arrive before class"], horizontal=True)
        timed = mode == "Arrive before class"

        if timed:
            if "deadline" not in state:
                state.deadline = time(18) if origin == COLLEGE else time(9)
            deadline = st.time_input("Arrive by (Hyderabad time)", value=None, key="deadline")
            st.caption("Sample timetable: first-stop departures from 6 AM to 8 PM, with 5 minutes for transfers.")
            if deadline is None:
                st.info("Choose an arrival time.")
                return
            legs = latest_trip(
                routes.values(), origin, destination,
                deadline.hour * 60 + deadline.minute,
            )
        else:
            legs = fewest_transfers(routes.values(), origin, destination)

        if legs is None:
            st.warning("No journey meets that deadline." if timed else "No running service connects these stops.")
            return
        if not legs:
            st.info("You are already at your destination.")
            return

        if timed:
            st.success(
                f"Latest boarding: {clock(legs[0].departure)} · "
                f"Arrive: {clock(legs[-1].arrival)}"
            )
        else:
            st.success("Direct journey" if len(legs) == 1 else f"Fewest transfers: {len(legs) - 1}")

        for index, leg in enumerate(legs):
            with st.container(border=True):
                st.subheader(f"{leg.code} · {routes[leg.code].name}")
                st.text(f"Board at {leg.board} → Get off at {leg.leave}")
                if timed:
                    st.caption(f"{clock(leg.departure)} – {clock(leg.arrival)}")
                else:
                    st.caption(f"Scheduled every {routes[leg.code].minutes} minutes")

                reference = LANDMARKS.get(leg.board.casefold())
                if reference:
                    st.caption(f"Area landmark: [{reference[0]}]({reference[1]})")

                try:
                    note = read_pickup(DATA, leg.code, leg.board)
                except (ValueError, OSError) as error:
                    st.warning(str(error))
                    note = {}

                if note:
                    st.text(f"Pickup landmark: {note['landmark']}\nBoarding side: {note['side']}")
                else:
                    st.caption("College pickup point and boarding side await confirmation.")

                with st.expander("Record confirmed pickup instructions"):
                    with st.form(f"pickup-{index}-{leg.code}-{leg.board}"):
                        landmark = st.text_input(
                            "Confirmed pickup landmark",
                            value=note.get("landmark", ""), max_chars=120,
                        )
                        side = st.text_input(
                            "Boarding side or gate",
                            value=note.get("side", ""), max_chars=120,
                        )
                        save = st.form_submit_button("Save pickup instructions")
                    if save:
                        try:
                            save_pickup(DATA, leg.code, leg.board, landmark, side)
                            st.rerun()
                        except (OSError, ValueError) as error:
                            st.error(str(error))

    elif page == "Add route":
        st.title("Add a college bus route")
        with st.form("add-route"):
            left, right = st.columns([1, 2])
            code = left.text_input("Route code", max_chars=8)
            name = right.text_input("Route name", max_chars=60)
            stops = st.text_area("Ordered stops, one per line", placeholder=f"Ameerpet\nSuraram\n{COLLEGE}")
            minutes = st.number_input("Interval in minutes", 1, 240, 30)
            status = st.selectbox("Service status", STATUSES)
            save = st.form_submit_button("Save route", type="primary")

        if save:
            try:
                route = Route.parse({
                    "code": code, "name": name, "status": status,
                    "minutes": minutes, "stops": stops.strip().splitlines(),
                })
                Directory(DATA).add(route)
                st.success(f"Saved {route.code}. You can now find it in the journey planner.")
            except (ValueError, OSError) as error:
                st.error(str(error))

    else:
        st.title("Network report")
        running = [r.minutes for r in routes.values() if r.status != "Suspended"]
        a, b, c = st.columns(3)
        a.metric("Routes", len(routes))
        b.metric("Unique stops", len(stop_names(routes.values())))
        c.metric("Average interval", f"{sum(running) / len(running):.1f} min" if running else "—")
        st.table([
            {"Service": status, "Routes": sum(r.status == status for r in routes.values())}
            for status in STATUSES
        ])
        fmt = st.selectbox("Download format", ["csv", "json", "md", "txt"])
        mime = {"csv": "text/csv", "json": "application/json", "md": "text/markdown", "txt": "text/plain"}
        st.download_button(
            "Download routes" if fmt in ("csv", "json") else "Download report",
            export_data(routes, fmt), file_name=f"campusloop.{fmt}", mime=mime[fmt],
        )


# --------------------------- Terminal interface ---------------------------

def terminal_route_form(original=None):
    def ask(label, default=""):
        return input(f"{label} [{default}]: ").strip() or str(default)

    return Route.parse({
        "code": ask("Route code", original.code if original else ""),
        "name": ask("Route name", original.name if original else ""),
        "status": ask("Status (In service / Limited / Suspended)",
                      original.status if original else "In service"),
        "minutes": ask("Interval in minutes", original.minutes if original else 30),
        "stops": ask("Stops separated by |",
                     "|".join(original.stops) if original else "").split("|"),
    })


def terminal_app():
    configure_logging()
    print("CAMPUSLOOP · MALLA REDDY VISHWAVIDYAPEETH")
    print("Sample Hyderabad college bus services")
    print(f"Data folder: {DATA.resolve()}")

    while True:
        print("""
1 Browse routes    2 Search by stop     3 Route details
4 Add route        5 Edit route         6 Delete route
7 Plan journey     8 Network report     9 Import CSV/JSON
10 Export file    11 Recent activity    0 Exit
""")
        choice = input("Choose an option: ").strip()
        if choice == "0":
            return

        try:
            directory = Directory(DATA)
            routes = directory.routes

            if choice in ("1", "2"):
                query = input("Stop, route name, or code: ") if choice == "2" else ""
                status = input("Service filter (Return for all): ").strip() if choice == "2" else ""
                matches = search_routes(routes.values(), query, status)
                if not matches:
                    print("No matches.")
                for index, route in enumerate(matches, 1):
                    print(f"{route.code:6} {route.name:32} {route.status:12} {route.minutes:3} min")
                    if index % 10 == 0 and index < len(matches):
                        if input("Return for more, q to finish: ").strip().lower() == "q":
                            break

            elif choice in ("3", "5", "6"):
                code = input("Route code: ").strip().upper()
                if code not in routes:
                    raise ValueError("Unknown route code.")
                route = routes[code]
                if choice == "3":
                    print(f"{route.code} · {route.name} · {route.status}")
                    for number, stop in enumerate(route.stops, 1):
                        print(f"{number}. {stop}")
                elif choice == "5":
                    directory.update(code, terminal_route_form(route))
                    print("Route updated.")
                elif input(f"Type {code} to confirm deletion: ").strip() == code:
                    directory.delete(code)
                    print("Route deleted.")

            elif choice == "4":
                directory.add(terminal_route_form())
                print("Route added.")

            elif choice == "7":
                origin = input("From: ").strip()
                destination = input(f"To [{COLLEGE}]: ").strip() or COLLEGE
                known = {s.casefold() for s in stop_names(routes.values())}
                if origin.casefold() not in known or destination.casefold() not in known:
                    raise ValueError("Unknown stop. Use option 2 to search the directory.")
                arrival = input("Arrive by HH:MM, or Return for fewest transfers: ").strip()
                if arrival:
                    deadline = datetime.strptime(arrival, "%H:%M")
                    legs = latest_trip(routes.values(), origin, destination,
                                       deadline.hour * 60 + deadline.minute)
                else:
                    legs = fewest_transfers(routes.values(), origin, destination)

                if legs is None:
                    print("No suitable journey.")
                elif not legs:
                    print("You are already at your destination.")
                else:
                    for leg in legs:
                        timing = f" ({clock(leg.departure)}–{clock(leg.arrival)})" if arrival else ""
                        print(f"{leg.code}: {leg.board} → {leg.leave}{timing}")
                        note = read_pickup(DATA, leg.code, leg.board)
                        if note:
                            print(f"  Pickup: {note['landmark']}, {note['side']}")

            elif choice == "8":
                print(network_report(routes.values()))

            elif choice == "9":
                incoming = read_import(Path(input("CSV or JSON file: ").strip()).expanduser())
                matches = len(routes.keys() & incoming.keys())
                print(f"{len(incoming)} valid routes. {matches} existing codes will be updated.")
                if input("Apply import? [y/N]: ").strip().lower() == "y":
                    directory.save({**routes, **incoming}, f"Imported {len(incoming)} routes")
                    print("Import complete.")

            elif choice == "10":
                fmt = input("Format (csv/json/md/txt): ").strip().lower()
                content = export_data(routes, fmt)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                path = DATA / "exports" / f"campusloop-{stamp}.{fmt}"
                atomic_write(path, content)
                print(f"Saved {path.resolve()}")

            elif choice == "11":
                log_file = DATA / "activity.log"
                print("\n".join(log_file.read_text(encoding="utf-8").splitlines()[-20:])
                      or "No recent activity.")
            else:
                print("Choose a number from the menu.")

        except (ValueError, OSError, RecursionError) as error:
            LOG.warning("Menu option %s: %s", choice, error)
            print(f"Could not complete the action: {error}")


# --------------------------- Automated tests ---------------------------

class CampusloopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.directory = Directory(self.folder)

    def test_sample_network(self):
        self.assertEqual(set(self.directory.routes), {"MRV1", "MRV2"})
        self.assertEqual(self.directory.routes["MRV1"].stops[-1], COLLEGE)

    def test_college_search(self):
        matches = search_routes(self.directory.routes.values(), "vishwavidyapeeth")
        self.assertEqual({r.code for r in matches}, {"MRV1", "MRV2"})

    def test_nine_am_class(self):
        leg, = latest_trip(self.directory.routes.values(), "Ameerpet", COLLEGE, 9 * 60)
        self.assertEqual((leg.code, leg.departure, leg.arrival), ("MRV1", 480, 522))

    def test_return_service(self):
        leg, = fewest_transfers(self.directory.routes.values(), COLLEGE, "Ameerpet")
        self.assertEqual(leg.code, "MRV2")

    def test_impossible_deadline(self):
        self.assertIsNone(latest_trip(self.directory.routes.values(), "Ameerpet", COLLEGE, 360))

    def test_same_stop(self):
        self.assertEqual(fewest_transfers(self.directory.routes.values(), "Ameerpet", "Ameerpet"), [])

    def test_transfer_buffer_and_suspended_route(self):
        routes = [
            Route("A", "First", "In service", 10, ("X", "Y")),
            Route("B", "Second", "In service", 10, ("Y", "Z")),
            Route("C", "Closed", "Suspended", 1, ("X", "Z")),
        ]
        first, second = latest_trip(routes, "X", "Z", 410)
        self.assertEqual((first.code, second.code), ("A", "B"))
        self.assertGreaterEqual(second.departure - first.arrival, TRANSFER_MINUTES)

    def test_route_validation(self):
        row = self.directory.routes["MRV1"].record()
        for value in (0, 241, True, "1.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Route.parse({**row, "minutes": value})

    def test_duplicate_code(self):
        with self.assertRaises(ValueError):
            self.directory.add(self.directory.routes["MRV1"])

    def test_pickup_notes_are_direction_specific(self):
        save_pickup(self.folder, "MRV1", "Ameerpet", "Confirmed landmark", "College-bound side")
        self.assertEqual(read_pickup(self.folder, "MRV1", "Ameerpet")["side"], "College-bound side")
        self.assertEqual(read_pickup(self.folder, "MRV2", "Ameerpet"), {})

    def test_export_import(self):
        for fmt in ("csv", "json"):
            path = self.folder / f"export.{fmt}"
            atomic_write(path, export_data(self.directory.routes, fmt))
            self.assertEqual(read_import(path), self.directory.routes)

    def test_persistence_and_recovery(self):
        previous = self.directory.routes.copy()
        self.directory.delete("MRV1")
        self.assertNotIn("MRV1", Directory(self.folder).routes)
        self.directory.path.write_text("broken", encoding="utf-8")
        with self.assertLogs("campusloop", level="WARNING"):
            recovered = Directory(self.folder)
        self.assertEqual(recovered.routes, previous)


# --------------------------- Entry point ---------------------------

def main():
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        in_browser = get_script_run_ctx(suppress_warning=True) is not None
    except ImportError:
        in_browser = False

    if in_browser:
        browser_app()
        return 0

    parser = argparse.ArgumentParser(description="Campusloop college bus route helper")
    parser.add_argument("--test", action="store_true", help="Run automated tests")
    args = parser.parse_args()

    if args.test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(CampusloopTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1

    try:
        terminal_app()
    except (EOFError, KeyboardInterrupt):
        print("\nSession closed.")
    except OSError as error:
        print(f"Could not access local data: {error}")
        return 1
    return 0


if __name__ == "__main__":
    exit_code = main()
    if exit_code:
        raise SystemExit(exit_code)
