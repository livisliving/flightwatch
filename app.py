#!/usr/bin/env python3
"""
FlightWatch - 伦敦 ⇄ 上海 商务舱 / 超级经济舱 票价监控

只用 Python 标准库，不需要安装任何依赖。
- 通过 SerpApi 的 Google Flights 接口查价
- 每一程总时长（含转机）不超过 MAX_HOURS 小时
- 长途段必须是商务/超经，国内转机短途段允许经济舱
- 按 SerpApi 剩余额度自动平均分配查询次数
- 降价时通过 ntfy 推送到手机
- 自带一个网页看板（端口 8080）
"""

import json
import math
import os
import random
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# 配置（全部来自环境变量，在 compose.yaml 里改）
# ----------------------------------------------------------------------------


def env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def env_int(name, default):
    try:
        return int(float(env(name, default)))
    except (TypeError, ValueError):
        return default


def parse_day(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def day_range(a, b):
    d, out = a, []
    while d <= b:
        out.append(d)
        d += timedelta(days=1)
    return out


SERPAPI_KEY = env("SERPAPI_KEY", "")
FAKE_MODE = env("FAKE_MODE", "false").lower() in ("1", "true", "yes")
NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = env("NTFY_TOPIC", "")
DASHBOARD_URL = env("DASHBOARD_URL", "")

ORIGINS = env("ORIGINS", "LHR")
DESTS = env("DESTS", "PVG,SHA")
OUT_DATES = day_range(parse_day(env("OUT_START", "2026-12-11")), parse_day(env("OUT_END", "2026-12-16")))
RET_DATES = day_range(parse_day(env("RET_START", "2026-12-27")), parse_day(env("RET_END", "2026-12-30")))
STAY_MIN = env_int("STAY_MIN", 0)  # 0 = 不限制
STAY_MAX = env_int("STAY_MAX", 0)
MAX_MIN = env_int("MAX_HOURS", 17) * 60
LONG_SEG_MIN = env_int("LONG_SEGMENT_HOURS", 5) * 60  # 超过这个时长的航段必须是高舱
CLASSES = [c.strip() for c in env("CLASSES", "business,premium").split(",") if c.strip()]
# 首选舱位：看板大字和提醒优先显示它（默认商务舱）
PRIMARY = env("PRIMARY_CLASS", "business" if "business" in CLASSES else CLASSES[0])
ROUND_TRIP = env("ROUND_TRIP", "true").lower() in ("1", "true", "yes")
CURRENCY = env("CURRENCY", "GBP")
TARGET_PRICE = env_int("TARGET_PRICE", 0)
ALERT_MIN_DROP = env_int("ALERT_MIN_DROP", 20)
RESERVE = env_int("RESERVE_SEARCHES", 10)
MIN_INTERVAL = env_int("MIN_INTERVAL_MINUTES", 10) * 60
DIGEST_HOUR_UTC = env_int("DIGEST_HOUR_UTC", 8)  # 12 月伦敦时间 = UTC
STOP_DATE = parse_day(env("STOP_DATE", str(OUT_DATES[-1])))
DATA_DIR = env("DATA_DIR", "data")
PORT = env_int("PORT", 8080)
OUTPUT_DIR = env("OUTPUT_DIR", "site")          # --once 模式：看板网页输出到这里（GitHub Pages）
RUN_INTERVAL_HOURS = float(env("RUN_INTERVAL_HOURS", "2"))  # --once 模式：多久运行一次（和 workflow 的 cron 一致）
MAX_PER_RUN = env_int("MAX_PER_RUN", 8)

CLASS_PARAM = {"economy": 1, "premium": 2, "business": 3, "first": 4}
CLASS_RANK = {"economy": 1, "premium economy": 2, "premium": 2, "business": 3, "first": 4}
CLASS_CN = {1: "经济", 2: "超经", 3: "商务", 4: "头等"}
CLASS_KEY_CN = {"business": "商务舱", "premium": "超级经济舱", "first": "头等舱", "economy": "经济舱"}

SYM = {"GBP": "£", "USD": "$", "EUR": "€", "CNY": "¥"}.get(CURRENCY, CURRENCY + " ")
HERE = os.path.dirname(os.path.abspath(__file__))

STATUS = {"started": time.time(), "next_run": None, "last_run": None, "left": None,
          "per_day": None, "renewal": None, "paused_reason": None, "last_error": None}
LOCK = threading.RLock()


def log(*a):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def money(v):
    return f"{SYM}{v:,.0f}"


# ----------------------------------------------------------------------------
# 数据存储：纯文本文件（JSON Lines），方便 GitHub Actions 每次运行后提交回仓库
# ----------------------------------------------------------------------------

_SUFFIX = "-demo" if FAKE_MODE else ""  # 演示模式用单独的文件，切换到正式模式时不会混进假数据
CHECKS_PATH = os.path.join(DATA_DIR, f"checks{_SUFFIX}.jsonl")
STATE_PATH = os.path.join(DATA_DIR, f"state{_SUFFIX}.json")
_CACHE = {"mtime": None, "rows": []}


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)


def _read_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def kv_get(k, default=None):
    return _read_state().get(k, default)


def kv_set(k, v):
    with LOCK:
        st = _read_state()
        st[k] = v
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, STATE_PATH)


def all_checks():
    """返回所有查询记录；data 字段保持为 JSON 字符串（和其他函数的约定一致）。"""
    try:
        mtime = os.path.getmtime(CHECKS_PATH)
    except FileNotFoundError:
        return []
    if _CACHE["mtime"] != mtime:
        rows = []
        with open(CHECKS_PATH, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                r["id"] = i
                r["data"] = json.dumps(r["data"], ensure_ascii=False) if r.get("data") is not None else None
                rows.append(r)
        _CACHE.update(mtime=mtime, rows=rows)
    return [dict(r) for r in _CACHE["rows"]]


def save_check(key, ok, price=None, data=None, error=None):
    rec = {"key": key, "ts": round(time.time(), 1), "ok": 1 if ok else 0, "price": price,
           "data": data, "error": error}
    with LOCK:
        with open(CHECKS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    _CACHE["mtime"] = None


# ----------------------------------------------------------------------------
# 查询列表
# ----------------------------------------------------------------------------


def active_out_dates():
    today = datetime.now(timezone.utc).date()
    return [d for d in OUT_DATES if d >= today]


def active_ret_dates():
    today = datetime.now(timezone.utc).date()
    return [d for d in RET_DATES if d >= today]


def pair_ok(o, r):
    stay = (r - o).days
    if stay <= 0:
        return False
    if STAY_MIN and stay < STAY_MIN:
        return False
    if STAY_MAX and stay > STAY_MAX:
        return False
    return True


def query_keys():
    keys = []
    for c in CLASSES:
        for d in active_out_dates():
            keys.append(f"ow|out|{d}|{c}")
        for d in active_ret_dates():
            keys.append(f"ow|ret|{d}|{c}")
        if ROUND_TRIP:
            for d in active_out_dates():
                if any(pair_ok(d, r) for r in active_ret_dates()):
                    keys.append(f"rt|{d}|{c}")
    return keys


def key_group(key):
    p = key.split("|")
    return f"{p[0]}|{p[1]}|{p[-1]}" if p[0] == "ow" else f"rt|{p[-1]}"


# ----------------------------------------------------------------------------
# SerpApi
# ----------------------------------------------------------------------------


def http_json(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "flightwatch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        try:
            msg = json.loads(body).get("error", body)
        except Exception:
            msg = body
        raise RuntimeError(f"HTTP {e.code}: {msg}")


def serp_search(params):
    if FAKE_MODE:
        return fake_search(params)
    q = dict(params, engine="google_flights", api_key=SERPAPI_KEY)
    return http_json("https://serpapi.com/search.json?" + urllib.parse.urlencode(q))


def serp_account():
    if FAKE_MODE:
        return fake_account()
    return http_json("https://serpapi.com/account.json?" + urllib.parse.urlencode({"api_key": SERPAPI_KEY}), 30)


def base_params(cls):
    return {
        "currency": CURRENCY, "hl": "en", "gl": "uk", "adults": 1,
        "travel_class": CLASS_PARAM[cls],
        # 给 Google 的上限放宽 3 小时，真正的 17 小时过滤在本地做
        "max_duration": MAX_MIN + 180,
        "show_hidden": "true", "deep_search": "true",
    }


# ----------------------------------------------------------------------------
# 解析和过滤
# ----------------------------------------------------------------------------


def seg_rank(seg):
    return CLASS_RANK.get(str(seg.get("travel_class", "")).strip().lower(), 1)


def parse_option(o):
    segs = []
    for s in o.get("flights", []):
        segs.append({
            "from": s.get("departure_airport", {}).get("id"),
            "to": s.get("arrival_airport", {}).get("id"),
            "dep": s.get("departure_airport", {}).get("time"),
            "arr": s.get("arrival_airport", {}).get("time"),
            "airline": s.get("airline"),
            "no": s.get("flight_number"),
            "cls": seg_rank(s),
            "dur": s.get("duration") or 0,
            "plane": s.get("airplane"),
        })
    lays = [{"at": l.get("id"), "dur": l.get("duration"), "overnight": bool(l.get("overnight"))}
            for l in o.get("layovers", []) or []]
    total = o.get("total_duration") or (sum(s["dur"] for s in segs) + sum(l["dur"] or 0 for l in lays))
    return {"price": o.get("price"), "total": total, "segs": segs, "lays": lays,
            "token": o.get("departure_token")}


def cabin_label(opt):
    ranks = sorted({s["cls"] for s in opt["segs"]}, reverse=True)
    return "全程" + CLASS_CN[ranks[0]] if len(ranks) == 1 else "+".join(CLASS_CN[r] for r in ranks)


def is_valid(opt, required_rank):
    """时长 ≤ 上限；所有长途段必须达到要求舱位；至少最长的那段达到要求舱位。"""
    if not opt["segs"] or not isinstance(opt["price"], (int, float)):
        return False
    if opt["total"] > MAX_MIN:
        return False
    longest = max(opt["segs"], key=lambda s: s["dur"])
    if longest["cls"] < required_rank:
        return False
    for s in opt["segs"]:
        if s["dur"] >= LONG_SEG_MIN and s["cls"] < required_rank:
            return False
    return True


def collect(res, required_rank):
    raw = (res.get("best_flights") or []) + (res.get("other_flights") or [])
    opts = [parse_option(o) for o in raw]
    valid = [o for o in opts if is_valid(o, required_rank)]
    valid.sort(key=lambda o: o["price"])
    for o in valid:
        o["cabin"] = cabin_label(o)
    return valid, len(opts)


def no_results_error(res):
    err = res.get("error")
    return err and "hasn't returned any results" in err


# ----------------------------------------------------------------------------
# 执行一次查询
# ----------------------------------------------------------------------------


def latest_by_key(rows=None, include_failed=False):
    rows = rows if rows is not None else all_checks()
    latest = {}
    for r in rows:
        if r["ok"] or include_failed:
            latest[r["key"]] = r
    return latest


def run_query(key):
    p = key.split("|")
    if p[0] == "ow":
        _, leg, d, cls = p
        params = base_params(cls)
        params.update({"type": 2, "outbound_date": d})
        if leg == "out":
            params.update({"departure_id": ORIGINS, "arrival_id": DESTS})
        else:
            params.update({"departure_id": DESTS, "arrival_id": ORIGINS})
        res = serp_search(params)
        if res.get("error") and not no_results_error(res):
            raise RuntimeError(res["error"])
        valid, n = collect(res, CLASS_PARAM[cls])
        best = valid[0] if valid else None
        if best:
            best.pop("token", None)
        save_check(key, True, best["price"] if best else None,
                   {"best": best, "n_valid": len(valid), "n_total": n,
                    "alts": [{"price": o["price"], "cabin": o["cabin"], "total": o["total"],
                              "airline": o["segs"][0]["airline"]} for o in valid[1:4]]})
        log(f"{key}: {len(valid)}/{n} 个符合条件, 最低 {money(best['price']) if best else '—'}")
        return 1

    # 往返：回程日期选当前单程最便宜的一天
    _, d, cls = p
    ret = pick_rt_return(date.fromisoformat(d), cls)
    params = base_params(cls)
    params.update({"type": 1, "outbound_date": d, "return_date": str(ret),
                   "departure_id": ORIGINS, "arrival_id": DESTS})
    res = serp_search(params)
    if res.get("error") and not no_results_error(res):
        raise RuntimeError(res["error"])
    valid, n = collect(res, CLASS_PARAM[cls])
    used = 1
    data = {"ret_date": str(ret), "n_valid": len(valid), "n_total": n, "out": None, "ret": None, "verified": False}
    price = None
    if valid:
        cand = valid[0]
        data["out"] = {k: v for k, v in cand.items() if k != "token"}
        data["unverified_price"] = cand["price"]
        cur_best = current_best_price(f"rt_{cls}")
        # 只有可能成为这个舱位往返的最低价时，才多花一次额度去核实回程（时长 + 真实总价）
        if cand.get("token") and (cur_best is None or cand["price"] < cur_best * 1.05):
            rparams = dict(params, departure_token=cand["token"])
            rres = serp_search(rparams)
            used += 1
            if rres.get("error") and not no_results_error(rres):
                raise RuntimeError("回程核实: " + rres["error"])
            rvalid, _ = collect(rres, CLASS_PARAM[cls])
            data["verified"] = True
            if rvalid:
                r0 = rvalid[0]
                r0.pop("token", None)
                data["ret"] = r0
                price = r0["price"]
    save_check(key, True, price, data)
    log(f"{key} (回 {ret}): 去程 {len(valid)}/{n} 个符合, "
        f"{'核实后往返 ' + money(price) if price else ('未核实 ' + money(valid[0]['price']) if valid else '无结果')}")
    return used


def pick_rt_return(out_d, cls):
    latest = latest_by_key()
    best, best_d = None, None
    for r in active_ret_dates() or RET_DATES:
        if not pair_ok(out_d, r):
            continue
        row = latest.get(f"ow|ret|{r}|{cls}")
        if row and row["price"] and (best is None or row["price"] < best):
            best, best_d = row["price"], r
    if best_d:
        return best_d
    cands = [r for r in (active_ret_dates() or RET_DATES) if pair_ok(out_d, r)]
    return cands[len(cands) // 2]


# ----------------------------------------------------------------------------
# 汇总：拼接所有日期组合，算出当前最低价
# ----------------------------------------------------------------------------


def leg_price(latest, leg, d, cls):
    """某一天某方向某舱位最便宜的单程。"""
    row = latest.get(f"ow|{leg}|{d}|{cls}")
    if row and row["price"]:
        return {"price": row["price"], "cls": cls, "opt": json.loads(row["data"])["best"], "ts": row["ts"]}
    return None


def summarize(latest):
    """
    每个日期组合算两个价格：
      - price：两程长途段都是首选舱位（默认商务舱）的最低价  ← 看板和提醒优先用这个
      - price_all：只要是商务或超经都算的最低价
    """
    cells, strategies = [], {}

    def keep(sk, cnd, o, r):
        if sk not in strategies or cnd["price"] < strategies[sk]["price"]:
            strategies[sk] = dict(cnd, out_date=str(o), ret_date=str(r))

    for o in OUT_DATES:
        for r in RET_DATES:
            if not pair_ok(o, r):
                continue
            cands = []
            for co in CLASSES:
                a = leg_price(latest, "out", o, co)
                if not a:
                    continue
                for cr in CLASSES:
                    b = leg_price(latest, "ret", r, cr)
                    if not b:
                        continue
                    cands.append({"price": a["price"] + b["price"], "kind": "ow",
                                  "label": (f"单程拼接（全程{CLASS_KEY_CN[co]}）" if co == cr else
                                            f"单程拼接（去{CLASS_CN[CLASS_PARAM[co]]}·回{CLASS_CN[CLASS_PARAM[cr]]}）"),
                                  "out": a["opt"], "ret": b["opt"], "out_cls": co, "ret_cls": cr,
                                  "ts": min(a["ts"], b["ts"])})
            for c in CLASSES:
                row = latest.get(f"rt|{o}|{c}")
                if row and row["price"]:
                    data = json.loads(row["data"])
                    if data.get("ret_date") == str(r):
                        cands.append({"price": row["price"], "kind": "rt",
                                      "label": f"往返{CLASS_KEY_CN[c]}", "out": data["out"], "ret": data["ret"],
                                      "out_cls": c, "ret_cls": c, "ts": row["ts"]})
            cell = {"out": str(o), "ret": str(r), "stay": (r - o).days, "price": None, "price_all": None}
            prim = [c for c in cands if c["out_cls"] == PRIMARY and c["ret_cls"] == PRIMARY]
            if prim:
                bp = min(prim, key=lambda x: x["price"])
                cell.update(price=bp["price"], kind=bp["kind"], label=bp["label"],
                            detail=dict(bp, out_date=str(o), ret_date=str(r)))
            if cands:
                ba = min(cands, key=lambda x: x["price"])
                cell.update(price_all=ba["price"], kind_all=ba["kind"], label_all=ba["label"],
                            detail_all=dict(ba, out_date=str(o), ret_date=str(r)))
            cells.append(cell)
            for cnd in cands:
                if cnd["kind"] == "rt":
                    keep(f"rt_{cnd['out_cls']}", cnd, o, r)
                elif cnd in prim:
                    keep("ow_primary", cnd, o, r)
                if cnd not in prim:
                    keep("alt", cnd, o, r)  # 至少有一程是超经的最便宜组合
    pp = [c for c in cells if c["price"]]
    pa = [c for c in cells if c["price_all"]]
    return {"cells": cells, "strategies": strategies,
            "best": min(pp, key=lambda c: c["price"])["detail"] if pp else None,
            "best_all": min(pa, key=lambda c: c["price_all"])["detail_all"] if pa else None}


def current_best_price(strategy=None):
    s = summarize(latest_by_key())
    if strategy:
        return s["strategies"][strategy]["price"] if strategy in s["strategies"] else None
    return s["best"]["price"] if s["best"] else None


def build_series(rows):
    # 第一轮查询期间价格会大幅跳动（还没查全），走势图从第一轮基本查完之后开始画
    total_keys = len({r["key"] for r in rows})
    seen, started = set(), False
    latest, series, last = {}, [], None
    for r in rows:
        seen.add(r["key"])
        if not r["ok"]:
            continue
        latest[r["key"]] = r
        if not started:
            started = len(seen) >= 0.9 * total_keys
            if not started:
                continue
        s = summarize(latest)["strategies"]
        point = {k: (s[k]["price"] if k in s else None) for k in [f"rt_{PRIMARY}", "ow_primary", "alt"]}
        sig = tuple(point.values())
        if sig != last:
            series.append(dict(point, ts=r["ts"]))
            last = sig
    if series:
        series.append(dict(series[-1], ts=time.time()))
    return series


# ----------------------------------------------------------------------------
# 推送
# ----------------------------------------------------------------------------


def notify(title, message, priority=3, tags=None):
    log("通知:", title, "|", message.replace("\n", " / "))
    if not NTFY_TOPIC:
        return
    body = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": priority,
            "tags": tags or ["airplane"]}
    if DASHBOARD_URL:
        body["click"] = DASHBOARD_URL
    try:
        req = urllib.request.Request(NTFY_SERVER, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        log("ntfy 推送失败:", e)


def fmt_dt(s):
    # "2026-12-12 13:05" -> "12/12 13:05"
    try:
        d = datetime.strptime(s, "%Y-%m-%d %H:%M")
        return f"{d.month}/{d.day} {d:%H:%M}"
    except Exception:
        return s or ""


def describe_leg(opt):
    if not opt:
        return "回程未核实"
    s0, s1 = opt["segs"][0], opt["segs"][-1]
    via = "直飞" if len(opt["segs"]) == 1 else "经" + "、".join(l["at"] for l in opt["lays"])
    airlines = "/".join(dict.fromkeys(s["airline"] for s in opt["segs"]))
    return (f"{s0['from']} {fmt_dt(s0['dep'])} → {s1['to']} {fmt_dt(s1['arr'])}，"
            f"{opt['total'] // 60}h{opt['total'] % 60:02d}m，{via}，{airlines}，{opt['cabin']}")


def describe_best(b):
    return (f"{b['out_date'][5:]} 去 · {b['ret_date'][5:]} 回 · {b['label']}\n"
            f"去：{describe_leg(b['out'])}\n回：{describe_leg(b['ret'])}")


def drop_alert(ref_key, price):
    """返回 (是否提醒, 之前的参考价)。价格涨了就把参考点上调，之后再降就能提醒。"""
    ref = kv_get(ref_key)
    if ref is None or price > ref:
        kv_set(ref_key, price)
        return False, ref
    if price <= ref - ALERT_MIN_DROP:
        kv_set(ref_key, price)
        return True, ref
    return False, ref


def check_alerts(sweep_done=True):
    if not sweep_done:
        return  # 第一轮还没查完时价格会不断刷新，先不推送
    s = summarize(latest_by_key())
    b, alt = s["best"], s["strategies"].get("alt")
    name = CLASS_KEY_CN[PRIMARY]

    if not kv_get("started"):
        if not (b or alt):
            return
        kv_set("started", True)
        if b:
            kv_set("alert_ref", b["price"])
        if alt:
            kv_set("alert_ref_alt", alt["price"])
        msg = describe_best(b) if b else f"暂时没有符合条件的{name}组合"
        if alt and (not b or alt["price"] < b["price"]):
            msg += f"\n\n含超经最低：{money(alt['price'])}（{alt['out_date'][5:]} 去 · {alt['ret_date'][5:]} 回 · {alt['label']}）"
        notify(f"✈️ 监控已开始：{name}最低 {money(b['price']) if b else '—'}", msg, 3)
        return

    # 1) 首选舱位（商务舱）—— 主要提醒
    if b:
        price = b["price"]
        fire, ref = drop_alert("alert_ref", price)
        target_hit = TARGET_PRICE and price <= TARGET_PRICE
        if fire:
            notify(f"📉 {name}降价：{money(price)}（之前 {money(ref)}）", describe_best(b),
                   5 if target_hit else 4, ["chart_with_downwards_trend"])
        if target_hit and not kv_get("target_alerted"):
            kv_set("target_alerted", True)
            notify(f"🎯 {name}低于目标价 {money(TARGET_PRICE)}：{money(price)}", describe_best(b), 5, ["dart"])
        if not target_hit and kv_get("target_alerted"):
            kv_set("target_alerted", False)

    # 2) 含超经的组合 —— 只在比商务舱便宜时才提醒，优先级低一些
    if alt:
        fire, ref = drop_alert("alert_ref_alt", alt["price"])
        if fire and (not b or alt["price"] < b["price"]):
            save = f"，比{name}便宜 {money(b['price'] - alt['price'])}" if b else ""
            notify(f"超经组合降价：{money(alt['price'])}（之前 {money(ref)}）{save}", describe_best(alt), 2,
                   ["seat"])


def maybe_digest():
    if DIGEST_HOUR_UTC < 0:
        return
    now = datetime.now(timezone.utc)
    today = str(now.date())
    if now.hour < DIGEST_HOUR_UTC or kv_get("digest_day") == today:
        return
    kv_set("digest_day", today)
    s = summarize(latest_by_key())
    b, alt = s["best"], s["strategies"].get("alt")
    if not (b or alt):
        return
    name = CLASS_KEY_CN[PRIMARY]
    top = sorted([c for c in s["cells"] if c["price"]], key=lambda c: c["price"])[:3]
    lines = [f"{c['out'][5:]}→{c['ret'][5:]}  {money(c['price'])}  {c['label']}" for c in top]
    if alt:
        lines.append(f"含超经最低：{money(alt['price'])}（{alt['out_date'][5:]}→{alt['ret_date'][5:]} {alt['label']}）")
    left = STATUS.get("left")
    if left is not None:
        lines.append(f"剩余额度 {left} 次")
    notify(f"☀️ 今日{name}最低 {money(b['price']) if b else '—'}", "\n".join(lines), 2, ["sunny"])


# ----------------------------------------------------------------------------
# 调度：按额度平均分配，优先刷新便宜的、最久没查的
# ----------------------------------------------------------------------------


def pick_next(keys, latest_all, attempts):
    now = time.time()
    group_min = {}
    for k in keys:
        r = latest_all.get(k)
        if r and r["price"]:
            g = key_group(k)
            group_min[g] = min(group_min.get(g, 1e18), r["price"])
    best_k, best_score = None, -1
    for k in keys:
        a = attempts.get(k)
        if not a:
            return k  # 从没查过的最优先
        r = latest_all.get(k)
        age = now - a["ts"]  # 失败的尝试也算，避免同一个查询反复失败一直烧额度
        if not r or r["price"] is None:
            w = 0.5
        elif r["price"] <= group_min[key_group(k)] * 1.08:
            w = 3.0
        else:
            w = 1.0
        score = age * w
        if score > best_score:
            best_k, best_score = k, score
    return best_k


def budget():
    acct = serp_account()
    left = acct.get("total_searches_left")
    if left is None:
        left = acct.get("plan_searches_left", 0)
    ren = acct.get("plan_renewal_date")
    now = datetime.now(timezone.utc)
    renewal = (datetime.strptime(ren, "%Y-%m-%d").replace(tzinfo=timezone.utc) if ren else now + timedelta(days=30))
    stop = datetime.combine(STOP_DATE + timedelta(days=1), datetime.min.time(), timezone.utc)
    horizon = max(min(renewal, stop) - now, timedelta(hours=12))
    usable = max(0, left - RESERVE)
    per_day = usable / (horizon.total_seconds() / 86400)
    STATUS.update(left=left, renewal=renewal.isoformat(), per_day=round(per_day, 1),
                  hourly_limit=acct.get("account_rate_limit_per_hour"))
    return left, usable, per_day, renewal


def scheduler():
    while True:
        try:
            if datetime.now(timezone.utc).date() > STOP_DATE:
                STATUS.update(paused_reason="已过截止日期，停止查询", next_run=None)
                time.sleep(3600)
                continue
            nr = STATUS.get("next_run")
            if nr and time.time() < nr:
                maybe_digest()
                time.sleep(20)
                continue

            left, usable, per_day, renewal = budget()
            keys = query_keys()
            if usable <= 0 or not keys:
                STATUS.update(paused_reason="额度用完，等额度刷新" if usable <= 0 else "没有需要查询的日期",
                              next_run=min(renewal.timestamp() + 600, time.time() + 6 * 3600))
                continue

            rows = all_checks()
            key = pick_next(keys, latest_by_key(rows), latest_by_key(rows, include_failed=True))
            try:
                run_query(key)
                STATUS["last_error"] = None
            except Exception as e:
                save_check(key, False, error=str(e)[:500])
                STATUS["last_error"] = f"{key}: {e}"
                log("查询失败", key, e)
            STATUS["last_run"] = time.time()
            left, usable, per_day, renewal = budget()
            attempts = latest_by_key(include_failed=True)
            unchecked = [k for k in keys if k not in attempts]
            check_alerts(sweep_done=not unchecked)
            maybe_digest()
            if unchecked and usable >= 1.5 * len(unchecked):
                gap = env_int("SWEEP_GAP_SECONDS", 3)  # 第一轮：所有组合先快速查一遍
            elif per_day <= 0:
                gap = 6 * 3600
            else:
                gap = max(MIN_INTERVAL, 86400 / per_day)
            STATUS.update(next_run=time.time() + gap, paused_reason=None)
        except Exception as e:
            STATUS["last_error"] = str(e)
            log("调度出错:", e)
            traceback.print_exc()
            STATUS["next_run"] = time.time() + 600


# ----------------------------------------------------------------------------
# 网页看板
# ----------------------------------------------------------------------------


def gf_link(frm, to, d, cls, ret=None):
    cls_txt = {"business": "business class", "premium": "premium economy", "first": "first class"}.get(cls, "")
    q = f"Flights from {frm} to {to} on {d}" + (f" returning {ret}" if ret else " one way") + f" {cls_txt}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q) + f"&curr={CURRENCY}"


def api_data():
    rows = all_checks()
    latest = latest_by_key(rows)
    s = summarize(latest)
    origin1, dest1 = ORIGINS.split(",")[0], DESTS.split(",")[0]

    def add_links(b):
        if not b:
            return b
        b = dict(b)
        if b["kind"] == "rt":
            b["links"] = [{"t": "往返", "u": gf_link(origin1, dest1, b["out_date"], b["out_cls"], b["ret_date"])}]
        else:
            b["links"] = [{"t": "去程", "u": gf_link(origin1, dest1, b["out_date"], b["out_cls"])},
                          {"t": "回程", "u": gf_link(dest1, origin1, b["ret_date"], b["ret_cls"])}]
        return b

    legs = {"out": [], "ret": []}
    for leg, dates in (("out", OUT_DATES), ("ret", RET_DATES)):
        for d in dates:
            row = {"date": str(d)}
            for c in CLASSES:
                r = latest.get(f"ow|{leg}|{d}|{c}")
                if r:
                    data = json.loads(r["data"])
                    row[c] = {"price": r["price"], "ts": r["ts"], "opt": data.get("best"),
                              "n": data.get("n_valid")}
            legs[leg].append(row)
    rts = []
    if ROUND_TRIP:
        for d in OUT_DATES:
            row = {"date": str(d)}
            for c in CLASSES:
                r = latest.get(f"rt|{d}|{c}")
                if r:
                    data = json.loads(r["data"])
                    row[c] = {"price": r["price"], "ts": r["ts"], "ret_date": data.get("ret_date"),
                              "verified": data.get("verified"), "unverified": data.get("unverified_price")}
            rts.append(row)
    errors = [r for r in rows[-50:] if not r["ok"]]
    return {
        "now": time.time(), "currency": CURRENCY, "symbol": SYM, "fake": FAKE_MODE,
        "config": {"origins": ORIGINS, "dests": DESTS, "out": [str(d) for d in OUT_DATES],
                   "ret": [str(d) for d in RET_DATES], "classes": CLASSES, "max_hours": MAX_MIN // 60,
                   "target": TARGET_PRICE, "stop": str(STOP_DATE), "round_trip": ROUND_TRIP},
        "status": dict(STATUS, total_checks=len(rows), n_queries=len(query_keys()),
                       last_errors=[{"key": e["key"], "ts": e["ts"], "error": e["error"]} for e in errors[-3:]]),
        "primary": PRIMARY, "primary_name": CLASS_KEY_CN[PRIMARY],
        "best": add_links(s["best"]), "best_all": add_links(s["best_all"]),
        "strategies": {k: add_links(v) for k, v in s["strategies"].items()},
        "cells": [{k: v for k, v in c.items() if not k.startswith("detail")} for c in s["cells"]],
        "legs": legs, "rt": rts, "series": build_series(rows),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self.send(200, f.read(), "text/html; charset=utf-8")
            elif path in ("/api/data", "/data.json"):
                with LOCK:
                    data = api_data()
                self.send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            elif path == "/health":
                self.send(200, b"ok", "text/plain")
            else:
                self.send(404, b"not found", "text/plain")
        except Exception as e:
            traceback.print_exc()
            self.send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")


# ----------------------------------------------------------------------------
# 演示模式：不用 API key，生成假数据，方便先看看效果
# ----------------------------------------------------------------------------

_FAKE = {"left": 250}
AIRLINES = [("China Eastern", "MU", "PVG"), ("Air China", "CA", "PEK"), ("Virgin Atlantic", "VS", None),
            ("British Airways", "BA", None), ("Emirates", "EK", "DXB"), ("Finnair", "AY", "HEL"),
            ("Cathay Pacific", "CX", "HKG"), ("Turkish Airlines", "TK", "IST")]


def fake_account():
    return {"total_searches_left": _FAKE["left"], "plan_searches_left": _FAKE["left"],
            "plan_renewal_date": str(datetime.now(timezone.utc).date() + timedelta(days=21)),
            "account_rate_limit_per_hour": 50}


def fake_search(params):
    _FAKE["left"] -= 1
    cls = params["travel_class"]
    d = params["outbound_date"]
    tok = params.get("departure_token")
    outbound = params["departure_id"].startswith("LHR") or params["departure_id"].startswith("LGW")
    if tok:
        outbound = False
        d = params["return_date"]
    seed = f"{d}|{cls}|{params['type']}|{bool(tok)}|{int(time.time() // 1800)}"
    rnd = random.Random(seed)
    base = {3: 1450, 2: 720}.get(cls, 500)
    day = int(d[-2:])
    base *= 1 + 0.04 * (day % 5) + (0.08 if day in (23, 24) else 0)
    if params["type"] == 1 and not tok:
        base *= 1.7
    if tok:
        base = params.get("_rt_base", base * 1.7)
    opts = []
    for i in range(rnd.randint(6, 12)):
        name, code, hub = rnd.choice(AIRLINES)
        ih = 0 if rnd.random() < 0.6 else 1
        start = datetime.strptime(d, "%Y-%m-%d") + timedelta(hours=rnd.choice([9, 11, 13, 16, 19, 21]), minutes=rnd.choice([0, 15, 35, 50]))
        frm, to = ("LHR", "PVG") if outbound else ("PVG", "LHR")
        segs, lays = [], []
        if hub is None or (hub == "PVG"):
            dur = rnd.randint(11 * 60 + 40, 13 * 60 + 10)
            segs.append((frm, to, start, dur, cls, name, code))
        else:
            long_d, short_d = rnd.randint(9 * 60, 11 * 60), rnd.randint(110, 7 * 60 + 30)
            lay = rnd.randint(70, 600)
            if hub in ("PEK",):
                short_d = rnd.randint(120, 150)
            legs = [(frm, hub), (hub, to)] if outbound else [(frm, hub), (hub, to)]
            if (hub == "PEK") == outbound:
                d1, d2 = long_d, short_d
            else:
                d1, d2 = short_d, long_d
            if hub in ("DXB", "IST", "HEL"):
                d1, d2 = (rnd.randint(4 * 60, 7 * 60 + 30), rnd.randint(8 * 60, 10 * 60)) if outbound else (rnd.randint(8 * 60, 10 * 60), rnd.randint(4 * 60, 7 * 60 + 30))
            c1 = c2 = cls
            if hub == "PEK" and rnd.random() < 0.6:
                if outbound:
                    c2 = 1
                else:
                    c1 = 1
            if rnd.random() < 0.12:
                c1 = c2 = 1 if rnd.random() < 0.5 else cls
                if c1 == cls:
                    c1 = 1
            t2 = start + timedelta(minutes=d1 + lay)
            segs.append((legs[0][0], legs[0][1], start, d1, c1, name, code))
            segs.append((legs[1][0], legs[1][1], t2, d2, c2, name, code))
            lays.append({"id": hub, "duration": lay, "overnight": lay > 480})
        flights = []
        for (a, b, t, du, c, nm, cd) in segs:
            flights.append({"departure_airport": {"id": a, "time": t.strftime("%Y-%m-%d %H:%M")},
                            "arrival_airport": {"id": b, "time": (t + timedelta(minutes=du + (480 if outbound else -480))).strftime("%Y-%m-%d %H:%M")},
                            "duration": du, "airline": nm, "flight_number": f"{cd} {rnd.randint(200, 999)}",
                            "travel_class": {1: "Economy", 2: "Premium economy", 3: "Business"}[c],
                            "airplane": rnd.choice(["Boeing 787", "Airbus A350", "Boeing 777"])})
        total = sum(f["duration"] for f in flights) + sum(l["duration"] for l in lays)
        price = round(base * rnd.uniform(0.85, 1.6) * (0.92 if len(segs) > 1 else 1.08))
        opts.append({"flights": flights, "layovers": lays, "total_duration": total, "price": price,
                     "departure_token": f"fake-{d}-{i}" if params["type"] == 1 and not tok else None})
    return {"best_flights": opts[:3], "other_flights": opts[3:]}


# ----------------------------------------------------------------------------


def run_once():
    """
    GitHub Actions 模式：每次被定时唤醒时跑一次，然后退出。
    用一个「额度余额」把每天可用的查询次数平均分到每次运行里。
    """
    import shutil
    saved = kv_get("status", {})
    STATUS.update({k: v for k, v in saved.items() if k != "started"})
    if datetime.now(timezone.utc).date() > STOP_DATE:
        log("已过截止日期，不再查询")
        STATUS.update(paused_reason="已过截止日期，停止查询")
    else:
        left, usable, per_day, renewal = budget()
        keys = query_keys()
        attempts = latest_by_key(include_failed=True)
        unchecked = [k for k in keys if k not in attempts]
        credit = kv_get("credit", 0.0) + per_day * RUN_INTERVAL_HOURS / 24
        if unchecked and usable >= 1.5 * len(unchecked):
            n, credit = len(unchecked), 0.0  # 第一轮：全部查一遍
        else:
            n = min(MAX_PER_RUN, int(credit), usable)
        log(f"剩余额度 {left}，每天约 {per_day:.1f} 次，本次查询 {n} 个")
        used = 0
        for _ in range(n):
            rows = all_checks()
            key = pick_next(keys, latest_by_key(rows), latest_by_key(rows, include_failed=True))
            try:
                used += run_query(key)
                STATUS["last_error"] = None
            except Exception as e:
                used += 1
                save_check(key, False, error=str(e)[:500])
                STATUS["last_error"] = f"{key}: {e}"
                log("查询失败", key, e)
        if not unchecked:
            credit = max(0.0, credit - used)
        kv_set("credit", min(credit, MAX_PER_RUN))
        STATUS["last_run"] = time.time()
        attempts = latest_by_key(include_failed=True)
        check_alerts(sweep_done=all(k in attempts for k in keys))
        maybe_digest()
        budget()
        STATUS["next_run"] = time.time() + RUN_INTERVAL_HOURS * 3600
    kv_set("status", {k: v for k, v in STATUS.items() if k != "started"})
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "data.json"), "w", encoding="utf-8") as f:
        json.dump(api_data(), f, ensure_ascii=False)
    shutil.copyfile(os.path.join(HERE, "index.html"), os.path.join(OUTPUT_DIR, "index.html"))
    log(f"看板已更新：{OUTPUT_DIR}/")


def main():
    import sys
    if not SERPAPI_KEY and not FAKE_MODE:
        raise SystemExit("缺少 SERPAPI_KEY。去 https://serpapi.com 注册免费账号拿 key，或者设置 FAKE_MODE=true 先看演示。")
    init_db()
    if "--once" in sys.argv:
        run_once()
        return
    log(f"FlightWatch 启动：{ORIGINS} ⇄ {DESTS}，去 {OUT_DATES[0]}~{OUT_DATES[-1]}，回 {RET_DATES[0]}~{RET_DATES[-1]}，"
        f"舱位 {CLASSES}，单程 ≤{MAX_MIN // 60}h，共 {len(query_keys())} 个查询{'（演示模式）' if FAKE_MODE else ''}")
    threading.Thread(target=scheduler, daemon=True).start()
    log(f"看板: http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
