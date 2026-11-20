"""
Robot harian Screener Saham IDX.

Langkah:
  1. Daftar SEMUA emiten IDX (situs BEI -> Yahoo screener -> tickers.txt sebagai cadangan)
  2. Harga penutupan & volume harian semua saham (Yahoo Finance)         -> data/prices.json
  3. Data fundamental (EPS, nilai buku, dividen, ROE, DER, jumlah saham) -> data/fundamentals.json
     (diperbarui seminggu sekali karena jarang berubah; PER/PBV/yield dihitung ulang
      di website memakai harga terbaru setiap hari)
  4. Broker summary harian: GRATIS dari halaman publik Indo Premier (IPOT),
     atau dari API IndexAlpha jika secret INDEXALPHA_TOKEN diisi       -> data/broker.json

Dijalankan otomatis oleh GitHub Actions. Bisa juga manual: python scripts/update.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WIB = timezone(timedelta(hours=7))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

PRICE_DAYS = 120            # hari bursa yang disimpan untuk RSI & grafik
FUND_MAX_AGE_DAYS = 6       # fundamental diambil ulang jika lebih tua dari ini
FUND_BUDGET_MIN = int(os.getenv("FUND_BUDGET_MIN", "20"))      # maks menit per run untuk fundamental
BROKER_BUDGET_MIN = int(os.getenv("BROKER_BUDGET_MIN", "30"))  # maks menit per run untuk broker summary
BROKER_KEEP_DAYS = 25       # hari bursa broker summary yang disimpan
BROKER_STORE_MIN = 5e8      # simpan baris broker jika beli atau net >= Rp 500 juta
BROKER_MIN_TURNOVER = float(os.getenv("BROKER_MIN_TURNOVER", "1e9"))  # hanya saham dgn nilai transaksi >= ini
BROKER_BACKFILL = int(os.getenv("BROKER_BACKFILL", "10"))           # hari mundur saat pertama kali jalan
INDEXALPHA = "https://api.indexalpha.id"


def log(*a):
    print(*a, flush=True)


def now_wib():
    return datetime.now(WIB)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def clean(x):
    try:
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------
# 1. DAFTAR SEMUA EMITEN
# --------------------------------------------------------------------------------------
def tickers_from_idx(session) -> dict:
    """Situs resmi BEI. Bisa diblokir Cloudflare -> kembalikan {} kalau gagal."""
    urls = [
        "https://www.idx.co.id/primary/StockData/GetSecuritiesStock?start=0&length=9999&code=&sector=&board=&language=id-id",
        "https://www.idx.co.id/primary/ListedCompany/GetCompanyProfiles?start=0&length=9999",
    ]
    for url in urls:
        try:
            r = session.get(url, timeout=30, headers={**UA, "Referer": "https://www.idx.co.id/"})
            if r.status_code != 200:
                continue
            rows = r.json().get("data") or []
            out = {}
            for row in rows:
                code = (row.get("Code") or row.get("KodeEmiten") or "").strip().upper()
                name = (row.get("Name") or row.get("NamaEmiten") or "").strip()
                if 3 <= len(code) <= 5 and code.isalpha():
                    out[code] = name
            if len(out) > 300:
                log(f"  Daftar emiten dari BEI: {len(out)}")
                return out
        except Exception as e:  # noqa: BLE001
            log(f"  BEI gagal: {e}")
    return {}


def tickers_from_yahoo() -> dict:
    try:
        import yfinance as yf
        from yfinance import EquityQuery
    except Exception:  # noqa: BLE001
        return {}
    out, offset = {}, 0
    try:
        q = EquityQuery("eq", ["exchange", "JKT"])
        while offset < 3000:
            res = yf.screen(q, offset=offset, size=250)
            quotes = res.get("quotes", []) if isinstance(res, dict) else []
            if not quotes:
                break
            for qt in quotes:
                sym = qt.get("symbol", "")
                if sym.endswith(".JK"):
                    out[sym[:-3]] = qt.get("longName") or qt.get("shortName") or ""
            offset += len(quotes)
            if len(quotes) < 250:
                break
    except Exception as e:  # noqa: BLE001
        log(f"  Yahoo screener gagal: {e}")
    if out:
        log(f"  Daftar emiten dari Yahoo: {len(out)}")
    return out


def tickers_from_file() -> dict:
    out = {}
    f = ROOT / "tickers.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            t = line.split("#")[0].strip().upper()
            if t:
                out[t] = ""
    return out


def get_universe(session) -> dict:
    log("1) Daftar emiten…")
    uni = tickers_from_idx(session) or tickers_from_yahoo()
    prev = read_json(DATA / "universe.json", {}).get("tickers", {})
    if not uni and prev:
        log(f"  Pakai daftar tersimpan: {len(prev)}")
        uni = dict(prev)
    for k, v in tickers_from_file().items():   # tickers.txt selalu ikut
        uni.setdefault(k, v)
    write_json(DATA / "universe.json", {"updated": now_wib().strftime("%Y-%m-%d"), "tickers": dict(sorted(uni.items()))})
    log(f"  Total dipantau: {len(uni)} saham")
    return uni


# --------------------------------------------------------------------------------------
# 2. HARGA
# --------------------------------------------------------------------------------------
def fetch_chart(session, code, rng="1y"):
    for attempt in range(2):
        host = "query1" if attempt % 2 == 0 else "query2"
        try:
            r = session.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{code}.JK",
                            params={"range": rng, "interval": "1d"}, timeout=20)
            if r.status_code == 429:
                time.sleep(3)
                continue
            if r.status_code != 200:
                return code, {}
            res = r.json()["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            out = {}
            for t, c, v in zip(res.get("timestamp") or [], q.get("close") or [], q.get("volume") or []):
                c = clean(c)
                if c is None:
                    continue
                out[datetime.fromtimestamp(t, WIB).strftime("%Y-%m-%d")] = (round(c, 2), int(clean(v) or 0))
            return code, out
        except Exception:  # noqa: BLE001
            time.sleep(1)
    return code, {}


def prices_yfinance(codes):
    """Cara utama: yfinance mengambil per kelompok 100 saham (cepat, tahan pembatasan Yahoo)."""
    try:
        import yfinance as yf
    except ImportError:
        return {}
    series = {}
    for k in range(0, len(codes), 100):
        chunk = codes[k:k + 100]
        for attempt in range(3):
            try:
                df = yf.download([f"{c}.JK" for c in chunk], period="1y", interval="1d", group_by="column",
                                 auto_adjust=False, threads=True, progress=False)
                break
            except Exception as e:  # noqa: BLE001
                log(f"  yfinance gagal ({e}), ulangi…")
                df = None
                time.sleep(10)
        if df is None or df.empty:
            continue
        close, vol = df["Close"], df["Volume"]
        if not hasattr(close, "columns"):          # hanya 1 saham -> Series
            close, vol = close.to_frame(f"{chunk[0]}.JK"), vol.to_frame(f"{chunk[0]}.JK")
        for col in close.columns:
            code = str(col).replace(".JK", "")
            s = {}
            for idx, c in close[col].items():
                c = clean(c)
                if c is None:
                    continue
                v = clean(vol[col].get(idx)) or 0
                s[idx.strftime("%Y-%m-%d")] = (round(c, 2), int(v))
            if s:
                series[code] = s
        log(f"  {min(k + 100, len(codes))}/{len(codes)} (berhasil {len(series)})")
    return series


def get_prices(session, codes):
    log(f"2) Harga {len(codes)} saham…")
    t0 = time.time()
    series = prices_yfinance(codes)
    missing = [c for c in codes if c not in series]
    if missing and len(missing) <= 150:           # cadangan: endpoint chart langsung, hanya untuk sisa sedikit
        log(f"  Cadangan endpoint chart untuk {len(missing)} saham…")
        with ThreadPoolExecutor(max_workers=4) as ex:
            for code, s in ex.map(lambda c: fetch_chart(session, c), missing):
                if s:
                    series[code] = s
    elif missing:
        log(f"  {len(missing)} saham tanpa data dari yfinance (dilewati agar cepat)")
    log(f"  Waktu: {int(time.time() - t0)} detik")
    failed = [c for c in codes if c not in series]
    log(f"  Berhasil {len(series)}, gagal {len(failed)}")
    if not series:
        return None
    # Gabungkan dengan data lama: hari yang sudah tersimpan tidak pernah dibuang
    # (Yahoo kadang melewatkan hari terakhir, misalnya saat diambil lewat tengah malam).
    old = read_json(DATA / "prices.json", {})
    od, oc, ov = old.get("dates") or [], old.get("close") or {}, old.get("vol") or {}
    newest = max((max(s) for s in series.values() if s), default="-")
    merged = 0
    for code, closes in oc.items():
        s = series.setdefault(code, {}) if code in codes else None
        if s is None:
            continue
        vols = ov.get(code) or [0] * len(closes)
        for i, d in enumerate(od):
            if d not in s and i < len(closes) and closes[i] is not None:
                s[d] = (closes[i], int(vols[i] or 0) if i < len(vols) else 0)
                merged += 1
    log(f"  Tanggal terbaru dari Yahoo: {newest}; {merged} nilai lama dipertahankan")
    # tanggal: hanya hari yang punya data di cukup banyak saham (hindari tanggal "nyasar")
    count = {}
    for s in series.values():
        for d in s:
            count[d] = count.get(d, 0) + 1
    min_n = max(1, len(series) // 10)
    dates = sorted(d for d, n in count.items() if n >= min_n or d in set(od))[-PRICE_DAYS:]
    close = {c: [s.get(d, (None, 0))[0] for d in dates] for c, s in sorted(series.items())}
    vol = {c: [s.get(d, (None, 0))[1] for d in dates] for c, s in sorted(series.items())}
    # nilai transaksi rata-rata 5 hari terakhir (Rp) = close * volume (Yahoo: volume dalam lembar)
    value = {}
    for c in close:
        vals = [(close[c][i] or 0) * (vol[c][i] or 0) for i in range(max(0, len(dates) - 5), len(dates))]
        value[c] = round(sum(vals) / max(1, len(vals)))
    last_val = {c: round((close[c][-1] or 0) * (vol[c][-1] or 0)) for c in close}
    payload = {"updated": now_wib().strftime("%d %b %Y %H:%M WIB"), "source": "Yahoo Finance",
               "failed": failed, "dates": dates, "close": close, "vol": vol, "value5d": value, "valueLast": last_val}
    write_json(DATA / "prices.json", payload)
    log(f"  Tersimpan: {len(close)} saham, {len(dates)} hari, terakhir {dates[-1]}")
    return payload


# --------------------------------------------------------------------------------------
# 3. FUNDAMENTAL (mingguan)
# --------------------------------------------------------------------------------------
def get_fundamentals(codes, names):
    """Data per saham disimpan dengan tanggal ambil ('t'). Setiap run hanya mengambil saham yang
    datanya belum ada / lebih tua dari FUND_MAX_AGE_DAYS, maksimal FUND_BUDGET_MIN menit,
    dan menyimpan hasil secara bertahap. Sisanya dilanjutkan run berikutnya."""
    path = DATA / "fundamentals.json"
    old = read_json(path, {})
    data = {k: v for k, v in old.get("data", {}).items() if k in set(codes)}
    today = now_wib().date()

    def age(code):
        try:
            return (today - datetime.strptime(data[code].get("t", "2000-01-01"), "%Y-%m-%d").date()).days
        except (KeyError, ValueError):
            return 9999

    todo = sorted((c for c in codes if age(c) >= FUND_MAX_AGE_DAYS), key=age, reverse=True)
    if not todo:
        log("3) Fundamental semua saham masih baru, dilewati.")
        return
    try:
        import yfinance as yf
    except ImportError:
        log("3) yfinance tidak terpasang, fundamental dilewati.")
        return
    log(f"3) Fundamental: {len(todo)} saham perlu diperbarui (maks {FUND_BUDGET_MIN} menit)…")
    deadline = time.time() + FUND_BUDGET_MIN * 60

    def one(code):
        for attempt in range(2):
            try:
                info = yf.Ticker(f"{code}.JK").info or {}
                if not info:
                    return code, None
                dr = clean(info.get("dividendRate")) or clean(info.get("trailingAnnualDividendRate"))
                roe, der = clean(info.get("returnOnEquity")), clean(info.get("debtToEquity"))
                return code, {
                    "n": info.get("longName") or info.get("shortName") or names.get(code, ""),
                    "s": info.get("sector") or "Lainnya",
                    "eps": clean(info.get("trailingEps")),
                    "bv": clean(info.get("bookValue")),
                    "div": dr,
                    "roe": round(roe * 100, 2) if roe is not None else None,
                    "der": round(der / 100, 2) if der is not None else None,
                    "sh": clean(info.get("sharesOutstanding")),
                    "t": today.strftime("%Y-%m-%d"),
                }
            except Exception:  # noqa: BLE001
                time.sleep(2 + attempt * 3)
        return code, None

    ok, done = 0, 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for k in range(0, len(todo), 40):
            if time.time() > deadline:
                log(f"  Batas waktu tercapai, {len(todo) - done} saham dilanjutkan run berikutnya.")
                break
            for code, d in ex.map(one, todo[k:k + 40]):
                done += 1
                if d:
                    data[code] = d
                    ok += 1
            write_json(path, {"date": today.strftime("%Y-%m-%d"), "source": "Yahoo Finance", "data": data})
            log(f"  {done}/{len(todo)} (berhasil {ok})")
    log(f"  Fundamental diperbarui: {ok} saham, total tersimpan {len(data)}")


# --------------------------------------------------------------------------------------
# 4. BROKER SUMMARY
#    Sumber A (gratis, default): Indo Premier / IPOT, halaman broker summary publik.
#    Sumber B (berbayar, opsional): IndexAlpha API, dipakai jika secret INDEXALPHA_TOKEN diisi.
#
#    Setiap baris disimpan sebagai [broker, nilai_beli, nilai_jual, pasti]
#    pasti = 1 jika nilai_jual diketahui persis, 0 jika itu batas atas (lihat ipot_rows).
# --------------------------------------------------------------------------------------
IPOT_DIRECT = "https://www.indopremier.com/module/saham/include/data-brokersummary.php"
IPOT_PROXY = os.getenv("IPOT_PROXY", "").strip()      # alamat Cloudflare Worker (jembatan), jika ada
IPOT_URL = IPOT_PROXY or IPOT_DIRECT
IPOT_ROWS = 10
BS_KEEP_DAYS = 5          # hari bursa broker summary lengkap (tabel per broker) yang disimpan
BS_BOARDS = ["RG", "NG"]  # pasar tambahan selain "all": Reguler dan Nego


def ipot_number(text):
    """'1.6 M' -> 1_600_000 ; '841.2 B' -> 841.2e9 ; '687,067' -> 687067 ; '' -> None"""
    t = (text or "").replace(",", "").replace("\xa0", " ").strip()
    if not t:
        return None
    mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(t[-1].upper())
    if mult:
        t = t[:-1].strip()
    try:
        return round(float(t) * (mult or 1))
    except ValueError:
        return None


def ipot_parse(html):
    """dict {buyers, sellers: [(kode,nilai)], b, s: [[kode,lot,nilai,avg]], total, tlot, fnet} | 'INVALID' | None"""
    import re
    if "invalid stock code" in html.lower():
        return "INVALID"
    a, b = html.find("<tbody>"), html.find("</tbody>")
    if a < 0 or b < 0:
        return None
    strip = lambda x: re.sub(r"<[^>]+>", "", x).replace("&nbsp;", " ").strip()
    B, S = [], []
    for row in html[a:b].split("</tr>"):
        c = [strip(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(c) < 9:
            continue
        if c[0]:
            B.append([c[0].upper(), ipot_number(c[1]) or 0, ipot_number(c[2]) or 0, ipot_number(c[3])])
        if c[5]:
            S.append([c[5].upper(), ipot_number(c[6]) or 0, ipot_number(c[7]) or 0, ipot_number(c[8])])

    def foot(label):
        m = re.search(re.escape(label) + r"\s*([^<]*)<", html)
        return ipot_number(m.group(1)) if m else None
    return {"buyers": [(x[0], x[2]) for x in B], "sellers": [(x[0], x[2]) for x in S], "b": B, "s": S,
            "total": foot("T. Val :"), "tlot": foot("T.Lot :"), "fnet": foot("F. NVal :")}


def ipot_rows(page):
    """Ubah daftar gross IPOT (10 pembeli & 10 penjual teratas) jadi baris per broker.

    Net buy = beli - jual. Jual broker diketahui persis jika ia ada di daftar penjual,
    atau jika daftar penjual < 10 baris (artinya semua penjual sudah tercantum -> jualnya 0).
    Jika tidak, jualnya PALING BESAR sama dengan penjual ke-10, sehingga kita pakai angka itu:
    net yang dihitung = batas bawah (konservatif). Akumulasi yang lolos filter pasti benar.
    """
    buyers, sellers = page["buyers"], page["sellers"]
    sell_map = dict(sellers)
    sellers_complete = len(sellers) < IPOT_ROWS
    sell_cap = 0 if sellers_complete else min(v for _, v in sellers)
    out = []
    for code, buy in buyers:
        if code in sell_map:
            out.append([code, buy, sell_map[code], 1])
        else:
            out.append([code, buy, sell_cap, 1 if sellers_complete else 0])
    return out


IPOT_ERRORS = {}   # penyebab kegagalan, untuk dicatat di log


def _ipot_err(key):
    IPOT_ERRORS[key] = IPOT_ERRORS.get(key, 0) + 1


def ipot_page(session, code, day, board="all"):
    """Satu halaman broker summary. Return dict | 'INVALID' | 'UNPUBLISHED' | None (gagal)."""
    y, m, d = day.split("-")
    md = f"{m}/{d}/{y}"
    for attempt in range(4):
        try:
            r = session.get(IPOT_URL, params={"code": code, "start": md, "end": md, "fd": "all", "board": board},
                            timeout=20, headers={**UA, "Referer": "https://www.indopremier.com/"})
            if r.status_code == 404:          # server IPOT kadang 404 palsu sesaat -> ulangi
                _ipot_err("HTTP 404")
                time.sleep(0.5 * 2 ** attempt)
                continue
            if r.status_code != 200:
                _ipot_err(f"HTTP {r.status_code}")
                time.sleep(2)
                continue
            page = ipot_parse(r.text)
            if page == "INVALID":
                return "INVALID"
            if page is None:
                _ipot_err("halaman tanpa tabel: " + r.text[:120].replace("\n", " "))
                time.sleep(2)
                continue
            if not page["b"] and not page["s"] and (page["total"] or 0) > 0:
                return "UNPUBLISHED"        # ada transaksi tapi daftar broker belum terbit
            return page
        except Exception as e:  # noqa: BLE001
            _ipot_err(f"{type(e).__name__}: {str(e)[:100]}")
            time.sleep(2)
    return None


def _pack(p):
    return {"b": p["b"], "s": p["s"], "t": p["total"], "l": p["tlot"], "f": p["fnet"]}


def ipot_one(session, code, day, detail=False):
    """Return (rows | [] | 'UNPUBLISHED' | None, detail_dict | None)."""
    p = ipot_page(session, code, day, "all")
    if p == "INVALID":
        return [], None
    if not isinstance(p, dict):
        return p, None
    det = None
    if detail:
        det = {"all": _pack(p)}
        for bd in BS_BOARDS:
            q = ipot_page(session, code, day, bd)
            if isinstance(q, dict) and (q["b"] or q["s"]):
                det[bd] = _pack(q)
    return ipot_rows(p), det


def indexalpha_one(session, code, day, detail=False):
    for attempt in range(4):
        try:
            r = session.get(f"{INDEXALPHA}/stocks/broker-summary",
                            params={"ticker": code, "from": day, "to": day, "investor": "all"}, timeout=30)
            if r.status_code == 429:
                time.sleep(5 + attempt * 10)
                continue
            if r.status_code == 403:
                return "QUOTA", None
            if r.status_code != 200:
                return None, None
            rows = r.json().get("data") or []
            return [[row.get("code", ""), round(clean(row.get("buy_value")) or 0),
                     round(clean(row.get("sell_value")) or 0), 1] for row in rows], None
        except Exception:  # noqa: BLE001
            time.sleep(3)
    return None, None


def get_broker(prices):
    token = os.getenv("INDEXALPHA_TOKEN", "").strip()
    s = requests.Session()
    if token:
        source, one, workers = "IndexAlpha", indexalpha_one, 4
        s.headers.update({"Authorization": f"Bearer {token}", "accept": "application/json"})
    else:
        source, one, workers = "Indo Premier (IPOT)", ipot_one, 3
        s.headers.update(UA)
    via = " lewat jembatan Cloudflare" if (IPOT_PROXY and not token) else ""
    path = DATA / "broker.json"
    bsdir = DATA / "bs"
    store = read_json(path, {"days": {}})
    days = store.get("days", {}) if store.get("source") in (None, source) else {}   # ganti sumber -> mulai baru
    dates = prices["dates"]
    want = dates[-max(BROKER_BACKFILL, 1):]
    if days:
        want = sorted(set(want) | {d for d in dates[-BROKER_KEEP_DAYS:] if d > max(days)})
    todo_acc = {d for d in want if d not in days}
    det_window = dates[-BS_KEEP_DAYS:] if not token else []
    todo_det = {d for d in det_window if not (bsdir / f"{d}.json").exists()}
    todo = sorted(todo_acc | todo_det, reverse=True)   # terbaru dulu
    if not todo:
        log(f"4) Broker summary ({source}) sudah lengkap.")
        return
    # Hanya saham likuid: broker mustahil net buy Rp 1 M/hari di saham yang total transaksinya < Rp 1 M
    cands = sorted(c for c, v in prices["value5d"].items() if v >= BROKER_MIN_TURNOVER)
    log(f"4) Broker summary dari {source}{via}: {len(todo)} hari × {len(cands)} saham likuid")
    probe, _ = one(s, "BBCA", todo[-1])
    log(f"  Uji koneksi (BBCA {todo[-1]}): {'OK, ' + str(len(probe)) + ' broker' if isinstance(probe, list) else probe}")
    if probe is None:
        log(f"  Sumber tidak bisa diakses, broker summary dilewati run ini. Penyebab: {IPOT_ERRORS or 'tidak diketahui'}")
        return

    stop = False
    deadline = time.time() + BROKER_BUDGET_MIN * 60

    def save():
        keep = sorted(days)[-BROKER_KEEP_DAYS:]
        write_json(path, {"updated": now_wib().strftime("%d %b %Y %H:%M WIB"), "source": source,
                          "topN": None if token else IPOT_ROWS,
                          "minTurnover": BROKER_MIN_TURNOVER, "storeMin": BROKER_STORE_MIN,
                          "calendar": [d for d in dates if keep and d >= keep[0]],
                          "days": {d: days[d] for d in keep}})

    def save_index():
        bsdir.mkdir(parents=True, exist_ok=True)
        for f in bsdir.glob("????-??-??.json"):           # buang hari di luar jendela
            if f.stem not in det_window:
                f.unlink()
        have = sorted(f.stem for f in bsdir.glob("????-??-??.json"))
        write_json(bsdir / "index.json", {"updated": now_wib().strftime("%d %b %Y %H:%M WIB"),
                                          "source": source + via, "topN": IPOT_ROWS, "days": have})

    for day in todo:
        if time.time() > deadline:
            log("  Batas waktu tercapai, sisa hari dilanjutkan run berikutnya.")
            break
        need_det = day in todo_det
        res, det_map, failed, unpublished = {}, {}, 0, 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for code, (rows, det) in zip(cands, ex.map(lambda c: one(s, c, day, need_det), cands)):
                if rows == "QUOTA":
                    stop = True
                elif rows == "UNPUBLISHED":
                    unpublished += 1
                elif rows is None:
                    failed += 1
                else:
                    res[code] = [r for r in rows if r[1] >= BROKER_STORE_MIN or (r[1] - r[2]) >= BROKER_STORE_MIN]
                    if det:
                        det_map[code] = det
        if stop:
            log("  ⚠ Kuota API IndexAlpha habis.")
        if unpublished > len(cands) // 2 or not res:
            log(f"  {day}: data broker belum terbit / gagal ({failed} gagal), dicoba lagi pada jadwal berikutnya")
            if IPOT_ERRORS:
                log(f"    penyebab gagal: {IPOT_ERRORS}")
                IPOT_ERRORS.clear()
            continue   # jangan simpan hari yang belum lengkap
        if day in todo_acc:
            days[day] = res
            save()
        if need_det and det_map:
            write_json(bsdir / f"{day}.json", {"day": day, "source": source + via, "data": det_map})
            save_index()
        log(f"  {day}: {len(res)} saham{' + tabel lengkap Semua/Reguler/Nego' if need_det else ''}, {failed} gagal")
        if IPOT_ERRORS:
            log(f"    penyebab gagal: {IPOT_ERRORS}")
            IPOT_ERRORS.clear()
        if stop:
            break


def main():
    session = requests.Session()
    session.headers.update(UA)
    uni = get_universe(session)
    codes = sorted(uni)
    prices = get_prices(session, codes)
    if not prices:
        log("GAGAL: tidak ada data harga. File lama tidak diubah.")
        sys.exit(1)
    get_fundamentals(sorted(prices["close"]), uni)
    get_broker(prices)
    log("Selesai.")


if __name__ == "__main__":
    main()
