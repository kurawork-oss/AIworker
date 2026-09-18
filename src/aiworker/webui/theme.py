"""The UI's design tokens and stylesheet.

A phone-shaped operations app: a coloured app bar, a scrolling card list, and a
fixed bottom tab bar, centred in a phone-width column when opened on a desktop.

Colour roles come from a validated palette rather than taste:

* **status** (good / warning / serious / critical) is reserved for item state and
  is always paired with a label, never carried by colour alone.
* **series** blue is for data marks only, so a chart line can never be mistaken
  for a status.
* Every foreground/background pair below was measured, not eyeballed: white on
  the app bar is 6.6:1, on the approve button 5.5:1, on the reject button 6.0:1.

Dark mode is a *selected* set of steps against the dark surface, not an
automatic inversion, and is declared under both the OS media query and the
explicit `data-theme` scope.
"""

from __future__ import annotations

TOKENS = """
:root{
  color-scheme: light;
  --brand:#1c5cab; --brand-ink:#ffffff; --brand-deep:#184f95;
  --page:#f2f2f0; --card:#ffffff; --raise:#f7f7f5;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --line:#e1e0d9; --ring:rgba(11,11,11,.10);
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --approve:#0f7a35; --reject:#b8322c;
  --series-1:#2a78d6; --series-fill:rgba(42,120,214,.16);
  --grid:#e1e0d9; --axis:#c3c2b7;
  --tab-h:60px;
}
@media (prefers-color-scheme: dark){
  :root:where(:not([data-theme="light"])){
    color-scheme: dark;
    --brand:#173f80; --brand-ink:#ffffff; --brand-deep:#12325f;
    --page:#0d0d0d; --card:#1a1a19; --raise:#232321;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --line:#2c2c2a; --ring:rgba(255,255,255,.10);
    --approve:#15803d; --reject:#c0392b;
    --series-1:#3987e5; --series-fill:rgba(57,135,229,.22);
    --grid:#2c2c2a; --axis:#383835;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --brand:#173f80; --brand-ink:#ffffff; --brand-deep:#12325f;
  --page:#0d0d0d; --card:#1a1a19; --raise:#232321;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --line:#2c2c2a; --ring:rgba(255,255,255,.10);
  --approve:#15803d; --reject:#c0392b;
  --series-1:#3987e5; --series-fill:rgba(57,135,229,.22);
  --grid:#2c2c2a; --axis:#383835;
}
"""

BASE = """
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0}
body{
  background:var(--page);color:var(--ink);
  font:15px/1.6 system-ui,-apple-system,"Segoe UI","Hiragino Sans","Noto Sans JP",sans-serif;
  padding-bottom:calc(var(--tab-h) + env(safe-area-inset-bottom,0px));
}
.phone{max-width:520px;margin:0 auto;background:var(--page);min-height:100vh;
  position:relative;box-shadow:0 0 0 1px var(--ring);overflow-x:hidden}
a{color:inherit;text-decoration:none}

/* ---- app bar ---------------------------------------------------------- */
/* The app bar and the view switcher stick as ONE block. Sticking them
   separately at top:0 makes the second slide under the first on scroll, and
   the switcher -- the only way between the five views -- silently vanishes. */
.topbars{position:sticky;top:0;z-index:20}
.appbar{background:var(--brand);color:var(--brand-ink);
  padding:calc(10px + env(safe-area-inset-top,0px)) 16px 10px;
  display:flex;align-items:center;gap:10px}
.appbar .title{font-weight:700;font-size:17px;flex:1;text-align:center}
.appbar .slot{width:30px;height:30px;display:grid;place-items:center;flex:none}
.avatar{width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,.22);
  display:grid;place-items:center;font-size:12px;font-weight:700}

/* ---- view switcher ---------------------------------------------------- */
.switch{background:var(--brand-deep);
  display:flex;overflow-x:auto;scrollbar-width:none}
.switch::-webkit-scrollbar{display:none}
.switch a{flex:1 0 auto;text-align:center;padding:9px 14px;font-size:13px;
  color:rgba(255,255,255,.72);white-space:nowrap;border-bottom:3px solid transparent}
.switch a[aria-current]{color:#fff;font-weight:700;border-bottom-color:#fff}

/* ---- layout ----------------------------------------------------------- */
main{padding:12px 14px 20px}
.section{font-size:12px;font-weight:700;letter-spacing:.06em;color:var(--muted);
  margin:16px 2px 8px;text-transform:none}
.section:first-child{margin-top:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:14px;margin-bottom:10px}
.card.flat{padding:0;overflow:hidden}
.row{display:flex;gap:10px;align-items:center}
.row.wrap{flex-wrap:wrap}
.grow{flex:1;min-width:0}
/* A long unbroken string (a file path, a URL in a draft) must wrap rather than
   widen the page: a phone layout with a horizontal scrollbar is broken. */
.meta{color:var(--ink-2);font-size:13px;overflow-wrap:anywhere;min-width:0}
.muted{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.empty{text-align:center;color:var(--muted);padding:34px 10px}

/* ---- item card -------------------------------------------------------- */
.item{display:flex;gap:11px;align-items:flex-start}
.thumb{width:36px;height:36px;border-radius:9px;flex:none;display:grid;place-items:center;
  background:var(--raise);border:1px solid var(--line)}
.item h3{margin:0 0 2px;font-size:15px;line-height:1.4;font-weight:700}
.body-preview{margin:8px 0 0;font-size:13px;color:var(--ink-2);
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.actions{display:flex;gap:8px;margin-top:12px;padding-top:11px;border-top:1px solid var(--line)}
.actions > *{flex:1}

/* ---- chips & badges --------------------------------------------------- */
.chips{display:flex;gap:7px;overflow-x:auto;padding:2px 0 10px;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;padding:6px 13px;border-radius:99px;border:1px solid var(--line);
  background:var(--card);font-size:13px;color:var(--ink-2)}
.chip[aria-current]{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:700}
.chip .n{opacity:.75;margin-left:5px;font-variant-numeric:tabular-nums}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:99px;
  font-size:11px;font-weight:700;border:1px solid var(--line);color:var(--ink-2)}
.badge::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.badge.good{color:var(--good);border-color:var(--good)}
.badge.warn{color:var(--serious);border-color:var(--serious)}
.badge.bad{color:var(--critical);border-color:var(--critical)}
.badge.flat::before{display:none}

/* ---- buttons ---------------------------------------------------------- */
button,.btn{font:inherit;font-size:14px;font-weight:700;padding:10px 12px;border-radius:10px;
  border:1px solid var(--line);background:var(--card);color:var(--ink);
  cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:6px;
  min-height:42px;width:100%}
.btn.approve,button.approve{background:var(--approve);border-color:var(--approve);color:#fff}
.btn.reject,button.reject{background:transparent;border-color:var(--reject);color:var(--reject)}
.btn.ghost{background:transparent;color:var(--ink-2)}
.btn.brand,button.brand{background:var(--brand);border-color:var(--brand);color:#fff}
/* The override is deliberately the least prominent control on a blocked item. */
button.override{border-color:var(--critical);color:var(--critical);background:transparent;
  font-size:13px;font-weight:600;min-height:38px}
button.lg{min-height:56px;font-size:15px;border-radius:14px}

/* ---- forms ------------------------------------------------------------ */
label.field{display:block;margin-bottom:8px}
label.field > span{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
input[type=text]{font:inherit;width:100%;padding:11px 12px;border-radius:10px;
  border:1px solid var(--line);background:var(--raise);color:var(--ink);min-height:44px}
.action-block{border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:10px}
.danger-zone{border:1px dashed var(--critical);border-radius:12px;padding:12px;margin-top:14px}
.danger-zone .meta{color:var(--critical)}
.notice{border-left:3px solid var(--warning);padding-left:10px;margin-bottom:12px}

/* ---- stat tiles & hero ------------------------------------------------ */
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px}
.tile .k{font-size:12px;color:var(--muted)}
.tile .v{font-size:24px;font-weight:700;margin-top:3px;line-height:1.2}
.hero{background:var(--brand);color:#fff;border-radius:14px;padding:16px;margin-bottom:10px}
.hero .k{font-size:12px;opacity:.85;letter-spacing:.06em;font-weight:700}
.hero .v{font-size:46px;font-weight:700;line-height:1.05;margin:4px 0 2px}
.hero .legend{font-size:12px;opacity:.9}
.hero .legend b{font-weight:700}
.hero .cta{display:flex;gap:8px;margin-top:12px}
.hero .cta .btn{background:rgba(255,255,255,.16);border-color:rgba(255,255,255,.28);color:#fff}
.hero .cta .btn.solid{background:#fff;color:var(--brand)}

/* ---- charts ----------------------------------------------------------- */
figure{margin:0}
figcaption{font-size:13px;font-weight:700;margin-bottom:2px}
.figsub{font-size:12px;color:var(--muted);margin-bottom:10px}
svg.chart{display:block;width:100%;height:auto;overflow:visible}
.barrow{display:grid;grid-template-columns:92px 1fr auto;gap:9px;align-items:center;
  margin-bottom:7px;font-size:13px}
.barrow > *{min-width:0}
.barrow .track{background:var(--raise);border-radius:4px;height:14px;overflow:hidden}
.barrow .fill{background:var(--series-1);height:100%;border-radius:0 4px 4px 0}
.barrow .val{font-variant-numeric:tabular-nums;color:var(--ink-2);font-size:12px}

/* ---- category grid (UI5) ---------------------------------------------- */
.catgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}
.cat h4{margin:0 0 6px;font-size:13px}
.cat .n{font-size:20px;font-weight:700}

/* ---- flow view (UI2) -------------------------------------------------- */
.flow{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;align-items:start}
.flowcol{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 8px}
.flowcol.human{background:color-mix(in srgb,var(--critical) 7%,var(--card))}
.flowcol h4{margin:0 0 8px;font-size:11px;text-align:center;color:var(--muted);
  letter-spacing:.04em}
.step{background:var(--raise);border:1px solid var(--line);border-radius:9px;
  padding:8px 7px;font-size:12px;text-align:center;margin-bottom:6px}
.step .n{display:block;font-size:19px;font-weight:700;line-height:1.2}
.step.on{background:color-mix(in srgb,var(--series-1) 14%,var(--card));
  border-color:var(--series-1)}
.step.act{background:color-mix(in srgb,var(--approve) 14%,var(--card));
  border-color:var(--approve)}
.arrow{text-align:center;color:var(--muted);font-size:13px;line-height:1;margin:-2px 0 4px}

/* ---- swipe view (UI3) ------------------------------------------------- */
.deck{position:relative}
.counter{text-align:center;font-size:12px;color:var(--muted);margin-bottom:8px;
  font-variant-numeric:tabular-nums}
.swipe-actions{display:grid;grid-template-columns:1fr 1fr 1fr;gap:9px;margin-top:12px}
.swipe-actions form{display:contents}
.dots{display:flex;gap:5px;justify-content:center;margin-top:12px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--line)}
.dot.on{background:var(--brand);width:18px;border-radius:99px}

/* ---- chat view -------------------------------------------------------- */
.msg{display:flex;gap:9px;margin-bottom:14px;align-items:flex-start}
.msg .who{width:30px;height:30px;border-radius:50%;flex:none;display:grid;place-items:center;
  background:var(--brand);color:#fff;font-size:12px;font-weight:700}
.bubble{background:var(--card);border:1px solid var(--line);border-radius:4px 14px 14px 14px;
  padding:12px 13px;max-width:82%}
.msg.me{flex-direction:row-reverse}
.msg.me .bubble{background:var(--brand);color:#fff;border-radius:14px 4px 14px 14px;
  border-color:var(--brand)}
.msg.me .who{background:var(--raise);color:var(--ink-2)}
.inline-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}
.inline-actions .btn{width:auto;flex:none;font-size:13px;min-height:36px;padding:7px 12px}
.daysep{text-align:center;font-size:11px;color:var(--muted);margin:6px 0 14px}

/* ---- bottom tab bar --------------------------------------------------- */
.tabbar{position:fixed;left:0;right:0;bottom:0;z-index:30;background:var(--card);
  border-top:1px solid var(--line);display:flex;
  padding-bottom:env(safe-area-inset-bottom,0px);max-width:520px;margin:0 auto}
.tabbar a{flex:1;display:grid;place-items:center;gap:2px;padding:7px 0 6px;
  color:var(--muted);font-size:10px;font-weight:700}
.tabbar a[aria-current]{color:var(--brand)}
.tabbar svg{width:22px;height:22px}
@media (prefers-color-scheme: dark){
  :root:where(:not([data-theme="light"])) .tabbar a[aria-current]{color:#7fb0f0}
}
:root[data-theme="dark"] .tabbar a[aria-current]{color:#7fb0f0}

/* ---- misc ------------------------------------------------------------- */
pre{white-space:pre-wrap;word-break:break-word;margin:0;
  font:12px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink-2)}
.halt{background:var(--critical);color:#fff;padding:9px 14px;font-size:13px;font-weight:700}
.copybar{display:flex;gap:8px;align-items:center;margin-top:10px}
.copybar button{width:auto;flex:none}
.copybar .said{font-size:12px;color:var(--good);font-weight:700}
.toast{background:var(--raise);border:1px solid var(--line);border-left:3px solid var(--brand);
  border-radius:10px;padding:11px 13px;margin-bottom:12px;font-size:14px;font-weight:700}
"""


def stylesheet() -> str:
    return TOKENS + BASE
