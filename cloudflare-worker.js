// Jembatan broker summary untuk Screener By Afi Cuy.
// Tempel seluruh isi file ini di Cloudflare Worker, lalu klik Deploy.
// Worker ini HANYA meneruskan permintaan broker summary ke Indo Premier (IPOT).
//
// Cara cek: buka  https://<alamat-worker-anda>/?cek=1  di browser.

const IPOT = "https://www.indopremier.com/module/saham/include/data-brokersummary.php";
const HEADERS = {
  "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
  "Referer": "https://www.indopremier.com/",
  "Accept": "text/html,*/*",
};

async function ambil(url) {
  let r;
  for (let i = 0; i < 3; i++) {                 // IPOT kadang memberi 404 palsu sesaat
    r = await fetch(url, { headers: HEADERS });
    if (r.status !== 404) break;
    await new Promise((s) => setTimeout(s, 400 * (i + 1)));
  }
  return r;
}

function mmddyyyy(d) {
  return String(d.getUTCMonth() + 1).padStart(2, "0") + "/" + String(d.getUTCDate()).padStart(2, "0") + "/" + d.getUTCFullYear();
}

export default {
  async fetch(request) {
    const u = new URL(request.url);

    // Mode cek: coba ambil broker summary BBCA hari bursa terakhir
    if (u.searchParams.get("cek")) {
      const d = new Date(Date.now() + 7 * 3600e3);             // waktu WIB
      for (let i = 0; i < 7; i++) {
        const w = d.getUTCDay();
        if (w !== 0 && w !== 6 && !(i === 0 && d.getUTCHours() < 17)) break;
        d.setUTCDate(d.getUTCDate() - 1);
      }
      const tgl = mmddyyyy(d);
      const r = await ambil(`${IPOT}?code=BBCA&start=${tgl}&end=${tgl}&fd=all&board=all`);
      const html = await r.text();
      const brokers = (html.match(/<tr/g) || []).length;
      const ok = r.status === 200 && html.includes("<tbody>");
      const pesan = ok
        ? `BERHASIL ✅  IPOT menjawab (status ${r.status}) untuk BBCA ${tgl}. Jembatan siap dipakai.`
        : `GAGAL ❌  IPOT menjawab status ${r.status}. Kirim screenshot halaman ini ke Claude.`;
      return new Response(`${pesan}\n\nDetail teknis: ${brokers} baris tabel, ${html.length} karakter.\n${html.slice(0, 200)}`,
        { headers: { "content-type": "text/plain; charset=utf-8" } });
    }

    const code = (u.searchParams.get("code") || "").toUpperCase();
    const start = u.searchParams.get("start") || "";
    const end = u.searchParams.get("end") || "";
    const fd = u.searchParams.get("fd") || "all";
    const board = u.searchParams.get("board") || "all";
    const tgl = /^\d{2}\/\d{2}\/\d{4}$/;
    if (!/^[A-Z0-9]{2,6}$/.test(code) || !tgl.test(start) || !tgl.test(end)
        || !["all", "F", "D"].includes(fd) || !["all", "RG", "TN", "NG"].includes(board)) {
      return new Response("Jembatan broker summary aktif. Buka ?cek=1 untuk menguji.",
        { status: 200, headers: { "content-type": "text/plain; charset=utf-8" } });
    }
    const r = await ambil(`${IPOT}?code=${code}&start=${start}&end=${end}&fd=${fd}&board=${board}`);
    return new Response(await r.text(), {
      status: r.status,
      headers: { "content-type": "text/html; charset=utf-8", "x-upstream-status": String(r.status) },
    });
  },
};
