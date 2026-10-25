// Drive the built phone app in a real browser against a fake Supabase.
//
//   python3 scripts/build_pwa.py --out dist/pwa   (with SUPABASE_URL=https://demo.supabase.co
//                                                  SUPABASE_PUBLISHABLE_KEY=sb_publishable_demo)
//   node scripts/check_pwa.mjs dist/pwa [screenshot-dir]
//
// The fake answers the handful of REST, RPC and Storage calls the app makes
// and mimics the database's decisions, so each screen and each button is
// exercised the way the owner would use it on a phone. Fails on any console
// error (which includes Content-Security-Policy violations), any horizontal
// scroll, and any action that does not reach the right database function.

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { createRequire } from "node:module";

// require() rather than import: it honours NODE_PATH, so a globally installed
// Playwright works without a package.json in this repository.
const { chromium } = createRequire(import.meta.url)("playwright");

const dir = path.resolve(process.argv[2] || "dist/pwa");
const shots = process.argv[3] || "";
const API = "https://demo.supabase.co";
const OWNER = "00000000-0000-4000-8000-000000000001";

// --- a tiny JPEG-free image: a valid PNG is enough for <img> --------------
function png(w, h, rgb) {
  const raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const o = y * (w * 3 + 1) + 1 + x * 3;
    raw[o] = (rgb[0] + x) & 255; raw[o + 1] = (rgb[1] + y) & 255; raw[o + 2] = rgb[2];
  }
  const chunk = (tag, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(tag), data]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(body) >>> 0);
    return Buffer.concat([len, body, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw)), chunk("IEND", Buffer.alloc(0))]);
}
const IMG = png(120, 120, [40, 90, 200]);

// --- fake database ---------------------------------------------------------
const id = (n) => `aaaaaaa${n}-0000-4000-8000-00000000000${n}`;
const db = {
  controls: [],
  businesses: [{ id: "b1", owner_id: OWNER, code: "stock_images", name: "AI画像販売",
    stage: "verifying", settings: {}, created_at: "2026-10-25T00:00:00Z" },
    { id: "b2", owner_id: OWNER, code: "affiliate", name: "A8アフィリエイト",
      stage: "verifying", settings: {}, created_at: "2026-10-25T00:00:01Z" }],
  affiliate_programs: [],
  accounts: [{ id: "a1", owner_id: OWNER, business_id: "b1", platform: "adobe_stock",
    daily_limit: 1, approval_policy: "auto", publish_method: "manual", status: "active",
    terms_status: "conditional", terms_source_url: "https://helpx.adobe.com/stock/" },
    { id: "a2", owner_id: OWNER, business_id: "b2", platform: "hatena_blog",
      daily_limit: 1, approval_policy: "required", publish_method: "api", status: "active",
      terms_status: "conditional", terms_source_url: "" }],
  items: [
    { id: id(1), kind: "stock_image", status: "pending_review", version: 2, title: "Calm gradient background",
      body: "淡いグラデーション", is_sample: false, blocking: false,
      preview_path: `${OWNER}/${id(1)}/preview.jpg`, final_path: `${OWNER}/${id(1)}/final.jpg`,
      meta: { keywords: ["abstract", "gradient"], category: 8, prompt: "soft gradient" },
      policy_report: { findings: [] }, quality_report: { issues: [],
        needs_human: ["破綻（手指・文字化け・不自然な形状）の目視確認"] },
      created_at: "2026-10-25T01:00:00Z" },
    { id: id(2), kind: "stock_image", status: "blocked", version: 1, title: "Castle <script>alert(1)</script>",
      body: "", is_sample: true, blocking: true, preview_path: "", final_path: "",
      meta: { keywords: [] }, policy_report: { blocking: true,
        findings: [{ detail: "著名な知的財産への言及があります" }] },
      quality_report: {}, created_at: "2026-10-25T02:00:00Z" },
    { id: id(3), kind: "stock_image", status: "awaiting_manual", version: 3, title: "Washi paper texture",
      body: "", is_sample: false, blocking: false,
      preview_path: `${OWNER}/${id(3)}/preview.jpg`, final_path: `${OWNER}/${id(3)}/final.jpg`,
      meta: { keywords: ["washi", "paper", "texture"], category: 8 },
      policy_report: {}, quality_report: {}, created_at: "2026-10-25T03:00:00Z" },
    { id: id(4), kind: "note_article", status: "awaiting_manual", version: 2,
      title: "AIで時短する3つの手順", is_sample: false, blocking: false,
      body: "## はじめに\n\n本文です。**大事**なところ。\n\n- 手順1\n- 手順2\n\n※この記事はAIの支援を受けて作成しています。",
      preview_path: "", final_path: "", meta: { price_hint_jpy: 300 },
      policy_report: {}, quality_report: {}, created_at: "2026-10-25T04:00:00Z" },
    { id: id(5), kind: "short_video", status: "awaiting_manual", version: 2,
      title: "朝が変わる5分の小ワザ", body: "", is_sample: false, blocking: false,
      preview_path: `${OWNER}/${id(5)}/preview.jpg`, final_path: `${OWNER}/${id(5)}/short.mp4`,
      meta: { description: "毎朝の小さな工夫。\n#shorts", scenes: [{ narration: "窓を開けよう", caption: "窓" }] },
      policy_report: {}, quality_report: {}, created_at: "2026-10-25T05:00:00Z" },
    { id: id(6), kind: "affiliate_article", status: "pending_review", version: 1,
      title: "テスト証券の選び方", is_sample: false, blocking: false, preview_path: "", final_path: "",
      body: '※本記事はプロモーション（広告）を含みます。\n\n## 選び方\n\n<a href="https://px.a8.net/svt/ejp?a8mat=T" rel="nofollow sponsored noopener">テスト証券の公式サイトを見る</a><img src="https://www10.a8.net/0.gif?a8mat=T" alt="">\n\n<!-- aiw:abc -->',
      meta: { program_name: "テスト証券" }, policy_report: {}, quality_report: {},
      created_at: "2026-10-25T06:00:00Z" },
  ],
  workers: [{ id: "github-actions", last_seen_at: new Date(Date.now() - 3600e3).toISOString() }],
  events: [{ id: 1, level: "info", category: "pipeline", message: "AI画像販売: 新規 1 / 確認待ち 1",
    created_at: "2026-10-25T03:00:00Z" }],
  revenue_entries: [],
};
const calls = [];

function filterRows(rows, params) {
  let out = rows.slice();
  for (const [k, v] of params) {
    if (["select", "order", "limit", "offset"].includes(k)) continue;
    const [op, ...rest] = v.split(".");
    const val = rest.join(".");
    if (op === "eq") out = out.filter((r) => String(r[k]) === val);
    else if (op === "in") {
      const set = val.replace(/^\(|\)$/g, "").split(",").map((s) => s.replace(/"/g, ""));
      out = out.filter((r) => set.includes(String(r[k])));
    } else if (op === "gte") out = out.filter((r) => String(r[k] ?? "") >= val);
    else if (op === "neq") out = out.filter((r) => String(r[k] ?? "") !== val);
  }
  const limit = params.get("limit");
  return limit ? out.slice(0, Number(limit)) : out;
}

function rpc(name, body) {
  calls.push({ rpc: name, body });
  const it = db.items.find((i) => i.id === body.p_item);
  const fail = (message) => ({ status: 400, json: { code: "23514", message } });
  if (name === "is_owner") return { json: true };
  if (name === "approve_item") {
    if (it.version !== body.p_expected_version) return fail("item changed since you viewed it");
    if (it.blocking && !body.p_force) return fail("guardrails block this item; pass force with a reason to override");
    if (body.p_force && !body.p_note.trim()) return fail("an override needs a written reason");
    it.status = it.final_path ? "awaiting_manual" : "approved";
    return { json: it };
  }
  if (name === "decide_item") {
    if (body.p_action === "revise" && !body.p_note.trim()) return fail("a revision request needs a note saying what to change");
    it.status = { revise: "needs_revision", reject: "rejected", hold: "on_hold", resubmit: "pending_review" }[body.p_action];
    return { json: it };
  }
  if (name === "mark_manual_done") {
    it.status = "published"; it.external_url = body.p_evidence; return { json: it };
  }
  if (name === "set_global_stop") {
    if (body.p_stop && !body.p_reason.trim()) return fail("say why you are stopping");
    db.controls = [{ owner_id: OWNER, global_stop: body.p_stop, stop_reason: body.p_reason }];
    return { json: db.controls[0] };
  }
  return { status: 404, json: { message: "no such function" } };
}

async function api(route) {
  const req = route.request();
  const url = new URL(req.url());
  const p = url.pathname;
  // Real Supabase exposes Content-Range across origins; without it the
  // browser hides the row count from the page.
  const cors = { "access-control-allow-origin": "*", "access-control-expose-headers": "content-range" };
  const reply = ({ status = 200, json, headers = {} }) => route.fulfill({ status,
    contentType: "application/json", headers: { ...cors, ...headers },
    body: json === undefined ? "" : JSON.stringify(json) });
  if (p === "/auth/v1/user") return reply({ json: { id: OWNER, email: "owner@example.com" } });
  if (p.startsWith("/storage/v1/object/sign/") && req.method() === "POST") {
    const bucket = p.split("/").pop();
    const { paths } = req.postDataJSON();
    return reply({ json: paths.map((x) => ({ path: x, error: null,
      signedURL: `/object/sign/${bucket}/${x}?token=t` })) });
  }
  if (p.startsWith("/storage/v1/object/sign/")) {
    return route.fulfill({ status: 200, contentType: "image/png", body: IMG });
  }
  if (p.startsWith("/rest/v1/rpc/")) {
    return reply(rpc(p.split("/").pop(), req.postDataJSON() || {}));
  }
  const table = p.replace("/rest/v1/", "");
  const rows = filterRows(db[table] || [], url.searchParams);
  if (req.method() === "HEAD") return route.fulfill({ status: 200, headers: { ...cors, "content-range": `*/${rows.length}` } });
  if (req.method() === "GET") return reply({ json: rows, headers: { "content-range": `0-${rows.length}/*` } });
  if (req.method() === "PATCH") {
    const changes = req.postDataJSON();
    calls.push({ patch: table, changes });
    for (const r of rows) Object.assign(db[table].find((x) => x.id === r.id), changes);
    return reply({ status: 204 });
  }
  if (req.method() === "POST") {
    const row = req.postDataJSON();
    calls.push({ insert: table, row });
    db[table].push({ id: `new-${db[table].length}`, status: "active", ...row });
    return reply({ status: 201 });
  }
  if (req.method() === "DELETE") {
    calls.push({ delete: table, ids: rows.map((r) => r.id) });
    db[table] = db[table].filter((r) => !rows.includes(r));
    return reply({ status: 204 });
  }
  return reply({ status: 405, json: { message: "unsupported" } });
}

// --- static server ---------------------------------------------------------
const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
  ".svg": "image/svg+xml", ".png": "image/png", ".webmanifest": "application/manifest+json" };
const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(new URL(req.url, "http://x").pathname).replace(/^\/+/, "") || "index.html";
  const file = path.join(dir, rel);
  if (!file.startsWith(dir) || !fs.existsSync(file)) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "content-type": types[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}/`;

// --- the run ---------------------------------------------------------------
const failures = [];
const check = (ok, what) => { if (!ok) failures.push(what); console.log(`${ok ? "ok  " : "FAIL"} ${what}`); };
const browser = await chromium.launch();

async function phone({ session = true, width = 390 } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 844 }, deviceScaleFactor: 2,
    acceptDownloads: true, locale: "ja-JP", timezoneId: "Asia/Tokyo",
    permissions: ["clipboard-read", "clipboard-write"] });
  if (session) {
    const jwt = ["e30", Buffer.from(JSON.stringify({ sub: OWNER, role: "authenticated",
      exp: 4102444800 })).toString("base64url"), "sig"].join(".");
    const s = { access_token: jwt, refresh_token: "r", token_type: "bearer", expires_in: 3600,
      expires_at: 4102444800, user: { id: OWNER, email: "owner@example.com", aud: "authenticated" } };
    await ctx.addInitScript((v) => localStorage.setItem("sb-demo-auth-token", v), JSON.stringify(s));
  }
  await ctx.route(`${API}/**`, api);
  const page = await ctx.newPage();
  page.errors = [];
  // The fake answers refused actions with 400, as the database would; the
  // browser logs those. Anything else on the console is a real fault.
  page.on("console", (m) => {
    if (m.type() === "error" && !/status of 400/.test(m.text())) page.errors.push(m.text());
  });
  page.on("pageerror", (e) => page.errors.push(String(e)));
  page.on("dialog", (d) => d.accept());
  return page;
}
const noSideScroll = (page) => page.evaluate(() =>
  document.documentElement.scrollWidth <= window.innerWidth);
const shot = async (page, name) => { if (shots) await page.screenshot({ path: path.join(shots, `${name}.png`), fullPage: true }); };

try {
  // signed out
  let page = await phone({ session: false });
  await page.goto(base);
  await page.getByRole("button", { name: "ログイン" }).waitFor();
  check(true, "signed out: login screen");
  await shot(page, "0-login");
  await page.close();

  page = await phone();
  await page.goto(base);
  await page.getByText("やること", { exact: true }).waitFor();
  check(await page.getByText("確認待ち 3件").isVisible(), "home: review count");
  check(await page.getByText("投稿・提出待ち 3件").isVisible(), "home: posting count across businesses");
  check(await page.getByText("未入力").isVisible(), "home: no revenue says 未入力, not ¥0");
  check(await noSideScroll(page), "home: no horizontal scroll");
  await shot(page, "1-home");

  await page.goto(base + "#/review");
  await page.getByText("Calm gradient background").waitFor();
  check(await page.locator("script").filter({ hasText: "alert(1)" }).count() === 0
    && await page.getByText("Castle <script>alert(1)</script>").isVisible(), "review: AI text is escaped");
  check(await page.getByText("サンプル").first().isVisible(), "review: sample is labelled");
  await shot(page, "2-review");

  await page.getByText("Calm gradient background").click();
  await page.getByRole("button", { name: "承認する" }).waitFor();
  check(await page.locator("img.preview").evaluate((i) => i.complete && i.naturalWidth > 0), "detail: signed preview loads");
  await shot(page, "3-detail");
  await page.getByRole("button", { name: "修正を依頼" }).click();
  await page.getByText("修正内容を書いてください").waitFor();
  check(true, "detail: revise without a note is refused in Japanese");
  await page.getByRole("button", { name: "承認する" }).click();
  await page.waitForURL(/#\/review$/);
  const approve = calls.find((c) => c.rpc === "approve_item");
  check(approve?.body.p_expected_version === 2 && approve.body.p_force === false, "detail: approve sends the viewed version");
  await page.getByText("承認しました").waitFor();
  check(true, "detail: approval toast");

  await page.getByText("Castle").click();
  await page.getByRole("button", { name: "理由を書いて承認" }).waitFor();
  check(await page.getByRole("button", { name: "承認する" }).count() === 0, "blocked: no plain approve button");
  await page.getByRole("button", { name: "理由を書いて承認" }).click();
  await page.getByText("理由を書いて承認してください").waitFor();
  check(true, "blocked: override without a reason is refused");
  await shot(page, "4-blocked");

  await page.goto(base + "#/post");
  await page.getByText("Washi paper texture").waitFor();
  check(await page.getByText("note（1件）").isVisible() && await page.getByText("YouTube ショート（1件）").isVisible(),
    "post: a section per destination");
  await shot(page, "5-post");
  const [download] = await Promise.all([page.waitForEvent("download"),
    page.getByRole("button", { name: "選んだ画像をZIPでダウンロード" }).click()]);
  const zipPath = await download.path();
  const zipBytes = fs.readFileSync(zipPath);
  const text = zipBytes.toString("latin1");
  // 2 finals awaiting upload now (one approved above) + the CSV
  check(zipBytes.readUInt32LE(0) === 0x04034b50 && text.includes("adobe_stock.csv"), "upload: zip with csv");
  check(text.includes("Filename,Title,Keywords,Category,Releases") &&
    text.includes('"aiw-aaaaaaa3.jpg","Washi paper texture","washi, paper, texture",8,'), "upload: Adobe CSV rows");
  const unzip = await page.evaluate(async () => {
    // Re-read our own ZIP with the browser to prove the central directory is right.
    const z = window.__aiworker.zip([{ name: "a.txt", data: new TextEncoder().encode("hello") }]);
    const b = new Uint8Array(await z.arrayBuffer());
    const v = new DataView(b.buffer);
    const end = b.length - 22;
    return { sig: v.getUint32(end, true), entries: v.getUint16(end + 10, true),
      crc: v.getUint32(14, true).toString(16) };
  });
  check(unzip.sig === 0x06054b50 && unzip.entries === 1 && unzip.crc === "3610a686", "zip: valid structure and CRC");
  await page.getByRole("button", { name: "選んだ分を「提出した」にする" }).click();
  await page.getByText("提出済みにしました").waitFor();
  check(calls.filter((c) => c.rpc === "mark_manual_done").length === 2, "upload: marks each item done");

  // note: rich copy, then the URL is the evidence
  await page.getByRole("button", { name: "本文をコピー" }).click();
  await page.getByText("本文をコピーしました").waitFor();
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  check(clip.includes("はじめに") && clip.includes("大事") && !clip.includes("<"), "note: body copied as text");
  const notePosted = page.getByRole("button", { name: "投稿した" }).first();
  await notePosted.click();
  await page.getByText("公開したページのURL").waitFor();
  check(true, "note: posting needs the page URL");
  await page.locator(`#url-${id(4)}`).fill("https://note.com/me/n/abc");
  await notePosted.click();
  await page.getByText("公開済みにしました").waitFor();
  check(calls.some((c) => c.rpc === "mark_manual_done" && c.body.p_evidence === "https://note.com/me/n/abc"),
    "note: marked published with its URL");
  // shorts: a download link to the signed video
  const save = page.getByRole("link", { name: "動画を保存" });
  check(await save.getAttribute("download") === "aiw-aaaaaaa5.mp4", "shorts: video download link");

  await page.goto(base + `#/item/${id(6)}`);
  await page.getByText("テスト証券の公式サイトを見る").waitFor();
  check(await page.locator(".article a").count() === 0 && await page.locator(".article img").count() === 0,
    "A8 detail: the article's link and pixel are shown as text, never live markup");

  await page.goto(base + `#/item/${id(5)}`);
  await page.locator("video.preview").waitFor();
  check(true, "shorts detail: video player");

  await page.goto(base + "#/biz");
  await page.getByRole("button", { name: "A8" }).click();
  await page.getByText("提携している案件（A8）").waitFor();
  await page.locator("#pname").fill("テスト証券");
  await page.locator("#pad").fill('<a href="https://evil.example/x">x</a>');
  await page.getByRole("button", { name: "登録する" }).click();
  await page.getByText("A8の広告リンク（px.a8.net のURL）が見つかりません").waitFor();
  check(true, "A8: a non-A8 link is refused");
  await page.locator("#pad").fill('<a href="https://px.a8.net/svt/ejp?a8mat=ABC" rel="nofollow">広告</a><img border="0" width="1" height="1" src="https://www13.a8.net/0.gif?a8mat=ABC" alt=""><script>alert(1)</script>');
  await page.locator("#pfacts").fill("口座開設は無料。");
  await page.getByRole("button", { name: "登録する" }).click();
  await page.getByText("案件を登録しました").waitFor();
  const prog = calls.find((c) => c.insert === "affiliate_programs")?.row;
  check(prog?.link_url === "https://px.a8.net/svt/ejp?a8mat=ABC" && prog.pixel_url.startsWith("https://www13.a8.net/")
    && prog.facts === "口座開設は無料。" && !JSON.stringify(prog).includes("script"), "A8: only the link and pixel are kept");
  await shot(page, "8-a8");
  await page.getByRole("button", { name: "画像販売" }).click();
  await page.getByText("Adobe Stock の設定").waitFor();
  await page.locator("#limit").fill("5");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.getByText("保存しました").waitFor();
  check(calls.some((c) => c.patch === "accounts" && c.changes.daily_limit === 5), "biz: daily limit saved");
  await page.locator("#ramount").fill("1200");
  await page.getByRole("button", { name: "記録する" }).click();
  await page.getByText("記録しました").waitFor();
  check(calls.some((c) => c.insert === "revenue_entries" && c.row.amount === 1200 && c.row.owner_id === OWNER),
    "biz: revenue recorded as the owner");
  await shot(page, "6-biz");

  await page.goto(base + "#/");
  await page.getByRole("button", { name: "全体停止" }).click();
  await page.getByRole("button", { name: "すべて止める" }).click();
  await page.getByText("停止の理由を書いてください").waitFor();
  await page.locator("#why").fill("テスト");
  await page.getByRole("button", { name: "すべて止める" }).click();
  await page.getByText("全体停止中: テスト").waitFor();
  check(true, "stop: needs a reason, then shows the banner");
  await shot(page, "7-stopped");

  await page.goto(base + "#/log");
  await page.getByText("新規 1").waitFor();
  check(true, "log: events listed");

  const narrow = await phone({ width: 320 });
  for (const h of ["#/", "#/review", `#/item/${id(1)}`, `#/item/${id(6)}`, "#/post", "#/biz"]) {
    await narrow.goto(base + h);
    await narrow.waitForSelector("main .card, main .empty, main .hero");
    check(await noSideScroll(narrow), `320px: no horizontal scroll on ${h}`);
  }
  check(page.errors.length === 0 && narrow.errors.length === 0,
    `no console errors${page.errors.length + narrow.errors.length ? ": " + [...page.errors, ...narrow.errors].join(" | ") : ""}`);
} catch (e) {
  failures.push(String(e));
  console.log("FAIL", e);
} finally {
  await browser.close();
  server.close();
}
console.log(failures.length ? `\n${failures.length} failed` : "\nall passed");
process.exit(failures.length ? 1 : 0);
