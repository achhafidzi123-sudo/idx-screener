# Screener Saham IDX — otomatis

Website screener untuk **semua saham di Bursa Efek Indonesia**, diperbarui otomatis setiap hari bursa pukul ±19:30 WIB oleh robot GitHub. Tidak perlu update manual.

| Fitur | Data | Sumber | Update |
|---|---|---|---|
| 📉 RSI ≤ 20 | Harga penutupan & volume semua saham | Yahoo Finance (gratis) | Setiap hari bursa |
| 📊 Fundamental | EPS, nilai buku, dividen, ROE, DER, jumlah saham | Yahoo Finance (gratis) | Mingguan (PER/PBV/yield dihitung ulang tiap hari dengan harga terbaru) |
| 🏦 Akumulasi broker | Broker summary per saham per hari | **Gratis:** halaman publik Indo Premier (IPOT). Opsional berbayar: IndexAlpha | Setiap hari bursa |
| Daftar emiten | Semua saham tercatat | Situs BEI → Yahoo → `tickers.txt` | Setiap hari |

## Pemasangan (sekali saja)

1. **Akun GitHub**: daftar di https://github.com.
2. **Repository baru**: klik **+ → New repository**, nama misalnya `idx-screener`, pilih **Public**, lalu **Create repository**.
3. **Unggah file**: klik **uploading an existing file**, lalu seret semua isi folder ini (termasuk folder tersembunyi `.github`), lalu **Commit changes**.
4. **Aktifkan Pages**: **Settings → Pages → Source: GitHub Actions**.
5. **(Opsional) Data broker lengkap berbayar**: tanpa langkah ini, broker summary tetap diambil **gratis** dari Indo Premier (IPOT). Jika ingin daftar broker lengkap, beli token di https://indexalpha.id lalu simpan di **Settings → Secrets and variables → Actions → New repository secret** dengan nama `INDEXALPHA_TOKEN`. Robot otomatis beralih ke IndexAlpha.
6. **Jalankan pertama kali**: **Actions → Update data & deploy website → Run workflow**. Run pertama butuh ±15–30 menit karena mengambil fundamental semua saham.
7. **Buka website**: `https://NAMA-AKUN.github.io/idx-screener/`. Di HP, pilih **Add to Home Screen**.

Setelah itu semuanya berjalan sendiri setiap Senin–Jumat.

## Tentang data broker gratis (IPOT)

- Sumber: halaman broker summary publik Indo Premier Sekuritas, tanpa login. Datanya berasal dari transaksi bursa, tapi **hanya 10 pembeli dan 10 penjual teratas** per saham, dan angka besar dibulatkan 1 desimal (mis. `841.2 B`).
- Daftarnya **gross** (broker yang sama bisa muncul sebagai pembeli dan penjual). Robot menghitung net beli = beli − jual. Kalau jual broker tidak terlihat karena ia tidak masuk 10 penjual teratas, robot memakai nilai penjual ke-10 sebagai jualnya (batas atas). Hasilnya: **net beli tidak pernah dilebihkan**, jadi akumulasi yang muncul pasti nyata; yang mungkin terjadi hanyalah ada akumulasi kecil yang terlewat.
- Hanya saham dengan rata-rata transaksi ≥ Rp 1 miliar/hari yang diambil (yang lebih sepi mustahil memenuhi kriteria). Pertama kali jalan, robot mengambil 10 hari bursa ke belakang.
- Ini bukan API resmi: Indo Premier bisa mengubah atau menutupnya sewaktu-waktu, dan syarat penggunaannya tidak dinyatakan. Untuk keamanan, sebaiknya website tidak dipromosikan ke publik.

## Catatan penting

- **Data publik**: repository Public berarti file data (termasuk broker summary) bisa dilihat siapa saja. Jika tidak ingin begitu, gunakan repository **Private** (GitHub Pages untuk repo private butuh GitHub Pro).
- Yahoo Finance adalah sumber tidak resmi. Sesekali ada saham yang gagal diambil atau data fundamental yang kosong/kurang akurat.
- Jadwal GitHub bisa terlambat 5–30 menit. GitHub bisa menonaktifkan jadwal di repo publik yang lama tanpa aktivitas. Kalau update berhenti, buka tab Actions dan aktifkan lagi.
- Website ini alat bantu penyaringan, bukan rekomendasi beli/jual.
