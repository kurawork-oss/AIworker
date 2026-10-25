// AIworker — the owner's phone app.
//
// A static page talking to Supabase with the *publishable* key and the
// owner's own login. It holds no secret and enforces nothing: every rule
// (who may approve, which status moves are legal, approval invalidated by an
// edit) lives in the database, so this page can only ask.

const cfg = window.AIWORKER_CONFIG || {};
const app = document.getElementById("app");
const sb = cfg.supabaseUrl && cfg.supabaseKey && window.supabase
  ? window.supabase.createClient(cfg.supabaseUrl, cfg.supabaseKey,
    { auth: { persistSession: true, autoRefreshToken: true } })
  : null;

const TZ = "Asia/Tokyo";
const GOAL_JPY = 50000;
// What each business makes and where it goes. The pipeline creates the
// business rows on its first run; these are only display names.
const BUSINESSES = {
  stock_images: { label: "画像販売", dest: "Adobe Stock" },
  note_articles: { label: "note", dest: "note" },
  affiliate: { label: "A8", dest: "はてなブログ" },
  youtube_shorts: { label: "ショート", dest: "YouTube" },
};
const KIND = {
  stock_image: "画像", note_article: "note記事", affiliate_article: "A8記事", short_video: "ショート動画",
};

const STATUS = {
  draft: ["下書き", ""], image_queued: ["画像生成待ち", ""], checking: ["チェック中", ""],
  pending_review: ["確認待ち", "warn"], blocked: ["ブロック", "bad"],
  needs_revision: ["AIが修正中", "warn"], on_hold: ["保留", ""],
  approved: ["承認済み", "good"], rejected: ["却下", ""], waiting_pc: ["PC待ち", ""],
  awaiting_manual: ["投稿待ち", "warn"], scheduled: ["投稿中", "good"],
  published: ["公開済み", "good"], failed: ["失敗", "bad"], cancelled: ["取消", ""],
};
const STAGE = { designing: "設計中", preparing: "準備中", verifying: "検証中",
  operating: "運用中", stopped: "停止" };
const ERRORS = [
  [/changed since you viewed/, "表示後に内容が更新されました。開き直してください"],
  [/pass force with a reason|needs a written reason/, "ブロック中です。理由を書いて承認してください"],
  [/needs a note/, "修正内容を書いてください"],
  [/say why/, "停止の理由を書いてください"],
  [/Invalid login credentials/, "メールアドレスかパスワードが違います"],
  [/not allowed|permission denied|row-level security/, "権限がありません"],
  [/Failed to fetch|NetworkError|Load failed/, "通信できませんでした。電波を確認してください"],
];

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const yen = (n) => "¥" + Math.round(Number(n) || 0).toLocaleString("ja-JP");
const when = (iso) => iso ? new Intl.DateTimeFormat("ja-JP", { timeZone: TZ,
  month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(iso)) : "—";
const ago = (iso) => {
  if (!iso) return "未実行";
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return "たった今";
  if (m < 60) return `${m}分前`;
  if (m < 1440) return `${Math.round(m / 60)}時間前`;
  return `${Math.round(m / 1440)}日前`;
};
const tokyoDate = (d = new Date()) => new Intl.DateTimeFormat("en-CA", { timeZone: TZ }).format(d);
const tokyoMidnightIso = () => new Date(`${tokyoDate()}T00:00:00+09:00`).toISOString();
const say = (err) => {
  const msg = String(err?.message || err || "");
  for (const [re, ja] of ERRORS) if (re.test(msg)) return ja;
  return "エラー: " + msg.slice(0, 160);
};
const badge = (status) => {
  const [label, tone] = STATUS[status] || [status, ""];
  return `<span class="badge ${tone}">${esc(label)}</span>`;
};
const sampleBadge = (it) => it.is_sample ? '<span class="badge flat">サンプル</span>' : "";
const kindBadge = (it) => `<span class="badge flat">${esc(KIND[it.kind] || it.kind)}</span>`;
async function must(promise) {
  const { data, error, count } = await promise;
  if (error) throw error;
  return count ?? data;
}

let flash = "";
function toast(msg) { flash = msg; }

// ---------------------------------------------------------------------------
// shell
// ---------------------------------------------------------------------------
const TABS = [
  ["#/", "ホーム", '<path d="M3 11l9-7 9 7v9h-6v-6H9v6H3z"/>'],
  ["#/review", "確認", '<path d="M9 5h10M9 12h10M9 19h10M4 5l1 1 2-2M4 12l1 1 2-2M4 19l1 1 2-2"/>'],
  ["#/post", "投稿", '<path d="M12 16V4M6 10l6-6 6 6M4 20h16"/>'],
  ["#/biz", "事業", '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>'],
  ["#/log", "履歴", '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3 2"/>'],
];

function shell(title, body, { controls = null, current = "" } = {}) {
  const stop = controls?.global_stop;
  const tabs = TABS.map(([href, label, icon]) =>
    `<a href="${href}"${href === current ? ' aria-current="page"' : ""}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round">${icon}</svg>${label}</a>`).join("");
  const note = flash ? `<div class="toast" role="status">${esc(flash)}</div>` : "";
  flash = "";
  app.innerHTML = `
    <div class="topbars">
      <header class="appbar">
        <span class="slot"><span class="avatar">AI</span></span>
        <span class="title">${esc(title)}</span>
        <span class="slot wide">${stop ? "" : '<button data-act="stop" aria-label="全体停止">停止</button>'}</span>
      </header>
      ${stop ? `<div class="halt" role="alert"><span>全体停止中: ${esc(controls.stop_reason)}</span>
        <button data-act="resume">再開</button></div>` : ""}
    </div>
    <main>${note}${body}</main>
    <nav class="tabbar">${tabs}</nav>`;
}

// ---------------------------------------------------------------------------
// data
// ---------------------------------------------------------------------------
async function controls() {
  const rows = await must(sb.from("controls").select("*").limit(1));
  return rows[0] || { global_stop: false, stop_reason: "" };
}
async function businesses() {
  return must(sb.from("businesses").select("*").order("created_at"));
}
async function signed(bucket, paths, opts) {
  const wanted = [...new Set(paths.filter(Boolean))];
  if (!wanted.length) return {};
  const data = await must(sb.storage.from(bucket).createSignedUrls(wanted, 600, opts));
  const out = {};
  for (const r of data) if (r.signedUrl) out[r.path] = r.signedUrl;
  return out;
}
const countWhere = (statuses) => must(sb.from("items")
  .select("id", { count: "exact", head: true }).in("status", statuses));
const thumb = (it, urls) => urls[it.preview_path]
  ? `<img class="thumb" src="${esc(urls[it.preview_path])}" alt="" loading="lazy">`
  : `<span class="thumb ph">${it.kind?.endsWith("article") ? "記事" : "画像なし"}</span>`;

// Article bodies are markdown with the affiliate link as HTML. Nothing in
// them is ever inserted as markup: links and the tracking pixel become plain
// text, then the text is escaped and given headings and paragraphs.
function plainBody(md) {
  return String(md || "")
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<img\b[^>]*>/gi, "")
    .replace(/<a\b[^>]*>([\s\S]*?)<\/a>/gi, "🔗 $1");
}
function articleHtml(md) {
  const blocks = plainBody(md).split(/\n{2,}/).map((b) => b.trim()).filter(Boolean);
  return blocks.map((b) => {
    const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>");
    if (/^###\s/.test(b)) return `<h3>${inline(b.replace(/^###\s+/, ""))}</h3>`;
    if (/^##?\s/.test(b)) return `<h2>${inline(b.replace(/^##?\s+/, ""))}</h2>`;
    if (/^[-*]\s/.test(b)) {
      return `<ul>${b.split("\n").map((l) => `<li>${inline(l.replace(/^[-*]\s+/, ""))}</li>`).join("")}</ul>`;
    }
    if (b === "---") return "<hr>";
    return `<p>${inline(b)}</p>`;
  }).join("");
}
async function copy(text, html) {
  try {
    if (html && window.ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({
        "text/html": new Blob([html], { type: "text/html" }),
        "text/plain": new Blob([text], { type: "text/plain" }) })]);
    } else {
      await navigator.clipboard.writeText(text);
    }
    return true;
  } catch { return false; }
}

// ---------------------------------------------------------------------------
// screens
// ---------------------------------------------------------------------------
async function home() {
  const ctl = await controls();
  const monthStart = tokyoDate().slice(0, 8) + "01";
  const [review, upload, autoPost, today, workers, revenue, lastRun] = await Promise.all([
    countWhere(["pending_review", "blocked", "on_hold"]),
    countWhere(["awaiting_manual"]),
    must(sb.from("items").select("id", { count: "exact", head: true })
      .eq("kind", "affiliate_article").in("status", ["approved", "scheduled"])),
    must(sb.from("items").select("id", { count: "exact", head: true })
      .gte("created_at", tokyoMidnightIso())),
    must(sb.from("workers").select("*").order("last_seen_at", { ascending: false }).limit(1)),
    must(sb.from("revenue_entries").select("amount,kind").eq("is_actual", true)
      .in("kind", ["sale", "reward_confirmed"]).gte("occurred_on", monthStart)),
    must(sb.from("events").select("message,level,created_at").eq("category", "pipeline")
      .order("id", { ascending: false }).limit(4)),
  ]);
  const actual = revenue.reduce((s, r) => s + Number(r.amount), 0);
  const worker = workers[0];
  const todo = [];
  if (review) todo.push(`<a class="card row" href="#/review"><span class="grow"><b>確認待ち ${review}件</b>
    <div class="meta">承認・修正依頼・却下を選んでください</div></span>›</a>`);
  if (upload) todo.push(`<a class="card row" href="#/post"><span class="grow"><b>投稿・提出待ち ${upload}件</b>
    <div class="meta">コピーして貼る・保存して上げるだけ</div></span>›</a>`);
  if (autoPost) todo.push(`<div class="card"><b>はてなブログへ自動投稿待ち ${autoPost}件</b>
    <div class="meta">承認済み。次回の自動実行で、1日の上限まで投稿されます</div></div>`);
  if (!worker) todo.push(`<div class="card"><b>クラウド実行: 未接続</b>
    <div class="meta">自動生成はまだ一度も動いていません（セットアップ待ち）</div></div>`);
  if (!todo.length) todo.push('<p class="empty">今やることはありません</p>');

  shell("AIworker", `
    <section class="hero">
      <div class="k">今月の実績売上（税引前）</div>
      <div class="v">${revenue.length ? yen(actual) : "未入力"}</div>
      <div class="legend">目標 <b>${yen(GOAL_JPY)}</b> ・ 実績のみ（見込み・サンプルは含みません）</div>
      <div class="bar"><i id="goalbar"></i></div>
    </section>
    <div class="section">やること</div>
    ${todo.join("")}
    <div class="section">状況</div>
    <div class="tiles">
      <div class="tile"><div class="k">確認待ち</div><div class="v">${review}</div></div>
      <div class="tile"><div class="k">投稿待ち</div><div class="v">${upload}</div></div>
      <div class="tile"><div class="k">今日の生成</div><div class="v">${today}</div></div>
      <div class="tile"><div class="k">クラウド実行</div><div class="v">${esc(ago(worker?.last_seen_at))}</div></div>
    </div>
    ${lastRun.length ? `<div class="card"><div class="muted">最新の実行（${esc(when(lastRun[0].created_at))}）</div>
      ${lastRun.map((e) => `<div class="meta">${esc(e.message)}</div>`).join("")}</div>` : ""}
  `, { controls: ctl, current: "#/" });
  const bar = document.getElementById("goalbar");
  if (bar) bar.style.width = Math.min(100, (actual / GOAL_JPY) * 100) + "%";
}

async function reviewList() {
  const [ctl, items] = await Promise.all([controls(), must(sb.from("items").select("*")
    .in("status", ["pending_review", "blocked", "on_hold", "needs_revision"])
    .order("created_at", { ascending: false }).limit(60))]);
  const urls = await signed("previews", items.map((i) => i.preview_path));
  const cards = items.map((it) => `
    <a class="card item" href="#/item/${esc(it.id)}">
      ${thumb(it, urls)}
      <span class="grow"><h3>${esc(it.title || "(無題)")}</h3>
        <span class="row wrap">${kindBadge(it)}${badge(it.status)}${sampleBadge(it)}
          ${(it.quality_report?.issues || []).length || it.policy_report?.blocking
            ? '<span class="badge bad">要注意</span>' : ""}</span></span>
    </a>`).join("");
  shell("確認", cards || '<p class="empty">確認待ちはありません</p>',
    { controls: ctl, current: "#/review" });
}

async function itemDetail(id) {
  const [ctl, rows] = await Promise.all([controls(),
    must(sb.from("items").select("*").eq("id", id).limit(1))]);
  const it = rows[0];
  if (!it) return shell("確認", '<p class="empty">見つかりません</p>', { controls: ctl });
  const [urls, videos] = await Promise.all([signed("previews", [it.preview_path]),
    it.kind === "short_video" ? signed("finals", [it.final_path]) : {}]);
  const m = it.meta || {};
  const policy = (it.policy_report?.findings || []);
  const issues = it.quality_report?.issues || [];
  const human = it.quality_report?.needs_human || [];
  const canDecide = ["pending_review", "on_hold", "blocked"].includes(it.status);
  const blocked = it.status === "blocked" || it.blocking;

  let actions = "";
  if (canDecide) {
    actions = `
      <div class="section">判断</div>
      ${blocked ? "" : `<button class="approve lg" data-act="approve">承認する</button>`}
      <div class="card stack">
        <label class="field"><span>修正依頼（AIが次回の実行で作り直します）</span>
          <textarea id="note" placeholder="例: もっと明るく、余白を左に"></textarea></label>
        <button class="brand" data-act="revise">修正を依頼</button>
      </div>
      <div class="two">
        ${it.status === "on_hold" ? '<button data-act="resubmit">確認待ちに戻す</button>'
          : '<button data-act="hold">保留</button>'}
        <button class="reject" data-act="reject">却下</button>
      </div>
      ${blocked ? `<div class="danger-zone stack">
        <div class="meta">チェックで止められています。内容を確認した上で、理由を書けば承認できます。</div>
        <textarea id="why" placeholder="承認する理由（必須）"></textarea>
        <button class="override" data-act="override">理由を書いて承認</button></div>` : ""}`;
  } else if (it.status === "awaiting_manual") {
    actions = '<a class="btn brand" href="#/post">投稿画面へ</a>';
  } else if (it.status === "needs_revision") {
    actions = `<div class="card"><b>AIが修正中</b><div class="meta">次回のクラウド実行で作り直し、
      ここに戻ってきます。依頼内容: ${esc(it.revision_note)}</div></div>`;
  }

  const article = it.kind === "note_article" || it.kind === "affiliate_article";
  let media = "";
  if (it.kind === "short_video" && videos[it.final_path]) {
    media = `<video class="preview" controls playsinline preload="metadata"
      poster="${esc(urls[it.preview_path] || "")}" src="${esc(videos[it.final_path])}"></video>`;
  } else if (urls[it.preview_path]) {
    media = `<img class="preview" src="${esc(urls[it.preview_path])}" alt="プレビュー">`;
  } else if (!article) {
    media = '<div class="card empty">画像・動画はまだありません</div>';
  }
  let details = "";
  if (it.kind === "stock_image") {
    details = `<div class="card">
      <div class="muted">キーワード（${(m.keywords || []).length}）</div>
      <div class="kw">${(m.keywords || []).map((k) => `<span>${esc(k)}</span>`).join("")}</div>
      <details><summary>生成プロンプト・詳細</summary>
        <pre>${esc(m.prompt)}\n\nnegative: ${esc(m.negative_prompt)}\ncategory: ${esc(m.category ?? "—")}\n${esc(m.description)}</pre>
      </details></div>`;
  } else if (it.kind === "short_video") {
    details = `<div class="card"><b>台本</b><ol class="steps">${(m.scenes || []).map((sc) =>
      `<li>${esc(sc.narration)}<div class="muted">字幕: ${esc(sc.caption)}</div></li>`).join("")}</ol>
      <details><summary>概要欄</summary><pre>${esc(m.description)}</pre></details></div>`;
  } else if (article) {
    details = `<div class="card article">${articleHtml(it.body)}</div>
      ${it.kind === "affiliate_article" ? `<div class="muted">案件: ${esc(m.program_name)} ・
        PR表記と広告リンクはシステムが付けています（AIは書いていません）</div>` : ""}
      ${m.price_hint_jpy ? `<div class="muted">有料にするなら目安 ${yen(m.price_hint_jpy)}</div>` : ""}`;
  }

  shell("確認", `
    ${media}
    <div class="card">
      <h3>${esc(it.title || "(無題)")}</h3>
      <div class="row wrap">${kindBadge(it)}${badge(it.status)}${sampleBadge(it)}<span class="muted">v${it.version}</span></div>
      ${it.body && it.kind === "stock_image" ? `<p class="meta">${esc(it.body)}</p>` : ""}
      ${policy.length ? `<ul class="plain bad">${policy.map((f) => `<li>${esc(f.detail)}</li>`).join("")}</ul>` : ""}
      ${issues.length ? `<ul class="plain bad">${issues.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    </div>
    ${human.length ? `<div class="card"><b>目で見て確認</b><ul class="plain">${human.map((x) =>
      `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    ${details}
    ${actions}
    <p class="err" id="err"></p>
  `, { controls: ctl, current: "#/review" });

  const err = document.getElementById("err");
  const run = async (fn, done) => {
    try { await fn(); toast(done); location.hash = "#/review"; }
    catch (e) { err.textContent = say(e); }
  };
  on("approve", () => run(() => must(sb.rpc("approve_item",
    { p_item: it.id, p_expected_version: it.version, p_note: "", p_force: false })),
  "承認しました"));
  on("override", () => run(() => must(sb.rpc("approve_item", { p_item: it.id,
    p_expected_version: it.version, p_note: document.getElementById("why").value, p_force: true })),
  "理由付きで承認しました"));
  const decide = (action, note, done) => run(() => must(sb.rpc("decide_item",
    { p_item: it.id, p_action: action, p_expected_version: it.version, p_note: note })), done);
  on("revise", () => decide("revise", document.getElementById("note").value, "修正を依頼しました"));
  on("hold", () => decide("hold", "", "保留にしました"));
  on("resubmit", () => decide("resubmit", "", "確認待ちに戻しました"));
  on("reject", () => decide("reject", "", "却下しました"));
}

async function postScreen() {
  const [ctl, items, autoPost, posted] = await Promise.all([controls(),
    must(sb.from("items").select("*").eq("status", "awaiting_manual").order("created_at").limit(200)),
    must(sb.from("items").select("*").eq("kind", "affiliate_article")
      .in("status", ["approved", "scheduled"]).order("created_at").limit(50)),
    must(sb.from("items").select("id,kind,title,external_url,published_at").eq("status", "published")
      .neq("external_url", "").order("published_at", { ascending: false }).limit(5))]);
  const stock = items.filter((i) => i.kind === "stock_image");
  const notes = items.filter((i) => i.kind === "note_article" || i.kind === "affiliate_article");
  const shorts = items.filter((i) => i.kind === "short_video");
  const [urls, videos] = await Promise.all([
    signed("previews", items.map((i) => i.preview_path)),
    signed("finals", shorts.map((i) => i.final_path), { download: true })]);

  const posted_ = (it, placeholder) => `
    <div class="pick"><input type="url" id="url-${esc(it.id)}" placeholder="${placeholder}" inputmode="url">
      <button class="approve" data-item-act="posted" data-id="${esc(it.id)}">投稿した</button></div>`;
  const sections = [];
  if (stock.length) {
    sections.push(`<div class="section">Adobe Stock（${stock.length}件）</div>
    <div class="card">
      <b>手順（まとめて週1回・約5分）</b>
      <ol class="steps">
        <li>下の「ZIPをダウンロード」（画像とCSVが入っています）</li>
        <li><a class="link" href="https://contributor.stock.adobe.com/" target="_blank" rel="noopener">Adobe Stock Contributor</a> で画像をまとめてアップロード</li>
        <li>「CSVをアップロード」で ZIP 内の <b>adobe_stock.csv</b> を選ぶ（タイトル・キーワードが一括で入ります）</li>
        <li>「生成AIを使用」と「人物・財産は架空」にチェックし、各画像を目で確認して提出</li>
        <li>ここに戻って「提出した」を押す</li>
      </ol>
    </div>
    ${stock.map((it) => `
    <label class="card pick">
      <input type="checkbox" data-id="${esc(it.id)}" checked>
      ${thumb(it, urls)}
      <span class="grow"><b>${esc(it.title)}</b>
        <div class="muted">${esc(fileName(it))}・キーワード${(it.meta?.keywords || []).length}個</div></span>
    </label>`).join("")}
    <div class="stack">
      <button class="brand lg" data-act="zip">選んだ画像をZIPでダウンロード</button>
      <button class="approve" data-act="done">選んだ分を「提出した」にする</button></div>`);
  }
  if (notes.length) {
    sections.push(`<div class="section">note（${notes.length}件）</div>
    <div class="card"><ol class="steps"><li>「本文をコピー」→ noteの新規作成に貼り付け（見出しも付いたまま）</li>
      <li>タイトルを貼って公開 → 記事のURLをここに貼って「投稿した」</li></ol></div>
    ${notes.map((it) => `<div class="card stack">
      <b>${esc(it.title)}</b>
      ${it.meta?.price_hint_jpy ? `<div class="muted">有料にするなら目安 ${yen(it.meta.price_hint_jpy)}</div>` : ""}
      <div class="three">
        <button data-item-act="copy-title" data-id="${esc(it.id)}">タイトルをコピー</button>
        <button class="brand" data-item-act="copy-body" data-id="${esc(it.id)}">本文をコピー</button>
        <a class="btn" href="https://note.com/notes/new" target="_blank" rel="noopener">noteを開く</a>
      </div>
      ${posted_(it, "https://note.com/…")}</div>`).join("")}`);
  }
  if (shorts.length) {
    sections.push(`<div class="section">YouTube ショート（${shorts.length}件）</div>
    <div class="card"><ol class="steps"><li>「動画を保存」→ YouTubeアプリの「＋」からショートとして投稿</li>
      <li>タイトルと説明文はコピーして貼る → 動画のURLをここに貼って「投稿した」</li></ol></div>
    ${shorts.map((it) => `<div class="card stack">
      <div class="pick">${thumb(it, urls)}<b class="grow">${esc(it.title)}</b></div>
      <div class="three">
        ${videos[it.final_path] ? `<a class="btn brand" href="${esc(videos[it.final_path])}" download="${esc(fileName(it, "mp4"))}">動画を保存</a>`
          : '<span class="muted">動画なし</span>'}
        <button data-item-act="copy-title" data-id="${esc(it.id)}">タイトルをコピー</button>
        <button data-item-act="copy-desc" data-id="${esc(it.id)}">説明文をコピー</button>
      </div>
      ${posted_(it, "https://youtube.com/shorts/…")}</div>`).join("")}`);
  }
  if (autoPost.length) {
    sections.push(`<div class="section">はてなブログ（自動投稿）</div>
    <div class="card"><b>承認済み ${autoPost.length}件</b><div class="meta">次回の自動実行で、1日の上限まで投稿されます。
      未接続のときは「履歴」に表示されます。</div>
      <ul class="plain">${autoPost.map((it) => `<li>${esc(it.title)}（${esc(STATUS[it.status]?.[0] || it.status)}）</li>`).join("")}</ul></div>`);
  }
  if (posted.length) {
    sections.push(`<div class="section">最近公開したもの</div><div class="card"><ul class="plain">${posted.map((it) =>
      `<li><a class="link" href="${esc(it.external_url)}" target="_blank" rel="noopener">${esc(it.title)}</a>
        <span class="muted">${esc(KIND[it.kind] || "")}</span></li>`).join("")}</ul></div>`);
  }
  shell("投稿・提出", (sections.join("") || '<p class="empty">投稿・提出待ちはありません</p>') +
    '<p class="err" id="err"></p><p class="ok" id="ok"></p>', { controls: ctl, current: "#/post" });

  const err = document.getElementById("err");
  const ok = document.getElementById("ok");
  const byId = (id) => items.find((i) => i.id === id);
  app.querySelectorAll("[data-item-act]").forEach((btn) => btn.addEventListener("click", async () => {
    const it = byId(btn.dataset.id);
    err.textContent = ""; ok.textContent = "";
    const act = btn.dataset.itemAct;
    if (act === "copy-title") ok.textContent = (await copy(it.title)) ? "タイトルをコピーしました" : "コピーできませんでした";
    if (act === "copy-body") {
      ok.textContent = (await copy(plainBody(it.body), articleHtml(it.body)))
        ? "本文をコピーしました（見出し付き）" : "コピーできませんでした";
    }
    if (act === "copy-desc") ok.textContent = (await copy(it.meta?.description || "")) ? "説明文をコピーしました" : "コピーできませんでした";
    if (act === "posted") {
      const url = document.getElementById(`url-${it.id}`).value.trim();
      if (!/^https:\/\/\S+$/.test(url)) { err.textContent = "公開したページのURL（https://…）を貼ってください"; return; }
      btn.disabled = true;
      try {
        await must(sb.rpc("mark_manual_done", { p_item: it.id, p_evidence: url }));
        toast("公開済みにしました"); route();
      } catch (e) { err.textContent = say(e); btn.disabled = false; }
    }
  }));

  const picked = () => [...app.querySelectorAll("input[data-id]:checked")]
    .map((el) => byId(el.dataset.id)).filter((i) => i && i.kind === "stock_image");
  on("zip", async (btn) => {
    const chosen = picked();
    if (!chosen.length) { err.textContent = "1件以上選んでください"; return; }
    btn.disabled = true; err.textContent = ""; ok.textContent = "準備中…";
    try {
      const finals = await signed("finals", chosen.map((i) => i.final_path));
      const files = [];
      for (const it of chosen) {
        const url = finals[it.final_path];
        if (!url) throw new Error(`元画像が見つかりません: ${fileName(it)}`);
        const res = await fetch(url);
        if (!res.ok) throw new Error(`ダウンロード失敗: ${fileName(it)}`);
        files.push({ name: fileName(it), data: new Uint8Array(await res.arrayBuffer()) });
      }
      files.push({ name: "adobe_stock.csv", data: new TextEncoder().encode(adobeCsv(chosen)) });
      const blob = new Blob([zip(files)], { type: "application/zip" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `aiworker-${tokyoDate()}.zip`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 60000);
      ok.textContent = `${chosen.length}件をZIPにしました`;
    } catch (e) { err.textContent = say(e); ok.textContent = ""; }
    btn.disabled = false;
  });
  on("done", async (btn) => {
    const chosen = picked();
    if (!chosen.length) { err.textContent = "1件以上選んでください"; return; }
    if (!confirm(`${chosen.length}件を「Adobe Stockに提出済み」にします。よろしいですか？`)) return;
    btn.disabled = true;
    try {
      for (const it of chosen) {
        await must(sb.rpc("mark_manual_done", { p_item: it.id,
          p_evidence: `Adobe Stock 提出（CSV一括） ${tokyoDate()} ${fileName(it)}` }));
      }
      toast(`${chosen.length}件を提出済みにしました`);
      route();
    } catch (e) { err.textContent = say(e); btn.disabled = false; }
  });
}

let currentBiz = "";

// A8 gives an HTML snippet (link + 1x1 pixel). Parse it inertly and keep the
// two URLs only; anything else in the paste is dropped.
function parseAd(text) {
  const raw = String(text || "").trim();
  if (/^https:\/\/\S+$/.test(raw)) return { link: raw, pixel: "" };
  const doc = new DOMParser().parseFromString(raw, "text/html");
  const link = doc.querySelector("a[href]")?.getAttribute("href") || "";
  const pixel = doc.querySelector("img[src]")?.getAttribute("src") || "";
  const okUrl = (u) => /^https:\/\/[^\s"<>]+$/.test(u);
  return { link: okUrl(link) ? link : "", pixel: okUrl(pixel) ? pixel : "" };
}
const isA8 = (u) => { try { return /(^|\.)a8\.net$/.test(new URL(u).hostname); } catch { return false; } };

async function bizScreen() {
  const [ctl, list] = await Promise.all([controls(), businesses()]);
  if (!list.length) {
    return shell("事業", `<div class="card"><b>まだ事業がありません</b><div class="meta">
      初回のクラウド実行で、画像販売・note・A8・ショートが自動で作られます。</div></div>`,
    { controls: ctl, current: "#/biz" });
  }
  const biz = list.find((b) => b.code === currentBiz) || list[0];
  currentBiz = biz.code;
  const [accounts, revenue, programs] = await Promise.all([
    must(sb.from("accounts").select("*").eq("business_id", biz.id).order("created_at")),
    must(sb.from("revenue_entries").select("*").order("occurred_on", { ascending: false }).limit(10)),
    biz.code === "affiliate"
      ? must(sb.from("affiliate_programs").select("*").order("created_at")) : []]);
  const acc = accounts[0];
  const dest = BUSINESSES[biz.code]?.dest || acc?.platform || "";
  const themes = (biz.settings?.themes || []).join("\n");
  const chips = list.map((b) => `<button class="chip" data-biz="${esc(b.code)}"${b.code === biz.code
    ? ' aria-current="true"' : ""}>${esc(BUSINESSES[b.code]?.label || b.name)}</button>`).join("");

  const programBlock = biz.code !== "affiliate" ? "" : `
    <div class="section">提携している案件（A8）</div>
    ${programs.map((pr) => `<div class="card stack">
      <div class="row"><b class="grow">${esc(pr.name)}</b>
        <span class="badge ${pr.status === "active" ? "good" : "flat"}">${pr.status === "active" ? "使用中" : "停止中"}</span></div>
      <div class="muted">${esc((pr.facts || "（提供情報なし）").slice(0, 80))}</div>
      <div class="two"><button data-prog="${esc(pr.id)}" data-to="${pr.status === "active" ? "paused" : "active"}">
        ${pr.status === "active" ? "一時停止" : "再開"}</button>
        <button class="reject" data-prog-del="${esc(pr.id)}">削除</button></div></div>`).join("")
      || '<div class="card muted">まだありません。A8で提携が承認された案件を登録すると、紹介記事を作り始めます。</div>'}
    <div class="card stack">
      <b>案件を登録</b>
      <label class="field"><span>案件名</span><input type="text" id="pname" maxlength="120"></label>
      <label class="field"><span>A8の広告リンク（「広告リンク作成」でコピーしたコード、またはURL）</span>
        <textarea id="pad" placeholder="&lt;a href=&quot;https://px.a8.net/…&quot;&gt;…"></textarea></label>
      <label class="field"><span>提供情報（公式ページの説明・料金・特典など）</span>
        <textarea id="pfacts" maxlength="4000" placeholder="記事はここに書かれた事実だけを使います"></textarea></label>
      <div class="muted">ここに無いこと（価格・効果・口コミ）はAIが書かないよう指示しています。</div>
      <button class="brand" data-act="add-prog">登録する</button>
    </div>`;

  shell("事業", `
    <div class="chips">${chips}</div>
    <div class="card">
      <h3>${esc(biz.name)}</h3>
      <div class="row wrap"><span class="badge flat">${esc(STAGE[biz.stage] || biz.stage)}</span>
        <span class="muted">投稿先: ${esc(dest)}</span></div>
    </div>
    ${acc ? `<div class="section">${esc(dest)} の設定</div>
    <div class="card stack">
      <label class="field"><span>承認のしかた</span>
        <select id="policy">
          <option value="auto"${acc.approval_policy === "auto" ? " selected" : ""}>自動（チェックを通ったものは投稿待ちへ）</option>
          <option value="required"${acc.approval_policy === "required" ? " selected" : ""}>毎回わたしが確認する</option>
        </select></label>
      <div class="muted">どちらでも、注意が付いたもの・修正依頼・サンプルは必ずあなたの確認に回ります。</div>
      <label class="field"><span>1日の上限（作る候補・自動投稿の数、0〜50）</span>
        <input type="number" id="limit" min="0" max="50" value="${Number(acc.daily_limit)}"></label>
      <label class="field"><span>状態</span>
        <select id="status">
          <option value="active"${acc.status === "active" ? " selected" : ""}>稼働</option>
          <option value="paused"${acc.status === "paused" ? " selected" : ""}>一時停止</option>
        </select></label>
      <div class="muted">規約確認: ${esc({ verified: "確認済み", conditional: "条件付きで可", unverified: "未確認",
        forbidden: "不可" }[acc.terms_status] || acc.terms_status)}${acc.terms_source_url
        ? ` ・ <a class="link" href="${esc(acc.terms_source_url)}" target="_blank" rel="noopener">出典</a>` : ""}</div>
      <button class="brand" data-act="save-acc">保存</button>
    </div>` : ""}
    ${programBlock}
    ${biz.code === "affiliate" ? "" : `<div class="section">テーマ（1行に1つ）</div>
    <div class="card stack">
      <textarea id="themes" placeholder="空なら標準のテーマを使います">${esc(themes)}</textarea>
      ${biz.code === "youtube_shorts" ? `<label class="field"><span>概要欄に載せるリンク（ブログやnoteのURL、任意）</span>
        <input type="url" id="dlink" value="${esc(biz.settings?.description_link || "")}"></label>` : ""}
      <button data-act="save-themes">テーマを保存</button>
    </div>`}
    <div class="section">売上を記録（実績のみ）</div>
    <div class="card stack">
      <div class="two">
        <label class="field"><span>日付</span><input type="date" id="rdate" value="${tokyoDate()}"></label>
        <label class="field"><span>金額（円・税引前）</span><input type="number" id="ramount" min="0" step="1"></label>
      </div>
      <label class="field"><span>種類</span><select id="rkind">
        <option value="sale">売上・確定報酬</option><option value="reward_pending">未確定の報酬</option>
        <option value="payout">入金</option><option value="cost">経費</option></select></label>
      <div class="muted">この事業（${esc(biz.name)}）の分として記録します</div>
      <button data-act="add-rev">記録する</button>
      ${revenue.length ? `<ul class="plain">${revenue.map((r) => `<li>${esc(r.occurred_on)} ${esc(
        { sale: "売上", reward_confirmed: "確定報酬", reward_pending: "未確定", payout: "入金",
          fee: "手数料", refund: "返金", cost: "経費" }[r.kind] || r.kind)} ${yen(r.amount)}</li>`).join("")}</ul>`
        : '<div class="muted">まだ記録はありません</div>'}
    </div>
    <p class="err" id="err"></p>
  `, { controls: ctl, current: "#/biz" });

  app.querySelectorAll("[data-biz]").forEach((b) => b.addEventListener("click", () => {
    currentBiz = b.dataset.biz; route();
  }));
  const err = document.getElementById("err");
  const save = async (fn, done) => {
    try { await fn(); toast(done); route(); } catch (e) { err.textContent = say(e); }
  };
  on("save-acc", () => save(() => must(sb.from("accounts").update({
    approval_policy: document.getElementById("policy").value,
    daily_limit: Math.max(0, Math.min(50, parseInt(document.getElementById("limit").value, 10) || 0)),
    status: document.getElementById("status").value,
  }).eq("id", acc.id)), "保存しました（変更履歴に記録）"));
  on("save-themes", () => save(() => {
    const settings = { ...(biz.settings || {}),
      themes: document.getElementById("themes").value.split("\n").map((t) => t.trim()).filter(Boolean) };
    const link = document.getElementById("dlink");
    if (link) {
      if (link.value && !/^https:\/\/\S+$/.test(link.value.trim())) throw new Error("リンクは https:// で始まるURLにしてください");
      settings.description_link = link.value.trim();
    }
    return must(sb.from("businesses").update({ settings }).eq("id", biz.id));
  }, "保存しました"));
  on("add-rev", () => save(async () => {
    const amount = Number(document.getElementById("ramount").value);
    if (!(amount > 0)) throw new Error("金額を入れてください");
    const { data: { user } } = await sb.auth.getUser();
    await must(sb.from("revenue_entries").insert({ owner_id: user.id, business_id: biz.id,
      occurred_on: document.getElementById("rdate").value, amount,
      kind: document.getElementById("rkind").value, is_actual: true, source: "manual" }));
  }, "記録しました"));
  on("add-prog", () => save(async () => {
    const name = document.getElementById("pname").value.trim();
    const ad = parseAd(document.getElementById("pad").value);
    if (!name) throw new Error("案件名を入れてください");
    if (!ad.link || !isA8(ad.link)) throw new Error("A8の広告リンク（px.a8.net のURL）が見つかりません");
    const { data: { user } } = await sb.auth.getUser();
    await must(sb.from("affiliate_programs").insert({ owner_id: user.id, asp: "a8", name,
      link_url: ad.link, pixel_url: isA8(ad.pixel) ? ad.pixel : "",
      facts: document.getElementById("pfacts").value.trim(), media: ["blog"] }));
  }, "案件を登録しました"));
  app.querySelectorAll("[data-prog]").forEach((b) => b.addEventListener("click", () => save(() =>
    must(sb.from("affiliate_programs").update({ status: b.dataset.to }).eq("id", b.dataset.prog)),
  "変更しました")));
  app.querySelectorAll("[data-prog-del]").forEach((b) => b.addEventListener("click", () => {
    if (!confirm("この案件を削除します。よろしいですか？")) return;
    save(() => must(sb.from("affiliate_programs").delete().eq("id", b.dataset.progDel)), "削除しました");
  }));
}

async function logScreen() {
  const [ctl, events] = await Promise.all([controls(),
    must(sb.from("events").select("*").order("id", { ascending: false }).limit(60))]);
  const tone = { warning: "warn", error: "bad", critical: "bad" };
  const rows = events.map((e) => `<div class="card"><div class="row">
      <span class="badge ${tone[e.level] || "flat"}">${esc(e.category)}</span>
      <span class="muted grow">${esc(when(e.created_at))}</span></div>
      <div class="meta">${esc(e.message)}</div></div>`).join("");
  shell("履歴", rows || '<p class="empty">まだ記録はありません</p>', { controls: ctl, current: "#/log" });
}

async function stopScreen() {
  const ctl = await controls();
  shell("全体停止", `
    <div class="card stack">
      <div class="meta">すべての自動処理（生成・自動承認）を止めます。止めている間もアプリでの確認はできます。</div>
      <textarea id="why" placeholder="止める理由（必須）"></textarea>
      <button class="reject lg" data-act="do-stop">すべて止める</button>
      <a class="btn ghost" href="#/">やめる</a>
    </div><p class="err" id="err"></p>`, { controls: ctl });
  on("do-stop", async () => {
    try {
      await must(sb.rpc("set_global_stop", { p_stop: true, p_reason: document.getElementById("why").value }));
      toast("全体停止しました"); location.hash = "#/";
    } catch (e) { document.getElementById("err").textContent = say(e); }
  });
}

// ---------------------------------------------------------------------------
// sign-in and states with nothing to show
// ---------------------------------------------------------------------------
function notConfigured() {
  app.innerHTML = `<main class="login"><h1>AIworker</h1>
    <div class="card"><b>未接続</b><div class="meta">データベース（Supabase）の接続設定がまだです。
    セットアップが終わるとここにログイン画面が出ます。</div></div></main>`;
}

function loginScreen(message = "") {
  app.innerHTML = `<main class="login">
    <h1>AIworker</h1><p class="muted">ログインしてください</p>
    <form class="card stack" id="login">
      <label class="field"><span>メールアドレス</span><input type="email" id="email" autocomplete="username" required></label>
      <label class="field"><span>パスワード</span><input type="password" id="password" autocomplete="current-password" required></label>
      <button class="brand lg" type="submit">ログイン</button>
      <p class="err" id="err">${esc(message)}</p>
    </form></main>`;
  document.getElementById("login").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const pw = document.getElementById("password").value;
    const { error } = await sb.auth.signInWithPassword({
      email: document.getElementById("email").value.trim(), password: pw });
    if (error) document.getElementById("err").textContent = say(error);
    else route();
  });
}

// ---------------------------------------------------------------------------
// zip + Adobe CSV, built in the browser so the files never pass through anything else
// ---------------------------------------------------------------------------
function fileName(it, ext = "jpg") { return `aiw-${String(it.id).slice(0, 8)}.${ext}`; }

function adobeCsv(items) {
  const q = (s) => `"${String(s ?? "").replace(/"/g, '""')}"`;
  const lines = ["Filename,Title,Keywords,Category,Releases"];
  for (const it of items) {
    const m = it.meta || {};
    lines.push([q(fileName(it)), q(it.title), q((m.keywords || []).join(", ")),
      m.category ?? "", ""].join(","));
  }
  return lines.join("\r\n") + "\r\n";
}

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(bytes) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}

// Stored (uncompressed) ZIP: JPEGs do not compress, and no library is needed.
export function zip(files) {
  const parts = [];
  const central = [];
  let offset = 0;
  for (const f of files) {
    const name = new TextEncoder().encode(f.name);
    const crc = crc32(f.data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true); local.setUint16(4, 20, true);
    local.setUint16(6, 0x0800, true); // UTF-8 names
    local.setUint32(14, crc, true); local.setUint32(18, f.data.length, true);
    local.setUint32(22, f.data.length, true); local.setUint16(26, name.length, true);
    parts.push(new Uint8Array(local.buffer), name, f.data);
    const cd = new DataView(new ArrayBuffer(46));
    cd.setUint32(0, 0x02014b50, true); cd.setUint16(4, 20, true); cd.setUint16(6, 20, true);
    cd.setUint16(8, 0x0800, true); cd.setUint32(16, crc, true);
    cd.setUint32(20, f.data.length, true); cd.setUint32(24, f.data.length, true);
    cd.setUint16(28, name.length, true); cd.setUint32(42, offset, true);
    central.push(new Uint8Array(cd.buffer), name);
    offset += 30 + name.length + f.data.length;
  }
  const size = central.reduce((s, p) => s + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true); end.setUint32(12, size, true);
  end.setUint32(16, offset, true);
  return new Blob([...parts, ...central, new Uint8Array(end.buffer)]);
}
window.__aiworker = { zip, adobeCsv, crc32 };

// ---------------------------------------------------------------------------
// routing
// ---------------------------------------------------------------------------
function on(act, fn) {
  const el = app.querySelector(`[data-act="${act}"]`);
  if (el) el.addEventListener("click", () => fn(el));
}

app.addEventListener("click", async (ev) => {
  const act = ev.target.closest("[data-act]")?.dataset.act;
  if (act === "stop") location.hash = "#/stop";
  if (act === "resume") {
    if (!confirm("自動処理を再開します。よろしいですか？")) return;
    try { await must(sb.rpc("set_global_stop", { p_stop: false, p_reason: "" })); toast("再開しました"); route(); }
    catch (e) { alert(say(e)); }
  }
});

async function route() {
  if (!sb) return notConfigured();
  const { data: { session } } = await sb.auth.getSession();
  if (!session) return loginScreen();
  try {
    const owner = await must(sb.rpc("is_owner"));
    if (!owner) {
      await sb.auth.signOut();
      return loginScreen("このアカウントには利用権限がありません");
    }
    const h = location.hash || "#/";
    if (h.startsWith("#/item/")) return await itemDetail(decodeURIComponent(h.slice(7)));
    const screens = { "#/": home, "#/review": reviewList, "#/post": postScreen,
      "#/upload": postScreen, "#/biz": bizScreen, "#/log": logScreen, "#/stop": stopScreen };
    await (screens[h] || home)();
    window.scrollTo(0, 0);
  } catch (e) {
    app.innerHTML = `<main><div class="card"><b>読み込めませんでした</b>
      <div class="meta">${esc(say(e))}</div><button data-retry>もう一度</button></div></main>`;
    app.querySelector("[data-retry]").addEventListener("click", route);
  }
}

window.addEventListener("hashchange", route);
if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}
route();
