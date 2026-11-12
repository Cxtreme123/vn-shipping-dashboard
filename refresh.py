#!/usr/bin/env python3
"""
Daily data refresh for the VN Port Watch dashboard (single-file edition).

Runs unattended in GitHub Actions (see .github/workflows/update.yml).
Standard library only — no pip installs needed.

What it does, in order:
  1. refresh_vpa()      — newest VPA monthly container-throughput report
                          (real association statistics; parsed & validated,
                          previous file kept on any failure).
  2. build_all()        — regenerates ports/calls/daily/customs/inland/report/meta
                          JSON for "today" (Asia/Ho_Chi_Minh). Vessel calls come
                          from the clearly-labelled DEMO fleet until a live AIS
                          feed is wired in.
  3. refresh_customs()  — best-effort scrape of GDVC headline monthly totals.
  4. refresh_news()     — latest Vietnam trade/logistics news from GDELT
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
            tsel, bsel = rng.choice(free)
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
            "geofenceNote": "AIS geofence line set in aisstream_collector (barge class filter)",
        })

    return {
        "corridors": corridors, "icds": icds, "transits": transits[:60],
        "notes": [
            "ICD volumes modelled from customs regional model (province catchment × ~ICD gate share) — no live gate feed in free tier",
            "Barge transits are demo events; live tracking uses the same AISstream geofences along river corridors (Class A/B AIS carriage varies on inland barges)",
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

# ---------------------------------------------------------------- main ----
def build_all() -> None:
    """Regenerate every operational JSON for 'today' (Asia/Ho_Chi_Minh)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(TZ).date()

    calls = make_demo_calls(today)
    mode = "demo"

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
    dump("calls.json", calls)
    dump("daily.json", daily)
    dump("customs.json", customs)
    dump("inland.json", inland)
    dump("report.json", report)
    dump("meta.json", {
        "generatedAt": iso(dt.datetime.now(TZ)),
        "dataMode": mode,
        "tz": "Asia/Ho_Chi_Minh",
        "notes": [
            "Vessel calls & TEU estimates: demo fleet (AISstream live feed not connected)"
            if mode == "demo" else
            "Vessel calls from AISstream live geofence feed",
            "TEU figures are model estimates from draft & port stay — not terminal-confirmed lifts",
            "National trade totals are official (GDVC); province/commodity split is a demo model",
            "Monthly national/region throughput is real VPA association data",
            "ICD volumes & barge flows are corridor-model estimates (see Inland network notes)",
        ],
    })


def main() -> None:
    print("=== 1/4 VPA monthly throughput ===")
    refresh_vpa()
    print("=== 2/4 build operational data ===")
    build_all()
    print("=== 3/4 refresh official customs totals ===")
    refresh_customs()
    print("=== 4/4 refresh news ===")
    refresh_news()
    print("refresh complete")


if __name__ == "__main__":
    main()
