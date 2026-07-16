# -*- coding: utf-8 -*-
"""
Vercel Serverless Function: 仲値3ロジック GO/NO-GO判定 (USDJPY)
データソース: GMOコイン 外国為替FX 公開API (認証不要)
  https://forex-api.coin.z.com/public/v1/klines
GET /api/judge?date=YYYY-MM-DD  (省略時=今日JST)
依存: jpholiday のみ (requirements.txt)
"""
import calendar
import concurrent.futures
import datetime as dt
import json
import math
import urllib.request
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import jpholiday

JST = dt.timezone(dt.timedelta(hours=9))
API = "https://forex-api.coin.z.com/public/v1/klines?symbol=USD_JPY&priceType={pt}&interval={iv}&date={d}"

# ===== L1 ロジスティック回帰パラメータ (全期間797トレードで学習; l1_model.json と同一) =====
L1_FEATURES = ["n_gap", "n_prevret", "n_ret79", "n_range79", "n_vol79",
               "n_ret5d", "spread_entry", "pos_prev_range", "n_ret9T", "n_range9T"]
L1_MODEL = None  # 起動時に同梱JSONから読み込み
import os
_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, "l1_model.json")) as _f:
    L1_MODEL = json.load(_f)


# ===== カレンダー判定 (nakane_alert.py と同一) =====
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
def fetch_klines(biz_date, interval, price_type, timeout=8):
    url = API.format(pt=price_type, iv=interval, d=biz_date.strftime("%Y%m%d"))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nakane-alert/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read().decode())
        if j.get("status") != 0:
            return []
        return j.get("data", [])
    except Exception:
        return []


def load_bars(target, lookback=16):
    """target日以前の1分足(当日)+15分足(過去)をJSTカレンダー日でバケット化。
    返値: bars[date_iso] = list of dict(t="HH:MM:SS", ob,hb,lb,cb, oa,ha,la,ca)"""
    biz_dates = [target - dt.timedelta(days=i) for i in range(lookback, -1, -1)]
    biz_dates = [d for d in biz_dates if d.weekday() < 6]  # 日曜はファイルなし
    jobs = []
    for d in biz_dates:
        iv = "1min" if (target - d).days <= 1 else "15min"
        jobs.append((d, iv, "BID"))
        jobs.append((d, iv, "ASK"))
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fetch_klines, d, iv, pt): (d, iv, pt) for d, iv, pt in jobs}
        for fu in concurrent.futures.as_completed(futs):
            results[futs[fu]] = fu.result()
    # openTimeでBID/ASKを結合し、JSTカレンダー日でバケット
    bars = {}
    for d, iv, _ in {(d, iv, "BID"): 1 for d, iv, pt in jobs}.keys():
        bid = {b["openTime"]: b for b in results.get((d, iv, "BID"), [])}
        ask = {b["openTime"]: b for b in results.get((d, iv, "ASK"), [])}
        for ts, b in bid.items():
            a = ask.get(ts)
            if not a:
                continue
            t = dt.datetime.fromtimestamp(int(ts) / 1000, tz=JST)
            key = t.date().isoformat()
            bars.setdefault(key, []).append({
                "t": t.strftime("%H:%M:%S"),
                "ob": float(b["open"]), "hb": float(b["high"]),
                "lb": float(b["low"]), "cb": float(b["close"]),
                "oa": float(a["open"]), "ha": float(a["high"]),
                "la": float(a["low"]), "ca": float(a["close"])})
    for k in bars:
        bars[k].sort(key=lambda x: x["t"])
    return bars


# ===== 特徴量 (nakane_alert.py と同一定義) =====
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
def judge_l1(f):
    m = L1_MODEL
    z = 0.0
    for k, mu, sd, c in zip(m["features"], m["scaler_mean"], m["scaler_scale"], m["coef"]):
        v = f.get(k, 0.0)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            v = 0.0
        z += (v - mu) / sd * c
    p = 1 / (1 + math.exp(-(z + m["intercept"])))
    if p >= m["q75"]:
        tier, note = "STRONG GO", "WF-OOS実績: 勝率61.1% / +3.13pips/回 / PF2.24"
    elif p >= m["q50"]:
        tier, note = "GO", "WF-OOS実績: 勝率61.6% / +2.72pips/回 / PF2.07"
    elif p >= m["q25"]:
        tier, note = "WEAK", "WF-OOS実績: 勝率54.4% / +1.61pips/回 — 小ロットか見送り推奨"
    else:
        tier, note = "NO-GO", "WF-OOS実績: 勝率46.3% / +0.25pips/回 — エッジ消失域"
    return tier, round(p, 4), note


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


def judge(target, bars=None):
    """メイン判定。barsを渡すとAPI取得をスキップ(テスト用)"""
    dow = target.weekday()
    res = {"date": target.isoformat(), "dow": "月火水木金土日"[dow],
           "generated_at": dt.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST"),
           "warnings": [], "disclaimer": "本判定は過去検証に基づく参考情報であり、将来の成績を保証しません。投資助言ではありません。"}
    if dow >= 5:
        res.update(day_type="週末", logic=None, tier="対象外", detail="土日は全ロジック対象外")
        return res
    holiday = is_trade_holiday(target)
    gotobi = is_gotobi(target)
    if bars is None:
        bars = load_bars(target)
    ti = target.isoformat()

    if holiday:
        f = compute_features(bars, ti, "09:00:00")
        res["day_type"] = f"祝日（{jpholiday.is_holiday_name(target)}）"
        res["logic"] = "L3 祝日仲値ショート"
        res["plan"] = "9:00 成行ショート → 9:55 決済（災害用SL 30pips）"
        if f["spread_entry"] >= 5:
            res.update(tier="NO-GO", detail=f"スプレッド{f['spread_entry']:.1f}pips = 実質取引不能")
        else:
            tier, score, note, sig = judge_l3(f)
            res.update(tier=tier, score=f"下向きスコア {score}/3", detail=note,
                       signals={k: bool(v) for k, v in sig.items()})
            if f["spread_entry"] > 1:
                res["warnings"].append(f"スプレッド拡大 {f['spread_entry']:.1f}pips（流動性低下）")
    elif gotobi:
        f = compute_features(bars, ti, "09:53:00")
        res["day_type"] = "ゴトー日"
        res["logic"] = "L1 ゴトー日仲値ショート"
        res["plan"] = "9:53 成行ショート / SL10・TP15・15分タイムアウト"
        tier, p, note = judge_l1(f)
        res.update(tier=tier, score=f"モデル確率 {p}", detail=note)
        if f["n_bars_9T"] < 45:
            res["warnings"].append(f"9:00-9:53のバー数 {f['n_bars_9T']}/53 — 9:52以降に再判定推奨")
        if f["spread_entry"] > 0.4:
            res["warnings"].append(f"スプレッド {f['spread_entry']:.2f}pips > 0.4（過去実績 勝率37%）")
    else:
        f = compute_features(bars, ti, "09:25:00")
        res["day_type"] = "非ゴトー日"
        res["logic"] = "L2 仲値前ロング"
        res["plan"] = "9:25 成行ロング → 9:55（仲値）決済"
        tier, v, note = judge_l2(f, dow)
        res.update(tier=tier, detail=note)
        if v is not None:
            res["score"] = f"n_ret79 = {v}"
    res["features"] = {k: (None if isinstance(v, float) and math.isnan(v) else round(v, 4))
                       for k, v in f.items() if k != "last_bar"}
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
