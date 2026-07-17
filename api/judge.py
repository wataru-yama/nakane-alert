# -*- coding: utf-8 -*-
"""
Vercel Serverless Function: 仲値3ロジック GO/NO-GO判定
データソース: GMOコイン 外国為替FX 公開API (認証不要)
GET /api/judge?date=YYYY-MM-DD  (省略時=今日JST)

ロジック:
  L1 ゴトー日 9:53ショート 3バリアント
    L1a: USDJPY SL10/TP15/15分  (高PF・低DD)
    L1b: USDJPY SL10/TP15/30分  (高pips, 4/6/8月除外オプション)
    L1c: EURJPY SL15/TP15/60分  (最高pips)
  L2 非ゴトー日(火水金) USDJPY 9:25ロング → 9:55決済
  L3 祝日 USDJPY 9:00ショート → 9:55決済
"""
import calendar
import concurrent.futures
import datetime as dt
import json
import math
import os
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import jpholiday

JST = dt.timezone(dt.timedelta(hours=9))
API = "https://forex-api.coin.z.com/public/v1/klines?symbol={sym}&priceType={pt}&interval={iv}&date={d}"

_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, "l1_models.json")) as _f:
    L1_MODELS = json.load(_f)

L1_VARIANTS = [
    {"key": "L1a", "symbol": "USD_JPY", "name": "L1a USDJPY ショート（SL10/TP15/15分）",
     "plan": "9:53 成行ショート / SL10・TP15・タイムアウト15分(10:08)", "month_excl": True},
    {"key": "L1b", "symbol": "USD_JPY", "name": "L1b USDJPY ショート（SL10/TP15/30分）",
     "plan": "9:53 成行ショート / SL10・TP15・タイムアウト30分(10:23)", "month_excl": True},
    {"key": "L1c", "symbol": "EUR_JPY", "name": "L1c EURJPY ショート（SL15/TP15/60分）",
     "plan": "9:53 成行ショート / SL15・TP15・タイムアウト60分(10:53)", "month_excl": False},
]


# ===== カレンダー判定 =====
def is_jp_banking_day(d):
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    if d.month == 12 and d.day == 31:
        return False
    if d.month == 1 and d.day in (1, 2, 3):
        return False
    return True


def prev_banking_day(d):
    d = d - dt.timedelta(days=1)
    while not is_jp_banking_day(d):
        d -= dt.timedelta(days=1)
    return d


def is_gotobi(d):
    if not is_jp_banking_day(d):
        return False
    cur = d
    for _ in range(10):
        last = calendar.monthrange(cur.year, cur.month)[1]
        if cur.day in (5, 10, 15, 20, 25, 30) or cur.day == last:
            adj = cur if is_jp_banking_day(cur) else prev_banking_day(cur)
            if adj == d:
                return True
            if adj > d:
                return False
        cur += dt.timedelta(days=1)
        if is_jp_banking_day(cur) and cur != d:
            return False
    return False


def is_trade_holiday(d):
    return d.weekday() < 5 and bool(jpholiday.is_holiday(d))


# ===== データ取得 =====
def fetch_klines(symbol, biz_date, interval, price_type, timeout=8):
    url = API.format(sym=symbol, pt=price_type, iv=interval, d=biz_date.strftime("%Y%m%d"))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nakane-alert/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode())
        if j.get("status") != 0:
            return []
        return j.get("data", [])
    except Exception:
        return []


def load_bars(target, symbol, lookback=16):
    """JSTカレンダー日ごとのBID/ASKバー辞書を返す"""
    biz_dates = [target - dt.timedelta(days=i) for i in range(lookback, -1, -1)]
    biz_dates = [d for d in biz_dates if d.weekday() < 6]
    jobs = []
    for d in biz_dates:
        iv = "1min" if (target - d).days <= 1 else "15min"
        jobs.append((d, iv, "BID"))
        jobs.append((d, iv, "ASK"))
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fetch_klines, symbol, d, iv, pt): (d, iv, pt) for d, iv, pt in jobs}
        for fu in concurrent.futures.as_completed(futs):
            results[futs[fu]] = fu.result()
    bars = {}
    for d, iv, _pt in {(d, iv, "BID"): 1 for d, iv, pt in jobs}.keys():
        bid = {b["openTime"]: b for b in results.get((d, iv, "BID"), [])}
        ask = {b["openTime"]: b for b in results.get((d, iv, "ASK"), [])}
        for ts, b in bid.items():
            a = ask.get(ts)
            if not a:
                continue
            t = dt.datetime.fromtimestamp(int(ts) / 1000, tz=JST)
            bars.setdefault(t.date().isoformat(), []).append({
                "t": t.strftime("%H:%M:%S"),
                "ob": float(b["open"]), "hb": float(b["high"]),
                "lb": float(b["low"]), "cb": float(b["close"]),
                "oa": float(a["open"]), "ha": float(a["high"]),
                "la": float(a["low"]), "ca": float(a["close"])})
    for k in bars:
        bars[k].sort(key=lambda x: x["t"])
    return bars


# ===== 特徴量 =====
def mid(b, k):
    return (b[k + "b"] + b[k + "a"]) / 2


def daily_agg(bs):
    return {"open": mid(bs[0], "o"), "close": mid(bs[-1], "c"),
            "high": max(mid(b, "h") for b in bs), "low": min(mid(b, "l") for b in bs)}


def find_bar(bs, t):
    for b in bs:
        if b["t"] == t:
            return b
    lo = (dt.datetime.strptime(t, "%H:%M:%S") - dt.timedelta(minutes=5)).strftime("%H:%M:%S")
    hi = (dt.datetime.strptime(t, "%H:%M:%S") + dt.timedelta(minutes=5)).strftime("%H:%M:%S")
    cand = [b for b in bs if lo <= b["t"] <= hi]
    after = [b for b in cand if b["t"] > t]
    if after:
        return after[0]
    return cand[-1] if cand else None


def stdev(xs):
    n = len(xs)
    if n < 2:
        return float("nan")
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def compute_features(bars, target_iso, entry_t):
    days = sorted(k for k in bars if k <= target_iso and bars[k])
    if target_iso not in days:
        raise ValueError(f"{target_iso} のデータが取得できません(市場休場または未配信)")
    i = days.index(target_iso)
    if i < 6:
        raise ValueError("過去6営業日分のデータが不足しています")
    dailies = {d: daily_agg(bars[d]) for d in days[max(0, i - 7):i]}
    prevs = days[i - 7:i] if i >= 7 else days[:i]
    prev = dailies[prevs[-1]]
    atr5 = sum((dailies[d]["high"] - dailies[d]["low"]) * 100 for d in prevs[-6:-1]) / 5
    p5c = dailies[prevs[-6]]["close"]
    td = bars[target_iso]

    b700 = find_bar(td, "07:00:00")
    b900 = find_bar(td, "09:00:00")
    bT = find_bar(td, entry_t)
    if not (b700 and b900 and bT):
        raise ValueError("当日朝(7:00/9:00/エントリー時刻)のバーが不足しています")
    o700, o900, oT = mid(b700, "o"), mid(b900, "o"), mid(bT, "o")
    f = {"spread_entry": (bT["oa"] - bT["ob"]) * 100,
         "gap_open": (o700 - prev["close"]) * 100,
         "prev_ret": (prev["close"] - prev["open"]) * 100,
         "prev_range": (prev["high"] - prev["low"]) * 100,
         "atr5": atr5,
         "ret5d": (prev["close"] - p5c) * 100,
         "ret_700_900": (o900 - o700) * 100,
         "ret_900_T": (oT - o900) * 100,
         "entry_bid": bT["ob"], "entry_ask": bT["oa"],
         "last_bar": td[-1]["t"]}
    f["pos_prev_range"] = ((oT - prev["low"]) / (prev["high"] - prev["low"])
                           if prev["high"] > prev["low"] else float("nan"))
    w79 = [b for b in td if "07:00:00" <= b["t"] < "09:00:00"]
    if len(w79) > 10:
        f["range_700_900"] = (max(mid(b, "h") for b in w79) - min(mid(b, "l") for b in w79)) * 100
        closes = [mid(b, "c") for b in w79]
        f["vol_700_900"] = stdev([closes[k + 1] - closes[k] for k in range(len(closes) - 1)]) * 100
    else:
        f["range_700_900"] = float("nan")
        f["vol_700_900"] = float("nan")
    f["n_bars_9T"] = 0
    if entry_t > "09:00:00":
        w9T = [b for b in td if "09:00:00" <= b["t"] < entry_t]
        f["n_bars_9T"] = len(w9T)
        f["range_900_T"] = ((max(mid(b, "h") for b in w9T) - min(mid(b, "l") for b in w9T)) * 100
                            if len(w9T) > 3 else float("nan"))
    else:
        f["range_900_T"] = float("nan")
    a = atr5 if atr5 > 0 else float("nan")
    f["n_gap"] = f["gap_open"] / a
    f["n_prevret"] = f["prev_ret"] / a
    f["n_ret79"] = f["ret_700_900"] / a
    f["n_range79"] = f["range_700_900"] / a
    f["n_vol79"] = f["vol_700_900"] / a * 100
    f["n_ret5d"] = f["ret5d"] / a
    f["n_ret9T"] = f["ret_900_T"] / a
    f["n_range9T"] = f["range_900_T"] / a
    return f


# ===== 判定 =====
def _clean(f):
    return {k: (None if isinstance(v, float) and math.isnan(v) else round(v, 4))
            for k, v in f.items() if k != "last_bar"}


def judge_l1_variant(var, f, month):
    m = L1_MODELS[var["key"]]
    z = 0.0
    for k, mu, sd, c in zip(m["features"], m["scaler_mean"], m["scaler_scale"], m["coef"]):
        v = f.get(k, 0.0)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            v = 0.0
        z += (v - mu) / sd * c
    p = 1 / (1 + math.exp(-(z + m["intercept"])))
    tier_key = ("STRONG" if p >= m["q75"] else "GO" if p >= m["q50"]
                else "WEAK" if p >= m["q25"] else "NOGO")
    s = m["tier_stats"][tier_key]
    tier_label = {"STRONG": "STRONG GO", "GO": "GO", "WEAK": "WEAK", "NOGO": "NO-GO"}[tier_key]
    note = f"WF-OOS実績: 勝率{s['wr']}% / {s['mp']:+}pips/回 / PF{s['pf']} (n={s['n']})"
    if tier_key == "WEAK":
        note += " — 小ロットか見送り推奨"
    v = {"key": var["key"], "name": var["name"], "plan": var["plan"],
         "tier": tier_label, "prob": round(p, 4), "detail": note,
         "warnings": [], "features": _clean(f),
         "price": {"bid": f["entry_bid"], "ask": f["entry_ask"]},
         "data_last_bar": f["last_bar"]}
    if f["n_bars_9T"] < 45:
        v["warnings"].append(f"9:00-9:53のバー数 {f['n_bars_9T']}/53 — 9:52以降に再判定推奨")
    sp_lim = 0.4 if var["symbol"] == "USD_JPY" else 0.9
    if f["spread_entry"] > sp_lim:
        v["warnings"].append(f"スプレッド {f['spread_entry']:.2f}pips > {sp_lim}（コスト増に注意）")
    if var["month_excl"] and month in (4, 6, 8):
        v["warnings"].append("4/6/8月はUSDJPYで月除外設定によりPF改善の報告あり（除外採用中なら見送り）")
    return v


def judge_l2(f, dow):
    if dow not in (1, 2, 4):
        return "NO-GO", None, "火・水・金のみ対象（月木は統計的優位性なし）"
    v = f["n_ret79"]
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "判定不能", None, "7:00-9:00のデータ不足"
    if v <= 0.15:
        return "GO", round(v, 3), "GO側実績: 勝率57.0% / +1.80pips/回（検証期間 62.0% / +2.68）"
    return "NO-GO", round(v, 3), f"朝の急騰 (n_ret79={v:.2f}>0.15) — 高値掴み領域。除外側実績 勝率51.4% / +0.31pips"


def judge_l3(f):
    sig = {"ギャップダウン": f["gap_open"] < 0,
           "前日陰線": f["prev_ret"] < 0,
           "5日下落": f["ret5d"] < 0}
    score = sum(sig.values())
    if score >= 3:
        tier, note = "STRONG GO", "実績(score=3): 勝率89.5% / +16.5pips/回 (n=19)"
    elif score == 2:
        tier, note = "STRONG GO", "実績(score≥2): 勝率76.4% / +8.70pips/回 / PF5.84"
    elif score == 1:
        tier, note = "GO", "実績(score=1): 勝率55.9% / +3.29pips/回 / PF2.58"
    else:
        tier, note = "CAUTION", "実績(score=0): 勝率61.5% / +0.85pips/回 — 小ロットか見送り"
    return tier, score, note, sig


def judge(target, bars_usd=None, bars_eur=None):
    """メイン判定。bars_*を渡すとAPI取得をスキップ(テスト用)"""
    dow = target.weekday()
    res = {"date": target.isoformat(), "dow": "月火水木金土日"[dow],
           "generated_at": dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
           "warnings": [], "disclaimer": "本判定は過去検証に基づく参考情報であり、将来の成績を保証しません。投資助言ではありません。"}
    if dow >= 5:
        res.update(day_type="週末", logic=None, tier="対象外", detail="土日は全ロジック対象外")
        return res
    holiday = is_trade_holiday(target)
    gotobi = is_gotobi(target)
    ti = target.isoformat()

    if holiday:
        if bars_usd is None:
            bars_usd = load_bars(target, "USD_JPY")
        f = compute_features(bars_usd, ti, "09:00:00")
        res["day_type"] = f"祝日（{jpholiday.is_holiday_name(target)}）"
        res["logic"] = "L3 祝日仲値ショート (USDJPY)"
        res["plan"] = "9:00 成行ショート → 9:55 決済（災害用SL 30pips）"
        if f["spread_entry"] >= 5:
            res.update(tier="NO-GO", detail=f"スプレッド{f['spread_entry']:.1f}pips = 実質取引不能")
        else:
            tier, score, note, sig = judge_l3(f)
            res.update(tier=tier, score=f"下向きスコア {score}/3", detail=note,
                       signals={k: bool(v) for k, v in sig.items()})
            if f["spread_entry"] > 1:
                res["warnings"].append(f"スプレッド拡大 {f['spread_entry']:.1f}pips（流動性低下）")
        res["features"] = _clean(f)
        res["data_last_bar"] = f["last_bar"]
        res["price"] = {"bid": f["entry_bid"], "ask": f["entry_ask"]}
        return res

    if gotobi:
        res["day_type"] = "ゴトー日"
        res["logic"] = "L1 ゴトー日仲値ショート（3バリアント）"
        if bars_usd is None:
            bars_usd = load_bars(target, "USD_JPY")
        if bars_eur is None:
            bars_eur = load_bars(target, "EUR_JPY")
        variants = []
        f_by_sym = {}
        for var in L1_VARIANTS:
            bars = bars_usd if var["symbol"] == "USD_JPY" else bars_eur
            try:
                if var["symbol"] not in f_by_sym:
                    f_by_sym[var["symbol"]] = compute_features(bars, ti, "09:53:00")
                variants.append(judge_l1_variant(var, f_by_sym[var["symbol"]], target.month))
            except ValueError as e:
                variants.append({"key": var["key"], "name": var["name"], "plan": var["plan"],
                                 "tier": "判定不能", "detail": str(e), "warnings": []})
        res["variants"] = variants
        ok = [v for v in variants if "data_last_bar" in v]
        if ok:
            res["data_last_bar"] = ok[0]["data_last_bar"]
        return res

    if bars_usd is None:
        bars_usd = load_bars(target, "USD_JPY")
    f = compute_features(bars_usd, ti, "09:25:00")
    res["day_type"] = "非ゴトー日"
    res["logic"] = "L2 仲値前ロング (USDJPY)"
    res["plan"] = "9:25 成行ロング → 9:55（仲値）決済"
    tier, v, note = judge_l2(f, dow)
    res.update(tier=tier, detail=note)
    if v is not None:
        res["score"] = f"n_ret79 = {v}"
    res["features"] = _clean(f)
    res["data_last_bar"] = f["last_bar"]
    res["price"] = {"bid": f["entry_bid"], "ask": f["entry_ask"]}
    return res


# ===== HTTPハンドラ (Vercel) =====
class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        # /api/ 以外のパスは判定画面(HTML)を返す
        if not parsed.path.startswith("/api"):
            try:
                from .page import HTML
            except ImportError:
                from page import HTML
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(body)
            return
        q = parse_qs(parsed.query)
        try:
            if "date" in q:
                target = dt.date(*map(int, q["date"][0].split("-")))
            else:
                target = dt.datetime.now(JST).date()
            res = judge(target)
            code = 200
        except ValueError as e:
            res, code = {"error": str(e)}, 400
        except Exception as e:
            res, code = {"error": f"internal: {e}"}, 500
        body = json.dumps(res, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "s-maxage=60, stale-while-revalidate=30")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
