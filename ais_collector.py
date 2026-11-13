#!/usr/bin/env python3
"""
AISstream live collector — the free real-vessel feed for VN Port Watch.

Setup
-----
1. Get a FREE API key at https://aisstream.io (GitHub login, no card).
2. Repo Settings -> Secrets and variables -> Actions -> New repository secret:
   name = AISSTREAM_API_KEY, value = your key.
3. The daily workflow runs this script as a 25-minute soak before refresh.py.
   (25 min/day ≈ 750 min/month — inside AISstream's free 2,000 min budget.)

What it collects
----------------
* Port geofences (Hai Phong, Da Nang, Cat Lai/HCMC, Cai Mep): a container-
  ship PositionReport entering a geofence appends an ARRIVAL event, leaving
  it appends DEPARTURE -> datastore/events.jsonl
* One wide South-China-Sea approach box: ShipStaticData carries Destination
  + ETA, so ships heading for the tracked ports give us EXPECTED arrivals
  with real ETAs -> stored in datastore/ais_state.json

refresh.py (build_live_calls) merges the datastore into live port calls.
No key / no coverage -> the dashboard simply stays in labelled demo mode.

Run standalone (soak test):
    AISSTREAM_API_KEY=xxxx python3 ais_collector.py --minutes 20
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import pathlib

from refresh import PORTS, CORRIDORS, CORRIDOR_BOXES, TZ  # port + corridor geofences

ROOT = pathlib.Path(__file__).resolve().parent
STORE = ROOT / "datastore"
EVENTS = STORE / "events.jsonl"
STATE = STORE / "ais_state.json"  # mmsi -> static data + last seen + inside

WS_URL = "wss://stream.aisstream.io/v0/stream"
# Wide approach box: the Vietnamese coast + main South China Sea lanes.
APPROACH_BOX = [[8.0, 103.0], [22.5, 112.5]]

# Corridor endpoints for direction labelling: axis 'from' -> 'to'.
CORRIDOR_ENDS = {cid: (frm, to) for cid, _n, _w, frm, to, _c, _t in CORRIDORS}


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(state):
    STORE.mkdir(exist_ok=True)
    # prune ships not seen for 10 days so the file stays small
    cutoff = (dt.datetime.now(TZ) - dt.timedelta(days=10)).isoformat()
    state = {k: v for k, v in state.items() if v.get("lastSeen", "") >= cutoff}
    STATE.write_text(json.dumps(state))


def port_of(lat, lon):
    for p in PORTS:
        (lat0, lon0), (lat1, lon1) = p["geofence"]
        if lat0 <= lat <= lat1 and lon0 <= lon <= lon1:
            return p
    return None


def corridor_of(lat, lon):
    for cid, spec in CORRIDOR_BOXES.items():
        (lat0, lon0), (lat1, lon1) = spec["box"]
        if lat0 <= lat <= lat1 and lon0 <= lon <= lon1:
            return cid
    return None


def flush_transit(mmsi, st, now_iso, min_disp=0.0):
    """Emit a TRANSIT event for a barge-sized vessel tracked inside a river-
    corridor geofence, once per vessel+corridor per 6 h. Direction comes from
    the displacement between first and last fix, projected on the corridor
    axis. min_disp gates how much movement we require before logging (loose
    at soak end / box exit, stricter mid-soak)."""
    track = st.get("corr")
    if not track:
        return
    cid = track["cid"]
    sent = (st.get("corrSent") or {}).get(cid, "")
    if sent and sent > (dt.datetime.now(TZ) - dt.timedelta(hours=6)).isoformat():
        st["corr"] = None
        return
    (f_lat, f_lon), (l_lat, l_lon) = track["first"], track["last"]
    d_lat, d_lon = l_lat - f_lat, l_lon - f_lon
    disp = (d_lat * d_lat + d_lon * d_lon) ** 0.5
    if disp < min_disp:
        return  # keep tracking — more fixes may come
    (a_lat, a_lon), (b_lat, b_lon) = CORRIDOR_BOXES[cid]["axis"]
    dot = d_lat * (b_lat - a_lat) + d_lon * (b_lon - a_lon)
    frm, to = CORRIDOR_ENDS[cid]
    toward = (to if dot > 0 else frm) if disp >= 0.008 else ""
    append_event({
        "event": "TRANSIT", "mmsi": mmsi, "corridor": cid,
        "time": track.get("firstT") or now_iso,
        "vessel": st.get("name") or f"MMSI {mmsi}",
        "loa": st.get("loa"), "sog": st.get("sog"), "toward": toward,
    })
    st.setdefault("corrSent", {})[cid] = now_iso
    print(f"TRANSIT {st.get('name') or mmsi} ~ {cid} -> {toward or '?'}")
    st["corr"] = None


def append_event(ev):
    STORE.mkdir(exist_ok=True)
    with EVENTS.open("a") as f:
        f.write(json.dumps(ev) + "\n")


async def run(minutes: int) -> None:
    import websockets  # pip install websockets (only dependency)

    key = os.environ.get("AISSTREAM_API_KEY", "").strip()
    subscribe = {
        "APIKey": key,
        "BoundingBoxes": ([p["geofence"] for p in PORTS]
                          + [spec["box"] for spec in CORRIDOR_BOXES.values()]
                          + [APPROACH_BOX]),
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }
    state = load_state()
    deadline = dt.datetime.now(TZ) + dt.timedelta(minutes=minutes)
    n_pos = n_static = 0

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps(subscribe))
        while dt.datetime.now(TZ) < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
            except Exception:
                break
            msg = json.loads(raw)
            mtype = msg.get("MessageType")
            meta = msg.get("MetaData", {})
            mmsi = str(meta.get("MMSI", ""))
            if not mmsi:
                continue
            now_iso = dt.datetime.now(TZ).isoformat(timespec="seconds")

            if mtype == "ShipStaticData":
                n_static += 1
                body = msg["Message"]["ShipStaticData"]
                dim = body.get("Dimension") or {}
                st = state.setdefault(mmsi, {})
                st.update({
                    "name": (meta.get("ShipName") or "").strip(),
                    "imo": body.get("ImoNumber"),
                    "maxDraft": body.get("MaximumStaticDraught"),
                    "loa": (dim.get("A") or 0) + (dim.get("B") or 0),
                    "type": body.get("Type"),
                    "dest": (body.get("Destination") or "").strip(),
                    "etaAis": body.get("Eta"),  # {Month,Day,Hour,Minute} UTC
                    "lastSeen": now_iso,
                })
                continue

            if mtype != "PositionReport":
                continue
            n_pos += 1
            body = msg["Message"]["PositionReport"]
            lat, lon = body.get("Latitude"), body.get("Longitude")
            if lat is None or lon is None:
                continue
            p = port_of(lat, lon)
            inside = p["id"] if p else None
            st = state.setdefault(mmsi, {})
            st["lastSeen"] = now_iso
            st["lastLat"], st["lastLon"] = lat, lon
            st["sog"] = body.get("Sog")
            if body.get("Draught"):
                st["draught"] = body["Draught"]
            if inside:
                # last confirmed presence in port waters — a ship that later
                # goes silent closed its VN stay at roughly this moment
                st["lastInside"] = {"port": inside, "time": now_iso}
            was = st.get("inside")

            if inside and was != inside:
                st["inside"] = inside
                append_event({
                    "event": "ARRIVAL", "mmsi": mmsi, "port": inside, "time": now_iso,
                    "vessel": st.get("name") or f"MMSI {mmsi}", "imo": st.get("imo"),
                    "maxDraft": st.get("maxDraft"), "loa": st.get("loa"),
                    "draft": st.get("draught"), "sog": st.get("sog"),
                })
                print(f"ARRIVAL {st.get('name') or mmsi} -> {inside}")
            elif not inside and was:
                st["inside"] = None
                append_event({
                    "event": "DEPARTURE", "mmsi": mmsi, "port": was, "time": now_iso,
                    "vessel": st.get("name") or f"MMSI {mmsi}", "imo": st.get("imo"),
                    "maxDraft": st.get("maxDraft"), "loa": st.get("loa"),
                    "draft": st.get("draught"),
                })
                print(f"DEPARTURE {st.get('name') or mmsi} <- {was}")

            # --- river-corridor barge transits (subset: AIS-equipped only) ---
            loa = float(st.get("loa") or 0)
            barge_sized = not loa or loa < 120  # small/unknown craft only
            cid = corridor_of(lat, lon)
            if cid and barge_sized and not inside:
                track = st.get("corr")
                if not track or track.get("cid") != cid:
                    st["corr"] = {"cid": cid, "first": [lat, lon],
                                  "last": [lat, lon], "firstT": now_iso}
                else:
                    track["last"] = [lat, lon]
                flush_transit(mmsi, st, now_iso, min_disp=0.015)
            elif st.get("corr"):
                flush_transit(mmsi, st, now_iso, min_disp=0.0)  # left the box
                st["corr"] = None

    # soak over — flush corridor tracks still open (direction from fixes we got)
    for mmsi, st in state.items():
        if st.get("corr"):
            flush_transit(mmsi, st, dt.datetime.now(TZ).isoformat(timespec="seconds"),
                          min_disp=0.0)
            st["corr"] = None

    save_state(state)
    print(f"soak done: {n_pos} position reports, {n_static} static reports, "
          f"{len(state)} ships tracked")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=25)
    args = ap.parse_args()
    if not os.environ.get("AISSTREAM_API_KEY", "").strip():
        print("AISSTREAM_API_KEY not set — skipping live collection "
              "(dashboard stays in demo mode). Get a free key at https://aisstream.io")
        return
    asyncio.run(run(args.minutes))


if __name__ == "__main__":
    main()
