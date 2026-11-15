"""
Energy Analytics — daily power-market collector (runs on GitHub Actions).

Collects:
  • IEX Day-Ahead Market (DAM) and Real-Time Market (RTM): daily average / max / min
    market clearing price (MCP, Rs/MWh), cleared volume, and the 96 fifteen-minute prices.
    Source: public "Market Snapshot" pages on iexindia.com.
  • Grid-India daily Power Supply Position (PSP) report: All-India and Eastern Region
    peak demand met, energy met, shortages, and Bihar's peak demand / energy met.
    Source: Grid-India's public report list (webapi.grid-india.in) and the daily .xls file.

Writes data/power.json (keeps ~120 days of daily figures, 15-minute prices for the last 4 days).
Each source is independent: if one website changes or is down, the other still updates and
the old figures are kept. A short report of what was read is printed to the job log.
"""
import io, json, re, sys, datetime as dt
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUT = Path(__file__).resolve().parent.parent / "data" / "power.json"
KEEP_DAYS = 120
KEEP_BLOCK_DAYS = 4
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})
LOG = []


def log(msg):
    LOG.append(msg)
    print(msg, flush=True)


def num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return None if x != x else float(x)  # NaN check
    s = str(x).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def iso(dmy):  # 14-11-2026 -> 2026-11-14
    d, m, y = dmy.split("-")
    return f"{y}-{m}-{d}"


# ----------------------------------------------------------------------------- IEX
IEX_URL = ("https://www.iexindia.com/market-data/{seg}/market-snapshot"
           "?interval=ONE_FOURTH_HOUR&dp={dp}&showGraph=false&toDate=1&fromDate=1")
SEGMENTS = {"dam": "day-ahead-market", "rtm": "real-time-market"}


def parse_iex(html):
    """Returns {date: {'avg','max','min','vol_mwh','blocks':[96 prices]}} from one snapshot page."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for table in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if any(h.lower().startswith("summary") for h in heads):
            # Summary table: Date | Summary | Purchase | Sell | MCV | FSV | MCP
            for tr in table.select("tbody tr"):
                c = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(c) < 7 or not re.match(r"\d{2}-\d{2}-\d{4}$", c[0]):
                    continue
                d = out.setdefault(iso(c[0]), {})
                kind = c[1].lower()
                if kind.startswith("avg"):
                    d["avg"] = num(c[6]); d["mcv_avg_mw"] = num(c[4])
                elif kind.startswith("max"):
                    d["max"] = num(c[6])
                elif kind.startswith("min"):
                    d["min"] = num(c[6])
                elif kind.startswith("total"):
                    d["vol_mwh"] = num(c[4])
        elif any("time block" in h.lower() for h in heads):
            cur = None
            for tr in table.select("tbody tr"):
                c = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                for x in c:
                    if re.match(r"\d{2}-\d{2}-\d{4}$", x):
                        cur = iso(x)
                if not cur or len(c) < 6:
                    continue
                block, price = c[-6], num(c[-1])
                if re.match(r"\d\d:\d\d\s*-\s*\d\d:\d\d", block) and price is not None:
                    out.setdefault(cur, {}).setdefault("blocks", []).append(round(price, 2))
    # Fill avg/max/min from blocks if the summary table was missing
    for d in out.values():
        b = d.get("blocks") or []
        if b and d.get("avg") is None:
            d["avg"] = round(sum(b) / len(b), 2); d["max"] = max(b); d["min"] = min(b)
    return out


def collect_iex(data):
    for key, seg in SEGMENTS.items():
        periods = ["YESTERDAY", "TODAY"] + (["TOMORROW"] if key == "dam" else [])
        for dp in periods:
            try:
                r = S.get(IEX_URL.format(seg=seg, dp=dp), timeout=60)
                r.raise_for_status()
                days = parse_iex(r.text)
                if not days:
                    log(f"IEX {key.upper()} {dp}: page read but no table found (site layout may have changed)")
                    continue
                for date, d in days.items():
                    blocks = d.pop("blocks", None)
                    complete = blocks is not None and len(blocks) >= 96
                    # RTM 'today' is only partly traded: keep blocks, not the daily figures
                    if key == "rtm" and dp == "TODAY" and not complete:
                        d = {}
                    if d.get("avg") is not None:
                        data["iex"][key][date] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}
                    if blocks:
                        data["iex_blocks"][key][date] = blocks
                    log(f"IEX {key.upper()} {dp} {date}: avg {d.get('avg')} Rs/MWh, {len(blocks or [])} blocks")
            except Exception as e:  # keep going: other sources still update
                log(f"IEX {key.upper()} {dp}: FAILED — {e}")


# ----------------------------------------------------------------------------- Grid-India
GI_LIST = "https://webapi.grid-india.in/api/v1/file"
GI_CDN = "https://webcdn.grid-india.in/"


def fy_of(d):
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def psp_files(today):
    """Latest .xls PSP reports: [(report_date, url)], newest first, one per date."""
    years = {fy_of(today), fy_of(today - dt.timedelta(days=10))}
    files = []
    for fy in years:
        r = S.post(GI_LIST, json={"_source": "GRDW", "_type": "DAILY_PSP_REPORT", "_fileDate": fy, "_month": "00"},
                   headers={"Origin": "https://grid-india.in", "Referer": "https://grid-india.in/"}, timeout=60)
        r.raise_for_status()
        for f in r.json().get("retData") or []:
            if "excel" not in (f.get("MimeType") or "") and not str(f.get("FilePath", "")).lower().endswith((".xls", ".xlsx")):
                continue
            m = re.match(r"(\d{2})\.(\d{2})\.(\d{2})", f.get("Title_") or "")
            if not m:
                continue
            rep = dt.date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
            files.append((rep, f.get("Id") or 0, GI_CDN + f["FilePath"].lstrip("/")))
    best = {}
    for rep, fid, url in files:  # highest Id per date = latest upload (e.g. a revised report)
        if rep not in best or fid > best[rep][0]:
            best[rep] = (fid, url)
    return sorted(((d, u) for d, (_, u) in best.items()), reverse=True)


def norm(x):
    return re.sub(r"\s+", " ", str(x if x is not None else "")).strip()


def parse_psp(content):
    """Reads the PSP .xls. Returns All-India, Eastern Region and Bihar figures (None when not found)."""
    import pandas as pd
    engine = "xlrd" if content[:4] == b"\xd0\xcf\x11\xe0" else None
    sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine=engine)
    res = {}
    found_bihar = False
    for name, df in sheets.items():
        rows = [[v for v in r] for r in df.itertuples(index=False)]
        # ---- Section A: header row with NR WR SR ER NER TOTAL
        for i, r in enumerate(rows):
            cells = [norm(v).upper() for v in r]
            if "ER" in cells and "NER" in cells and any(c in ("TOTAL", "ALL INDIA") for c in cells) and "all_peak_mw" not in res:
                c_er = cells.index("ER")
                c_tot = next(k for k, c in enumerate(cells) if c in ("TOTAL", "ALL INDIA"))
                for r2 in rows[i + 1:i + 25]:
                    label = next((norm(v) for v in r2 if isinstance(v, str) and norm(v)), "").lower()
                    if not label:
                        continue
                    tot = r2[c_tot] if c_tot < len(r2) else None
                    er = r2[c_er] if c_er < len(r2) else None
                    if "evening peak" in label:
                        res["all_evening_peak_mw"] = num(tot); res["er_evening_peak_mw"] = num(er)
                    elif label.startswith("peak shortage"):
                        res["all_peak_shortage_mw"] = num(tot)
                    elif label.startswith("energy met"):
                        res["all_energy_mu"] = num(tot); res["er_energy_mu"] = num(er)
                    elif label.startswith("energy shortage"):
                        res["all_energy_shortage_mu"] = num(tot)
                    elif label.startswith("maximum demand met"):
                        res["all_peak_mw"] = num(tot); res["er_peak_mw"] = num(er)
                    elif label.startswith("time of maximum demand"):
                        t = norm(tot)
                        res["all_peak_time"] = t[:5] if re.match(r"\d{1,2}:\d{2}", t) else (t or None)
                    elif label.startswith("solar gen"):
                        res["all_solar_mu"] = num(tot)
                    elif label.startswith("wind gen"):
                        res["all_wind_mu"] = num(tot)
        # ---- Section C: state table, Bihar row
        if found_bihar:
            continue
        for i, r in enumerate(rows):
            texts = [norm(v) for v in r]
            if not any(t.lower() == "bihar" for t in texts):
                continue
            k = next(j for j, t in enumerate(texts) if t.lower() == "bihar")
            vals = [(j, num(v)) for j, v in enumerate(r) if j > k and num(v) is not None]
            if len(vals) < 5:
                continue  # some other table mentioning Bihar
            # find the column headings above (Max demand / Shortage / Energy Met / Energy Shortage)
            hdr = {}
            for r0 in rows[max(0, i - 40):i][::-1]:
                for j, v in enumerate(r0):
                    t = norm(v).lower()
                    if not t:
                        continue
                    if ("max" in t and "demand" in t) and "max_d" not in hdr:
                        hdr["max_d"] = j
                    elif t.startswith("shortage during") and "short" not in hdr:
                        hdr["short"] = j
                    elif t.startswith("energy met") and "energy" not in hdr:
                        hdr["energy"] = j
                    elif t.startswith("energy shortage") and "e_short" not in hdr:
                        hdr["e_short"] = j
                if len(hdr) >= 3:
                    break
            by_col = dict(vals)
            pick = lambda key, pos: by_col.get(hdr[key]) if key in hdr and hdr[key] in by_col else (vals[pos][1] if len(vals) > pos else None)
            res["bihar_peak_mw"] = pick("max_d", 0)
            res["bihar_peak_shortage_mw"] = pick("short", 1)
            res["bihar_energy_mu"] = pick("energy", 2)
            res["bihar_energy_shortage_mu"] = pick("e_short", 6)
            found_bihar = True
            break
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in res.items() if v is not None}


def psp_debug(content, url):
    """Writes data/psp-debug.txt: sheet names and the first rows of each sheet (public report data)."""
    try:
        import pandas as pd
        engine = "xlrd" if content[:4] == b"\xd0\xcf\x11\xe0" else None
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, engine=engine)
        lines = [f"Source: {url}", f"Sheets: {list(sheets)}"]
        for name, df in sheets.items():
            lines.append(f"\n===== {name}  ({df.shape[0]} rows x {df.shape[1]} cols)")
            for i, r in enumerate(df.itertuples(index=False)):
                cells = [norm(v)[:40] for v in r]
                if any(c and c.lower() != "nan" for c in cells):
                    lines.append(f"{i:>3}: " + " | ".join("" if c.lower() == "nan" else c for c in cells))
                if i > 140:
                    lines.append("...")
                    break
        (OUT.parent / "psp-debug.txt").write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        (OUT.parent / "psp-debug.txt").write_text(f"Could not read {url}: {e!r}", encoding="utf-8")


def collect_grid(data, today):
    try:
        files = psp_files(today)
    except Exception as e:
        log(f"Grid-India report list: FAILED — {e!r}")
        return
    log(f"Grid-India report list: {len(files)} Excel reports found, newest {files[0][0] if files else 'none'}")
    for rep, url in files[:5]:
        key = rep.isoformat()
        have = data["grid"].get(key)
        if have and have.get("all_peak_mw") and have.get("bihar_peak_mw") and have.get("src") == url:
            continue
        try:
            r = S.get(url, timeout=90)
            r.raise_for_status()
            if rep == files[0][0]:
                psp_debug(r.content, url)
            vals = parse_psp(r.content)
            if not vals:
                log(f"Grid-India {key}: file read but no figures recognised (report layout may have changed)")
                continue
            vals["src"] = url
            data["grid"][key] = vals
            log(f"Grid-India {key}: All-India peak {vals.get('all_peak_mw')} MW at {vals.get('all_peak_time')}, "
                f"energy {vals.get('all_energy_mu')} MU; Bihar peak {vals.get('bihar_peak_mw')} MW, "
                f"energy {vals.get('bihar_energy_mu')} MU")
        except Exception as e:
            log(f"Grid-India {key}: FAILED — {e!r}")


# ----------------------------------------------------------------------------- main
def main():
    data = {"iex": {"dam": {}, "rtm": {}}, "iex_blocks": {"dam": {}, "rtm": {}}, "grid": {}}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8"))
            for k in data:
                if isinstance(old.get(k), dict):
                    data[k].update(old[k])
        except Exception as e:
            log(f"Old power.json unreadable, starting fresh: {e}")
    before = json.dumps({k: data[k] for k in ("iex", "iex_blocks", "grid")}, sort_keys=True)

    now = dt.datetime.now(IST)
    collect_iex(data)
    collect_grid(data, now.date())

    # Trim history
    cut = (now.date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    cut_b = (now.date() - dt.timedelta(days=KEEP_BLOCK_DAYS)).isoformat()
    for k in ("dam", "rtm"):
        data["iex"][k] = dict(sorted((d, v) for d, v in data["iex"][k].items() if d >= cut))
        data["iex_blocks"][k] = dict(sorted((d, v) for d, v in data["iex_blocks"][k].items() if d >= cut_b))
    data["grid"] = dict(sorted((d, v) for d, v in data["grid"].items() if d >= cut))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    (OUT.parent / "power-log.txt").write_text(f"Run {now.isoformat(timespec='minutes')}\n" + "\n".join(LOG), encoding="utf-8")
    after = json.dumps({k: data[k] for k in ("iex", "iex_blocks", "grid")}, sort_keys=True)
    if after == before and OUT.exists():
        log("No new figures; power.json left unchanged.")
        return 0
    data["updated"] = now.isoformat(timespec="minutes")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"Saved {OUT.name}: {len(data['iex']['dam'])} DAM days, {len(data['iex']['rtm'])} RTM days, "
        f"{len(data['grid'])} Grid-India days.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
