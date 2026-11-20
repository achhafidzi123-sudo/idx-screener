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
    for attempt in range(4):
        host = "query1" if attempt % 2 == 0 else "query2"
        try:
            r = session.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{code}.JK",
                            params={"range": rng, "interval": "1d"}, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
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


def get_prices(session, codes):
    log(f"2) Harga {len(codes)} saham…")
    series = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(fetch_chart, session, c) for c in codes]
        for i, f in enumerate(as_completed(futs), 1):
            code, s = f.result()
            if s:
                series[code] = s
            if i % 100 == 0:
                log(f"  {i}/{len(codes)}")
    failed = [c for c in codes if c not in series]
    log(f"  Berhasil {len(series)}, gagal {len(failed)}")
    if not series:
        return None
    # tanggal: hanya hari yang punya data di cukup banyak saham (hindari tanggal "nyasar")
    count = {}
    for s in series.values():
        for d in s:
            count[d] = count.get(d, 0) + 1
    min_n = max(1, len(series) // 10)
    dates = sorted(d for d, n in count.items() if n >= min_n)[-PRICE_DAYS:]
    close = {c: [s.get(d, (None, 0))[0] for d in dates] for c, s in sorted(series.items())}
    vol = {c: [s.get(d, (None, 0))[1] for d in dates] for c, s in sorted(series.items())}
    # nilai transaksi rata-rata 5 hari terakhir (Rp) = close * volume (Yahoo: volume dalam lembar)
    value = {}
    for c in close:
        vals = [(close[c][i] or 0) * (vol[c][i] or 0) for i in range(max(0, len(dates) - 5), len(dates))]
        value[c] = round(sum(vals) / max(1, len(vals)))
    last_val = {c: round((close[c][-1] or 0) * (vol[c][-1] or 0)) for c in close}
    payload = {"updated": now_wib().strftime("%d %b %Y %H:%M WIB"), "source": "Yahoo Finance",
               "failed": failed, "dates": dates, "close": close, "value5d": value, "valueLast": last_val}
    write_json(DATA / "prices.json", payload)
    log(f"  Tersimpan: {len(close)} saham, {len(dates)} hari, terakhir {dates[-1]}")
    return payload


# --------------------------------------------------------------------------------------
# 3. FUNDAMENTAL (mingguan)
# --------------------------------------------------------------------------------------
def get_fundamentals(codes, names):
    path = DATA / "fundamentals.json"
    old = read_json(path, {})
    force = os.getenv("FORCE_FUNDAMENTALS") == "1"
    try:
        age = (now_wib().date() - datetime.strptime(old.get("date", "2000-01-01"), "%Y-%m-%d").date()).days
    except ValueError:
        age = 999
    if not force and age < FUND_MAX_AGE_DAYS and old.get("data"):
        log(f"3) Fundamental masih baru ({age} hari), dilewati.")
        return
    try:
        import yfinance as yf
    except ImportError:
        log("3) yfinance tidak terpasang, fundamental dilewati.")
        return
    log(f"3) Fundamental {len(codes)} saham (mingguan)…")

    def one(code):
        for attempt in range(3):
            try:
                info = yf.Ticker(f"{code}.JK").info or {}
                if not info:
                    return code, None
                dr = clean(info.get("dividendRate")) or clean(info.get("trailingAnnualDividendRate"))
                return code, {
                    "n": info.get("longName") or info.get("shortName") or names.get(code, ""),
                    "s": info.get("sector") or "Lainnya",
                    "eps": clean(info.get("trailingEps")),
                    "bv": clean(info.get("bookValue")),
                    "div": dr,
                    "roe": round(clean(info.get("returnOnEquity")) * 100, 2) if clean(info.get("returnOnEquity")) is not None else None,
                    "der": round(clean(info.get("debtToEquity")) / 100, 2) if clean(info.get("debtToEquity")) is not None else None,
                    "sh": clean(info.get("sharesOutstanding")),
                }
            except Exception:  # noqa: BLE001
                time.sleep(2 + attempt * 3)
        return code, None

    data = dict(old.get("data", {}))
    ok = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, (code, d) in enumerate(ex.map(one, codes), 1):
            if d:
                data[code] = d
                ok += 1
            if i % 100 == 0:
                log(f"  {i}/{len(codes)}")
    data = {k: v for k, v in data.items() if k in set(codes)}
    write_json(path, {"date": now_wib().strftime("%Y-%m-%d"), "source": "Yahoo Finance", "data": data})
    log(f"  Fundamental diperbarui: {ok} saham")


# --------------------------------------------------------------------------------------
# 4. BROKER SUMMARY
#    Sumber A (gratis, default): Indo Premier / IPOT, halaman broker summary publik.
#    Sumber B (berbayar, opsional): IndexAlpha API, dipakai jika secret INDEXALPHA_TOKEN diisi.
#
#    Setiap baris disimpan sebagai [broker, nilai_beli, nilai_jual, pasti]
#    pasti = 1 jika nilai_jual diketahui persis, 0 jika itu batas atas (lihat ipot_rows).
# --------------------------------------------------------------------------------------
IPOT_URL = "https://www.indopremier.com/module/saham/include/data-brokersummary.php"
IPOT_ROWS = 10


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
    """Kembalikan dict {buyers:[(kode,nilai)], sellers:[(kode,nilai)], total} | 'INVALID' | None."""
    import re
    if "invalid stock code" in html.lower():
        return "INVALID"
    a, b = html.find("<tbody>"), html.find("</tbody>")
    if a < 0 or b < 0:
        return None
    strip = lambda x: re.sub(r"<[^>]+>", "", x).replace("&nbsp;", " ").strip()
    buyers, sellers = [], []
    for row in html[a:b].split("</tr>"):
        c = [strip(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(c) < 9:
            continue
        if c[0]:
            buyers.append((c[0].upper(), ipot_number(c[2]) or 0))
        if c[5]:
            sellers.append((c[5].upper(), ipot_number(c[7]) or 0))
    m = re.search(r"T\. Val :\s*([^<]*)<", html)
    total = ipot_number(m.group(1)) if m else None
    return {"buyers": buyers, "sellers": sellers, "total": total}


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


def ipot_fetch(session, code, day):
    """day = 'YYYY-MM-DD'. Return rows | [] | 'UNPUBLISHED' | None (gagal)."""
    y, m, d = day.split("-")
    md = f"{m}/{d}/{y}"
    for attempt in range(5):
        try:
            r = session.get(IPOT_URL, params={"code": code, "start": md, "end": md, "fd": "all", "board": "all"},
                            timeout=30, headers={**UA, "Referer": "https://www.indopremier.com/"})
            if r.status_code == 404:          # server IPOT kadang 404 palsu sesaat -> ulangi
                time.sleep(0.5 * 2 ** attempt)
                continue
            if r.status_code != 200:
                time.sleep(2)
                continue
            page = ipot_parse(r.text)
            if page == "INVALID":
                return []
            if page is None:
                time.sleep(2)
                continue
            if not page["buyers"] and not page["sellers"] and (page["total"] or 0) > 0:
                return "UNPUBLISHED"        # ada transaksi tapi daftar broker belum terbit
            return ipot_rows(page)
        except Exception:  # noqa: BLE001
            time.sleep(2)
    return None


def indexalpha_fetch(session, code, day):
    for attempt in range(4):
        try:
            r = session.get(f"{INDEXALPHA}/stocks/broker-summary",
                            params={"ticker": code, "from": day, "to": day, "investor": "all"}, timeout=30)
            if r.status_code == 429:
                time.sleep(5 + attempt * 10)
                continue
            if r.status_code == 403:
                return "QUOTA"
            if r.status_code != 200:
                return None
            rows = r.json().get("data") or []
            return [[row.get("code", ""), round(clean(row.get("buy_value")) or 0),
                     round(clean(row.get("sell_value")) or 0), 1] for row in rows]
        except Exception:  # noqa: BLE001
            time.sleep(3)
    return None


def get_broker(prices):
    token = os.getenv("INDEXALPHA_TOKEN", "").strip()
    s = requests.Session()
    if token:
        source, fetch, workers = "IndexAlpha", indexalpha_fetch, 4
        s.headers.update({"Authorization": f"Bearer {token}", "accept": "application/json"})
    else:
        source, fetch, workers = "Indo Premier (IPOT)", ipot_fetch, 3
        s.headers.update(UA)
    path = DATA / "broker.json"
    store = read_json(path, {"days": {}})
    days = store.get("days", {}) if store.get("source") in (None, source) else {}   # ganti sumber -> mulai baru
    dates = prices["dates"]
    if days:
        todo = [d for d in dates[-BROKER_KEEP_DAYS:] if d > max(days)]
    else:
        todo = dates[-BROKER_BACKFILL:]
    if not todo:
        log(f"4) Broker summary ({source}) sudah lengkap.")
        return
    # Hanya saham likuid: broker mustahil net buy Rp 1 M/hari di saham yang total transaksinya < Rp 1 M
    cands = sorted(c for c, v in prices["value5d"].items() if v >= BROKER_MIN_TURNOVER)
    log(f"4) Broker summary dari {source}: {len(todo)} hari × {len(cands)} saham likuid")

    stop = False
    for day in todo:
        res, failed, unpublished = {}, 0, 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for code, rows in zip(cands, ex.map(lambda c: fetch(s, c, day), cands)):
                if rows == "QUOTA":
                    stop = True
                elif rows == "UNPUBLISHED":
                    unpublished += 1
                elif rows is None:
                    failed += 1
                else:
                    res[code] = [r for r in rows if r[1] >= BROKER_STORE_MIN or (r[1] - r[2]) >= BROKER_STORE_MIN]
        if stop:
            log("  ⚠ Kuota API IndexAlpha habis.")
        if unpublished > len(cands) // 2 or not res:
            log(f"  {day}: data broker belum terbit / gagal ({failed} gagal), dicoba lagi pada jadwal berikutnya")
            break   # jangan simpan hari yang belum lengkap; hari berikutnya juga pasti belum ada
        days[day] = res
        log(f"  {day}: {len(res)} saham tersimpan, {failed} gagal")
        if stop:
            break
    keep = sorted(days)[-BROKER_KEEP_DAYS:]
    write_json(path, {"updated": now_wib().strftime("%d %b %Y %H:%M WIB"), "source": source,
                      "topN": None if token else IPOT_ROWS,
                      "minTurnover": BROKER_MIN_TURNOVER, "storeMin": BROKER_STORE_MIN,
                      "days": {d: days[d] for d in keep}})


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
