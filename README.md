# Cryptonym Screener IDX

Website screener saham IDX (fundamental + likuiditas/bandar) oleh @cryptonym.trade.

- `index.html`: website statis, membaca `data.json`
- `data.json`: hasil screening terakhir
- `screener/`: script Python yang menghasilkan `data.json` dari Yahoo Finance
- `.github/workflows/update-data.yml`: GitHub Actions menjalankan screener setiap hari bursa jam 16.30 WIB, commit `data.json`, lalu Vercel otomatis deploy ulang

Update manual: tab **Actions** > **Update data screener** > **Run workflow**.

Jalankan lokal:
```bash
pip install -r screener/requirements.txt
python screener/screener_idx.py --json data.json
python -m http.server 8000   # buka http://localhost:8000
```

Bukan rekomendasi beli/jual. Data Yahoo Finance; broker summary dan foreign flow tidak tersedia.
