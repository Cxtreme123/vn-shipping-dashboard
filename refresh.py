#!/usr/bin/env python3
"""
Daily data refresh for the VN Port Watch dashboard (single-file edition, v5.2).

Runs unattended in GitHub Actions (see .github/workflows/update.yml).
Standard library only — no pip installs needed.

What it does, in order:
  1. refresh_vpa()      — newest VPA monthly container-throughput report
                          (real association statistics; parsed & validated,
                          previous file kept on any failure).
  2. refresh_schedules()— free public daily pilot/terminal plans (Tan Cang
                          Pilot: Cat Lai / HCMC river + Cai Mep; Cảng vụ
                          Hải Phòng data: Hai Phong arrivals/departures).
  3. Open Waters AIS snapshots (AISHub aggregate) folded into the persistent
                          visit table datastore/ow_visits.json.
  4. build_all()        — regenerates ports/calls/daily/customs/inland/report/
                          meta/visits JSON for "today" (Asia/Ho_Chi_Minh).
                          Vessel calls come from the AIS visit table when
                          available, else the public plans, else the clearly
                          labelled DEMO fleet. Modelled TEU are calibrated to
                          the VPA monthly regional anchors once ≥10 days of
                          observed history exist. Observed stays are learned
                          into datastore/history.jsonl and drive predicted
                          port stays.
  5. refresh_customs()  — best-effort scrape of GDVC headline monthly totals.
  6. refresh_news()     — latest Vietnam trade/logistics news from GDELT
                          (free, no key); keeps yesterday's file on failure.
                          Headlines are machine-translated VI→EN (free
                          endpoints, per-string cache; original kept on any
                          failure and shown on hover).

Every source fetch appends to the HEALTH list; meta.json carries it so the
UI can flag stale/failed feeds instead of silently serving old data.

Data honesty contract: every JSON carries a dataMode marker ("live"/"demo") and
the UI shows a visible DEMO DATA badge for synthetic content.
"""
import datetime as dt
import html as _html
import json
import math
import pathlib
import random
import re
import time
import unicodedata
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
DATA_DIR = ROOT  # flat repo: JSONs live next to index.html
TZ = dt.timezone(dt.timedelta(hours=7))  # Asia/Ho_Chi_Minh

# ---------------------------------------------------------------- ports ----
# terminal "geo" = approximate centroid for AIS->terminal assignment only
def _berths(ids, length, depth, maxclass):
    """Berth specs are approximate public figures (terminal operator data)."""
    return [{"id": i, "lengthM": length, "depthM": depth, "maxClass": maxclass} for i in ids]

PORTS = [
    {
        "id": "HAIPHONG", "name": "Hai Phong", "locode": "VNHPH", "region": "North",
        "lat": 20.865, "lon": 106.683, "maxDraftM": 16.8,
        "terminals": [
            # Lach Huyen deep-sea berths (sourced: HICT b1-2 750m/-16m;
            # HTIT b3-4 750m/-16m; HHIT b5-6 ~900m/16.8-18.4m alongside)
            {"name": "Lach Huyen (HICT)", "geo": [20.832, 106.789], "berths": _berths(["H1", "H2"], 380, 16.0, "NEOPAN")},
            {"name": "Lach Huyen (HTIT)", "geo": [20.827, 106.797], "berths": _berths(["T3", "T4"], 375, 16.0, "NEOPAN")},
            {"name": "Lach Huyen (HHIT)", "geo": [20.822, 106.805], "berths": _berths(["I5", "I6"], 450, 16.8, "ULCV")},
            # Dinh Vu / river terminals — berth splits INDICATIVE (operator
            # berth numbering not public); depths from operator/VPA data
            {"name": "Nam Dinh Vu", "berths": _berths(["NDV1", "NDV2", "NDV3", "NDV4"], 240, 9.5, "FEEDER")},
            {"name": "Nam Hai Dinh Vu", "berths": _berths(["NHD1", "NHD2"], 228, 8.5, "FEEDER")},
            {"name": "VIMC Dinh Vu", "berths": _berths(["VDV1", "VDV2", "VDV3"], 210, 8.5, "FEEDER")},
            {"name": "VIP Greenport", "berths": _berths(["VGP1", "VGP2"], 220, 8.5, "FEEDER")},
            {"name": "Tan Vu", "berths": _berths(["TV1", "TV2"], 200, 8.5, "FEEDER")},
            {"name": "Hai An", "berths": _berths(["HAN1", "HAN2"], 200, 8.0, "FEEDER")},
            {"name": "Nam Hai", "berths": _berths(["NH1", "NH2"], 200, 8.0, "FEEDER")},
            {"name": "Chua Ve", "geo": [20.869, 106.667], "berths": _berths(["C1", "C2", "C3", "C4"], 180, 8.5, "FEEDER")},
        ],
        "role": "Northern deep-water gateway; direct US/EU mainline calls at Lach Huyen",
        "geofence": [[20.72, 106.55], [20.98, 106.95]],  # AISstream bounding box
    },
    {
        "id": "DANANG", "name": "Da Nang", "locode": "VNDAD", "region": "Central",
        "lat": 16.115, "lon": 108.210, "maxDraftM": 11.0,
        "terminals": [
            {"name": "Tien Sa", "geo": [16.117, 108.213], "berths": _berths(["T1", "T2", "T3"], 230, 11.0, "PANAMAX")},
        ],
        "role": "Central coast regional port; intra-Asia feeders",
        "geofence": [[16.02, 108.12], [16.22, 108.32]],
    },
    {
        "id": "HCMC", "name": "Ho Chi Minh (Cat Lai)", "locode": "VNSGN", "region": "South",
        "lat": 10.755, "lon": 106.760, "maxDraftM": 11.0,
        "terminals": [
            {"name": "Cat Lai", "geo": [10.767, 106.772], "berths": _berths(["A1", "A2", "A3", "A4", "A5", "A6"], 220, 10.5, "PANAMAX")},
            {"name": "Tan Cang", "geo": [10.772, 106.715], "berths": _berths(["R1", "R2"], 170, 9.0, "FEEDER")},
            # berth splits indicative (operator numbering not public)
            {"name": "Tan Cang Hiep Phuoc", "geo": [10.702, 106.743], "berths": _berths(["TCH1", "TCH2"], 200, 9.5, "FEEDER")},
            {"name": "SP-ITC", "berths": _berths(["SPC1", "SPC2"], 210, 10.0, "FEEDER")},
        ],
        "role": "Busiest river terminal; draft-limited to ~5,000 TEU feeders",
        "geofence": [[10.68, 106.68], [10.83, 106.85]],
    },
    {
        "id": "CAIMEP", "name": "Cai Mep - Thi Vai", "locode": "VNCMT", "region": "South",
        "lat": 10.520, "lon": 107.020, "maxDraftM": 16.0,
        "terminals": [
            {"name": "CMIT", "geo": [10.546, 107.02], "berths": _berths(["M1", "M2", "M3"], 330, 15.8, "ULCV")},
            {"name": "TCIT", "geo": [10.552, 107.014], "berths": _berths(["T1", "T2", "T3"], 300, 15.5, "ULCV")},
            {"name": "SSIT", "geo": [10.6, 107.005], "berths": _berths(["S1", "S2"], 320, 16.0, "ULCV")},
            {"name": "SP-PSA", "geo": [10.59, 106.985], "berths": _berths(["P1", "P2"], 300, 14.0, "NEOPAN")},
            # Gemalink (Gemadept–Terminal Link) — berth split indicative
            {"name": "Gemalink", "geo": [10.612, 107.022], "berths": _berths(["GML1", "GML2"], 400, 16.0, "ULCV")},
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
            pool = hinted or free  # prefer the plan's / observed terminal
            # best-fit: the shallowest berth that still floats the vessel —
            # deep-water berths stay reserved for the ships that need them
            # (otherwise river barges would occupy Lach Huyen / Cai Mep rows)
            tsel, bsel = min(pool, key=lambda tb: (tb[1]["depthM"], tb[1]["lengthM"]))
        else:
            pool = fit or [(t, b) for t in pmeta["terminals"] for b in t["berths"]]
            tsel, bsel = min(pool, key=lambda tb: busy_until.get(
                (c["port"], tb[0]["name"], tb[1]["id"]), never))
            free_at = busy_until.get((c["port"], tsel["name"], bsel["id"]), never)
            wait_h = (free_at - eta).total_seconds() / 3600
            if wait_h > 6 and c.get("_obsArr"):
                # AIS saw this vessel inside the port, but no modelled berth is
                # free: it's alongside a terminal we don't track or at anchor.
                # Show it in the call list unassigned rather than slip it days.
                c["terminal"], c["berth"] = "", ""
                c["waitH"] = 0.0
                c["status"] = "IN_PORT" if not c.get("_obsDep") else "DEPARTED"
                if c["status"] == "DEPARTED":
                    c["stayHours"] = round((etd - eta).total_seconds() / 3600, 1)
                else:
                    c["stayHours"] = round(max(0.0, (now - eta).total_seconds() / 3600), 1)
                    c["ata"] = iso(eta)
                continue
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
def build_report(today: dt.date, calls: list, daily: list, customs: dict,
                 calibrated: bool = False) -> dict:
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
        f"Estimated {teu_today:,} TEU being exchanged today "
        f"(draft/stay model{', rescaled to VPA regional anchors' if calibrated else ', uncalibrated'}).",
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
        _log_src("gdvc-customs", "GDVC customs totals", False, None,
                 "customs.json missing on disk")
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
        _log_src("gdvc-customs", "GDVC customs totals", True, None,
                 f"{data.get('month', '')} totals refreshed")
    else:
        print("no new official totals parsed — keeping existing (official Oct-2026 anchors)")
        _log_src("gdvc-customs", "GDVC customs totals", False, None,
                 "fetch/parse failed — kept existing official anchors")


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


# --------------------------------------------- VI→EN machine translation ----
# GDELT returns Vietnamese-language headlines for most VN trade sources. The
# robot translates title + summary to English via free keyless endpoints
# (Google gtx first, MyMemory fallback), cached per exact string in
# datastore/news_translate_cache.json so repeat articles cost nothing and the
# daily endpoint load stays tiny. Failure mode is safe: the original text is
# kept and shown as-is — a dead endpoint never breaks the feed.
TRANS_CACHE = ROOT / "datastore" / "news_translate_cache.json"

_VI_CHARS = frozenset(
    "ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ"
    "ĂÂĐÊÔƠƯÁÀẢÃẠẤẦẨẪẬẮẰẲẴẶÉÈẺẼẸẾỀỂỄỆÍÌỈĨỊÓÒỎÕỌỐỒỔỖỘỚỜỞỠỢÚÙỦŨỤỨỪỬỮỰÝỲỶỸỴ")


def _lang_hint(s: str) -> str:
    """'en' (leave as-is) / 'vi' (Vietnamese diacritics) / 'other' (any other
    non-ASCII script — GDELT occasionally returns CN/PT/TR items)."""
    if any(c in _VI_CHARS for c in s):
        return "vi"
    return "other" if any(ord(c) > 127 for c in s) else "en"


def _translate_one(text: str, cache: dict, stats: dict) -> str:
    key = text.strip()
    hint = _lang_hint(key)
    if not key or hint == "en":
        return key
    if key in cache:
        return cache[key]
    out = None
    try:  # Google Translate free endpoint (gtx), auto-detected source language
        q = urllib.parse.quote(key[:1800])
        url = ("https://translate.googleapis.com/translate_a/single"
               f"?client=gtx&sl=auto&tl=en&dt=t&q={q}")
        req = urllib.request.Request(url, headers={"User-Agent": "vn-port-dashboard/1.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=20).read())
        segs = data[0] if isinstance(data, list) and data else None
        cand = "".join(s[0] for s in segs if s and s[0]).strip() if segs else ""
        if cand:
            out = cand
    except Exception:
        stats["fail"] += 1
    if not out and hint == "vi":
        try:  # MyMemory fallback — Vietnamese only (it needs an explicit pair)
            q = urllib.parse.quote(key[:450])
            url = f"https://api.mymemory.translated.net/get?q={q}&langpair=vi|en"
            req = urllib.request.Request(url, headers={"User-Agent": "vn-port-dashboard/1.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=20).read())
            cand = ((data.get("responseData") or {}).get("translatedText") or "").strip()
            if cand and "MYMEMORY WARNING" not in cand and cand.lower() != key.lower():
                out = cand
        except Exception:
            stats["fail"] += 1
    if out:
        cache[key] = out
        stats["new"] += 1
        return out
    return key  # untranslated — original shown


def translate_news(items: list) -> list:
    """Attach titleEn/summaryEn to every item (original kept on failure).
    The per-string cache makes 2-hourly runs nearly free: only newly-seen
    headlines hit an endpoint."""
    cache = {}
    if TRANS_CACHE.exists():
        try:
            cache = json.loads(TRANS_CACHE.read_text())
        except Exception:
            cache = {}
    stats = {"new": 0, "fail": 0}
    for it in items:
        for src_f, dst_f in (("title", "titleEn"), ("summary", "summaryEn")):
            orig = (it.get(src_f) or "").strip()
            if orig:
                it[dst_f] = _translate_one(orig, cache, stats)
    if stats["new"]:
        TRANS_CACHE.parent.mkdir(exist_ok=True)
        TRANS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    print(f"news translate: {stats['new']} new strings EN-translated "
          f"(cache {len(cache)}, {stats['fail']} endpoint failures)")
    _log_src("news-translate", "News VI→EN machine translation",
             stats["fail"] == 0, stats["new"],
             "" if stats["fail"] == 0
             else f"{stats['fail']} endpoint call(s) failed — those headlines kept in Vietnamese")
    return items


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
        _log_src("gdelt", "GDELT news feed", True, len(items))
    except Exception as e:
        print("GDELT failed:", e)
        _log_src("gdelt", "GDELT news feed", False, err=e)
        if OUT.exists():
            print("keeping previous news.json")
            return
        items = [dict(a, category=classify(a["title"])) for a in FALLBACK]
        mode = "curated-fallback"
    payload = {"mode": mode, "items": translate_news(items[:40])}
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
            _log_src("vpa", "VPA monthly throughput report", True,
                     None, f"month {have} still current")
            return
        OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        print(f"wrote vpa.json ({data['monthLabel']}: {data['national']['teu']:,} TEU)")
        _log_src("vpa", "VPA monthly throughput report", True,
                 data["national"]["teu"], f"updated to {data['month']}")
        return
    print("no VPA report fetched — keeping existing vpa.json")
    _log_src("vpa", "VPA monthly throughput report", bool(have), None,
             "no new report fetched; kept existing" if have
             else "fetch failed and no stored report on disk")

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
HCM_URL = "https://shippingschedule.com.vn/ke-hoach-tau/ho-chi-minh.html"
SCHED_STORE = ROOT / "datastore" / "schedules.json"

# display names for the free plan sources (meta notes + frontend badges)
SRC_NAMES = {
    "tancangpilot": "Tan Cang Pilot",
    "haiphong-plan": "Cảng vụ Hải Phòng port authority",
    "cangvu-hcmc": "Cảng vụ Hồ Chí Minh port authority",
}
# plan-source precedence when two plans cover the same vessel+port:
# Tan Cang Pilot decodes the real alongside time (//HHMM) — it wins ties.
SRC_RANK = {"tancangpilot": 3, "cangvu-hcmc": 2, "haiphong-plan": 2}

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

HP_GOV_URLS = [  # Cảng vụ Hải Phòng official plan: ?d=0 today, ?d=1 tomorrow
    "https://csdltau.cangvuhaiphong.gov.vn/pages/ship_plan.aspx?d=0",
    "https://csdltau.cangvuhaiphong.gov.vn/pages/ship_plan.aspx?d=1",
]
HCM_GOV_URL = ("https://cangvuhanghaitphcm.gov.vn/index.aspx"
               "?page=shipschedule&cat=2033")


def _unacc(s: str) -> str:
    """Strip Vietnamese diacritics so terminal names match ('ĐÌNH VŨ'->'DINH VU')."""
    s = (s or "").replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


# Hai Phong plan terminal text -> our terminal name (exact PORTS names so the
# berth allocator can honour the plan's terminal). ORDERED: longer/more
# specific names first ('NAM HAI DINH VU' before 'NAM DINH VU'/'NAM HAI').
HP_TERM_RULES = [
    (r"NAM\s*HAI\s*DINH\s*VU", "Nam Hai Dinh Vu"),
    (r"NAM\s*DINH\s*VU", "Nam Dinh Vu"),
    (r"VIP\s*GREEN", "VIP Greenport"),
    (r"\bVIMC\b", "VIMC Dinh Vu"),
    (r"\bNAM\s*HAI\b", "Nam Hai"),
    (r"\bTAN\s*VU\b", "Tan Vu"),
    (r"\bHAI\s*AN\b", "Hai An"),
    (r"\bHHIT\b", "Lach Huyen (HHIT)"),
    (r"\bHTIT\b", "Lach Huyen (HTIT)"),
    (r"\bHICT\b", "Lach Huyen (HICT)"),
    (r"LACH\s*HUYEN\s*1", "Lach Huyen (HICT)"),
    (r"LACH\s*HUYEN\s*2", "Lach Huyen (HICT)"),
    (r"LACH\s*HUYEN\s*[34]", "Lach Huyen (HTIT)"),
    (r"LACH\s*HUYEN\s*[56]", "Lach Huyen (HHIT)"),
    (r"CHUA\s*VE", "Chua Ve"),
]
# not tracked on the berth board: PTSC/EURO DINH VU (oil&gas), HOANG DIEU,
# BACH DANG, THUONG LY, VAT CACH, CANG 128 (general cargo), HON DAU (anchorage)


def _hp_terminal(text: str):
    t = _unacc(text).upper()
    for pat, name in HP_TERM_RULES:
        if re.search(pat, t):
            return name
    return None



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
            if (thint or "").strip().upper() in ("CR", "CRUISE"):
                continue  # passenger/cruise terminal row — not a container call
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
    raw = _html.unescape(raw)
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
            frm, to = cells[8], cells[9]
            if direction == "ARRIVING":
                thint = _hp_terminal(to)
                if not thint:
                    continue  # real call, but not one of our tracked terminals
                recs.append({"source": "haiphong-plan", "direction": direction,
                             "vessel": name, "draught": _vn_float(cells[3]), "loa": _vn_float(cells[4]),
                             "port": "HAIPHONG", "terminalHint": thint,
                             "time": cells[1], "prevPort": frm.title(), "nextPort": "",
                             "planDt": day.isoformat()})
            else:
                thint = _hp_terminal(frm)
                if not thint:
                    continue  # departs a terminal we don't track (general cargo,
                              # oil&gas, anchorage) — don't mis-assign it elsewhere
                recs.append({"source": "haiphong-plan", "direction": direction,
                             "vessel": name, "draught": _vn_float(cells[3]), "loa": _vn_float(cells[4]),
                             "port": "HAIPHONG", "terminalHint": thint,
                             "time": cells[1], "prevPort": "", "nextPort": to.title(),
                             "planDt": day.isoformat()})
    return recs


def parse_hp_cangvu(raw: str):
    """Cảng vụ Hải Phòng official plan (csdltau.../ship_plan.aspx and the
    cangvuhaiphong.gov.vn/ke-hoach-tau mirror). Self-validating, header-mapped:
    sections 'Kế hoạch tàu rời cảng / di chuyển / vào cảng / qua luồng', columns
    STT | Thời gian | Tên tàu | Mớn nước | LOA | DWT | GT | Tàu lai | T.Luồng |
    Từ | Đến | Đại lý | Hoa tiêu. Refuses to guess: any schema drift raises and
    the source contributes nothing rather than feeding the board garbage."""
    raw = _html.unescape(raw)  # page serves Vietnamese as numeric entities (&#7901;…)
    m = re.search(r"ngày\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})", raw, re.I)
    if not m:
        raise ValueError("hp-gov: no plan date on page")
    day = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    secs = re.split(r"Kế hoạch tàu", raw)
    if len(secs) < 2:
        raise ValueError("hp-gov: no 'Kế hoạch tàu' sections found")
    recs = []
    for sec in secs[1:]:
        head = _unacc(re.sub(r"<[^>]+>", " ", sec))
        head = re.sub(r"\s+", " ", head).strip()[:30].lower()
        if head.startswith("roi cang"):
            direction = "DEPARTING"
        elif head.startswith("vao cang"):
            direction = "ARRIVING"
        elif head.startswith("di chuyen"):
            direction = "SHIFTING"
        else:
            continue  # qua luồng duplicates vào cảng; unknown sections skipped
        col = {}
        rows = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", sec, re.S):
            cells = [re.sub(r"<[^>]+>", " ", c) for c in
                     re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
            if not cells:
                continue
            low = [_unacc(c).lower() for c in cells]
            if any("ten tau" in c for c in low):  # column header row
                col.clear()
                for i, h in enumerate(low):
                    if "ten tau" in h: col["name"] = i
                    elif "thoi gian" in h: col["time"] = i
                    elif "mon nuoc" in h or h.strip() == "mon": col["draft"] = i
                    elif h == "loa": col["loa"] = i
                    elif h in ("tu", "xuat phat"): col["from"] = i
                    elif h in ("den", "noi den"): col["to"] = i
                if "name" not in col or "time" not in col:
                    raise ValueError(f"hp-gov: header mapped incompletely: {low}")
                continue
            if not col:
                if cells[0].isdigit():
                    raise ValueError("hp-gov: data row before column header")
                continue
            def cell(k):
                i = col.get(k)
                return cells[i] if i is not None and i < len(cells) else ""
            if not cells[0].isdigit() or not re.fullmatch(r"\d{1,2}:\d{2}", cell("time")):
                continue
            name = re.sub(r"\(SB\)", "", cell("name"), flags=re.I).strip()
            name = re.sub(r"^[*\\]+", "", name).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .'\-()]{2,40}", name):
                continue
            frm, to = cell("from"), cell("to")
            if direction == "SHIFTING":
                # a terminal-to-terminal move: show the arrival side when it is
                # a tracked terminal, else the departure side
                thint = _hp_terminal(to)
                d2 = "ARRIVING" if thint else "DEPARTING"
                thint = thint or _hp_terminal(frm)
                if not thint:
                    continue
            elif direction == "ARRIVING":
                d2, thint = "ARRIVING", _hp_terminal(to)
                if not thint:
                    continue
            else:
                d2, thint = "DEPARTING", _hp_terminal(frm)
                if not thint:
                    continue  # untracked origin terminal — don't mis-assign
            recs.append({"source": "haiphong-plan", "direction": d2,
                         "vessel": name.upper(), "draught": _vn_float(cell("draft")),
                         "loa": _vn_float(cell("loa")), "port": "HAIPHONG",
                         "terminalHint": thint, "time": cell("time"),
                         "prevPort": frm.title() if d2 == "ARRIVING" else "",
                         "nextPort": to.title() if d2 == "DEPARTING" else "",
                         "planDt": day.isoformat()})
            rows += 1
    if not recs:
        raise ValueError("hp-gov: 0 usable rows — page empty or schema changed")
    if len(recs) > 400:
        raise ValueError(f"hp-gov: implausible row count {len(recs)}")
    return recs


# Cảng vụ Hồ Chí Minh plan (ho-chi-minh.html): wharf/berth text -> (port, hint).
# Regex-based (the page writes free text, unlike the TCP wharf codes).
HCM_TERMINALS = [
    (r"CAI\s?MEP|CÁI\s?MÉP|THI\s?VAI|THI\s?VẢI|CMIT|TCIT|TCTT|VICT|SSIT|GEM|CM\d",
     ("CAIMEP", "Cai Mep – Thi Vai")),
    (r"CAT\s?LAI|CÁT\s?LÁI|CL\d", ("HCMC", "Cat Lai")),
    (r"TAN\s?CANG|TÂN\s?CẢNG|BEN\s?NGHE|BẾN\s?NGHÉ|BNPH|HIEP\s?PHUOC|HIỆP\s?PHƯỚC|KHANH\s?HOI|KHÁNH\s?HỘI|SAIGON|SÀI\s?GÒN",
     ("HCMC", "HCMC river")),
]


def _hcm_wharf_port(text: str):
    """Free berth/origin/destination text -> (port id, terminal hint) or None."""
    t = (text or "").upper()
    for pat, res in HCM_TERMINALS:
        if re.search(pat, t):
            return res
    return None


def parse_hcmc_cangvu(raw: str):
    """Cảng vụ Hồ Chí Minh vessel plan — SELF-VALIDATING parser.
    The page schema differs from Hai Phong's (extra nationality/call-sign
    columns) and we cannot fetch it from every network, so this parser never
    assumes fixed column indices: it locates the header row, maps columns by
    their Vietnamese titles, and RAISES (=> contributes nothing, yesterday's
    store kept) unless the structure matches. Garbage can never reach the
    board."""
    m = re.search(r"NGÀY\s*(\d{2})/(\d{2})/(\d{4})", raw)
    page_day = (dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                if m else dt.datetime.now(TZ).date())
    recs, saw_section = [], False
    secs = re.split(r"Kế hoạch tàu", raw)
    for sec in secs:
        head = re.sub(r"^\s*(?:</?[^>]+>\s*)*", "", sec)[:30]
        if re.match(r"rời cảng", head):
            direction = "DEPARTING"
        elif re.match(r"vào cảng|cập bến|đến cảng", head):
            direction = "ARRIVING"
        else:
            continue
        saw_section = True
        col = {}
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", sec, re.S):
            cells = [re.sub(r"<[^>]+>", " ", c) for c in
                     re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
            cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
            low = [c.lower() for c in cells]
            if any("tên tàu" in c for c in low):
                # header row — map columns by Vietnamese titles
                col.clear()
                for i, h in enumerate(low):
                    if "tên tàu" in h: col["name"] = i
                    elif "thời gian" in h or "ngày giờ" in h: col["time"] = i
                    elif "mớn nước" in h: col["draft"] = i
                    elif h == "loa" or "chiều dài" in h: col["loa"] = i
                    elif h.startswith("từ") or "nơi đi" in h: col["from"] = i
                    elif h.startswith("đến") or "nơi đến" in h: col["to"] = i
                    elif "cầu bến" in h or h.startswith("bến") or "vị trí" in h: col["berth"] = i
                if "name" not in col or "time" not in col:
                    raise ValueError(f"hcm-cangvu: header mapped {sorted(col)} — schema changed?")
                continue
            if not cells or not cells[0].isdigit():
                continue
            if not col:
                raise ValueError("hcm-cangvu: data row before header row")
            def cell(k):
                i = col.get(k)
                return cells[i] if i is not None and i < len(cells) else ""
            name = cell("name").replace("(SB)", "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9 .'\-()]{2,40}", name):
                continue
            tcell = cell("time")
            tm = re.search(r"(\d{1,2}):(\d{2})", tcell)
            dm = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", tcell)
            if not tm:
                continue  # no parseable time -> row unusable
            row_day = (dt.date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
                       if dm else page_day)
            berth, frm, to = cell("berth"), cell("from"), cell("to")
            # arrivals berth at the 'to' side, departures leave from 'from'
            key = (berth + " " + (to if direction == "ARRIVING" else frm)).strip()
            mapped = _hcm_wharf_port(key)
            if not mapped:
                continue  # real call but not one of our tracked terminals
            port, thint = mapped
            recs.append({
                "source": "cangvu-hcmc", "direction": direction,
                "vessel": name, "draught": _vn_float(cell("draft")),
                "loa": _vn_float(cell("loa")),
                "port": port, "terminalHint": thint,
                "time": f"{int(tm.group(1)):02d}:{tm.group(2)}",
                "prevPort": frm.title() if direction == "ARRIVING" else "",
                "nextPort": to.title() if direction == "DEPARTING" else "",
                "planDt": row_day.isoformat(),
            })
    if not saw_section:
        raise ValueError("hcm-cangvu: no 'Kế hoạch tàu' sections — page structure changed")
    if not recs:
        raise ValueError("hcm-cangvu: 0 usable rows after terminal/time filters")
    if len(recs) > 400:
        raise ValueError(f"hcm-cangvu: {len(recs)} rows — implausible, refusing")
    return recs


# Cảng vụ HCM official shipschedule (cangvuhanghaitphcm.gov.vn): the
# 'Vị trí neo đậu' (berth/anchorage position) text -> (port, terminal hint).
# Positive matches only — anchorages (NEO …), oil/gas jetties and worksites
# fall through and the row is skipped. ORDER matters: specific names first.
HCM_GOV_POSITIONS = [
    (r"GEMALINK|GEMADEPT.?TERMINAL\s*LINK", ("CAIMEP", "Gemalink")),
    (r"CMIT|CAI\s*MEP\s*\(CMIT\)|CANG\s*QUOC\s*TE\s*CAI\s*MEP", ("CAIMEP", "CMIT")),
    (r"TCIT", ("CAIMEP", "TCIT")),
    (r"TCTT|TAN\s*CANG.*THI\s*VAI", ("CAIMEP", "")),
    (r"SSIT", ("CAIMEP", "SSIT")),
    (r"SP.?PSA", ("CAIMEP", "SP-PSA")),
    (r"\bVICT\b", ("CAIMEP", "")),
    (r"CAI\s*MEP|THI\s*VAI", ("CAIMEP", "")),  # generic Cai Mep – Thi Vai
    (r"C\.?\s?LAI\s*\d?|CL\s?\d", ("HCMC", "Cat Lai")),
    (r"SP.?ITC", ("HCMC", "SP-ITC")),
    (r"TCHP|HIEP\s*PHUOC", ("HCMC", "Tan Cang Hiep Phuoc")),
    (r"BN_?PH|BP\s?\d|BEN\s*NGHE|TAN\s*CANG|KHANH\s*HOI|SAIGON", ("HCMC", "Tan Cang")),
]


def _hcm_gov_pos(text: str):
    t = _unacc(text).upper()
    if re.search(r"NEO|MỎ|MO\s|BO\s*BANG|PTSC|PVGAS|CALTEX|ELF\s*GAS|GAS\b|"
                 r"HYDROCARBON|XMCP|BARIA|BEN\s*DAM|CAU\s*CANG\s*SO|CẦU\s*CẢNG|"
                 r"KV\s*THI\s*CONG|RONG\s*DOI|TE\s*GIAC|SOWATCO-DT|"
                 r"^(CG|SR|HL|NB|K|V|B|G)\d|^HL\s", _unacc(text).upper()):
        return None
    for pat, res in HCM_GOV_POSITIONS:
        if re.search(pat, t):
            return res
    return None


def parse_hcmc_gov(raw: str):
    """Cảng vụ Hồ Chí Minh official daily ship schedule (fetched OK where the
    shippingschedule mirror is blocked). One row per vessel with BOTH the
    expected arrival ('Dự kiến đến VT') and departure ('Thời gian rời VT') at
    the position — berth-side times, no pilot-station steam offset. Today's
    plan only (page carries no date column). Self-validating: raises on any
    schema drift so a changed page contributes nothing instead of garbage."""
    raw = _html.unescape(raw)
    low_raw = _unacc(re.sub(r"<[^>]+>", " ", raw)).lower()
    if "du kien den" not in low_raw or "vi tri neo dau" not in low_raw:
        raise ValueError("hcm-gov: expected columns not present on page")
    day = dt.datetime.now(TZ).date()
    col = {}
    recs = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", raw, re.S):
        cells = [re.sub(r"<[^>]+>", " ", c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        if not cells:
            continue
        low = [_unacc(c).lower() for c in cells]
        if any("ten tau" in c for c in low):  # column header row
            col.clear()
            for i, h in enumerate(low):
                if "ten tau" in h: col["name"] = i
                elif "vi tri" in h: col["pos"] = i
                elif "du kien den" in h: col["eta"] = i
                elif "roi" in h and ("vt" in h or "thoi gian" in h): col["etd"] = i
                elif "mon nuoc" in h or h.strip() == "mon": col["draft"] = i
                elif "chieu dai" in h: col["loa"] = i
                elif "tuyen luong" in h: col["route"] = i
            if not {"name", "pos", "eta"} <= set(col):
                raise ValueError(f"hcm-gov: header mapped incompletely: {low}")
            continue
        if not col:
            if cells[0].isdigit():
                raise ValueError("hcm-gov: data row before column header")
            continue
        def cell(k):
            i = col.get(k)
            return cells[i] if i is not None and i < len(cells) else ""
        if not cells[0].isdigit():
            continue
        if not re.fullmatch(r"\d{1,2}:\d{2}", cell("eta")):
            continue
        mapped = _hcm_gov_pos(cell("pos"))
        if not mapped:
            continue  # anchorage / oil & gas / worksite — not a container berth
        port, hint = mapped
        name = re.sub(r"^[*\\\s]+", "", cell("name"))
        name = re.sub(r"\(SB\)", "", name, flags=re.I).strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9 .'\-()]{2,40}", name):
            continue
        etd = cell("etd") if re.fullmatch(r"\d{1,2}:\d{2}", cell("etd")) else ""
        recs.append({"source": "cangvu-hcmc", "direction": "ARRIVING",
                     "vessel": name, "draught": _vn_float(cell("draft")),
                     "loa": _vn_float(cell("loa")), "port": port,
                     "terminalHint": hint, "time": cell("eta"), "timeDep": etd,
                     "prevPort": "", "nextPort": "",
                     "planDt": day.isoformat()})
    if not recs:
        raise ValueError("hcm-gov: 0 usable rows — page empty or schema changed")
    if len(recs) > 300:
        raise ValueError(f"hcm-gov: implausible row count {len(recs)}")
    return recs


def _fetch(url: str) -> str:
    """Fetch with one retry and a longer fuse — the Hai Phong port-authority
    page in particular is slow and drops connections from CI runners."""
    last = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 vn-port-dashboard/1.0",
                "Accept": "text/html,application/xhtml+xml,*/*"})
            return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            print(f"  fetch attempt {attempt} failed ({url.split('/')[2]}): {e}")
            time.sleep(6)
    raise last


def refresh_schedules() -> list:
    """Fetch + parse the public daily plans. Keeps yesterday's file on failure.
    Returns normalized records (may be empty)."""
    recs = []
    try:
        got = parse_tancangpilot(_fetch(TCP_URL))
        print(f"tancangpilot: {len(got)} plan rows")
        recs += got
        _log_src("tancangpilot", "Tan Cang Pilot plan", True, len(got))
    except Exception as e:
        print("tancangpilot fetch/parse failed:", e)
        _log_src("tancangpilot", "Tan Cang Pilot plan", False, err=e)
    try:
        got = parse_haiphong(_fetch(HP_URL))
        print(f"haiphong plan: {len(got)} rows")
        recs += got
        _log_src("haiphong-mirror", "Hai Phong plan (mirror)", True, len(got))
    except Exception as e:
        print("haiphong plan fetch/parse failed:", e)
        _log_src("haiphong-mirror", "Hai Phong plan (mirror)", False, err=e)
    # Cảng vụ Hải Phòng official plan pages — independent of the mirror above,
    # so Hai Phong no longer goes dark when one host blocks the CI runner
    hp_gov_rows, hp_gov_errs = 0, []
    for url in HP_GOV_URLS:
        try:
            got = parse_hp_cangvu(_fetch(url))
            print(f"haiphong cangvu ({url.split('/')[2]}): {len(got)} rows")
            recs += got
            hp_gov_rows += len(got)
        except Exception as e:
            print(f"haiphong cangvu ({url.split('/')[2]}) failed — refused to "
                  f"guess, contributing nothing:", e)
            hp_gov_errs.append(str(e))
    _log_src("haiphong-cangvu", "Cảng vụ Hải Phòng ship plan", hp_gov_rows > 0,
             hp_gov_rows if hp_gov_rows else None, "; ".join(hp_gov_errs))
    try:
        got = parse_hcmc_gov(_fetch(HCM_GOV_URL))
        print(f"hcmc cangvu gov plan: {len(got)} rows")
        recs += got
        _log_src("cangvu-hcmc-gov", "Cảng vụ TP.HCM gov schedule", True, len(got))
    except Exception as e:
        print("hcmc cangvu gov plan failed (refused to guess):", e)
        _log_src("cangvu-hcmc-gov", "Cảng vụ TP.HCM gov schedule", False, err=e)
    try:
        got = parse_hcmc_cangvu(_fetch(HCM_URL))
        print(f"hcmc cangvu plan: {len(got)} rows")
        recs += got
        _log_src("cangvu-hcmc", "Cảng vụ TP.HCM plan", True, len(got))
    except Exception as e:
        print("hcmc cangvu plan fetch/parse failed (page may block this network "
              "or its schema changed — parser refuses to guess):", e)
        _log_src("cangvu-hcmc", "Cảng vụ TP.HCM plan", False, err=e)
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
    else:  # port-authority plans (hai-phong.html / ho-chi-minh.html / gov pages)
        t = _hm_to_dt(day, sr["time"], now)
        if sr.get("timeDep"):
            # Cảng vụ HCM gov page publishes BOTH the berth-side arrival and
            # departure — use them directly (no pilot-station steam offset);
            # equal/earlier departure rolls to the next day (00:00->00:00 = 24h)
            eta = t
            etd = _hm_to_dt(day, sr["timeDep"], now)
            if etd <= eta:
                etd += dt.timedelta(days=1)
            pred, basis = predict_stay("", vclass, port)
        elif sr["direction"] == "DEPARTING":
            # pilot station -> berth steam time per port (Cat Lai is far upriver)
            steam = {"HAIPHONG": 1.5, "HCMC": 2.5, "CAIMEP": 1.2}.get(port, 1.5)
            etd = t + dt.timedelta(minutes=30)
            pred, basis = predict_stay("", vclass, port)
            eta = etd - dt.timedelta(hours=pred)
        else:
            # pilot station -> berth steam time per port (Cat Lai is far upriver)
            steam = {"HAIPHONG": 1.5, "HCMC": 2.5, "CAIMEP": 1.2}.get(port, 1.5)
            eta = t + dt.timedelta(hours=steam)
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
        "planSrc": sr.get("source", ""),
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
            # when two PLAN sources cover the same call (e.g. Tan Cang Pilot
            # and the Cảng vụ HCM page), the higher-ranked plan drives the
            # times; Tan Cang's //HHMM alongside decode outranks the others.
            prev_src = hit.get("planSrc") or ""
            better = SRC_RANK.get(sr["source"], 1) > SRC_RANK.get(prev_src, 0)
            # the plan refines modelled times only — observed actuals untouched
            if sr["direction"] == "DEPARTING":
                if not hit.get("_obsDep") and (better or not prev_src):
                    # the plan often lists BOTH legs of a quick turnaround:
                    # the departing row can belong to an earlier visit than
                    # the arrival we already hold — never move etd before eta
                    if call["etd"] > hit["eta"]:
                        hit["etd"] = call["etd"]
            elif not hit.get("_obsArr") and (better or not prev_src):
                hit["eta"] = call["eta"]
                hit["etd"] = call["etd"]
            # planSrc labels every plan that contributed (drives badge display)
            if sr["source"] and sr["source"] not in prev_src.split("+"):
                hit["planSrc"] = (sr["source"] + "+" + prev_src).strip("+") if better or not prev_src \
                                 else (prev_src + "+" + sr["source"]).strip("+")
            if hit.get("stayPredH") is None:
                hit["stayPredH"], hit["stayBasis"] = call["stayPredH"], call["stayBasis"]
        else:
            calls.append(call)
            by_key[key] = call
            added += 1
    if added or enriched:
        print(f"schedule merge: {added} plan-only call(s) added, {enriched} AIS call(s) enriched")
    return calls



# ------------------------------------------- live AIS: Open Waters snapshots ----
# Live in-port state comes from Open Waters AIS REST snapshots
# (https://ais.openwaters.io/v1/vessels?bbox=...): anonymous, keyless,
# worldwide terrestrial via the AISHub aggregate (~1 min behind real time).
# This replaced the aisstream.io websocket soak: forensically, that free
# network has almost no VN shore receivers (zero in-port messages in weeks of
# soaking), while the AISHub aggregate hears hundreds of vessels inside our
# port boxes. Source terms require credit: AISHub contributors + Open Waters AIS.
OW_API = "https://ais.openwaters.io/v1/vessels"
OW_VISITS = ROOT / "datastore" / "ow_visits.json"

# --------------------------------------- inland ICDs: separate live boards ----
# ICD Sotrans & SOWATCO Long Binh (both SOTRANS-group river facilities in
# TP. Thủ Đức) sit INSIDE the wide fleet snapshot box below, so their boards
# cost no extra API calls: their geofences are carved out of that snapshot.
# Visits are recorded under their own port ids (ICDSOTRANS / SOWATCOLB) which
# build_live_calls ignores (not in PORTS) — they never touch the seaport
# board. Geofences: ICD Sotrans is pinned empirically to the moored-barge
# cluster along its 412 m wharf on the Trường Thọ channel; SOWATCO Long Binh
# is estimated from the street address (1352/36 Nguyễn Xiển, Đồng Nai
# riverbank) — mark geoApprox until dwell clusters confirm it, then tighten.
ICD_SITES = [
    {
        "id": "ICDSOTRANS", "name": "ICD Sotrans",
        "operator": "SOTRANS Group (SOWATCO)",
        "address": "Km 9 Xa Lộ Hà Nội, P. Trường Thọ, TP. Thủ Đức",
        "waterway": "Trường Thọ channel — Saigon River system",
        "geo": (10.8135, 106.7505, 10.8185, 106.7550),  # la0, lo0, la1, lo1
        "coverage": "good",
        "coverageNote": ("Busy barge channel, well covered by the free AIS "
                         "network (20+ barges visible on a typical snapshot)."),
        "wharfNote": ("412 m continuous wharf (operator data) shown as 5 "
                      "indicative barge slots of ~82 m — slot split is ours, "
                      "not an operator numbering"),
        "specs": {
            "Opened": "2010",
            "Area": "10 ha",
            "Wharf": "412 m (operator)",
            "Barge transit": "Sài Gòn/VICT 2h · Cát Lái 3h · SPCT 4h · Cái Mép 8h (operator)",
        },
        "berths": [{"id": f"B{i}", "lenM": 82, "draftM": None, "maxDwt": 1500}
                   for i in range(1, 6)],
        "axis": "lat",  # wharf runs N–S: order alongside vessels by latitude
        "geoApprox": False,
    },
    {
        "id": "SOWATCOLB", "name": "SOWATCO Long Binh",
        "operator": "SOWATCO / SOTRANS Group",
        "address": "1352/36 Nguyễn Xiển, P. Long Bình, TP. Thủ Đức",
        "waterway": "Đồng Nai River",
        "geo": (10.8165, 106.8185, 10.8285, 106.8285),
        "coverage": "limited",
        "coverageNote": ("The Đồng Nai reach at Long Bình sits at the edge of "
                         "free-AIS coverage — alongside barges can be "
                         "undercounted or invisible here"),
        "wharfNote": ("3 piers per SOWATCO: 44 m / 88 m / 70 m, low-tide "
                      "draft 3 / 3 / 7 m; pier 3 takes 5,000 DWT (parent-co "
                      "page lists piers 1–2 at 600 DWT)"),
        "specs": {
            "Area": "200,000 m²",
            "Yard capacity": "22,000 TEU",
            "Quay cranes": "45 t class",
            "Depot clients": "CMA CGM · MSC · HMM · ONE · Hapag-Lloyd · Evergreen (depot lists)",
        },
        "berths": [
            {"id": "P1", "lenM": 44, "draftM": 3, "maxDwt": 600},
            {"id": "P2", "lenM": 88, "draftM": 3, "maxDwt": 600},
            {"id": "P3", "lenM": 70, "draftM": 7, "maxDwt": 5000},
        ],
        "axis": "lat",
        "geoApprox": True,
    },
]

# area attribution for the SOWATCO barge fleet tracker — checked in order,
# ICD geofences first (handled separately), then these named reaches
FLEET_AREAS = [
    ("Trường Thọ channel — ICD row", (10.808, 106.744, 10.836, 106.760)),
    ("Bình Triệu reach — Saigon R.", (10.874, 106.833, 10.906, 106.857)),
    ("Cát Lái reach", (10.745, 106.755, 10.782, 106.800)),
    ("SP-ITC / Phú Hữu", (10.755, 106.795, 10.790, 106.825)),
    ("Đồng Nai river (lower)", (10.780, 106.800, 10.830, 106.840)),
    ("Saigon river (central)", (10.700, 106.680, 10.874, 106.760)),
    ("Nhà Bè / Hiệp Phước", (10.600, 106.700, 10.720, 106.790)),
    ("Soài Rạp / Lòng Tàu channel", (10.580, 106.790, 10.720, 106.920)),
    ("Cần Giờ / approach", (10.400, 106.700, 10.600, 106.900)),
    ("Cái Mép – Thị Vải", (10.450, 106.900, 10.650, 107.150)),
    ("Vũng Tàu anchorage", (10.250, 106.950, 10.450, 107.150)),
]

# most recent snapshot round, incl. the wide "_fleet" box — build_icds()
# reads live positions from here (set by fetch_ow_snapshots)
LAST_SNAPS: dict = {}

# --------------------------------------------------------- source health ----
# Every fetch round appends one record here; the run's final meta.json carries
# the list so the UI can show per-source health instead of silently going stale.
HEALTH: list = []


def _log_src(sid: str, label: str, ok: bool, rows=None, err: str = "") -> None:
    HEALTH.append({"id": sid, "label": label, "ok": bool(ok),
                   "rows": rows, "err": (str(err)[:180] if err else "")})


def _ow_fetch_box(bbox: str) -> list:
    """One bbox snapshot -> list of property dicts with lat/lon attached."""
    req = urllib.request.Request(f"{OW_API}?bbox={bbox}",
        headers={"User-Agent": "vn-port-watch/1.0 (openwaters snapshot)"})
    data = json.loads(urllib.request.urlopen(req, timeout=45).read())
    recs = []
    for f in data.get("features", []):
        pr, geo = f.get("properties", {}), f.get("geometry") or {}
        xy = geo.get("coordinates") or []
        if len(xy) != 2 or not pr.get("mmsi"):
            continue
        pr = dict(pr)
        pr["lon"], pr["lat"] = xy[0], xy[1]
        recs.append(pr)
    return recs


def fetch_ow_snapshots() -> dict:
    """One REST snapshot per port geofence; per-port failure -> empty list
    (a down feed never blocks the plan data). Plus one wide SE-waterways box
    for the inland ICD boards & the SOWATCO barge fleet tracker."""
    global LAST_SNAPS
    out = {}
    for p in PORTS:
        (la0, lo0), (la1, lo1) = p["geofence"]
        try:
            recs = _ow_fetch_box(f"{la0},{lo0},{la1},{lo1}")
            out[p["id"]] = recs
            print(f"openwaters {p['id']}: {len(recs)} vessels in box")
            _log_src(f"ais-{p['id'].lower()}", f"AIS snapshot · {p['name']}",
                     True, len(recs))
        except Exception as e:
            print(f"openwaters snapshot failed for {p['id']}:", e)
            out[p["id"]] = []
            _log_src(f"ais-{p['id'].lower()}", f"AIS snapshot · {p['name']}",
                     False, err=e)
        time.sleep(1)
    # wide box: HCMC + Nhà Bè + lower Đồng Nai + Cái Mép approach — the whole
    # operating area of the SOWATCO barge fleet (both ICDs sit inside it)
    try:
        recs = _ow_fetch_box("10.40,106.55,10.95,107.15")
        out["_fleet"] = recs
        print(f"openwaters _fleet (wide SE box): {len(recs)} vessels")
        _log_src("ais-fleet", "AIS snapshot · SOWATCO fleet (wide SE box)",
                 True, len(recs))
    except Exception as e:
        print("openwaters wide fleet snapshot failed:", e)
        out["_fleet"] = []
        _log_src("ais-fleet", "AIS snapshot · SOWATCO fleet (wide SE box)",
                 False, err=e)
    LAST_SNAPS = out
    return out


def _ow_is_cargo(pr: dict) -> bool:
    """Keep cargo & tanker hulls (AIS types 70-89; that band includes the
    self-propelled river barges). Tugs/pilots/fishing/passenger stay off the
    berth board. Unknown type: keep only if it looks ship-sized."""
    t = pr.get("type")
    if isinstance(t, int):
        return 70 <= t <= 89
    return float(pr.get("length") or 0) >= 80


def _ow_terminal(pmeta: dict, lat: float, lon: float) -> str:
    """Nearest terminal centroid within 2.5 km -> hint, else '' (the berth
    allocator picks instead of guessing). Centroids are approximate; the
    Dinh Vu cluster terminals sit too close together to resolve -> no geo."""
    best, best_d = "", 3.0
    for t in pmeta["terminals"]:
        g = t.get("geo")
        if not g:
            continue
        d = math.hypot((lat - g[0]) * 111.0,
                       (lon - g[1]) * 111.0 * math.cos(math.radians(lat)))
        if d < best_d:
            best, best_d = t["name"], d
    return best if best_d <= 2.5 else ""


def observe_openwaters(snaps: dict) -> None:
    """Fold one snapshot round into the persistent visit table
    (datastore/ow_visits.json): who is inside which port box, since when.
    A vessel counts as IN PORT while inside the geofence moored/anchored/
    effectively stopped; a visit closes as DEPARTED when the vessel has been
    absent and silent for 3h+. The table persists across runs, so even the
    CI cadence learns multi-day stays."""
    try:
        state = json.loads(OW_VISITS.read_text()) if OW_VISITS.exists() else {}
    except Exception:
        state = {}
    visits = state.setdefault("visits", {})
    now = dt.datetime.now(dt.timezone.utc)
    present = set()

    def _record(pr: dict, port_id: str, terminal: str) -> None:
        """Fold one snapshot record into the visit table for port_id."""
        present.add(f"{pr['mmsi']}|{port_id}")
        if not _ow_is_cargo(pr):
            return
        try:
            seen = dt.datetime.fromisoformat(str(pr["seen"]).replace("Z", "+00:00"))
        except Exception:
            return
        if (now - seen).total_seconds() > 3 * 3600:
            return  # stale snapshot entry — long gone or AIS off
        mmsi = str(pr["mmsi"])
        key = f"{mmsi}|{port_id}"
        nav, sog = pr.get("nav_status"), float(pr.get("sog") or 0)
        inside = nav in (1, 5) or sog <= 1.0  # anchored / moored / drifting
        v = visits.get(key)
        if inside:
            if not v or v.get("departed"):
                v = {"mmsi": mmsi, "port": port_id, "ata": iso(seen.astimezone(TZ))}
                visits[key] = v
            v.update({
                "name": (pr.get("name") or "").strip(),
                "imo": str(pr.get("imo") or ""),
                "type": pr.get("type"),
                "loa": float(pr.get("length") or 0),
                "draught": float(pr.get("draught") or 0),
                "dest": (pr.get("destination") or "").strip(),
                "lat": pr["lat"], "lon": pr["lon"],
                "lastSeen": iso(seen.astimezone(TZ)), "departed": False,
                "terminal": terminal,
            })

    for port, recs in snaps.items():
        if port.startswith("_"):
            continue  # the wide fleet box is consumed via the ICD loop below
        pmeta = next(p for p in PORTS if p["id"] == port)
        for pr in recs:
            _record(pr, port, _ow_terminal(pmeta, pr["lat"], pr["lon"]))
    # inland ICD carve-outs: both facilities sit inside the wide fleet box
    # (fall back to the HCMC box if the wide fetch failed this run)
    icd_src = snaps.get("_fleet") or snaps.get("HCMC") or []
    for site in ICD_SITES:
        la0, lo0, la1, lo1 = site["geo"]
        for pr in icd_src:
            if la0 <= pr["lat"] <= la1 and lo0 <= pr["lon"] <= lo1:
                _record(pr, site["id"], "")
    # close visits whose vessel vanished from this round's snapshots
    for key, v in visits.items():
        if v.get("departed"):
            continue
        try:
            last = dt.datetime.fromisoformat(v["lastSeen"]).astimezone(dt.timezone.utc)
        except Exception:
            continue
        if key not in present and (now - last).total_seconds() > 3 * 3600:
            v["departed"] = True
            v["atd"] = v["lastSeen"]  # last certain time inside
    # purge closed visits older than 7 days so the store can't grow forever
    cutoff_7d = iso(now.astimezone(TZ) - dt.timedelta(days=7))
    for k in [k for k, v in visits.items()
              if v.get("departed") and (v.get("atd") or v.get("lastSeen") or "") < cutoff_7d]:
        del visits[k]
    OW_VISITS.parent.mkdir(exist_ok=True)
    state["updatedAt"] = iso(now.astimezone(TZ))
    OW_VISITS.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    open_n = sum(1 for v in visits.values() if not v.get("departed"))
    print(f"openwaters visits: {open_n} cargo vessel(s) currently inside port boxes")


def build_live_calls() -> list:
    """Turn the Open Waters visit table into PortCall-shaped live records.
    Returns [] when nothing usable was observed (dashboard falls back to
    plan-only / demo)."""
    if not OW_VISITS.exists():
        return []
    try:
        visits = json.loads(OW_VISITS.read_text()).get("visits", {})
    except Exception:
        return []
    now = dt.datetime.now(TZ)
    calls = []
    for v in visits.values():
        port = v.get("port")
        if port not in {p["id"] for p in PORTS}:
            continue
        try:
            ata = dt.datetime.fromisoformat(v["ata"])
        except Exception:
            continue
        if ata > now + dt.timedelta(hours=1):
            continue  # clock-skew guard
        loa = float(v.get("loa") or 0)
        cap, vclass = _est_capacity(loa)
        departed = bool(v.get("departed"))
        if departed:
            try:
                atd = dt.datetime.fromisoformat(v.get("atd") or v["lastSeen"])
            except Exception:
                continue
            if atd <= ata or atd < now - dt.timedelta(hours=96):
                continue  # malformed or too old for the board
            etd = atd
        else:
            if ata < now - dt.timedelta(days=10):
                continue  # abandoned visit that never closed — drop quietly
            pred, _basis = predict_stay(f"A{v['mmsi']}", vclass, port)
            etd = ata + dt.timedelta(hours=pred)
            atd = None
        max_draft = round(float(v["draught"]) + 0.5, 1) if v.get("draught") else CLASS_DRAFT[vclass]
        draft_in = round(float(v.get("draught") or max_draft * 0.8), 1)
        draft_out = round(min(max_draft, draft_in + 0.6), 1)
        name = (v.get("name") or "").strip() or f"MMSI {v['mmsi']}"
        r = {
            "id": f"A{v['mmsi']}", "vessel": name, "imo": v.get("imo", ""),
            "carrier": "—", "vclass": vclass, "capacity": cap, "loa": loa,
            "maxDraft": max_draft, "port": port, "terminal": "",
            "berth": "", "terminalHint": v.get("terminal") or "",
            "eta": iso(ata), "schedEta": iso(ata),
            "ata": iso(ata), "etd": iso(etd), "atd": iso(atd) if departed else None,
            "draftIn": draft_in, "draftOut": draft_out,
            "prevPort": "", "nextPort": (v.get("dest") or "").upper(),
            "status": "EXPECTED", "stayHours": None, "waitH": 0.0,
            "speedKn": CLASS_SPEED_KN[vclass],
            "estTeu": estimate_teu(cap, max_draft, draft_in, draft_out, vclass, port),
            "stayPredH": None, "stayBasis": "AIS-observed arrival (Open Waters / AISHub)",
            "source": "ais", "planSrc": "",
            "_obsArr": True, "_obsDep": departed,
        }
        if departed:
            record_stay(r)  # real turnaround times feed the stay predictor
        calls.append(r)
    if calls:
        print(f"live calls from Open Waters visits: {len(calls)}")
    return calls


# ------------------------------------------- TEU calibration (VPA anchor) ----
# The model estimates TEU per call from draft & stay. That is unbiased-ish per
# vessel but the daily totals drift from reality. The VPA monthly report gives
# REAL regional throughput, so once ≥10 days of observed daily history exist
# we compute one multiplier per port that reconciles our recent daily mean
# with the association's monthly anchor. Applied at presentation time only —
# the stored daily history stays raw, so the factor can never compound.
VPA_PORT_REGION = {  # dashboard port -> VPA region name prefix in vpa.json
    "HAIPHONG": "Northern",
    "HCMC": "Southeast",
    "CAIMEP": "Cai Mep",
    "DANANG": "South Central",
}


def calibrate_teu(hist: dict) -> dict:
    """Return {portId: {factor, days, anchor, region, month}} — empty when the
    VPA file or enough observed history is missing. Factor clamps keep a thin
    or noisy history from ever producing an absurd rescale."""
    vpa_file = DATA_DIR / "vpa.json"
    if not vpa_file.exists():
        print("teu calibration: no vpa.json — uncalibrated")
        return {}
    try:
        vpa = json.loads(vpa_file.read_text())
    except Exception as e:
        print("teu calibration: vpa.json unreadable:", e)
        return {}
    out = {}
    for pid, region_key in VPA_PORT_REGION.items():
        anchor = next((r for r in vpa.get("regions", [])
                       if r.get("name", "").startswith(region_key)), None)
        if not anchor or not anchor.get("teu"):
            continue
        days = [r for r in hist.values()
                if isinstance(r.get(pid), (int, float))][-14:]
        if len(days) < 10:
            print(f"teu calibration: {pid} has {len(days)} observed day(s) "
                  f"(<10) — left uncalibrated")
            continue
        mean_daily = sum(float(r[pid]) for r in days) / len(days)
        if mean_daily <= 0:
            continue
        factor = max(0.25, min(4.0, anchor["teu"] / (mean_daily * 30.4)))
        out[pid] = {"factor": round(factor, 3), "days": len(days),
                    "anchor": anchor["teu"], "region": anchor["name"],
                    "month": vpa.get("month", "")}
        print(f"teu calibration: {pid} x{factor:.3f} (raw mean "
              f"{mean_daily:,.0f}/day -> VPA {anchor['name']} "
              f"{anchor['teu']:,} TEU/month over {len(days)} obs days)")
    return out


def dump_visits() -> None:
    """Public, cleaned copy of the AIS visit table for the vessel detail
    drawer. Only the fields the UI needs; capped at 500 most recent arrivals
    so the file stays light."""
    if not OW_VISITS.exists():
        return
    try:
        visits = json.loads(OW_VISITS.read_text()).get("visits", {})
    except Exception:
        return
    rows = []
    for v in visits.values():
        if not v.get("ata"):
            continue
        rows.append({
            "mmsi": v.get("mmsi", ""),
            "name": (v.get("name") or "").strip(),
            "port": v.get("port", ""),
            "terminal": v.get("terminal") or "",
            "loa": v.get("loa") or 0,
            "draught": v.get("draught") or 0,
            "dest": v.get("dest") or "",
            "ata": v.get("ata"),
            "lastSeen": v.get("lastSeen"),
            "atd": v.get("atd"),
            "departed": bool(v.get("departed")),
        })
    rows.sort(key=lambda r: r["ata"], reverse=True)
    (DATA_DIR / "visits.json").write_text(
        json.dumps(rows[:500], indent=1, ensure_ascii=False))
    print(f"wrote visits.json ({min(len(rows), 500)} visits)")


# ------------------------------------------- ICD boards & SOWATCO fleet ----
def _pr_fresh(pr: dict) -> bool:
    try:
        seen = dt.datetime.fromisoformat(str(pr["seen"]).replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - seen).total_seconds() <= 3 * 3600
    except Exception:
        return False


def _sane_loa(x) -> float:
    """Inland transponders often carry corrupt dimension fields (a 6 m
    'cargo barge'). Absurd LOA is shown as no-data (0), never as fact."""
    x = float(x or 0)
    return x if 20 <= x <= 400 else 0.0


def _fleet_area(lat: float, lon: float) -> str:
    for site in ICD_SITES:
        la0, lo0, la1, lo1 = site["geo"]
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            return site["name"]
    for name, (la0, lo0, la1, lo1) in FLEET_AREAS:
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            return name
    return "in range — other waters"


def build_icds() -> None:
    """Live ICD berth boards (ICD Sotrans · SOWATCO Long Binh) + the SOWATCO
    barge fleet tracker -> data/icds.json. Positions come from the wide
    '_fleet' snapshot (fallback: the HCMC box); alongside history comes from
    the persistent visit table. Everything here is AIS-observed except the
    berth-slot assignment, which is positional ordering along the wharf."""
    try:
        visits = (json.loads(OW_VISITS.read_text()).get("visits", {})
                  if OW_VISITS.exists() else {})
    except Exception:
        visits = {}
    recs = LAST_SNAPS.get("_fleet") or LAST_SNAPS.get("HCMC") or []

    icds = []
    for site in ICD_SITES:
        la0, lo0, la1, lo1 = site["geo"]
        inside = [pr for pr in recs
                  if la0 <= pr["lat"] <= la1 and lo0 <= pr["lon"] <= lo1
                  and _pr_fresh(pr) and _ow_is_cargo(pr)]
        moored = sorted(
            (pr for pr in inside
             if pr.get("nav_status") in (1, 5) or float(pr.get("sog") or 0) <= 1.0),
            key=lambda pr: pr["lat"] if site.get("axis") == "lat" else pr["lon"])
        berths = []
        for i, b in enumerate(site["berths"]):
            pr = moored[i] if i < len(moored) else None
            berths.append({
                "id": b["id"], "lenM": b["lenM"], "draftM": b.get("draftM"),
                "maxDwt": b.get("maxDwt"),
                "vessel": ((pr.get("name") or "").strip() or f"MMSI {pr['mmsi']}") if pr else None,
                "mmsi": str(pr["mmsi"]) if pr else None,
                "loa": _sane_loa(pr.get("length")) if pr else None,
                "draught": float(pr.get("draught") or 0) if pr else None,
            })
        overflow = [{"name": (pr.get("name") or "").strip() or f"MMSI {pr['mmsi']}",
                     "mmsi": str(pr["mmsi"]),
                     "loa": _sane_loa(pr.get("length"))}
                    for pr in moored[len(site["berths"]):]]
        recent = []
        cand = sorted((v for v in visits.values() if v.get("port") == site["id"]),
                      key=lambda v: v.get("ata") or "", reverse=True)[:8]
        for v in cand:
            stay = None
            try:
                end = v.get("atd") or v.get("lastSeen")
                stay = round((dt.datetime.fromisoformat(end)
                              - dt.datetime.fromisoformat(v["ata"])).total_seconds() / 3600, 1)
            except Exception:
                pass
            recent.append({
                "name": (v.get("name") or "").strip() or f"MMSI {v.get('mmsi')}",
                "mmsi": str(v.get("mmsi") or ""),
                "ata": v.get("ata"), "atd": v.get("atd"),
                "departed": bool(v.get("departed")), "stayH": stay,
                "loa": v.get("loa") or 0, "draught": v.get("draught") or 0,
            })
        icds.append({
            "id": site["id"], "name": site["name"], "operator": site["operator"],
            "address": site["address"], "waterway": site["waterway"],
            "coverage": site["coverage"], "coverageNote": site["coverageNote"],
            "wharfNote": site["wharfNote"], "specs": site["specs"],
            "geoApprox": bool(site.get("geoApprox")),
            "berths": berths, "overflow": overflow,
            "alongside": len(moored), "recent": recent,
        })
        _log_src(f"ais-{site['id'].lower()}", f"AIS geofence · {site['name']}",
                 bool(recs), len(moored),
                 err="" if recs else "no snapshot this run")

    # ---- SOWATCO barge fleet: every name-matched hull in the wide snapshot.
    # Fresh (<3h) entries are live; stale ones still carry their last-heard
    # position & time (honestly labelled). The visit table is the deepest
    # fallback for hulls the aggregate doesn't hold at all this round. ----
    fleet: dict = {}
    now_utc = dt.datetime.now(dt.timezone.utc)
    for pr in recs:
        nm = (pr.get("name") or "").strip().upper()
        if not (nm.startswith("SOWATCO") or nm.startswith("SOTRANS")):
            continue
        if not pr.get("mmsi"):
            continue
        try:
            seen = dt.datetime.fromisoformat(str(pr["seen"]).replace("Z", "+00:00"))
        except Exception:
            continue
        fresh = (now_utc - seen).total_seconds() <= 3 * 3600
        sog = float(pr.get("sog") or 0)
        fleet[str(pr["mmsi"])] = {
            "name": (pr.get("name") or "").strip(),
            "mmsi": str(pr["mmsi"]),
            "inRange": fresh,
            "underway": bool(fresh and sog > 1.0 and pr.get("nav_status") not in (1, 5)),
            "sog": sog,
            "lat": pr["lat"], "lon": pr["lon"],
            "loa": _sane_loa(pr.get("length")),
            "draught": float(pr.get("draught") or 0),
            "dest": (pr.get("destination") or "").strip(),
            "area": _fleet_area(pr["lat"], pr["lon"]),
            "lastSeen": iso(seen.astimezone(TZ)),
        }
    for v in visits.values():
        nm = (v.get("name") or "").strip().upper()
        if not (nm.startswith("SOWATCO") or nm.startswith("SOTRANS")):
            continue
        mmsi = str(v.get("mmsi") or "")
        if not mmsi or mmsi in fleet:
            continue
        fleet[mmsi] = {
            "name": (v.get("name") or "").strip(),
            "mmsi": mmsi,
            "inRange": False, "underway": False, "sog": None,
            "lat": v.get("lat"), "lon": v.get("lon"),
            "loa": _sane_loa(v.get("loa")),
            "draught": v.get("draught") or 0,
            "dest": v.get("dest") or "",
            "area": (f"last: {v.get('port')}"
                     + (" (departed)" if v.get("departed") else " (AIS silent)")),
            "lastSeen": v.get("atd") or v.get("lastSeen"),
        }
    fleet_rows = sorted(fleet.values(), key=lambda r: r["name"])

    notes = [
        "Berth occupancy & visit history are AIS-observed (Open Waters / AISHub aggregate, ~1 min delayed); "
        "the berth a barge is shown in is positional ordering along the wharf, not an operator assignment",
        "ICD Sotrans: 412 m wharf shown as 5 indicative ~82 m barge slots (operator publishes no slot numbering)",
        "SOWATCO Long Binh: pier specs (44/88/70 m, draft 3/3/7 m) from SOWATCO; the geofence is approximate "
        "until dwell clusters confirm it, and free-AIS coverage on this Đồng Nai reach is limited",
        "Fleet tracker lists AIS name-matched SOWATCO/SOTRANS hulls only — entries older than 3h show "
        "their last-heard position & time; a barge with AIS off may be absent or stale",
        "AIS data credit: AISHub contributors (aishub.net) via Open Waters AIS (openwaters.io)",
    ]
    (DATA_DIR / "icds.json").write_text(json.dumps({
        "generatedAt": iso(dt.datetime.now(TZ)),
        "icds": icds, "fleet": fleet_rows, "notes": notes,
    }, indent=1, ensure_ascii=False))
    print(f"wrote icds.json ({len(icds)} ICD boards, "
          f"{sum(i['alongside'] for i in icds)} alongside, "
          f"{len(fleet_rows)} SOWATCO-named vessels)")


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
    if calls:
        calls.sort(key=lambda c: c["eta"])
        assign_berths(calls, random.Random(7))  # berth + status pass incl. plan ships

    # daily TEU series (last 14 days). Today is computed from the current call
    # list; past days come from the persisted history store, so the chart
    # accumulates real history instead of re-deriving zeros for every day
    # before the live feed existed.
    hist_file = ROOT / "datastore" / "daily_history.json"
    hist = {}
    if hist_file.exists():
        try:
            hist = {r["date"]: r for r in json.loads(hist_file.read_text())}
        except Exception:
            hist = {}
    day0 = today.isoformat()
    row_today = {"date": day0}
    total = 0
    for p in PORTS:
        v = sum(c["estTeu"] for c in calls
                if c["port"] == p["id"] and c["eta"][:10] == day0)
        row_today[p["id"]] = v
        total += v
    row_today["total"] = total
    hist[day0] = row_today
    hist = {d: r for d, r in hist.items()
            if d >= (today - dt.timedelta(days=60)).isoformat()}  # keep 60d on disk
    hist_file.parent.mkdir(exist_ok=True)
    hist_file.write_text(json.dumps(list(hist.values()), ensure_ascii=False))
    daily = []
    for offset in range(-13, 1):
        day = (today + dt.timedelta(days=offset)).isoformat()
        if day in hist:
            daily.append(hist[day])
        else:
            row = {"date": day}
            for p in PORTS:
                row[p["id"]] = 0
            row["total"] = 0
            daily.append(row)

    # TEU calibration against the VPA monthly regional anchors (real
    # association statistics). Runs AFTER the raw daily row is persisted so
    # stored history is never double-scaled; calls + the presentation series
    # are scaled here, before the report & KPIs are derived from them.
    teu_calib = calibrate_teu(hist)
    if teu_calib:
        for c in calls:
            f = teu_calib.get(c["port"])
            if f:
                c["estTeu"] = int(round(c["estTeu"] * f["factor"]))
        for row in daily:
            for p in PORTS:
                f = teu_calib.get(p["id"])
                if f:
                    row[p["id"]] = int(round(row.get(p["id"], 0) * f["factor"]))
            row["total"] = sum(row.get(p["id"], 0) for p in PORTS)

    customs = build_customs()
    inland = make_inland(today, customs)
    report = build_report(today, calls, daily, customs, calibrated=bool(teu_calib))

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
        c.setdefault("planSrc", "")
        # final sanity net: no call may show a departure before its arrival
        # (two visits' plan rows can collide on one vessel — see merge_schedules)
        if c.get("eta") and c.get("etd") and c["etd"] <= c["eta"]:
            pred = float(c.get("stayPredH") or CLASS_STAY_H.get(c.get("vclass"), 18.0))
            c["etd"] = iso(dt.datetime.fromisoformat(c["eta"])
                           + dt.timedelta(hours=pred))
            print(f"repaired etd<eta for {c['vessel']} (plan rows of two visits collided)")
            if c.get("atd") and c.get("ata") and c["atd"] <= c["ata"]:
                c["atd"] = c["etd"]
    dump("calls.json", calls)
    dump("daily.json", daily)
    dump("customs.json", customs)
    dump("inland.json", inland)
    dump("report.json", report)
    dump_visits()  # public AIS visit table for the vessel detail drawer
    build_icds()   # live ICD berth boards + SOWATCO barge fleet (icds.json)
    src_names = [SRC_NAMES.get(x, x) for x in sorted({s.get("source") for s in scheds})] if scheds else []
    plans_txt = " + ".join(src_names)
    if mode == "demo":
        note_calls = ("Vessel calls & TEU estimates: demo fleet "
                      "(AISstream live feed and public terminal plans not connected)")
    elif mode == "schedule":
        note_calls = (f"Vessel calls from free public pilot/terminal plans ({plans_txt}, fetched daily); "
                      "times beyond the published plan, berth windows & TEU are modelled estimates; "
                      "no live AIS snapshot available this run")
    else:
        note_calls = ("Vessel calls: live in-port positions observed via AIS "
                      "(Open Waters / AISHub aggregate, ~1 min delayed), merged with public "
                      "pilot/port-authority plans"
                      + (f" ({plans_txt})" if plans_txt else "")
                      + "; berth assignment, forward ETDs & TEU remain modelled estimates")
    notes_horizon = ("Pilot/port-authority plans publish ~24–36h ahead — empty berths beyond that "
                     "horizon are unplanned, not necessarily idle")
    notes = [
        note_calls,
        "TEU figures are model estimates from draft & port stay — not terminal-confirmed lifts",
        "National trade totals are official (GDVC); province/commodity split is a demo model",
        "Monthly national/region throughput is real VPA association data",
        "ICD volumes & barge flows are corridor-model estimates (see Inland network notes)",
        "ICD berth watch (Sotrans · SOWATCO Long Binh) & the SOWATCO fleet list are AIS-observed "
        "from geofences — berth slots indicative; the Long Binh geofence is approximate and "
        "free-AIS coverage on that reach is limited",
    ]
    if mode == "live":
        notes.append("AIS data credit: AISHub contributors (aishub.net) via Open Waters AIS "
                     "(openwaters.io) — source terms require this attribution")
    if mode != "demo":
        notes.append(notes_horizon)
        notes.append("Berth layouts for the Dinh Vu-area terminals (Nam Dinh Vu, Nam Hai "
                     "Dinh Vu, VIMC Dinh Vu, VIP Greenport, Tan Vu, Hai An, Nam Hai), "
                     "Tan Cang Hiep Phuoc, SP-ITC and Gemalink are indicative — depths from "
                     "operator/VPA publications, but per-berth splits are not public")
    if mode != "demo":
        notes.append("Port-stay predictions learn from AIS-observed stay history (per vessel, then "
                     "class-at-port, then class default) — basis shown per call")
    if teu_calib:
        month = next(iter(teu_calib.values()))["month"]
        bits = ", ".join(f"{pid} ×{v['factor']:.2f} ({v['days']}d obs)"
                         for pid, v in sorted(teu_calib.items()))
        notes.append(f"TEU calibration: modelled TEU rescaled to the real VPA {month} "
                     f"regional throughput anchors — {bits}. Per-call TEU remain "
                     f"estimates; the port-level trend is reconciled with association data")
    else:
        notes.append("TEU calibration: inactive until ≥10 days of observed daily history "
                     "exist per port — shown TEU are raw draft×stay model estimates")
    dump("meta.json", {
        "generatedAt": iso(dt.datetime.now(TZ)),
        "dataMode": mode,
        "schedSources": sorted({s.get("source") for s in scheds}) if scheds else [],
        "tz": "Asia/Ho_Chi_Minh",
        "notes": notes,
        "teuCalib": {pid: {"factor": v["factor"], "days": v["days"],
                           "anchor": v["anchor"], "region": v["region"],
                           "month": v["month"]}
                     for pid, v in teu_calib.items()},
        "sources": HEALTH,
    })


def main() -> None:
    print("=== 1/5 VPA monthly throughput ===")
    refresh_vpa()
    print("=== 2/5 terminal & pilot schedules ===")
    refresh_schedules()
    print("=== 3/5 live AIS snapshots (Open Waters / AISHub) ===")
    try:
        observe_openwaters(fetch_ow_snapshots())
    except Exception as e:
        print("openwaters observation round failed (plans still apply):", e)
    print("=== 4/5 build operational data ===")
    build_all()
    print("=== 5/5 refresh official customs totals ===")
    refresh_customs()
    print("=== 6/5 refresh news ===")
    refresh_news()
    # customs & news run after build_all, so their health records only exist
    # now — restamp meta.json with the complete per-source list
    meta_file = DATA_DIR / "meta.json"
    try:
        m = json.loads(meta_file.read_text())
        m["sources"] = HEALTH
        meta_file.write_text(json.dumps(m, indent=1, ensure_ascii=False))
        print(f"meta.json restamped with {len(HEALTH)} source-health records")
    except Exception as e:
        print("meta source-health restamp failed:", e)
    print("refresh complete")


if __name__ == "__main__":
    main()
