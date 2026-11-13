#!/usr/bin/env python3
"""Build trains.json: Renfe trains that stop at Casetas (Zaragoza).

Sources (Renfe Data, CC BY 4.0): the Cercanias GTFS (line C1) and the
Media Distancia GTFS (regional trains). Both feeds are merged.
"""
import csv, datetime as dt, io, json, re, sys, urllib.request, zipfile

FEEDS = [
    ("cer", "https://ssl.renfe.com/ftransit/Fichero_CER_FOMENTO/fomento_transit.zip"),
    ("ld", "https://ssl.renfe.com/gtransit/Fichero_AV_LD/google_transit.zip"),
]
CASETAS = "Casetas"
MIN_TRAINS = 10
RADIUS_KM = 90


def rows(z, name):
    if name not in z.namelist():
        return []
    reader = csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig")))
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]


def tables_from_zip(z):
    return {n: rows(z, n + ".txt") for n in ["stops", "stop_times", "trips", "calendar", "calendar_dates", "routes"]}


def day(s):
    return dt.datetime.strptime(s, "%Y%m%d").date()


def clock(s):
    h, m = s.split(":")[:2]
    return f"{int(h)}:{int(m):02d}"


def number(trip_id):
    m = re.search(r"(\d{5})R\d+$", trip_id)
    return m.group(1) if m else re.sub(r"\D", "", trip_id)[:5]


def trips_through_casetas(t):
    """Yield (number, [(stop_name, time)], set_of_dates), and station coordinates."""
    stops = {s["stop_id"]: s for s in t["stops"]}
    home = {i for i, s in stops.items() if s["stop_name"] == CASETAS}
    by = {}
    for r in t["stop_times"]:
        by.setdefault(r["trip_id"], []).append(r)
    keep = {k: v for k, v in by.items() if any(r["stop_id"] in home for r in v)}
    trips = {x["trip_id"]: x for x in t["trips"] if x["trip_id"] in keep}
    routes = {r["route_id"]: r for r in t["routes"]}
    cal = {c["service_id"]: c for c in t["calendar"]}
    add, rem = {}, {}
    for c in t["calendar_dates"]:
        (add if c["exception_type"] == "1" else rem).setdefault(c["service_id"], set()).add(day(c["date"]))
    names = "monday tuesday wednesday thursday friday saturday sunday".split()
    coords = {}
    out = []
    for trip_id, st in keep.items():
        trip = trips.get(trip_id)
        if not trip:
            continue
        sid = trip["service_id"]
        dates = set(add.get(sid, set()))
        c = cal.get(sid)
        if c:
            d, end = day(c["start_date"]), day(c["end_date"])
            while d <= end:
                if c[names[d.weekday()]] == "1" and d not in rem.get(sid, set()):
                    dates.add(d)
                d += dt.timedelta(days=1)
        st.sort(key=lambda r: int(r["stop_sequence"]))
        seq = []
        for r in st:
            s = stops.get(r["stop_id"])
            if not s:
                continue
            coords[s["stop_name"]] = [round(float(s["stop_lat"]), 6), round(float(s["stop_lon"]), 6)]
            seq.append((s["stop_name"], clock(r["departure_time"] or r["arrival_time"])))
        if dates:
            short = routes.get(trip["route_id"], {}).get("route_short_name", "").strip()
            out.append((number(trip_id), "C1" if short.startswith("C1") else "Regional", seq, dates))
    return out, coords


def build(feeds, today):
    """feeds: list of (name, tables). C1 and regional trains come from different feeds, so all are kept."""
    services, coords = [], {}
    for name, t in feeds:
        trips, c = trips_through_casetas(t)
        coords.update({k: v for k, v in c.items() if k not in coords})
        services += trips
    home = coords.get(CASETAS)
    if home:  # keep only stations within reach (long regional trains run to Pamplona or Madrid)
        def near(st):
            la, lo = coords[st]
            return ((la - home[0]) * 111) ** 2 + ((lo - home[1]) * 83) ** 2 <= RADIUS_KM ** 2
        services = [(n, l, [x for x in seq if near(x[0])], d) for n, l, seq, d in services]
    # The Cercanias feed also lists regional trains on the Casetas-Miraflores stretch: drop those copies.
    at = lambda seq: next((h for st, h in seq if st == CASETAS), None)
    regional = {(d, at(seq)) for n, l, seq, ds in services if l == "Regional" for d in ds}
    services = [(n, l, seq, ds if l == "Regional" else {d for d in ds if (d, at(seq)) not in regional})
                for n, l, seq, ds in services]
    services = [x for x in services if x[3] and len(x[2]) > 1]
    last = max((max(d) for *_, d in services), default=today)
    days = max(1, min((last - today).days + 1, 120))
    merged = {}
    for n, line, seq, dates in services:
        key = (n, line, json.dumps(seq, ensure_ascii=False))
        mask = merged.setdefault(key, ["0"] * days)
        for d in dates:
            i = (d - today).days
            if 0 <= i < days:
                mask[i] = "1"
    trains = [{"n": n, "l": line, "s": json.loads(seq), "m": "".join(m)} for (n, line, seq), m in sorted(merged.items()) if "1" in m]
    used = {s for t in trains for s, _ in t["s"]}
    return {"updated": today.isoformat(), "base": today.isoformat(), "days": days,
            "stations": {k: v for k, v in sorted(coords.items()) if k in used}, "trains": trains}


def validate(data):
    week = [t for t in data["trains"] if "1" in t["m"][:7]]
    if len(week) < MIN_TRAINS or CASETAS not in data["stations"]:
        sys.exit(f"Refusing to publish: {len(week)} trains in the next 7 days")


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    feeds = []
    for name, url in FEEDS:
        req = urllib.request.Request(url, headers={"User-Agent": "casetas-app/1.0"})
        with urllib.request.urlopen(req, timeout=180) as r:
            feeds.append((name, tables_from_zip(zipfile.ZipFile(io.BytesIO(r.read())))))
    data = build(feeds, today)
    validate(data)
    with open("trains.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(data['trains'])} trains, {data['days']} days from {data['base']}")


if __name__ == "__main__":
    main()
