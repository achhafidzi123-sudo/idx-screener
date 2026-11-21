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

// ---- Berita pasar (RSS) ----
const FEEDS = {
  "Antara Bursa": "https://www.antaranews.com/rss/ekonomi-bursa.xml",
  "Antara Ekonomi": "https://www.antaranews.com/rss/ekonomi.xml",
  "CNBC Indonesia": "https://www.cnbcindonesia.com/market/rss",
  "Kontan": "https://investasi.kontan.co.id/rss",
  "Bisnis": "https://market.bisnis.com/rss",
  "Detik Finance": "https://rss.detik.com/index.php/finance",
};
const CORS = { "access-control-allow-origin": "*" };
const dekode = (x) => x.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1").replace(/<[^>]+>/g, " ")
  .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#0?39;|&apos;/g, "'")
  .replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim();
const tag = (b, n) => { const m = b.match(new RegExp("<" + n + "[^>]*>([\\s\\S]*?)</" + n + ">", "i")); return m ? dekode(m[1]) : ""; };
async function beritaSatu(nama, url) {
  try {
    const r = await fetch(url, { headers: HEADERS, cf: { cacheTtl: 30 } });
    if (!r.ok) return { nama, ok: false, status: r.status, items: [] };
    const x = await r.text();
    const items = (x.match(/<item[\s>][\s\S]*?<\/item>/gi) || []).slice(0, 25).map((b) => {
      const l = (b.match(/<link[^>]*>([\s\S]*?)<\/link>/i) || [])[1] || "";
      const t = Date.parse(tag(b, "pubDate") || tag(b, "dc:date"));
      return { j: tag(b, "title"), u: dekode(l), r: tag(b, "description").slice(0, 220), t: isNaN(t) ? 0 : t, s: nama };
    }).filter((i) => i.j && i.u);
    return { nama, ok: items.length > 0, status: r.status, items };
  } catch (e) { return { nama, ok: false, status: 0, items: [] }; }
}

export default {
  async fetch(request) {
    const u = new URL(request.url);

    if (u.searchParams.get("berita")) {
      const hasil = await Promise.all(Object.entries(FEEDS).map(([n, f]) => beritaSatu(n, f)));
      const items = hasil.flatMap((h) => h.items).sort((a, b) => b.t - a.t).slice(0, 150);
      const sumber = hasil.map((h) => ({ nama: h.nama, ok: h.ok, status: h.status, jumlah: h.items.length }));
      return new Response(JSON.stringify({ waktu: Date.now(), sumber, items }),
        { headers: { ...CORS, "content-type": "application/json; charset=utf-8", "cache-control": "public, max-age=30" } });
    }

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
