#!/usr/bin/env python3
"""
Daily data refresh for the VN Port Watch dashboard (single-file edition).

Runs unattended in GitHub Actions (see .github/workflows/update.yml).
Standard library only — no pip installs needed.

What it does, in order:
  1. refresh_vpa()      — newest VPA monthly container-throughput report
                          (real association statistics; parsed & validated,
                          previous file kept on any failure).
  2. refresh_schedules()— free public daily pilot/terminal plans (Tan Cang
                          Pilot: Cat Lai / HCMC river + Cai Mep; Cảng vụ
                          Hải Phòng data: Hai Phong arrivals/departures).
  3. build_all()        — regenerates ports/calls/daily/customs/inland/report/meta
                          JSON for "today" (Asia/Ho_Chi_Minh). Vessel calls come
                          from AISstream live observations when available, else
                          from the public plans, else the clearly-labelled DEMO
                          fleet. Observed stays are learned into
                          datastore/history.jsonl and drive predicted port stays.
  4. refresh_customs()  — best-effort scrape of GDVC headline monthly totals.
  5. refresh_news()     — latest Vietnam trade/logistics news from GDELT
                          (free, no key); keeps yesterday's file on failure.

Data honesty contract: every JSON carries a dataMode marker ("live"/"demo") and
the UI shows a visible DEMO DATA badge for synthetic content.
"""
import datetime as dt
import json
import math
import pathlib
import random
import re
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
DATA_DIR = ROOT  # flat repo: JSONs live next to index.html
TZ = dt.timezone(dt.timedelta(hours=7))  # Asia/Ho_Chi_Minh

# ---------------------------------------------------------------- ports ----
def _berths(ids, length, depth, maxclass):
    """Berth specs are approximate public figures (terminal operator data)."""
    return [{"id": i, "lengthM": length, "depthM": depth, "maxClass": maxclass} for i in ids]

PORTS = [
    {
        "id": "HAIPHONG", "name": "Hai Phong", "locode": "VNHPH", "region": "North",
        "lat": 20.865, "lon": 106.683, "maxDraftM": 14.5,
        "terminals": [
            {"name": "Lach Huyen (HICT)", "berths": _berths(["H1", "H2"], 360, 14.5, "NEOPAN")},
            {"name": "Lach Huyen (HHIT)", "berths": _berths(["I1", "I2"], 350, 14.5, "NEOPAN")},
            {"name": "Chua Ve", "berths": _berths(["C1", "C2", "C3", "C4"], 180, 8.5, "FEEDER")},
        ],
        "role": "Northern deep-water gateway; direct US/EU mainline calls at Lach Huyen",
        "geofence": [[20.72, 106.55], [20.98, 106.95]],  # AISstream bounding box
    },
    {
        "id": "DANANG", "name": "Da Nang", "locode": "VNDAD", "region": "Central",
        "lat": 16.115, "lon": 108.210, "maxDraftM": 11.0,
        "terminals": [
            {"name": "Tien Sa", "berths": _berths(["T1", "T2", "T3"], 230, 11.0, "PANAMAX")},
        ],
        "role": "Central coast regional port; intra-Asia feeders",
        "geofence": [[16.02, 108.12], [16.22, 108.32]],
    },
    {
        "id": "HCMC", "name": "Ho Chi Minh (Cat Lai)", "locode": "VNSGN", "region": "South",
        "lat": 10.755, "lon": 106.760, "maxDraftM": 11.0,
        "terminals": [
            {"name": "Cat Lai", "berths": _berths(["A1", "A2", "A3", "A4", "A5", "A6"], 220, 10.5, "PANAMAX")},
            {"name": "Tan Cang", "berths": _berths(["R1", "R2"], 170, 9.0, "FEEDER")},
        ],
        "role": "Busiest river terminal; draft-limited to ~5,000 TEU feeders",
        "geofence": [[10.68, 106.68], [10.83, 106.85]],
    },
    {
        "id": "CAIMEP", "name": "Cai Mep - Thi Vai", "locode": "VNCMT", "region": "South",
        "lat": 10.520, "lon": 107.020, "maxDraftM": 16.0,
        "terminals": [
            {"name": "CMIT", "berths": _berths(["M1", "M2", "M3"], 330, 15.8, "ULCV")},
            {"name": "TCIT", "berths": _berths(["T1", "T2", "T3"], 300, 15.5, "ULCV")},
            {"name": "SSIT", "berths": _berths(["S1", "S2"], 320, 16.0, "ULCV")},
            {"name": "SP-PSA", "berths": _berths(["P1", "P2"], 300, 14.0, "NEOPAN")},
        ],
        "role": "Deep-water cluster for ULCVs; direct transpacific & Europe loops",
        "geofence": [[10.42, 106.92], [10.62, 107.12]],
    },
]

# --------------------------------------------------- demo vessel model ----
# Realistic synthetic fleet. Names are fictional on purpose: demo data must
# not be mistaken for the real position of a real ship.
FLEET = [
    # (name, carrier, class, capacity_teu, loa_m, max_draft_m, typical ports)
    ("MERIDIAN ORCHID",   "OceanNet",   "ULCV",    24188, 399.9, 15.5, ["CAIMEP"]),
    ("GOLDEN ANCHOR",     "OceanNet",   "ULCV",    21413, 399.9, 15.5, ["CAIMEP"]),
    ("PACIFIC HERON",     "TransLine",  "ULCV",    18000, 399.0, 15.5, ["CAIMEP"]),
    ("MEKONG GIANT",      "TransLine",  "ULCV",    16020, 366.0, 15.0, ["CAIMEP"]),
    ("LOTUS PIONEER",     "PearlSea",   "NEOPAN",  14074, 366.0, 14.5, ["CAIMEP", "HAIPHONG"]),
    ("DRAGON BAY",        "PearlSea",   "NEOPAN",  11714, 337.0, 14.5, ["CAIMEP", "HAIPHONG"]),
    ("SAIGON EMPRESS",    "BlueRiver",  "PANAMAX",  6850, 294.0, 13.5, ["CAIMEP"]),
    ("HANOI TRADER",      "BlueRiver",  "PANAMAX",  5600, 272.0, 13.0, ["HAIPHONG"]),
    ("AN NAM STAR",       "AsiaFeed",   "FEEDER",   4253, 260.0, 10.5, ["HCMC", "CAIMEP"]),
    ("RED RIVER",         "AsiaFeed",   "FEEDER",   3800, 246.0, 12.0, ["HAIPHONG"]),
    ("CHAM ISLAND",       "AsiaFeed",   "FEEDER",   2824, 222.0, 10.5, ["DANANG", "HCMC"]),
    ("TIENTSA BRIDGE",    "CoastLink",  "FEEDER",   1740, 175.0, 10.0, ["DANANG", "HAIPHONG"]),
    ("HUONG RIVER",       "CoastLink",  "FEEDER",   1436, 168.0,  9.5, ["DANANG"]),
    ("BACH DANG",         "CoastLink",  "FEEDER",   1118, 147.0,  8.5, ["HAIPHONG", "DANANG"]),
    ("CAN GIO",           "AsiaFeed",   "FEEDER",   4900, 270.0, 10.5, ["HCMC", "CAIMEP"]),
    ("LAM DONG",          "StraitsBox", "FEEDER",   2200, 196.0, 10.5, ["DANANG", "HCMC"]),
    ("HA LONG",           "StraitsBox", "FEEDER",   1900, 185.0, 10.0, ["HAIPHONG"]),
    ("THI VAI",           "StraitsBox", "FEEDER",   3200, 232.0, 10.5, ["CAIMEP", "HCMC"]),
]
ROUTES = {
    "ULCV":    (["Singapore", "Cai Mep"], ["Rotterdam", "Long Beach", "Felixstowe", "Los Angeles"]),
    "NEOPAN":  (["Shanghai", "Ningbo", "Singapore"], ["Hamburg", "New York", "Antwerp"]),
    "PANAMAX": (["Kaohsiung", "Hong Kong", "Busan"], ["Singapore", "Port Klang", "Manila"]),
    "FEEDER":  (["Singapore", "Hong Kong", "Shenzhen", "Kaohsiung"], ["Bangkok", "Manila", "Jakarta", "Ho Chi Minh City"]),
}
CLASS_SPEED_KN = {"ULCV": 19, "NEOPAN": 18, "PANAMAX": 17, "FEEDER": 14}


def iso(t: dt.datetime) -> str:
    return t.astimezone(TZ).isoformat(timespec="minutes")


def make_demo_calls(today: dt.date) -> list:
    """Deterministic synthetic port calls covering D-14 .. D+5."""
    rng = random.Random(20261111)
    calls = []
    cid = 0
    # second rotation of distinct fictional names so multi-call days stay plausible
    NAMES2 = [
        "EASTERN SOVEREIGN", "OCEANIC JADE", "SILVER KESTREL", "EMERALD BAY",
        "CORAL EMPEROR", "JADE PHOENIX", "SOUTHERN CROWN", "PEARL DELTA",
        "INDOCHINA WIND", "GOLDEN TURTLE", "SAPPHIRE COAST", "BAMBOO STAR",
        "ORIENTAL BREEZE", "MARINE LOTUS", "SILK ROUTE", "TONKIN SPIRIT",
        "AN GIANG PEARL", "BLUE SAIGON",
    ]
    fleet = FLEET + [
        (NAMES2[i], c, k, cap, loa, md, hp)
        for i, (_, c, k, cap, loa, md, hp) in enumerate(FLEET)
    ]
    for offset in range(-14, 6):
        day = today + dt.timedelta(days=offset)
        n_calls = rng.randint(11, 17)
        day_ships = rng.sample(fleet, k=min(n_calls, len(fleet)))
        for ship in day_ships:
            name, carrier, cls, cap, loa, max_draft, home_ports = ship
            port = rng.choice(home_ports)
            arr_h = rng.randint(0, 21)
            ata = dt.datetime.combine(day, dt.time(arr_h, rng.choice([0, 15, 30, 45])), TZ)
            stay_h = round(rng.uniform(8, 26) * (1.45 if cls in ("ULCV", "NEOPAN") else 1.0), 1)
            atd = ata + dt.timedelta(hours=stay_h)
            # drafts: inbound lighter than outbound for exports-heavy ports
            draft_in = round(max_draft * rng.uniform(0.62, 0.93), 1)
            draft_out = round(min(max_draft, draft_in + rng.uniform(0.2, 1.8)), 1)
            prev_port = rng.choice(ROUTES[cls][0])
            next_port = rng.choice(ROUTES[cls][1])
            cid += 1
            calls.append({
                "id": f"C{cid:04d}",
                "vessel": name, "imo": f"97{cid:05d}", "carrier": carrier,
                "vclass": cls, "capacity": cap, "loa": loa, "maxDraft": max_draft,
                # terminal / berth / status are finalised by assign_berths below
                "port": port, "terminal": "", "berth": "",
                # eta = berthing time (slips if the vessel waits at anchorage);
                # schedEta = original arrival — the anchorage interval is
                # [schedEta, eta] whenever waitH > 0
                "eta": iso(ata), "schedEta": iso(ata), "ata": None,
                "etd": iso(atd), "atd": None,
                "draftIn": draft_in, "draftOut": draft_out,
                "prevPort": prev_port, "nextPort": next_port,
                "status": "EXPECTED", "stayHours": None, "waitH": 0.0,
                "speedKn": CLASS_SPEED_KN[cls],
                "estTeu": estimate_teu(cap, max_draft, draft_in, draft_out, cls, port),
            })
    calls.sort(key=lambda c: c["eta"])
    assign_berths(calls, rng)
    return calls


def assign_berths(calls: list[dict], rng: random.Random) -> None:
    """
    Greedy berth-allocation pass in ETA order — the same logic a terminal
    planner applies: a berth can take the vessel only if it is deep enough
    and its previous occupant has sailed. If every fit berth is still busy,
    the vessel waits at anchorage for the earliest-free berth (its ETA/ETD
    slip by the wait, recorded as waitH) — how a real berth plan resequences
    arrivals — so the resulting plan is clash-free.

    Depth is the hard operational constraint (why Cat Lai can't take mainline
    tonnage); LOA is not checked — big ships routinely overhang into the
    adjacent berth on multi-berth quays.

    Also finalises status / ata / atd / stayHours from the final schedule.
    """
    never = dt.datetime.min.replace(tzinfo=TZ)
    now = dt.datetime.now(TZ)
    busy_until: dict[tuple[str, str, str], dt.datetime] = {}
    for c in calls:  # already sorted by eta
        pmeta = next(p for p in PORTS if p["id"] == c["port"])
        eta = dt.datetime.fromisoformat(c["eta"])
        etd = dt.datetime.fromisoformat(c["etd"])
        fit = [(t, b) for t in pmeta["terminals"] for b in t["berths"]
               if b["depthM"] >= c["maxDraft"]]
        free = [(t, b) for t, b in fit
                if busy_until.get((c["port"], t["name"], b["id"]), never) <= eta]
        if free:
            hint = (c.get("terminalHint") or "").strip()
            hinted = [(t, b) for t, b in free if hint and t["name"] == hint]
            tsel, bsel = rng.choice(hinted or free)  # prefer the plan's terminal
        else:
            pool = fit or [(t, b) for t in pmeta["terminals"] for b in t["berths"]]
            tsel, bsel = min(pool, key=lambda tb: busy_until.get(
                (c["port"], tb[0]["name"], tb[1]["id"]), never))
            free_at = busy_until.get((c["port"], tsel["name"], bsel["id"]), never)
            wait_h = (free_at - eta).total_seconds() / 3600
            if wait_h > 0:
                eta += dt.timedelta(hours=wait_h)
                etd += dt.timedelta(hours=wait_h)
                c["eta"], c["etd"] = iso(eta), iso(etd)
                c["waitH"] = round(wait_h, 1)
        c["terminal"], c["berth"] = tsel["name"], bsel["id"]
        key = (c["port"], tsel["name"], bsel["id"])
        prev = busy_until.get(key)
        busy_until[key] = etd if prev is None else max(prev, etd)
        # final status from the (possibly slipped) schedule
        if eta > now:
            # arrived but still waiting for its berth -> physically at anchorage
            arrived = dt.datetime.fromisoformat(c["schedEta"])
            if c["waitH"] > 0 and arrived <= now:
                c["status"] = "AT_ANCHORAGE"
            else:
                c["status"] = "EXPECTED"
            c["ata"], c["atd"], c["stayHours"] = None, None, None
        elif eta <= now < etd:
            c["status"] = "IN_PORT"
            c["ata"], c["atd"] = iso(eta), None
            c["stayHours"] = round((now - eta).total_seconds() / 3600, 1)
        else:
            c["status"] = "DEPARTED"
            c["ata"], c["atd"] = iso(eta), iso(etd)
            c["stayHours"] = round((etd - eta).total_seconds() / 3600, 1)


# ------------------------------------------------------ TEU estimation ----
def estimate_teu(capacity: int, max_draft: float, draft_in: float,
                 draft_out: float, vclass: str, port: str) -> int:
    """
    Estimated TEU exchanged at this call.

    load factor  = mean(inbound, outbound) draft utilisation, floored at 0.25
                   (ships carry ballast/empty legs; raw draft ratio overstates)
    port share   = fraction of the round-voyage cargo exchanged at this port.
                   Deep-water mainline calls spread cargo over fewer ports than
                   feeders shuttling 4-6 ports per loop.
    """
    load_in = max(draft_in / max_draft, 0.25)
    load_out = max(draft_out / max_draft, 0.25)
    load_factor = (load_in + load_out) / 2
    share_by_class = {"ULCV": 0.34, "NEOPAN": 0.40, "PANAMAX": 0.52, "FEEDER": 0.68}
    share = share_by_class.get(vclass, 0.5)
    # southbound export weighting: Cai Mep / Cat Lai lift more export boxes
    if port in ("CAIMEP", "HCMC"):
        load_factor = min(1.0, load_factor * 1.06)
    return int(round(capacity * load_factor * share))


# ------------------------------------------------ inland: ICDs & barges ----
# ICDs: static registry of real, well-known facilities (public information).
# Volumes are MODELLED from the customs regional model (province catchment
# share of containerised exports, ~60% gating via ICDs) — no live gate feed
# exists in the free tier.
ICDS = [
    # (name, province, region, operator, modes, gateway ports, catchment share of province flow)
    ("Tan Cang - Song Than ICD", "Binh Duong", "Southeast", "Saigon Newport", "road · barge", ["HCMC", "CAIMEP"], .55),
    ("Tan Cang - Long Binh ICD", "Dong Nai", "Southeast", "Saigon Newport", "road · barge", ["CAIMEP", "HCMC"], .50),
    ("Sotrans ICD", "Ho Chi Minh City", "Southeast", "Sotrans", "road · barge", ["HCMC"], .35),
    ("Transimex ICD", "Ho Chi Minh City", "Southeast", "Transimex", "road", ["HCMC", "CAIMEP"], .30),
    ("Phuoc Long ICD", "Ho Chi Minh City", "Southeast", "Phuoc Long Corp", "road", ["CAIMEP"], .25),
    ("Can Tho ICD", "Can Tho", "Mekong Delta", "Tan Cang - Can Tho", "barge · road", ["CAIMEP", "HCMC"], .60),
    ("Que Vo ICD", "Bac Ninh", "Red River Delta", "T&M Forwarding", "road · rail", ["HAIPHONG"], .45),
    ("Dinh Vu ICD", "Hai Phong", "Red River Delta", "Dinh Vu Port JSC", "road", ["HAIPHONG"], .40),
    ("My Dinh ICD", "Ha Noi", "Red River Delta", "Military Logistics", "road · rail", ["HAIPHONG"], .35),
    ("Da Nang ICD", "Da Nang", "Central Coast", "Da Nang Port", "road · rail", ["DANANG"], .45),
]

# River barge corridors — the Mekong Delta moves most of its boxes by barge.
CORRIDORS = [
    # (id, name, waterway, from, to, barge capacity range, transits/day range)
    ("MEKONG-CM", "Mekong Delta ↔ Cai Mep", "Hau River · Cho Gao canal", "Can Tho / ICDs", "Cai Mep", (80, 250), (6, 11)),
    ("MEKONG-HCMC", "Mekong Delta ↔ Cat Lai", "Tien River · Soai Rap", "My Tho / Ben Tre", "Cat Lai", (60, 180), (3, 6)),
    ("CATLAI-CM", "Cat Lai ↔ Cai Mep shuttle", "Dong Nai · Thi Vai rivers", "Cat Lai", "Cai Mep", (150, 400), (4, 8)),
    ("DNGNAI-ICD", "Song Than / Long Binh ICD barges", "Dong Nai River", "ICD jetties", "Cat Lai / Cai Mep", (60, 150), (2, 5)),
]

# AIS bounding boxes + direction axes for barge corridors (used by
# ais_collector.py to log TRANSIT events; many barges carry no AIS, so any
# real observations are a subset of true traffic).
CORRIDOR_BOXES = {
    # corridor id -> {"box": [[lat0, lon0], [lat1, lon1]],
    #                 "axis": ((lat, lon) of 'from' end, (lat, lon) of 'to' end)}
    # Boxes are checked in order — the Thi Vai / lower Dong Nai reach is
    # claimed by the Cat Lai <-> Cai Mep shuttle before the Mekong route.
    "CATLAI-CM":   {"box": [[10.48, 106.82], [10.76, 107.04]],
                    "axis": ((10.74, 106.83), (10.52, 107.02))},
    "MEKONG-CM":   {"box": [[10.30, 106.20], [10.62, 106.95]],
                    "axis": ((10.42, 106.30), (10.52, 106.93))},
    "MEKONG-HCMC": {"box": [[10.28, 106.55], [10.72, 106.80]],
                    "axis": ((10.38, 106.62), (10.70, 106.77))},
    "DNGNAI-ICD":  {"box": [[10.78, 106.72], [11.02, 106.98]],
                    "axis": ((10.95, 106.90), (10.79, 106.75))},
}

BARGE_NAMES = [
    "MEKONG GLORY 08", "DELTA PEARL 12", "CAN THO 66", "SAIGON BARGE 21",
    "THI VAI 09", "DONG NAI 18", "HAU GIANG 03", "TIEN GIANG 77",
    "BEN TRE 11", "LONG AN 28", "VINH LONG 05", "MY THO 33",
]


def make_inland(today: dt.date, customs: dict) -> dict:
    rng = random.Random(20261111 + 7)
    exp_total = customs["totals"]["export"]  # US$ bn per month

    # --- ICD weekly TEU from customs model ---
    prov_exp = {}  # province -> export bn
    for r in customs["regions"]:
        for p in r["provinces"]:
            prov_exp[p["name"]] = p["export"]
    icds = []
    for name, prov, region, op, modes, gw, catch in ICDS:
        monthly_bn = prov_exp.get(prov, 0.5) * catch
        weekly_teu = int(round(monthly_bn * 1e9 / USD_PER_TEU / 4.345))
        icds.append({
            "name": name, "province": prov, "region": region, "operator": op,
            "modes": modes, "gateways": gw, "catchment": catch,
            "estTeuWeek": weekly_teu,
        })

    # --- barge transits for today + yesterday ---
    transits = []
    bid = 0
    for d_off in (-1, 0):
        day = today + dt.timedelta(days=d_off)
        for cid, cname, water, frm, to, cap_rng, n_rng in CORRIDORS:
            for _ in range(rng.randint(*n_rng)):
                bid += 1
                cap = rng.randint(*cap_rng)
                load = round(rng.uniform(0.55, 0.98), 2)
                direction = rng.random() < 0.55
                hh = rng.randint(0, 23)
                t = dt.datetime.combine(day, dt.time(hh, rng.choice([0, 10, 20, 30, 40, 50])), TZ)
                underway = d_off == 0 and rng.random() < 0.35
                transits.append({
                    "id": f"B{bid:03d}", "vessel": rng.choice(BARGE_NAMES),
                    "corridor": cid, "corridorName": cname,
                    "origin": frm if direction else to,
                    "destination": to if direction else frm,
                    "time": iso(t), "capacityTeu": cap, "loadFactor": load,
                    "estTeu": int(round(cap * load)),
                    "status": "UNDERWAY" if underway else "COMPLETED",
                })
    for t in transits:
        t["source"] = "demo"

    # --- real AIS-observed barge transits (ais_collector TRANSIT events) ---
    cmap = {cid: (cname, frm, to) for cid, cname, _w, frm, to, _c, _n in CORRIDORS}
    n_ais = 0
    ev_file = ROOT / "datastore" / "events.jsonl"
    if ev_file.exists():
        day0 = (today - dt.timedelta(days=1)).isoformat()
        for line in ev_file.read_text().splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") != "TRANSIT" or ev.get("time", "") < day0:
                continue
            if ev.get("corridor") not in cmap:
                continue
            cname, frm, to = cmap[ev["corridor"]]
            toward = (ev.get("toward") or "").strip()
            loa = float(ev.get("loa") or 0)
            cap = int(min(400, max(40, round(loa * 1.5)))) if loa else 100
            load = 0.8
            transits.append({
                "id": f"AIS{ev.get('mmsi', '')}", "vessel": (ev.get("vessel") or "AIS barge").strip() or "AIS barge",
                "corridor": ev["corridor"], "corridorName": cname,
                "origin": (frm if toward == to else to) if toward else "—",
                "destination": toward or "—",
                "time": ev["time"], "capacityTeu": cap, "loadFactor": load,
                "estTeu": int(round(cap * load)),
                "status": "AIS", "source": "ais",
            })
            n_ais += 1

    transits.sort(key=lambda x: x["time"], reverse=True)

    corridors = []
    for cid, cname, water, frm, to, cap_rng, n_rng in CORRIDORS:
        mine = [t for t in transits if t["corridor"] == cid]
        today_mine = [t for t in mine if t["time"][:10] == today.isoformat()]
        corridors.append({
            "id": cid, "name": cname, "waterway": water, "fromTo": [frm, to],
            "typicalTeu": list(cap_rng),
            "transitsToday": len(today_mine),
            "estTeuToday": sum(t["estTeu"] for t in today_mine),
            "underway": sum(1 for t in today_mine if t["status"] == "UNDERWAY"),
            "aisToday": sum(1 for t in mine if t.get("source") == "ais"),
            "geofenceNote": "AIS corridor geofence active in ais_collector.py (TRANSIT events, 6 h dedupe)",
        })

    return {
        "corridors": corridors, "icds": icds, "transits": transits[:60],
        "notes": [
            "ICD volumes modelled from customs regional model (province catchment × ~ICD gate share) — no live gate feed in free tier",
            (f"{n_ais} AIS-observed barge transit(s) in the last 48 h — a subset only: many river barges do not transmit AIS"
             if n_ais else
             "Barge transits are modelled estimates; corridor AIS geofences are armed — real transits appear here as the collector observes them (many river barges do not transmit AIS)"),
        ],
    }


# ------------------------------------------------------------- customs ----
# Official anchors (GDVC preliminary, published via press, Oct-2026):
#   exports US$64.67bn, imports US$57.36bn (October 2026).
# Province / commodity splits are a DEMO model pending a licensed
# transaction-level feed (Volza / EximPedia / GDVC microdata).
OFFICIAL_OCT_2026 = {"export": 64.67, "import": 57.36}  # US$ bn, official
USD_PER_TEU = 45_000  # planning assumption: avg declared value per TEU

REGION_MODEL = [  # (region, share, gateway port ids, provinces[(name, share within region)])
    ("Southeast",       0.47, ["HCMC", "CAIMEP"],
     [("Ho Chi Minh City", .38), ("Binh Duong", .27), ("Dong Nai", .22),
      ("Ba Ria - Vung Tau", .08), ("Tay Ninh", .05)]),
    ("Red River Delta", 0.34, ["HAIPHONG"],
     [("Bac Ninh", .26), ("Hai Phong", .24), ("Ha Noi", .16), ("Hung Yen", .11),
      ("Vinh Phuc", .10), ("Hai Duong", .08), ("Quang Ninh", .05)]),
    ("Central Coast",   0.07, ["DANANG"],
     [("Da Nang", .34), ("Thanh Hoa", .26), ("Quang Nam", .22), ("Hue", .18)]),
    ("Mekong Delta",    0.08, ["HCMC", "CAIMEP"],
     [("Can Tho", .26), ("Long An", .30), ("Tien Giang", .24), ("An Giang", .20)]),
    ("North Central",   0.02, ["DANANG"],
     [("Nghe An", .55), ("Ha Tinh", .45)]),
    ("Central Highlands", 0.02, ["DANANG", "CAIMEP"],
     [("Dak Lak", .42), ("Lam Dong", .34), ("Gia Lai", .24)]),
]
IMPORT_BIAS = {  # region -> import/export ratio modifier (provinces importing inputs)
    "Southeast": 1.05, "Red River Delta": 1.18, "Central Coast": 0.8,
    "Mekong Delta": 0.55, "North Central": 0.7, "Central Highlands": 0.4,
}
COMMODITY_MODEL = [  # (HS-2, label, export share, import share)
    ("85", "Electrical machinery & equipment",      .30, .24),
    ("84", "Machinery & mechanical appliances",     .10, .16),
    ("61", "Knitted apparel",                       .055, .01),
    ("62", "Woven apparel",                         .045, .008),
    ("64", "Footwear",                              .05, .012),
    ("90", "Optical & precision instruments",       .025, .05),
    ("39", "Plastics & articles",                   .03, .07),
    ("72", "Iron & steel",                          .02, .055),
    ("08", "Edible fruit & nuts",                   .02, .008),
    ("03", "Fish & seafood",                        .025, .01),
    ("09", "Coffee, tea & spices",                  .02, .004),
    ("94", "Furniture & bedding",                   .03, .006),
    ("27", "Mineral fuels & oils",                  .012, .09),
    ("87", "Vehicles & parts",                      .012, .045),
    ("44", "Wood & articles of wood",               .022, .01),
]
PARTNERS_MODEL = [  # (partner, export share, import share)
    ("United States", .30, .045), ("China", .15, .38), ("South Korea", .065, .17),
    ("Japan", .06, .07), ("Hong Kong (China)", .03, .02), ("Netherlands", .025, .01),
    ("Germany", .02, .015), ("Taiwan (China)", .02, .06), ("Thailand", .015, .035),
    ("India", .02, .02), ("Singapore", .012, .03), ("Australia", .015, .02),
]


def build_customs() -> dict:
    exp_total, imp_total = OFFICIAL_OCT_2026["export"], OFFICIAL_OCT_2026["import"]
    regions = []
    for region, share, gateways, provinces in REGION_MODEL:
        reg_exp = round(exp_total * share, 2)
        reg_imp = round(imp_total * share * IMPORT_BIAS[region] * 0.92, 2)
        provs = [
            {"name": name,
             "export": round(reg_exp * ps, 2),
             "import": round(reg_imp * ps * (1 + 0.1 * math.sin(i * 2.1)), 2)}
            for i, (name, ps) in enumerate(provinces)
        ]
        regions.append({"region": region, "export": reg_exp, "import": reg_imp,
                        "gateways": gateways, "provinces": provs})
    commodities = [
        {"hs": hs, "name": name,
         "export": round(exp_total * es, 2), "import": round(imp_total * ims, 2)}
        for hs, name, es, ims in COMMODITY_MODEL
    ]
    partners = [
        {"partner": p, "export": round(exp_total * es, 2), "import": round(imp_total * ims, 2)}
        for p, es, ims in PARTNERS_MODEL
    ]
    return {
        "month": "2026-10",
        "unit": "USD bn",
        "totals": {**OFFICIAL_OCT_2026,
                   "balance": round(exp_total - imp_total, 2)},
        "totalsSource": "GDVC preliminary October-2026 figures (official, via press)",
        "breakdownSource": "DEMO model — province/commodity split requires a licensed "
                           "transaction-level feed (Volza / EximPedia / GDVC microdata)",
        "regions": regions, "commodities": commodities, "partners": partners,
    }


# -------------------------------------------------------------- report ----
def build_report(today: dt.date, calls: list, daily: list, customs: dict) -> dict:
    in_port = [c for c in calls if c["status"] == "IN_PORT"]
    # vessels waiting at anchorage are still pending arrivals for planning purposes
    expected = [c for c in calls if c["status"] in ("EXPECTED", "AT_ANCHORAGE")]
    dep24 = [c for c in calls if c["status"] == "DEPARTED" and c["atd"] and
             c["atd"][:10] >= (today - dt.timedelta(days=1)).isoformat()]
    teu_today = sum(c["estTeu"] for c in calls
                    if c["status"] in ("IN_PORT",) or
                    (c["eta"][:10] == today.isoformat()))
    per_port = {}
    port_region = {p["id"]: p["region"] for p in PORTS}
    per_region = {}
    for c in calls:
        if c["status"] != "EXPECTED":
            per_port.setdefault(c["port"], 0)
            per_port[c["port"]] += c["estTeu"]
            reg = port_region.get(c["port"], "?")
            per_region.setdefault(reg, 0)
            per_region[reg] += c["estTeu"]
    t = customs["totals"]
    bullets = [
        f"{len(in_port)} container vessels alongside across the four tracked ports; "
        f"{len(expected)} further arrivals expected in the next 24-72 h.",
        f"Estimated {teu_today:,} TEU being exchanged today (draft/stay model, uncalibrated).",
        f"{len(dep24)} departures in the last 24 h.",
        f"Official October-2026 trade: exports US${t['export']}bn, imports US${t['import']}bn, "
        f"balance {'surplus' if t['balance'] >= 0 else 'deficit'} US${abs(t['balance'])}bn.",
        "Cai Mep continues to absorb Cat Lai overflow on mainline services; "
        "Lach Huyen (Hai Phong) holds the northern deep-water calls.",
    ]
    return {
        "date": today.isoformat(),
        "generatedAt": iso(dt.datetime.now(TZ)),
        "headline": f"Vietnam daily port & trade brief — {today.strftime('%d %b %Y')}",
        "bullets": bullets,
        "perPortTeu": per_port,
        "perRegionTeu": per_region,
    }




# ------------------------------------------------------------- customs ----
GDVC_STATS = "https://www.customs.gov.vn/index.jsp?pageId=5806&language=en-US"


def fetch_gdvc_totals():
    """Best-effort scrape of GDVC headline monthly totals.
    Returns (export_bn, import_bn, month_str) or None."""
    req = urllib.request.Request(GDVC_STATS, headers={"User-Agent": "vn-port-dashboard/1.0"})
    html = urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "ignore")
    m = re.findall(r"(?:export|import)[^0-9]{0,40}([0-9]{2,3}(?:[.,][0-9]{1,2})?)\s*(?:billion|bn)",
                   html, re.I)
    if len(m) >= 2:
        return float(m[0].replace(",", ".")), float(m[1].replace(",", ".")), None
    return None


def refresh_customs() -> None:
    OUT = ROOT / "customs.json"
    if not OUT.exists():
        print("customs.json missing — skipping totals refresh")
        return
    data = json.loads(OUT.read_text())
    try:
        res = fetch_gdvc_totals()
    except Exception as e:
        print("GDVC fetch failed:", e)
        res = None
    if res:
        exp, imp, month = res
        data["totals"] = {"export": exp, "import": imp, "balance": round(exp - imp, 2)}
        data["totalsSource"] = "GDVC website (scraped, latest monthly release)"
        if month:
            data["month"] = month
        OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        print("customs totals refreshed:", data["totals"])
    else:
        print("no new official totals parsed — keeping existing (official Oct-2026 anchors)")


# ---------------------------------------------------------------- news ----
QUERY = 'vietnam (export OR import OR customs OR tariff OR port OR shipping OR logistics OR container)'
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode({
    "query": QUERY, "mode": "artlist", "maxrecords": 50, "format": "json",
    "sourcelang": "english", "sort": "datedesc", "timespan": "3d",
})

RULES = [
    ("Policy & tariffs", re.compile(r"tariff|duty|decree|customs (rule|law|policy)|tax|CBP|Section 301|IEEPA", re.I)),
    ("Ports & shipping", re.compile(r"port|terminal|berth|container|vessel|shipping|freight|draft|TEU|wharf", re.I)),
    ("Disruption",       re.compile(r"typhoon|strike|congestion|delay|closure|flood|storm", re.I)),
    ("Agreements",       re.compile(r"agreement|FTA|CPTPP|RCEP|EVFTA|trade mission|MOU", re.I)),
    ("Trade data",       re.compile(r"export|import|turnover|surplus|deficit|growth|statistics", re.I)),
]

# Curated real fallback (used only when GDELT is unreachable on first run).
FALLBACK = [
    {"title": "Vietnam's merchandise trade tops US$1 trillion in Jan–Oct 2026, first time ever",
     "url": "https://www.vietnam-briefing.com/news/vietnam-trade-us-1-trillion-2026.html/",
     "source": "Vietnam Briefing", "seendate": "20261103",
     "summary": "Exports US$499.24bn (+27.5% y/y), imports US$511.31bn (+37.4%); October alone "
                "posted a US$7.31bn surplus. FDI firms account for 81.2% of exports."},
    {"title": "USDA agribusiness trade mission heads to Ho Chi Minh City, Nov 9–12",
     "url": "https://www.fas.usda.gov/topics/trade-missions/vietnam-november-2026",
     "source": "USDA FAS", "seendate": "20261106",
     "summary": "Vietnam is the 7th-largest US agriculture export market (US$5.6bn in 2025); "
                "mission targets food-processing and retail demand."},
    {"title": "Two Vietnamese container ports hold Top-20 spots in World Bank/S&P CPPI",
     "url": "https://en.baochinhphu.vn/two-vietnamese-container-ports-named-among-worlds-top-20-111260612154143255.htm",
     "source": "VGP News", "seendate": "20260612",
     "summary": "Cai Mep cluster ranked 11th, Hai Phong 13th. National container throughput "
                "up ~14% y/y in the first five months of 2026."},
    {"title": "Mexico's imports from Vietnam double, transshipment scrutiny rises",
     "url": "https://mexiconewsdaily.com/business/vietnams-exports-to-mexico-transshipment-tariffs/",
     "source": "Mexico News Daily", "seendate": "20260820",
     "summary": "Jan–Jun imports from Vietnam +103% to US$18.4bn; suspected Chinese "
                "tariff-dodging via CPTPP partner Vietnam draws attention."},
    {"title": "Exports accelerate as Vietnam moves towards trade balance",
     "url": "https://www.youtube.com/watch?v=N9DJDCaWCn0",
     "source": "Vietnam News Agency", "seendate": "20261019",
     "summary": "Jan–Sep 2026 import-export turnover ≈ US$888bn, +30.4% y/y; FTA "
                "utilisation credited for export-mix improvement."},
    {"title": "Cai Mep–Thi Vai: 6.5m TEU in 2024 (+33%), direct US/EU services expand",
     "url": "https://ship4wd.com/import-guides/top-5-container-ports-in-vietnam",
     "source": "Ship4wd", "seendate": "20261026",
     "summary": "Deep-water cluster now handles ULCVs up to ~214k DWT; only Vietnam complex "
                "without draft restriction for 24k-TEU ships."},
]


def classify(title: str) -> str:
    for label, rx in RULES:
        if rx.search(title):
            return label
    return "Trade data"


def summarise(title: str) -> str:
    return title  # hook: plug any free LLM endpoint here for 2-line summaries


def fetch_gdelt():
    req = urllib.request.Request(GDELT, headers={"User-Agent": "vn-port-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        data = json.load(r)
    items, seen = [], set()
    for a in data.get("articles", []):
        url = a.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        title = a.get("title") or ""
        items.append({
            "title": title, "url": url,
            "source": a.get("domain") or "",
            "seendate": a.get("seendate") or "",
            "category": classify(title),
            "summary": summarise(title),
        })
    return items


def refresh_news() -> None:
    OUT = ROOT / "news.json"
    items, mode = None, "live"
    try:
        items = fetch_gdelt()
        print(f"GDELT returned {len(items)} articles")
    except Exception as e:
        print("GDELT failed:", e)
        if OUT.exists():
            print("keeping previous news.json")
            return
        items = [dict(a, category=classify(a["title"])) for a in FALLBACK]
        mode = "curated-fallback"
    payload = {"mode": mode, "items": items[:40]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    print("wrote news.json", f"({mode}, {len(payload['items'])} items)")
# ------------------------------------------------------------------ VPA ----
# Vietnam Seaports Association (VPA) publishes a free public monthly container
# throughput report (HTML). Real association statistics — the only official
# monthly TEU volume series in the free tier. Refreshed monthly; on any fetch
# or parse failure the previous vpa.json is kept.
VPA_REGIONS = [  # (find key in report text, display EN, display VI)
    ("Miền Bắc", "Northern", "Miền Bắc"),
    ("Bắc Trung Bộ", "North Central", "Bắc Trung Bộ"),
    ("Nam Trung Bộ", "South Central", "Nam Trung Bộ"),
    ("Đông Nam Bộ", "Southeast (HCMC · Dong Nai)", "Đông Nam Bộ"),
    ("Cái Mép", "Cai Mep – Thi Vai", "Cái Mép – Thị Vải"),
    ("Cửu Long", "Mekong Delta", "Đồng bằng sông Cửu Long"),
]
VPA_MIX = [
    ("Nhập ngoại", "Import", "import"),
    ("Xuất ngoại", "Export", "export"),
    ("Nội địa", "Domestic", "domestic"),
    ("Trung chuyển", "Transshipment", "transshipment"),
    ("Chưa phân loại", "Unclassified", "unclassified"),
]


def _vn_int(s: str) -> int:
    return int(s.replace(".", "").replace(",", ""))


def _vn_pct(s: str) -> float:
    return float(s.replace(",", "."))


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    txt = re.sub(r"(?s)<[^>]+>", " ", raw)
    import html as _html
    txt = _html.unescape(txt).replace("\xa0", " ")
    return re.sub(r"\s+", " ", txt)


def _vpa_url(month: int, year: int) -> str:
    """T<M>-<Y> report is uploaded the following month: uploads/<Y or Y+1>/<M+1>."""
    pub_y, pub_m = (year, month + 1) if month < 12 else (year + 1, 1)
    return (f"https://www.vpa.org.vn/vn/wp-content/uploads/{pub_y}/{pub_m:02d}/"
            f"VPA_Bao_cao_container_T{month}-{year}_cong_khai.html")


def _parse_vpa_text(txt: str, url: str) -> dict:
    m = re.search(r"Sản lượng container tháng\s*(\d{1,2})/(\d{4})", txt)
    if not m:
        m2 = re.search(r"T(\d{1,2})-(\d{4})_cong_khai", url)
        if not m2:
            raise ValueError("report month not found")
        month, year = int(m2.group(1)), int(m2.group(2))
    else:
        month, year = int(m.group(1)), int(m.group(2))

    def _near(marker: str, pattern: str, span: int = 220):
        i = txt.find(marker)
        if i < 0:
            return None
        return re.search(pattern, txt[i:i + span])

    nat = _near("Sản lượng tháng", r"([\d.]{4,})\s*TEU")
    prev = _near("Cùng kỳ tháng", r"([\d.]{4,})\s*TEU")
    yoym = re.search(r"Tăng so với cùng kỳ|Giảm so với cùng kỳ", txt)
    if not (nat and prev and yoym):
        raise ValueError("national block not parsed")
    seg = txt[yoym.start():yoym.start() + 300]
    yoy = _vn_pct(re.search(r"([\d]+,[\d]{1,2})\s*%", seg).group(1))
    if seg[:6].lower().startswith("giảm"):
        yoy = -yoy
    teu, teu_prev = _vn_int(nat.group(1)), _vn_int(prev.group(1))
    delta = teu - teu_prev

    regions = []
    for key, en, vi in VPA_REGIONS:
        i = txt.find(key)
        if i < 0:
            raise ValueError(f"region not found: {key}")
        seg = txt[i:i + 320]
        ints = re.findall(r"[\d][\d.]{2,}", seg)
        pctm = re.search(r"([+\-\d]+,[\d]{1,2})\s*%", seg)
        if len(ints) < 2 or not pctm:
            raise ValueError(f"region row not parsed: {key}")
        regions.append({"name": en, "nameVi": vi,
                        "teu": _vn_int(ints[0]), "teuPrev": _vn_int(ints[1]),
                        "yoyPct": _vn_pct(pctm.group(1))})

    mix = []
    for vn, en, key in VPA_MIX:
        i = txt.find(vn)
        if i < 0:
            raise ValueError(f"mix row not found: {vn}")
        seg = txt[i:i + 200]
        mm = re.search(r"([\d][\d.]{2,})\s*TEU\s*\(([+\-\d]+,[\d]{1,2})\s*%\)", seg)
        if not mm:
            raise ValueError(f"mix row not parsed: {vn}")
        mix.append({"key": key, "label": en,
                    "teu": _vn_int(mm.group(1)), "pct": _vn_pct(mm.group(2))})

    # sanity: region & mix totals must match the national figure (~0.5% tolerance)
    tol = max(1500, 0.005 * teu)
    if abs(sum(r["teu"] for r in regions) - teu) > tol:
        raise ValueError("region sum mismatch")
    if abs(sum(x["teu"] for x in mix) - teu) > tol:
        raise ValueError("mix sum mismatch")

    import calendar
    label = f"{calendar.month_name[month]} {year}"
    grown = sum(1 for r in regions if r["yoyPct"] >= 0)
    fastest = max(regions, key=lambda r: r["yoyPct"])
    se = next(r for r in regions if r["name"].startswith("Southeast"))
    cm = next(r for r in regions if r["name"].startswith("Cai Mep"))
    north = next(r for r in regions if r["name"] == "Northern")
    highlights = [
        f"Throughput reached {teu:,} TEU in {label}, {yoy:+.2f}% ({delta:+,} TEU) year on year.",
        f"{grown} of 6 regions grew year on year — fastest: {fastest['name']} ({fastest['yoyPct']:+.2f}%).",
        f"Southeast + Cai Mep – Thi Vai handled {(se['teu'] + cm['teu']) / teu * 100:.1f}% of the month; "
        f"the Northern region {north['teu'] / teu * 100:.1f}%.",
    ]
    return {
        "month": f"{year}-{month:02d}", "monthLabel": label,
        "national": {"teu": teu, "teuPrev": teu_prev, "yoyPct": yoy, "deltaTeu": delta},
        "regions": regions, "mix": mix, "highlights": highlights,
        "source": "Vietnam Seaports Association (VPA) — public monthly container report",
        "sourceUrl": url,
        "note": "VPA: includes some estimated/reference figures; barge handling excluded. "
                "Some ports do not report transshipment, so that share may understate actual volume.",
        "dataMode": "live",
    }


def refresh_vpa() -> None:
    """Fetch the newest available VPA monthly report; keep vpa.json on failure."""
    OUT = ROOT / "vpa.json"
    have = None
    if OUT.exists():
        try:
            have = json.loads(OUT.read_text()).get("month")
        except Exception:
            have = None
    today = dt.datetime.now(TZ).date()
    candidates = []
    for lag in (1, 2, 3):
        m = today.month - lag
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        candidates.append((m, y))
    for m, y in candidates:
        url = _vpa_url(m, y)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "vn-port-dashboard/1.0"})
            raw = urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "ignore")
        except Exception as e:
            print(f"VPA T{m}-{y}: fetch failed ({e})")
            continue
        try:
            data = _parse_vpa_text(_strip_html(raw), url)
        except Exception as e:
            print(f"VPA T{m}-{y}: parse failed ({e})")
            continue
        if have and data["month"] <= have:
            print(f"VPA report {data['month']} already stored — keeping existing vpa.json")
            return
        OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        print(f"wrote vpa.json ({data['monthLabel']}: {data['national']['teu']:,} TEU)")
        return
    print("no VPA report fetched — keeping existing vpa.json")

# ------------------------------------------------- live AIS merge ----
# Destination-string → tracked port (AIS destinations are free text).
DEST_RULES = [
    ("CAIMEP",   re.compile(r"CAI\s?MEP|CAIMEP|VN\s?CMT|THI\s?VAI|VUNG\s?TAU|VUNGTAU", re.I)),
    ("HCMC",     re.compile(r"HOCHIMINH|HO\s?CHI\s?MINH|VN\s?SGN|CAT\s?LAI|CATLAI|SAIGON", re.I)),
    ("HAIPHONG", re.compile(r"VN\s?HPH|HAIPHONG|HAI\s?PHONG", re.I)),
    ("DANANG",   re.compile(r"VN\s?DAD|DANANG|DA\s?NANG", re.I)),
]
CLASS_DRAFT = {"ULCV": 15.5, "NEOPAN": 14.5, "PANAMAX": 13.0, "FEEDER": 10.0}


def _est_capacity(loa: float):
    """Rough (capacity, class) from length overall — AIS carries dimensions,
    not TEU capacity, so this is a labelled estimate."""
    if loa >= 330:
        return 18000, "ULCV"
    if loa >= 292:
        return 12500, "NEOPAN"
    if loa >= 240:
        return 5500, "PANAMAX"
    if loa >= 170:
        return 2500, "FEEDER"
    return 1200, "FEEDER"


def _dest_port(dest: str):
    for pid, rx in DEST_RULES:
        if rx.search(dest or ""):
            return pid
    return None


def _ais_eta(e, now):
    """AIS ETA = {Month,Day,Hour,Minute} in UTC, no year; 0s = not available."""
    try:
        month, day = int(e["Month"]), int(e["Day"])
        hour, minute = int(e["Hour"]), int(e["Minute"])
        if not (1 <= month <= 12 and 1 <= day <= 31 and hour <= 23 and minute <= 59):
            return None
        cand = dt.datetime(now.year, month, day, hour, minute, tzinfo=dt.timezone.utc)
        if cand < now - dt.timedelta(days=180):
            cand = cand.replace(year=now.year + 1)
        return cand.astimezone(TZ)
    except Exception:
        return None




# ------------------------------------------------ terminal/pilot schedules ----
# Free public daily plans, best-effort parsed, previous data kept on failure.
#   - Tan Cang Pilot (tancangpilot.com.vn): Cat Lai / HCMC river + Cai Mep —
#     arriving / Cai Mep / departing / shifting with draught, LOA, wharf, POB.
#   - shippingschedule.com.vn (Cảng vụ Hải Phòng data): Hai Phong arrivals /
#     departures with destination terminal (HICT / HHIT / Chua Ve ...).
TCP_URL = "https://tancangpilot.com.vn/en/ship-plan"
HP_URL = "https://shippingschedule.com.vn/ke-hoach-tau/hai-phong.html"
SCHED_STORE = ROOT / "datastore" / "schedules.json"

# wharf/terminal codes -> (our port id, terminal hint for display)
def _tcp_wharf_port(wharf: str, direction: str):
    w = (wharf or "").upper()
    # last leg is the berth side for arrivals, first leg for departures
    leg = w.split(" - ")[-1] if direction.startswith("ARRIV") else w.split(" - ")[0]
    if re.search(r"CM\d|TCIT|CMIT|TCTT|VICT|VN\s?CMT", leg):
        return "CAIMEP", leg
    if re.search(r"CL\d", leg):
        return "HCMC", "Cat Lai"
    if re.search(r"BNPH|BP\d|H\d{2}|CANGIO|CAN\s?GIO|TC\b", leg):
        return "HCMC", "Tan Cang"
    if direction == "ARRIVING_CM":
        return "CAIMEP", leg
    return "HCMC", leg  # Tan Cang pilot covers the HCMC river system

HP_TERMINALS = {  # destination text -> (terminal hint) — only our tracked terminals
    "HICT": "Lach Huyen (HICT)", "HHIT": "Lach Huyen (HHIT)",
    "HTIT": "Lach Huyen (HHIT)", "CHUA VE": "Chua Ve",
    "LACH HUYEN 1": "Lach Huyen (HICT)", "LACH HUYEN 2": "Lach Huyen (HHIT)",
}



def _vn_float(s: str):
    """VN formats: '9,2' -> 9.2 ; '25.329' -> 25329 ; '1.234,5' -> 1234.5"""
    s = (s or "").strip()
    if not s:
        return None
    try:
        if "," in s and "." in s:
            return float(s.replace(".", "").replace(",", "."))
        if "," in s:
            return float(s.replace(",", "."))
        if s.count(".") == 1 and len(s.split(".")[1]) == 3:
            return float(s.replace(".", ""))   # thousands
        return float(s)
    except ValueError:
        return None

def _hm_to_dt(day: dt.date, hm: str, ref: dt.datetime, roll_past=False):
    hh, mm = int(hm[:2]), int(hm[3:5])
    t = dt.datetime.combine(day, dt.time(hh, mm), TZ)
    if roll_past and t < ref - dt.timedelta(hours=3):
        t += dt.timedelta(days=1)  # "//0230" after 23:30 POB = next day
    return t


def parse_tancangpilot(raw: str):
    m = re.search(r"Update time:\s*(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})", raw)
    if not m:
        raise ValueError("tcp: no update time")
    plan_dt = dt.datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                          int(m.group(4)), int(m.group(5)), tzinfo=TZ)
    panes = re.split(r'id="shiptype_\d"', raw)[1:]
    direction = {0: "ARRIVING", 1: "ARRIVING_CM", 2: "DEPARTING", 3: "SHIFTING"}
    recs = []
    for idx, pane in enumerate(panes[:4]):
        rows = re.findall(r'<div class="tr">(.*?)(?=<div class="tr">|$)', pane, re.S)
        for row in rows:
            cells = [re.sub(r"<[^>]+>", " ", c) for c in
                     re.findall(r'<div class="td">(.*?)</div>', row, re.S)]
            cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
            if len(cells) < 8 or not cells[0].isdigit():
                continue
            name = cells[2].strip()
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9 .'\-()]{2,40}", name):
                continue
            pob = re.search(r"(\d{1,2}:\d{2})", cells[7])
            if not pob:
                continue
            note = cells[8] if len(cells) > 8 else ""
            alongside = re.search(r"//\s*(\d{1,2})(\d{2})", note)
            port, thint = _tcp_wharf_port(cells[6], direction[idx])
            recs.append({
                "source": "tancangpilot", "direction": direction[idx],
                "vessel": name, "draught": _vn_float(cells[3]), "loa": _vn_float(cells[4]),
                "wharf": cells[6], "port": port, "terminalHint": thint,
                "pob": pob.group(1),
                "alongside": (alongside.group(1).zfill(2) + ":" + alongside.group(2))
                             if alongside else None,
                "planDt": plan_dt.isoformat(),
            })
    return recs


def parse_haiphong(raw: str):
    m = re.search(r"NGÀY\s*(\d{2})/(\d{2})/(\d{4})", raw)
    if not m:
        raise ValueError("hp: no plan date")
    day = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    recs = []
    # sections in page order; qua luồng duplicates vào cảng -> skip
    secs = re.split(r"Kế hoạch tàu", raw)
    for sec in secs:
        direction = None
        head = re.sub(r"^[\s<][^>]*>", "", sec, count=1)  # leading tag/space
        head = re.sub(r"^\s*(?:</?[^>]+>\s*)*", "", sec)[:20]
        if re.match(r"rời cảng", head):
            direction = "DEPARTING"
        elif re.match(r"vào cảng", head):
            direction = "ARRIVING"
        if not direction:
            continue
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", sec, re.S):
            cells = [re.sub(r"<[^>]+>", " ", c) for c in
                     re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
            if len(cells) < 10 or not cells[0].isdigit():
                continue
            name = cells[2].replace("(SB)", "").strip()
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9 .'\-()]{2,40}", name):
                continue
            frm, to = cells[8].upper(), cells[9].upper()
            if direction == "ARRIVING":
                thint = HP_TERMINALS.get(to)
                if not thint:
                    continue  # real call, but not one of our tracked terminals
                recs.append({"source": "haiphong-plan", "direction": direction,
                             "vessel": name, "draught": _vn_float(cells[3]), "loa": _vn_float(cells[4]),
                             "port": "HAIPHONG", "terminalHint": thint,
                             "time": cells[1], "prevPort": frm.title(), "nextPort": "",
                             "planDt": day.isoformat()})
            else:
                thint = HP_TERMINALS.get(frm, frm.title())
                recs.append({"source": "haiphong-plan", "direction": direction,
                             "vessel": name, "draught": _vn_float(cells[3]), "loa": _vn_float(cells[4]),
                             "port": "HAIPHONG", "terminalHint": thint,
                             "time": cells[1], "prevPort": "", "nextPort": to.title(),
                             "planDt": day.isoformat()})
    return recs


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "vn-port-dashboard/1.0"})
    return urllib.request.urlopen(req, timeout=45).read().decode("utf-8", "ignore")


def refresh_schedules() -> list:
    """Fetch + parse the public daily plans. Keeps yesterday's file on failure.
    Returns normalized records (may be empty)."""
    recs = []
    try:
        got = parse_tancangpilot(_fetch(TCP_URL))
        print(f"tancangpilot: {len(got)} plan rows")
        recs += got
    except Exception as e:
        print("tancangpilot fetch/parse failed:", e)
    try:
        got = parse_haiphong(_fetch(HP_URL))
        print(f"haiphong plan: {len(got)} rows")
        recs += got
    except Exception as e:
        print("haiphong plan fetch/parse failed:", e)
    if recs:
        SCHED_STORE.parent.mkdir(exist_ok=True)
        SCHED_STORE.write_text(json.dumps(recs, ensure_ascii=False, indent=1))
        return recs
    if SCHED_STORE.exists():
        try:
            return json.loads(SCHED_STORE.read_text())
        except Exception:
            pass
    return []

# ------------------------------------------- stay history & predictions ----
# Every completed, AIS-observed port stay is appended to datastore/history.jsonl
# so the dashboard learns real turn-around times per vessel / class / port and
# applies them to future calls instead of flat model windows.
HIST_STORE = ROOT / "datastore" / "history.jsonl"
CLASS_STAY_H = {"ULCV": 30.0, "NEOPAN": 26.0, "PANAMAX": 18.0, "FEEDER": 14.0}


def _hist_rows() -> list:
    if not HIST_STORE.exists():
        return []
    rows = []
    for line in HIST_STORE.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def record_stay(rec: dict) -> None:
    """Append one completed observed stay (real eta + real etd) to the history
    store. Dedupes on (id, eta) so daily reruns never double-count a stay."""
    try:
        eta = dt.datetime.fromisoformat(rec["eta"])
        etd = dt.datetime.fromisoformat(rec["etd"])
    except Exception:
        return
    stay_h = (etd - eta).total_seconds() / 3600.0
    if not (1.0 <= stay_h <= 240.0):
        return  # implausible pair — don't learn from bad data
    key = (rec.get("id"), rec["eta"])
    if any((r.get("id"), r.get("eta")) == key for r in _hist_rows()):
        return
    HIST_STORE.parent.mkdir(exist_ok=True)
    with HIST_STORE.open("a") as f:
        f.write(json.dumps({
            "id": rec.get("id"), "vessel": rec.get("vessel"),
            "port": rec.get("port"), "terminal": rec.get("terminal") or "",
            "vclass": rec.get("vclass"), "loa": rec.get("loa"),
            "stayH": round(stay_h, 1), "eta": rec["eta"], "etd": rec["etd"],
        }, ensure_ascii=False) + "\n")


def predict_stay(vessel_id: str, vclass: str, port: str):
    """Median observed port stay — the vessel itself first (n>=2), then its
    class at this port (n>=3), then a class default. Returns (hours, basis)."""
    rows = _hist_rows()
    own = sorted(r["stayH"] for r in rows if r.get("id") == vessel_id)
    if len(own) >= 2:
        return own[len(own) // 2], f"this vessel's observed history (n={len(own)})"
    cp = sorted(r["stayH"] for r in rows
                if r.get("vclass") == vclass and r.get("port") == port)
    if len(cp) >= 3:
        return cp[len(cp) // 2], f"observed {vclass} stays at {port} (n={len(cp)})"
    return CLASS_STAY_H.get(vclass, 18.0), f"{vclass} class default (learning)"


# ------------------------------------------------- schedule -> port calls ----
def _sched_day(sr: dict) -> dt.date:
    try:
        return dt.date.fromisoformat((sr.get("planDt") or "")[:10])
    except Exception:
        return dt.datetime.now(TZ).date()


def _norm_name(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def sched_to_call(sr: dict, now: dt.datetime):
    """Convert one pilot/terminal plan row into a PortCall-shaped dict.
    Plan times: Tan Cang 'P.O.B' = pilot-on-board (arrivals) / unmooring
    (departures); '//HHMM' in the note = real alongside time. Hai Phong
    'time' = planned pilot time. Returns None for stale/far-future rows."""
    day = _sched_day(sr)
    port = sr.get("port")
    if port not in {p["id"] for p in PORTS}:
        return None
    dr = sr.get("draught")
    loa = float(sr.get("loa") or 0)
    cap, vclass = _est_capacity(loa) if loa else (2500, "FEEDER")
    if loa and loa < 100 and float(dr or 9) < 4.5:
        cap = max(60, int(loa * 1.6))  # river barge / small coaster in the pilot plan
    draft_in = round(float(dr), 1) if dr else round(CLASS_DRAFT[vclass] * 0.8, 1)
    # The plan's draught is REAL (the terminal accepted it), so berth
    # feasibility keys off plan draught + a small margin — the class-default
    # proxy would wrongly bar small feeders from their published berths.
    max_draft = round(float(dr) + 0.5, 1) if dr else CLASS_DRAFT[vclass]
    draft_out = round(min(max_draft, draft_in + 0.6), 1)

    if sr["source"] == "tancangpilot":
        pob = _hm_to_dt(day, sr["pob"], now)
        if sr["direction"] == "DEPARTING":
            etd = pob + dt.timedelta(minutes=30)
            pred, basis = predict_stay("", vclass, port)
            eta = etd - dt.timedelta(hours=pred)
        else:
            if sr.get("alongside"):
                eta = _hm_to_dt(day, sr["alongside"], now, roll_past=True)
            else:
                steam = 1.2 if port == "CAIMEP" else 2.5  # pilot station -> berth, h
                eta = pob + dt.timedelta(hours=steam)
            pred, basis = predict_stay("", vclass, port)
            etd = eta + dt.timedelta(hours=pred)
        prev, nxt = "", ""
    else:  # haiphong-plan
        t = _hm_to_dt(day, sr["time"], now)
        if sr["direction"] == "DEPARTING":
            etd = t + dt.timedelta(minutes=30)
            pred, basis = predict_stay("", vclass, port)
            eta = etd - dt.timedelta(hours=pred)
        else:
            eta = t + dt.timedelta(hours=1.5)   # pilot station -> Lach Huyen / Chua Ve
            pred, basis = predict_stay("", vclass, port)
            etd = eta + dt.timedelta(hours=pred)
        prev, nxt = sr.get("prevPort", ""), sr.get("nextPort", "")

    if etd < now - dt.timedelta(days=1) or eta > now + dt.timedelta(days=3):
        return None
    return {
        "id": f"S{_norm_name(sr['vessel'])[:12]}{day.strftime('%m%d')}",
        "vessel": sr["vessel"], "imo": "", "carrier": "—",
        "vclass": vclass, "capacity": cap, "loa": loa,
        "maxDraft": round(max_draft, 1), "port": port,
        "terminal": sr.get("terminalHint") or "", "berth": "",
        "terminalHint": sr.get("terminalHint") or "",
        "eta": iso(eta), "schedEta": iso(eta), "ata": None,
        "etd": iso(etd), "atd": None,
        "draftIn": draft_in, "draftOut": draft_out,
        "prevPort": prev, "nextPort": nxt,
        "status": "EXPECTED", "stayHours": None, "waitH": 0.0,
        "speedKn": CLASS_SPEED_KN[vclass],
        "estTeu": estimate_teu(cap, max_draft, draft_in, draft_out, vclass, port),
        "stayPredH": pred, "stayBasis": basis,
        "source": "schedule",
    }


def load_schedules() -> list:
    if not SCHED_STORE.exists():
        return []
    try:
        return json.loads(SCHED_STORE.read_text())
    except Exception:
        return []


def merge_schedules(calls: list, scheds: list) -> list:
    """Fold public pilot/terminal plans into the call list.
    An AIS-observed call for the same vessel+port keeps its observed actuals;
    the plan refines modelled ETD/ETA and adds the wharf. Ships seen only in
    the plans become schedule-sourced calls."""
    if not scheds:
        return calls
    now = dt.datetime.now(TZ)
    by_key = {}
    for c in calls:
        by_key.setdefault((_norm_name(c["vessel"]), c["port"]), c)
    added = enriched = 0
    for sr in scheds:
        if sr.get("direction") == "SHIFTING":
            continue
        call = sched_to_call(sr, now)
        if not call:
            continue
        key = (_norm_name(call["vessel"]), call["port"])
        hit = by_key.get(key)
        if hit:
            enriched += 1
            if "schedule" not in (hit.get("source") or ""):
                hit["source"] = (hit.get("source") or "ais") + "+schedule"
            if call["terminalHint"] and not hit.get("terminalHint"):
                hit["terminalHint"] = call["terminalHint"]
            # the plan refines modelled times only — observed actuals untouched
            if sr["direction"] == "DEPARTING":
                if not hit.get("_obsDep"):
                    hit["etd"] = call["etd"]
            elif not hit.get("_obsArr"):
                hit["eta"] = call["eta"]
                hit["etd"] = call["etd"]
            if hit.get("stayPredH") is None:
                hit["stayPredH"], hit["stayBasis"] = call["stayPredH"], call["stayBasis"]
        else:
            calls.append(call)
            by_key[key] = call
            added += 1
    if added or enriched:
        print(f"schedule merge: {added} plan-only call(s) added, {enriched} AIS call(s) enriched")
    return calls


def build_live_calls() -> list:
    """Merge datastore AIS events/state into PortCall-shaped live records.
    Returns [] when nothing usable was observed (dashboard stays demo)."""
    store = ROOT / "datastore"
    ev_file, st_file = store / "events.jsonl", store / "ais_state.json"
    if not ev_file.exists() and not st_file.exists():
        return []
    now = dt.datetime.now(TZ)
    cutoff = (now - dt.timedelta(days=20)).isoformat()

    events = []
    if ev_file.exists():
        for line in ev_file.read_text().splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("time", "") >= cutoff:
                events.append(ev)
    events.sort(key=lambda e: e["time"])

    state = {}
    if st_file.exists():
        try:
            state = json.loads(st_file.read_text())
        except Exception:
            state = {}

    def is_cargo(st):
        t = st.get("type")
        return not isinstance(t, int) or 70 <= t <= 79  # unknown type: keep

    def mk_record(mmsi, ev, st, port, eta, etd):
        loa = float(ev.get("loa") or st.get("loa") or 0)
        cap, vclass = _est_capacity(loa)
        max_draft = float(ev.get("maxDraft") or st.get("maxDraft") or 0) or CLASS_DRAFT[vclass]
        draft_in = float(ev.get("draft") or st.get("draught") or 0) or round(max_draft * 0.8, 1)
        draft_out = round(min(max_draft, draft_in + 0.6), 1)
        name = (ev.get("vessel") or st.get("name") or "").strip() or f"MMSI {mmsi}"
        return {
            "id": f"M{mmsi}", "vessel": name, "imo": str(ev.get("imo") or st.get("imo") or ""),
            "carrier": "—", "vclass": vclass, "capacity": cap, "loa": loa,
            "maxDraft": round(max_draft, 1), "port": port, "terminal": "", "berth": "",
            "eta": iso(eta), "schedEta": iso(eta), "ata": None,
            "etd": iso(etd), "atd": None,
            "draftIn": round(draft_in, 1), "draftOut": draft_out,
            "prevPort": "",
            # real broadcast destination (cleaned); tracked-port id when it maps
            "nextPort": (st.get("dest") or "").strip().upper() or (_dest_port(st.get("dest", "")) or ""),
            "status": "EXPECTED", "stayHours": None, "waitH": 0.0,
            "speedKn": float(st.get("sog") or CLASS_SPEED_KN[vclass]),
            "estTeu": estimate_teu(cap, max_draft, draft_in, draft_out, vclass, port),
            "terminalHint": "", "stayPredH": None, "stayBasis": None,
            "source": "ais",
        }

    # --- observed arrivals/departures (geofence events) ---
    calls, open_arr, seen = [], {}, {}
    for ev in events:
        mmsi, port = ev.get("mmsi", ""), ev.get("port", "")
        if not mmsi or port not in {p["id"] for p in PORTS}:
            continue
        st = state.get(mmsi, {})
        if not is_cargo(st):
            continue
        loa_chk = float(ev.get("loa") or st.get("loa") or 0)
        if loa_chk and loa_chk < 100:
            continue  # river-barge band — tracked as corridor TRANSIT, not a port call
        key = (mmsi, port)
        if ev["event"] == "ARRIVAL":
            open_arr.setdefault(key, ev)
        elif ev["event"] == "DEPARTURE" and key in open_arr:
            arr = open_arr.pop(key)
            eta = dt.datetime.fromisoformat(arr["time"])
            etd = dt.datetime.fromisoformat(ev["time"])
            if etd <= eta:
                continue
            r = mk_record(mmsi, arr, st, port, eta, etd)
            r["stayBasis"] = "observed via AIS"
            r["_obsArr"] = r["_obsDep"] = True
            record_stay(r)
            calls.append(r)
            seen[key] = eta
    n_silent = 0
    for (mmsi, port), arr in open_arr.items():
        st = state.get(mmsi, {})
        eta = dt.datetime.fromisoformat(arr["time"])
        if eta > now:
            continue
        last_seen = None
        try:
            last_seen = dt.datetime.fromisoformat(st["lastSeen"])
        except Exception:
            pass
        if last_seen and last_seen > eta and (now - last_seen) > dt.timedelta(hours=12):
            # Ship broadcast from inside the port, then went silent for 12h+.
            # Class-A AIS transmits every ~3 min even at anchor, so long
            # silence = left reception range = sailed. Close the call at the
            # last real contact in VN waters (real timestamp, honestly labelled).
            r = mk_record(mmsi, arr, st, port, eta, last_seen)
            r["stayBasis"] = "closed at last AIS contact (12h silent = sailed)"
            r["_obsArr"] = r["_obsDep"] = True
            record_stay(r)
            n_silent += 1
        else:
            loa = float(arr.get("loa") or st.get("loa") or 0)
            _, vclass = _est_capacity(loa)
            pred, basis = predict_stay(f"M{mmsi}", vclass, port)
            r = mk_record(mmsi, arr, st, port, eta, eta + dt.timedelta(hours=pred))
            r["stayPredH"], r["stayBasis"] = round(pred, 1), basis
            r["_obsArr"] = True
        calls.append(r)
        seen[(mmsi, port)] = eta
    if n_silent:
        print(f"closed {n_silent} call(s) at last AIS contact (ship went silent = sailed)")

    # --- expected arrivals (Destination + ETA broadcast) ---
    max_draft_of = {p["id"]: p["maxDraftM"] for p in PORTS}
    for mmsi, st in state.items():
        port = _dest_port(st.get("dest", ""))
        if not port or st.get("inside") == port or not is_cargo(st):
            continue
        loa_chk = float(st.get("loa") or 0)
        if loa_chk and loa_chk < 100:
            continue  # barge band
        # A ship too deep for its broadcast destination's river berths
        # operationally means the deep-water cluster (Cat Lai max 11 m):
        # remap HCMC -> Cai Mep so the berth plan stays physically possible.
        md = float(st.get("maxDraft") or 0)
        if port == "HCMC" and md and md > max_draft_of["HCMC"]:
            port = "CAIMEP"
        eta = _ais_eta(st.get("etaAis") or {}, now)
        if not eta or not (now < eta <= now + dt.timedelta(days=5)):
            continue
        near = seen.get((mmsi, port))
        if near and abs((near - eta).total_seconds()) < 12 * 3600:
            continue  # already have the observed call for this ship
        loa = float(st.get("loa") or 0)
        _, vclass = _est_capacity(loa)
        pred, basis = predict_stay(f"M{mmsi}", vclass, port)
        r = mk_record(mmsi, {}, st, port, eta, eta + dt.timedelta(hours=pred))
        r["nextPort"] = port
        r["stayPredH"], r["stayBasis"] = round(pred, 1), basis
        calls.append(r)

    calls.sort(key=lambda c: c["eta"])
    if not calls:
        return []
    assign_berths(calls, random.Random(7))
    return calls


# ---------------------------------------------------------------- main ----
def build_all() -> None:
    """Regenerate every operational JSON for 'today' (Asia/Ho_Chi_Minh)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(TZ).date()

    live_calls = build_live_calls()
    scheds = load_schedules()
    if live_calls:
        calls, mode = live_calls, "live"
        print(f"live mode: {len(calls)} AIS-derived port calls")
        for c in calls:
            c.setdefault("source", "ais")
    elif scheds:
        # no AIS key yet, but real public pilot/terminal plans are available:
        # the board runs on those instead of the demo fleet
        calls, mode = [], "schedule"
    else:
        calls, mode = make_demo_calls(today), "demo"
        for c in calls:
            c.setdefault("source", "demo")
    if scheds:
        calls = merge_schedules(calls, scheds)
        calls.sort(key=lambda c: c["eta"])
        assign_berths(calls, random.Random(7))  # berth + status pass incl. plan ships

    # daily TEU series from calls (last 14 days)
    daily = []
    for offset in range(-13, 1):
        day = (today + dt.timedelta(days=offset)).isoformat()
        row = {"date": day}
        total = 0
        for p in PORTS:
            v = sum(c["estTeu"] for c in calls
                    if c["port"] == p["id"] and c["eta"][:10] == day)
            row[p["id"]] = v
            total += v
        row["total"] = total
        daily.append(row)

    customs = build_customs()
    inland = make_inland(today, customs)
    report = build_report(today, calls, daily, customs)

    # VPA monthly throughput bullet (real association statistics, if present)
    vpa_file = DATA_DIR / "vpa.json"
    if vpa_file.exists():
        try:
            vpa = json.loads(vpa_file.read_text())
            nv = vpa["national"]
            cm = next((r for r in vpa["regions"] if r["name"].startswith("Cai Mep")), None)
            bullet = (f"VPA {vpa['monthLabel']}: national throughput {nv['teu'] / 1e6:.2f}m TEU "
                      f"({nv['yoyPct']:+.2f}% y/y)"
                      + (f"; Cai Mep – Thi Vai {cm['yoyPct']:+.2f}% y/y." if cm else "."))
            report["bullets"].append(bullet)
        except Exception as e:
            print("vpa bullet skipped:", e)

    def dump(name, obj):
        (DATA_DIR / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False))
        print("wrote", name)

    dump("ports.json", PORTS)
    for c in calls:
        c.pop("_obsArr", None)
        c.pop("_obsDep", None)
    dump("calls.json", calls)
    dump("daily.json", daily)
    dump("customs.json", customs)
    dump("inland.json", inland)
    dump("report.json", report)
    if mode == "demo":
        note_calls = ("Vessel calls & TEU estimates: demo fleet "
                      "(AISstream live feed and public terminal plans not connected)")
    elif mode == "schedule":
        note_calls = ("Vessel calls from free public pilot/terminal plans (Tan Cang Pilot + "
                      "Cảng vụ Hải Phòng port authority, fetched daily); times beyond the published "
                      "plan, berth windows & TEU are modelled estimates; AISstream live feed not connected")
    else:
        note_calls = ("Vessel arrivals/departures observed via AISstream (live)"
                      + (" + free public pilot/terminal plans (Tan Cang Pilot, Cảng vụ Hải Phòng)"
                         if scheds else "")
                      + "; unobserved exits close at last AIS contact; berth windows & TEU remain modelled estimates")
    notes = [
        note_calls,
        "TEU figures are model estimates from draft & port stay — not terminal-confirmed lifts",
        "National trade totals are official (GDVC); province/commodity split is a demo model",
        "Monthly national/region throughput is real VPA association data",
        "ICD volumes & barge flows are corridor-model estimates (see Inland network notes)",
    ]
    if mode != "demo":
        notes.append("Port-stay predictions learn from AIS-observed stay history (per vessel, then "
                     "class-at-port, then class default) — basis shown per call")
    dump("meta.json", {
        "generatedAt": iso(dt.datetime.now(TZ)),
        "dataMode": mode,
        "schedSources": sorted({s.get("source") for s in scheds}) if scheds else [],
        "tz": "Asia/Ho_Chi_Minh",
        "notes": notes,
    })


def main() -> None:
    print("=== 1/5 VPA monthly throughput ===")
    refresh_vpa()
    print("=== 2/5 terminal & pilot schedules ===")
    refresh_schedules()
    print("=== 3/5 build operational data ===")
    build_all()
    print("=== 4/5 refresh official customs totals ===")
    refresh_customs()
    print("=== 5/5 refresh news ===")
    refresh_news()
    print("refresh complete")


if __name__ == "__main__":
    main()
