#!/usr/bin/env python3
"""
Screener Saham IDX - Fundamental + Likuiditas
=============================================
Data: Yahoo Finance via yfinance (kode saham IDX + ".JK").

Cara pakai:
    pip install -r requirements.txt
    python screener_idx.py                      # pakai tickers_idx.txt
    python screener_idx.py --tickers saya.txt   # daftar ticker sendiri
    python screener_idx.py --mode likuiditas    # cepat, tanpa data fundamental
    python screener_idx.py --mode fundamental
    python screener_idx.py --out hasil          # folder output
    python screener_idx.py --json data.json     # data untuk website screener

Output: CSV di folder output + ringkasan di terminal.
Semua ambang batas bisa diubah di bagian CONFIG di bawah.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

# ======================================================================
# CONFIG - ubah sesuai gaya trading kamu
# ======================================================================
CONFIG = {
    # --- Likuiditas / jejak bandar ---
    "min_harga": 50,                    # Rp, hindari saham "gocap ke bawah"
    "min_avg_value_20d": 5e9,           # rata-rata nilai transaksi 20 hari (Rp 5 M)
    "min_value_hari_ini": 1e9,          # nilai transaksi hari terakhir (Rp 1 M)
    "volume_spike_ratio": 2.0,          # volume hari ini >= 2x rata-rata 20 hari
    "akumulasi_hari": 5,                # jendela cek akumulasi senyap
    "akumulasi_vol_ratio": 1.3,         # avg vol 5 hari >= 1.3x avg 20 hari
    "akumulasi_max_naik": 0.05,         # ...tapi harga naik <= 5% (belum lari)

    # --- Fundamental ---
    "per_min": 0.0,                     # PER > 0 (perusahaan untung)
    "per_max": 15.0,
    "pbv_max": 2.0,
    "roe_min": 0.12,                    # 12%
    "der_max": 1.0,                     # 1x ekuitas (bank/keuangan dikecualikan)
    "div_yield_min": 0.0,               # 0 = tidak wajib bayar dividen
    "earnings_growth_min": 0.0,         # pertumbuhan laba YoY > 0

    # --- Teknis eksekusi ---
    "history_period": "1y",
    "max_workers": 6,                   # jangan terlalu tinggi, bisa kena rate limit Yahoo
}

SEKTOR_KEUANGAN = {"Financial Services"}


# ======================================================================
# Ambil data
# ======================================================================
def load_tickers(path: Path) -> list[str]:
    tickers = []
    for line in path.read_text(encoding="utf-8").splitlines():
        t = line.split("#")[0].strip().upper()
        if t:
            tickers.append(t if t.endswith(".JK") else f"{t}.JK")
    return sorted(set(tickers))


def download_history(tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    out: dict[str, pd.DataFrame] = {}
    batch = 50
    for i in range(0, len(tickers), batch):
        chunk = tickers[i : i + batch]
        print(f"  harga {i + 1}-{i + len(chunk)} dari {len(tickers)}...", flush=True)
        df = yf.download(
            chunk, period=period, interval="1d", group_by="ticker",
            auto_adjust=False, progress=False, threads=True,
        )
        for t in chunk:
            try:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub.dropna(subset=["Close", "Volume"])
                if len(sub) >= 25:
                    out[t] = sub
            except KeyError:
                pass
        time.sleep(1)
    return out


def _fetch_info(ticker: str) -> dict:
    import yfinance as yf

    for attempt in range(3):
        try:
            return yf.Ticker(ticker).info or {}
        except Exception:
            time.sleep(2 * (attempt + 1))
    return {}


def download_fundamentals(tickers: list[str], workers: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_info, t): t for t in tickers}
        for n, fut in enumerate(cf.as_completed(futs), 1):
            out[futs[fut]] = fut.result()
            if n % 20 == 0 or n == len(tickers):
                print(f"  fundamental {n}/{len(tickers)}...", flush=True)
    return out


# ======================================================================
# Hitung metrik
# ======================================================================
def liquidity_metrics(hist: pd.DataFrame, cfg: dict) -> dict:
    close = hist["Close"].astype(float)
    vol = hist["Volume"].astype(float)
    value = close * vol

    n = cfg["akumulasi_hari"]
    avg_vol_20 = vol.iloc[-21:-1].mean()          # 20 hari SEBELUM hari terakhir
    avg_val_20 = value.iloc[-21:-1].mean()
    med_vol_n = vol.iloc[-n:].median()           # median: 1 hari spike tidak dihitung akumulasi
    harga = close.iloc[-1]

    return {
        "tanggal_data": hist.index[-1].strftime("%Y-%m-%d"),
        "harga": harga,
        "chg_1d": close.iloc[-1] / close.iloc[-2] - 1,
        "chg_5d": close.iloc[-1] / close.iloc[-1 - n] - 1,
        "chg_20d": close.iloc[-1] / close.iloc[-21] - 1,
        "value_hari_ini": value.iloc[-1],
        "avg_value_20d": avg_val_20,
        "vol_ratio": vol.iloc[-1] / avg_vol_20 if avg_vol_20 > 0 else math.nan,
        "vol_ratio_5d": med_vol_n / avg_vol_20 if avg_vol_20 > 0 else math.nan,
        "jarak_high_52w": harga / close.max() - 1,
    }


def _num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else math.nan
    except (TypeError, ValueError):
        return math.nan


_FX_CACHE: dict[str, float] = {}


def fx_rate(dari: str, ke: str) -> float:
    """Kurs dari mata uang laporan keuangan ke mata uang harga (mis. USD -> IDR)."""
    key = f"{dari}{ke}"
    if key not in _FX_CACHE:
        try:
            import yfinance as yf
            h = yf.Ticker(f"{dari}{ke}=X").history(period="5d")
            _FX_CACHE[key] = float(h["Close"].iloc[-1])
        except Exception:
            _FX_CACHE[key] = math.nan
    return _FX_CACHE[key]


def fundamental_metrics(info: dict) -> dict:
    dy = _num(info.get("dividendYield"))
    if not math.isnan(dy) and dy > 0.5:   # yfinance baru kirim dalam persen (mis. 4.2)
        dy = dy / 100
    if math.isnan(dy):
        dy = _num(info.get("trailingAnnualDividendYield"))

    der = _num(info.get("debtToEquity"))
    if not math.isnan(der):
        der = der / 100                   # yfinance kirim dalam persen (45 = 0.45x)

    # Koreksi PBV untuk emiten yang laporan keuangannya bukan Rupiah (mis. ADRO, ESSA dalam USD)
    pbv = _num(info.get("priceToBook"))
    cur, fin_cur = info.get("currency"), info.get("financialCurrency")
    if cur and fin_cur and cur != fin_cur:
        price = _num(info.get("currentPrice") or info.get("regularMarketPrice"))
        bv = _num(info.get("bookValue"))
        fx = fx_rate(fin_cur, cur)
        pbv = price / (bv * fx) if bv and bv > 0 and not math.isnan(fx) else math.nan

    return {
        "nama": info.get("shortName") or info.get("longName") or "",
        "sektor": info.get("sector") or "",
        "market_cap": _num(info.get("marketCap")),
        "per": _num(info.get("trailingPE")),
        "pbv": pbv,
        "mata_uang_laporan": fin_cur or "",
        "roe": _num(info.get("returnOnEquity")),
        "der": der,
        "div_yield": dy,
        "earnings_growth": _num(info.get("earningsGrowth")),
    }


# ======================================================================
# Filter & skor
# ======================================================================
def check_liquidity(m: dict, cfg: dict) -> dict:
    likuid = (
        m["harga"] >= cfg["min_harga"]
        and m["avg_value_20d"] >= cfg["min_avg_value_20d"]
        and m["value_hari_ini"] >= cfg["min_value_hari_ini"]
    )
    spike = likuid and m["vol_ratio"] >= cfg["volume_spike_ratio"]
    akumulasi = (
        likuid
        and not spike
        and m["vol_ratio_5d"] >= cfg["akumulasi_vol_ratio"]
        and abs(m["chg_5d"]) <= cfg["akumulasi_max_naik"]
    )
    sinyal = []
    if spike:
        sinyal.append("VOLUME SPIKE" + (" naik" if m["chg_1d"] > 0 else " turun"))
    if akumulasi:
        sinyal.append("AKUMULASI SENYAP")
    return {"lolos_likuid": likuid, "sinyal_bandar": ", ".join(sinyal)}


def check_fundamental(f: dict, cfg: dict) -> dict:
    alasan_gagal = []
    keuangan = f["sektor"] in SEKTOR_KEUANGAN

    def cek(nama, nilai, ok):
        if math.isnan(nilai):
            alasan_gagal.append(f"{nama} n/a")
        elif not ok(nilai):
            alasan_gagal.append(nama)

    cek("PER", f["per"], lambda v: cfg["per_min"] < v <= cfg["per_max"])
    cek("PBV", f["pbv"], lambda v: 0 < v <= cfg["pbv_max"])
    cek("ROE", f["roe"], lambda v: v >= cfg["roe_min"])
    if not keuangan:
        cek("DER", f["der"], lambda v: v <= cfg["der_max"])
    if cfg["div_yield_min"] > 0:
        cek("DivYield", f["div_yield"], lambda v: v >= cfg["div_yield_min"])
    cek("Growth", f["earnings_growth"], lambda v: v > cfg["earnings_growth_min"])

    # skor 0-100: makin murah & makin profitable makin tinggi
    skor = 0.0
    if not math.isnan(f["per"]) and f["per"] > 0:
        skor += max(0, 25 * (1 - f["per"] / (cfg["per_max"] * 2)))
    if not math.isnan(f["pbv"]) and f["pbv"] > 0:
        skor += max(0, 20 * (1 - f["pbv"] / (cfg["pbv_max"] * 2)))
    if not math.isnan(f["roe"]):
        skor += min(25, max(0, f["roe"] / 0.25 * 25))
    if not math.isnan(f["div_yield"]):
        skor += min(15, f["div_yield"] / 0.08 * 15)
    if not math.isnan(f["earnings_growth"]):
        skor += min(15, max(0, f["earnings_growth"] / 0.30 * 15))

    return {
        "lolos_fundamental": not alasan_gagal,
        "gagal_di": ", ".join(alasan_gagal),
        "skor_fundamental": round(skor, 1),
    }


def build_table(hist: dict, infos: dict, cfg: dict, mode: str) -> pd.DataFrame:
    rows = []
    for t in sorted(set(hist) | set(infos)):
        row = {"kode": t.replace(".JK", "")}
        if t in hist:
            m = liquidity_metrics(hist[t], cfg)
            row.update(m)
            row.update(check_liquidity(m, cfg))
        elif mode != "fundamental":
            continue
        if mode != "likuiditas":
            f = fundamental_metrics(infos.get(t, {}))
            row.update(f)
            row.update(check_fundamental(f, cfg))
        rows.append(row)
    return pd.DataFrame(rows)


# ======================================================================
# Output
# ======================================================================
def fmt_rp(x):
    if pd.isna(x):
        return "-"
    for div, suf in ((1e12, "T"), (1e9, "M"), (1e6, "jt")):
        if abs(x) >= div:
            return f"{x / div:.1f}{suf}"
    return f"{x:,.0f}"


def fmt_pct(x):
    return "-" if pd.isna(x) else f"{x * 100:+.1f}%"


def print_section(title: str, df: pd.DataFrame, cols: list[str]):
    print(f"\n=== {title} ({len(df)}) ===")
    if df.empty:
        print("  (tidak ada)")
        return
    view = df[cols].copy()
    for c in view.columns:
        if c in ("value_hari_ini", "avg_value_20d", "market_cap"):
            view[c] = view[c].map(fmt_rp)
        elif c in ("chg_1d", "chg_5d", "chg_20d", "roe", "div_yield", "earnings_growth", "jarak_high_52w"):
            view[c] = view[c].map(fmt_pct)
        elif c in ("per", "pbv", "der", "vol_ratio", "vol_ratio_5d"):
            view[c] = view[c].map(lambda v: "-" if pd.isna(v) else f"{v:.2f}")
        elif c == "harga":
            view[c] = view[c].map(lambda v: f"{v:,.0f}")
    print(view.to_string(index=False))


def report(df: pd.DataFrame, mode: str, outdir: Path):
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path_all = outdir / f"screener_semua_{stamp}.csv"
    df.to_csv(path_all, index=False)

    if mode != "fundamental":
        bandar = df[df["sinyal_bandar"].fillna("") != ""].sort_values("vol_ratio", ascending=False)
        print_section("SINYAL LIKUIDITAS / BANDAR", bandar,
                      ["kode", "harga", "chg_1d", "chg_5d", "value_hari_ini",
                       "avg_value_20d", "vol_ratio", "vol_ratio_5d", "sinyal_bandar"])
        bandar.to_csv(outdir / f"screener_bandar_{stamp}.csv", index=False)

    if mode != "likuiditas":
        fund = df[df["lolos_fundamental"] == True].sort_values("skor_fundamental", ascending=False)  # noqa: E712
        print_section("LOLOS FUNDAMENTAL", fund,
                      ["kode", "harga", "per", "pbv", "roe", "der", "div_yield",
                       "earnings_growth", "skor_fundamental"])
        fund.to_csv(outdir / f"screener_fundamental_{stamp}.csv", index=False)

        gagal = df["gagal_di"].fillna("")
        hampir = df[(gagal != "") & (~gagal.str.contains(",")) & (~gagal.str.contains("n/a"))]
        hampir = hampir.sort_values("skor_fundamental", ascending=False)
        print_section("HAMPIR LOLOS (gagal 1 kriteria)", hampir,
                      ["kode", "harga", "per", "pbv", "roe", "der", "div_yield",
                       "earnings_growth", "gagal_di", "skor_fundamental"])

    if mode == "semua":
        combo = df[(df["lolos_fundamental"] == True) & (df["lolos_likuid"] == True)]  # noqa: E712
        combo = combo.sort_values("skor_fundamental", ascending=False)
        print_section("FUNDAMENTAL BAGUS + LIKUID (watchlist utama)", combo,
                      ["kode", "harga", "per", "pbv", "roe", "avg_value_20d",
                       "vol_ratio", "sinyal_bandar", "skor_fundamental"])
        combo.to_csv(outdir / f"screener_watchlist_{stamp}.csv", index=False)

    print("\nKeterangan: jt = juta, M = miliar, T = triliun Rupiah. vol_ratio = volume hari ini / rata-rata 20 hari.")
    print(f"CSV tersimpan di: {outdir.resolve()}")


def export_json(df: pd.DataFrame, hist: dict, path: Path, n_close: int = 60):
    """Simpan hasil + 60 harga penutupan terakhir untuk website screener."""
    import json

    rows = []
    for rec in df.to_dict(orient="records"):
        clean = {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in rec.items()}
        h = hist.get(f"{rec['kode']}.JK")
        clean["closes"] = [round(float(x), 2) for x in h["Close"].iloc[-n_close:]] if h is not None else []
        rows.append(clean)
    tanggal = max((r.get("tanggal_data") or "" for r in rows), default="")
    payload = {"tanggal_data": tanggal, "dibuat": datetime.now().isoformat(timespec="minutes"), "saham": rows}
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"JSON untuk website: {path.resolve()}")


# ======================================================================
def main(argv=None):
    p = argparse.ArgumentParser(description="Screener saham IDX (fundamental + likuiditas)")
    p.add_argument("--tickers", default=str(Path(__file__).with_name("tickers_idx.txt")))
    p.add_argument("--mode", choices=["semua", "fundamental", "likuiditas"], default="semua")
    p.add_argument("--out", default="hasil_screener")
    p.add_argument("--json", help="simpan juga data untuk website (mis. data.json)")
    a = p.parse_args(argv)

    tickers = load_tickers(Path(a.tickers))
    print(f"Screening {len(tickers)} saham, mode: {a.mode}")

    hist = download_history(tickers, CONFIG["history_period"])
    print(f"  data harga OK: {len(hist)} saham")
    if not hist and a.mode != "fundamental":
        sys.exit("Tidak ada data harga. Cek koneksi internet / ticker.")

    infos = {}
    if a.mode != "likuiditas":
        targets = list(hist) if hist else tickers
        infos = download_fundamentals(targets, CONFIG["max_workers"])

    df = build_table(hist, infos, CONFIG, a.mode)
    report(df, a.mode, Path(a.out))
    if a.json:
        export_json(df, hist, Path(a.json))


if __name__ == "__main__":
    main()
