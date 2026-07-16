# -*- coding: utf-8 -*-
"""判定画面HTML (index.htmlと同内容)"""
HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>仲値アラート — USDJPY 3ロジック判定</title>
<style>
  :root { --bg:#0f1420; --card:#1a2233; --txt:#e8ecf4; --sub:#93a0b8; --line:#2a3550; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--txt); font-family:"Hiragino Sans","Noto Sans JP",Meiryo,sans-serif;
         max-width:640px; margin:0 auto; padding:20px 14px 48px; }
  h1 { font-size:1.25rem; margin-bottom:2px; }
  .sub { color:var(--sub); font-size:.8rem; margin-bottom:16px; }
  .bar { display:flex; gap:8px; margin-bottom:14px; align-items:center; flex-wrap:wrap; }
  input[type=date] { background:var(--card); color:var(--txt); border:1px solid var(--line);
                     border-radius:8px; padding:8px 10px; font-size:.95rem; }
  button { background:#2f6fed; color:#fff; border:none; border-radius:8px; padding:9px 16px;
           font-size:.95rem; cursor:pointer; }
  button:disabled { opacity:.5; }
  label.auto { color:var(--sub); font-size:.8rem; display:flex; gap:4px; align-items:center; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:18px; margin-bottom:12px; }
  .daytype { color:var(--sub); font-size:.85rem; }
  .logic { font-size:1.1rem; font-weight:700; margin:4px 0 8px; }
  .plan { font-size:.9rem; color:var(--sub); margin-bottom:14px; }
  .tier { display:inline-block; font-size:1.5rem; font-weight:800; padding:10px 22px; border-radius:12px; margin-bottom:10px; }
  .t-strong { background:#0d3d2a; color:#3ddc84; border:1px solid #3ddc84; }
  .t-go     { background:#0d3524; color:#9be29b; border:1px solid #6fbf6f; }
  .t-weak, .t-caution { background:#3d330d; color:#f2c744; border:1px solid #f2c744; }
  .t-nogo   { background:#3d0d14; color:#ff6b6b; border:1px solid #ff6b6b; }
  .t-na     { background:#222b3f; color:var(--sub); border:1px solid var(--line); }
  .score { font-size:.9rem; color:var(--sub); margin-bottom:8px; }
  .detail { font-size:.92rem; line-height:1.6; }
  .warn { background:#3d2a0d; border:1px solid #f2a744; color:#f2c98a; border-radius:8px;
          padding:8px 12px; font-size:.85rem; margin-top:10px; }
  .sig { display:flex; gap:8px; margin:10px 0; flex-wrap:wrap; }
  .sig span { font-size:.8rem; padding:4px 10px; border-radius:20px; border:1px solid var(--line); color:var(--sub); }
  .sig span.on { border-color:#3ddc84; color:#3ddc84; }
  details { margin-top:12px; }
  summary { color:var(--sub); font-size:.85rem; cursor:pointer; }
  table { width:100%; border-collapse:collapse; margin-top:8px; font-size:.82rem; }
  td { padding:4px 6px; border-bottom:1px solid var(--line); }
  td:last-child { text-align:right; font-variant-numeric:tabular-nums; }
  .meta { color:var(--sub); font-size:.75rem; margin-top:12px; line-height:1.5; }
  .err { background:#3d0d14; border:1px solid #ff6b6b; border-radius:10px; padding:14px; font-size:.9rem; }
  .loading { color:var(--sub); padding:30px 0; text-align:center; }
  .foot { color:var(--sub); font-size:.72rem; margin-top:20px; line-height:1.6; }
</style>
</head>
<body>
<h1>仲値アラート <span style="color:#2f6fed">USDJPY</span></h1>
<div class="sub">ゴトー日9:53S / 非ゴトー9:25L / 祝日9:00S — エントリー前 GO/NO-GO 判定</div>
<div class="bar">
  <input type="date" id="date">
  <button id="run">判定</button>
  <label class="auto"><input type="checkbox" id="auto">60秒毎に自動更新</label>
</div>
<div id="out"><div class="loading">読み込み中...</div></div>
<div class="foot">
  データ: GMOコイン 外国為替FX 公開API（リアルタイム）。判定タイミング目安: 祝日=9:00直前 / 非ゴトー日=9:25直前 / ゴトー日=9:52頃。<br>
  本サイトは過去データの統計的検証に基づく参考情報です。将来の成績を保証するものではなく、投資助言ではありません。
</div>
<script>
const out = document.getElementById("out");
const dateEl = document.getElementById("date");
dateEl.value = new Date(Date.now() + 9*3600*1000).toISOString().slice(0,10); // JST今日

function tierClass(t) {
  if (!t) return "t-na";
  if (t.startsWith("STRONG")) return "t-strong";
  if (t === "GO") return "t-go";
  if (t === "WEAK" || t === "CAUTION") return "t-weak";
  if (t.startsWith("NO-GO")) return "t-nogo";
  return "t-na";
}
async function run() {
  out.innerHTML = '<div class="loading">データ取得中...（数秒かかります）</div>';
  try {
    const r = await fetch("/api/judge?date=" + dateEl.value);
    const d = await r.json();
    if (d.error) { out.innerHTML = `<div class="err">エラー: ${d.error}</div>`; return; }
    let h = `<div class="card">`;
    h += `<div class="daytype">${d.date}（${d.dow}） — ${d.day_type ?? ""}</div>`;
    if (!d.logic) {
      h += `<div class="tier t-na" style="margin-top:10px">${d.tier}</div><div class="detail">${d.detail ?? ""}</div></div>`;
      out.innerHTML = h; return;
    }
    h += `<div class="logic">${d.logic}</div><div class="plan">${d.plan}</div>`;
    h += `<div><span class="tier ${tierClass(d.tier)}">${d.tier}</span></div>`;
    if (d.score) h += `<div class="score">${d.score}</div>`;
    h += `<div class="detail">${d.detail}</div>`;
    if (d.signals) {
      h += `<div class="sig">` + Object.entries(d.signals).map(([k,v]) =>
        `<span class="${v ? "on" : ""}">${v ? "○" : "×"} ${k}</span>`).join("") + `</div>`;
    }
    (d.warnings ?? []).forEach(w => h += `<div class="warn">⚠ ${w}</div>`);
    if (d.features) {
      const jp = {gap_open:"ギャップ(前日終値→7:00) pips", prev_ret:"前日リターン pips", prev_range:"前日レンジ pips",
        atr5:"ATR5 pips", ret5d:"5日リターン pips", ret_700_900:"7:00→9:00 pips", ret_900_T:"9:00→エントリー pips",
        range_700_900:"早朝レンジ pips", vol_700_900:"早朝1分ボラ", spread_entry:"スプレッド pips",
        pos_prev_range:"前日レンジ内位置", n_ret79:"n_ret79(正規化 朝リターン)", n_ret5d:"n_ret5d(正規化 5日)",
        n_vol79:"n_vol79(正規化 朝ボラ)"};
      h += `<details><summary>特徴量の詳細</summary><table>` +
        Object.entries(d.features).filter(([k,v]) => v !== null && jp[k])
          .map(([k,v]) => `<tr><td>${jp[k]}</td><td>${v}</td></tr>`).join("") +
        `</table></details>`;
    }
    h += `<div class="meta">現在値 BID ${d.price?.bid ?? "-"} / ASK ${d.price?.ask ?? "-"} ・ 最終バー ${d.data_last_bar ?? "-"} JST<br>判定生成 ${d.generated_at}</div>`;
    h += `</div>`;
    out.innerHTML = h;
  } catch (e) {
    out.innerHTML = `<div class="err">通信エラー: ${e}</div>`;
  }
}
document.getElementById("run").onclick = run;
let timer = null;
document.getElementById("auto").onchange = (e) => {
  if (e.target.checked) timer = setInterval(run, 60000); else clearInterval(timer);
};
run();
</script>
</body>
</html>
"""
