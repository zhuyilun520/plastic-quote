#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
塑料原料行情 - 数据抓取脚本

数据源（全部免费公开）:
- 新浪财经: 期货实时行情 / 期货历史K线 / 7x24财经快讯
- 生意社:   现期表(现货价) / 现期图(现货历史) / 报价中心(ABS、GPPS) / 商品资讯

输出: docs/data.json
失败策略: 任何一块抓取失败时沿用上一次 data.json 的对应数据, 并标记 stale, 不编造数字。
"""

import gzip
import json
import os
import re
import sys
import time
import urllib.request

from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "docs")
DATA_PATH = os.path.join(DATA_DIR, "data.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CST = timezone(timedelta(hours=8))

# 新浪行情代码
SINA_REALTIME = "https://hq.sinajs.cn/list=" + ",".join(
    ["hf_CL", "hf_OIL", "nf_PP0", "nf_L0", "nf_V0", "nf_SC0"])
SINA_INNER_KLINE = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
                    "var%20_=/InnerFuturesNewService.getDailyKLine?symbol=")
SINA_GLOBAL_KLINE = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
                     "var%20_=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=")
SINA_ZHIBO = ("https://zhibo.sina.com.cn/api/zhibo/feed?"
              "page={page}&page_size=40&zhibo_id=152&tag_id=0&dire=f&dpc=1")

# 生意社 Cookie(其 JS 安全检查要求), 失效时脚本会自动重新提取
PPI_COOKIE = ["HW_CHECK=45dad6d31047b2ec7923089de786c519"]
PPI_HEADERS = {"Referer": "https://www.100ppi.com/"}


def now_cst():
    return datetime.now(CST)


def http_get(url, headers=None, timeout=35, retries=3):
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def http_text(url, encoding="utf-8", headers=None, timeout=35):
    return http_get(url, headers=headers, timeout=timeout).decode(encoding, errors="replace")


def ppi_get(path, timeout=35):
    """访问生意社页面, 自动处理其 HW_CHECK 安全检查 Cookie。"""
    url = path if path.startswith("http") else "https://www.100ppi.com" + path
    for _ in range(2):
        headers = dict(PPI_HEADERS)
        headers["Cookie"] = PPI_COOKIE[0]
        html = http_text(url, headers=headers, timeout=timeout)
        if "正在进行安全检查" not in html:
            return html
        m = re.search(r'var _0x2 = "([0-9a-f]+)"', html)
        if m:
            PPI_COOKIE[0] = "HW_CHECK=" + m.group(1)
    raise RuntimeError("生意社安全检查未能通过: " + url)


# ---------------------------------------------------------------------------
# 新浪财经期货
# ---------------------------------------------------------------------------

def fetch_sina_realtime():
    text = http_text(SINA_REALTIME, encoding="gbk",
                     headers={"Referer": "https://finance.sina.com.cn"})
    out = {}
    for code in ("hf_CL", "hf_OIL", "nf_PP0", "nf_L0", "nf_V0", "nf_SC0"):
        m = re.search(r'var hq_str_%s="([^"]*)"' % code, text)
        if not m or not m.group(1).strip():
            continue
        p = m.group(1).split(",")
        try:
            if code.startswith("hf_"):
                price = float(p[0])
                prev = float(p[7])
                rec = {"price": price, "prev_settle": prev,
                       "change": round(price - prev, 4),
                       "pct": round((price - prev) / prev * 100, 2) if prev else None,
                       "date": p[12], "name": p[13]}
            else:
                price = float(p[8]) or float(p[10])
                prev = float(p[10])
                rec = {"price": price, "prev_settle": prev,
                       "change": round(price - prev, 2),
                       "pct": round((price - prev) / prev * 100, 2) if prev else None,
                       "date": p[17], "name": p[0],
                       "open": float(p[2]), "high": float(p[3]), "low": float(p[4])}
            out[code] = rec
        except (ValueError, IndexError):
            continue
    return out


def _parse_kline(text, date_key):
    start = text.find("[{")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise RuntimeError("K线数据格式异常")
    arr = json.loads(text[start:end + 1])
    hist = []
    for x in arr:
        # 外盘接口字段为 close, 内盘接口字段为 c
        close = x.get("close") if x.get("close") is not None else x.get("c")
        try:
            hist.append({"d": x[date_key], "c": float(close)})
        except (KeyError, ValueError, TypeError):
            continue
    # 去重按日期排序, 只保留最近 180 个交易日
    seen = {}
    for x in hist:
        seen[x["d"]] = x["c"]
    items = sorted(seen.items(), key=lambda kv: kv[0])[-180:]
    return [{"d": d, "c": round(v, 2)} for d, v in items]


def fetch_sina_klines():
    out = {}
    inner = [("PP", "PP0"), ("L", "L0"), ("V", "V0"), ("SC", "SC0")]
    for name, sym in inner:
        text = http_text(SINA_INNER_KLINE + sym,
                         headers={"Referer": "https://finance.sina.com.cn"})
        out[name] = _parse_kline(text, "d")
    for name, sym in (("BRENT", "OIL"), ("WTI", "CL")):
        text = http_text(SINA_GLOBAL_KLINE + sym,
                         headers={"Referer": "https://finance.sina.com.cn"})
        out[name] = _parse_kline(text, "date")
    return out


# ---------------------------------------------------------------------------
# 生意社: 现货价格
# ---------------------------------------------------------------------------

def fetch_ppi_sf():
    """现期表: PP/PE/PVC 现货价 + 主力期货结算价 + 基差。"""
    html = ppi_get("/sf/")
    tm = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", html)
    as_of = ("%04d-%02d-%02d" % (int(tm.group(1)), int(tm.group(2)), int(tm.group(3)))
             if tm else now_cst().strftime("%Y-%m-%d"))
    result = {}
    mapping = {"聚丙烯": "PP", "聚乙烯": "PE", "聚氯乙烯": "PVC"}
    # 行内含嵌套表格(基差), 不能用非贪婪 </tr> 截取; 按商品锚点区间解析
    anchors = list(re.finditer(
        r'<a href="https://www\.100ppi\.com/sf/\d+\.html"[^>]*>([^<]+)</a></td>',
        html))
    for i, m in enumerate(anchors):
        cname = m.group(1).strip()
        code = mapping.get(cname)
        if not code or code in result:
            continue
        start = m.end()
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
        seg = html[start:end]
        sm = re.search(r"<td>([\d.]+)&nbsp;</td>", seg)
        pairs = re.findall(r"<td>(\d{4})&nbsp;</td>\s*<td>([\d.]+)&nbsp;</td>", seg)
        if not sm or not pairs:
            continue
        result[code] = {
            "name": cname,
            "spot": float(sm.group(1)),
            "near_contract": pairs[0][0],
            "near_price": float(pairs[0][1]),
            "main_contract": pairs[-1][0],
            "main_price": float(pairs[-1][1]),
            "date": as_of,
            "source": "生意社现期表",
        }
    return result


def _fix_year(mmdd, today):
    mm, dd = (int(v) for v in mmdd.split("-"))
    y = today.year
    if (mm, dd) > (today.month, today.day):
        y -= 1
    return "%04d-%02d-%02d" % (y, mm, dd)


def fetch_ppi_spot_history(ppid):
    """现期图页注释里的现货价采样序列(约 90 天, 10 天一个点)。"""
    html = ppi_get("/sf/%s.html" % ppid)
    m = re.search(r'<td height="25">[^<]*现货价格</td>(.*?)</tr>', html, re.S)
    m2 = re.search(r'<td[^>]*height="25">[^<]*日期</td>(.*?)</tr>', html, re.S)
    if not m or not m2:
        raise RuntimeError("现期图数据结构异常: %s" % ppid)
    vals = re.findall(r"<td>([\d.]+|-)</td>", m.group(1))
    dates = re.findall(r"<td>(\d{2}-\d{2})</td>", m2.group(1))
    today = now_cst()
    hist = []
    for d, v in zip(dates, vals):
        if v == "-" or not re.match(r"^\d{2}-\d{2}$", d):
            continue
        hist.append({"d": _fix_year(d, today), "v": float(v)})
    return hist


def fetch_ppi_quotes(plist_path):
    """报价中心列表页: 取最新一天报价的中位数。"""
    html = ppi_get(plist_path)
    rows = re.findall(
        r'<td class="p-name">.*?</td>\s*'
        r'<td title="([^"]*)">.*?</td>\s*'
        r'<td class="txtc">([^<]*)</td>\s*'
        r'<td class="txtc">\s*([\d.]+)元/吨\s*</td>\s*'
        r'<td class="txtc">\s*([^<]*?)\s*</td>\s*'
        r'<td class="txtc">.*?</td>\s*'
        r'<td[^>]*class="txtc"[^>]*>.*?</td>\s*'
        r'<td class="txtc">(\d{4}-\d{2}-\d{2})</td>',
        html, re.S)
    if not rows:
        raise RuntimeError("报价数据解析失败: %s" % plist_path)
    latest = max(r[4] for r in rows)
    prices = [float(r[2]) for r in rows if r[4] == latest]
    prices.sort()
    n = len(prices)
    median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    return {
        "median": median, "min": prices[0], "max": prices[-1],
        "count": n, "date": latest,
        "source": "生意社报价中心(最新日期中位数)",
        "samples": [{"spec": r[0], "brand": r[1], "price": float(r[2]),
                     "type": r[3]} for r in rows if r[4] == latest],
    }


# ---------------------------------------------------------------------------
# 新闻
# ---------------------------------------------------------------------------

def parse_ppi_news(html, source):
    items = []
    pat = re.compile(
        r"<li>\s*\[\s*<a href=\"[^\"]*\"[^>]*>([^<]+)</a>\s*\]\s*"
        r"<a href=\"([^\"]+)\"[^>]*>([^<]+)</a>\s*<span>([^<]+)</span>",
        re.S)
    for m in pat.finditer(html):
        tag = m.group(1).strip()
        if tag in ("均差", "基准价"):
            continue
        items.append({
            "tag": tag,
            "title": m.group(3).strip(),
            "url": m.group(2).strip(),
            "time": m.group(4).strip(),
            "source": source,
        })
    return items


def fetch_ppi_news():
    pages = (
        ("/news/list-15--414-1.html", "生意社PP"),
        ("/news/list-15--409-1.html", "生意社LLDPE"),
        ("/news/list-15--1-1.html", "生意社橡塑"),
        ("/news/list-11--45-1.html", "生意社原油"),
    )
    out = {"plastic": [], "crude": []}
    for path, src in pages:
        try:
            html = ppi_get(path, timeout=30)
        except Exception:
            continue
        items = parse_ppi_news(html, src)
        if "原油" in src:
            out["crude"].extend(items)
        else:
            out["plastic"].extend(items)
    return out


def fill_summaries(items, old_items, limit=18):
    """给生意社新闻补导语(抓详情页 meta description), 旧数据里已有的直接复用。"""
    cached = {it.get("url"): it.get("summary") for it in old_items if it.get("summary")}
    done = 0
    for it in items:
        if it.get("summary"):
            continue
        if it.get("url") in cached:
            it["summary"] = cached[it["url"]]
            continue
        if done >= limit:
            break
        done += 1
        try:
            html = ppi_get(it["url"], timeout=15)
            m = re.search(r'<meta name="description" content="([^"]*)"', html)
            if m and len(m.group(1)) > 8:
                it["summary"] = m.group(1).strip()[:160]
        except Exception:
            continue
    return items


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def fetch_sina_zhibo():
    out = {"plastic": [], "crude": [], "macro": []}
    for page in (1, 2):
        try:
            data = json.loads(http_text(
                SINA_ZHIBO.format(page=page),
                headers={"Referer": "https://finance.sina.com.cn"}))
            items = data["result"]["data"]["feed"]["list"]
        except Exception:
            continue
        for it in items:
            text = strip_tags(it.get("rich_text", ""))
            if not text:
                continue
            # 只保留有原文链接的快讯, 保证每条新闻可点击跳转
            docurl = (it.get("docurl") or "").strip()
            if not docurl.startswith("http"):
                continue
            mt = re.match(r"^【([^】]+)】\s*(.*)$", text, re.S)
            title = mt.group(1).strip() if mt else text[:60]
            body = (mt.group(2).strip() if mt else text)
            cat = None
            if re.search(r"原油|油价|OPEC|欧佩克|布伦特|WTI|美油|布油|成品油|炼油|"
                         r"页岩油|EIA|石油库存", text):
                cat = "crude"
            elif re.search(r"塑料|聚丙烯|聚乙烯|聚氯乙烯|PVC|ABS|丙烯|乙烯|苯乙烯", text):
                cat = "plastic"
            elif re.search(r"美联储|央行|降息|加息|通胀|CPI|PPI|GDP|关税|汇率|"
                           r"国债|财政|出口|进口|PMI", text):
                cat = "macro"
            if not cat:
                continue
            out[cat].append({
                "tag": "快讯",
                "title": title,
                "summary": body[:180],
                "url": docurl,
                "time": it.get("create_time", ""),
                "source": "新浪财经",
            })
    return out


def fetch_ppi_macro_news():
    """生意社「商品与宏观」频道新闻, 链接完整。"""
    html = ppi_get("/info/")
    items = []
    seen = set()
    pat = re.compile(
        r'<a[^>]*href="(https://www\.100ppi\.com/info/detail-'
        r'(\d{4})(\d{2})(\d{2})-\d+\.html)"[^>]*>(.*?)</a>', re.S)
    for m in pat.finditer(html):
        url = m.group(1)
        date = "%s-%s-%s" % (m.group(2), m.group(3), m.group(4))
        tag_html = m.group(0)
        tm = re.search(r'title="([^"]*)"', tag_html)
        text = strip_tags(m.group(5))
        title = (tm.group(1) if tm else text).strip()
        if not title or len(title) < 4 or title in seen:
            continue
        seen.add(title)
        items.append({
            "tag": "宏观",
            "title": title,
            "url": url,
            "time": date,
            "source": "生意社宏观",
        })
    items.sort(key=lambda x: x["time"], reverse=True)
    return items[:15]


# ---------------------------------------------------------------------------
# 信号计算
# ---------------------------------------------------------------------------

def pct_change(closes, n):
    if len(closes) <= n:
        return None
    base = closes[-1 - n]
    if not base:
        return None
    return round((closes[-1] / base - 1) * 100, 2)


def percentile(win, cur):
    if not win:
        return None
    return round(sum(1 for x in win if x <= cur) / len(win) * 100, 1)


def calc_streak(closes):
    if len(closes) < 2:
        return 0
    s = 0
    for i in range(len(closes) - 1, 0, -1):
        d = closes[i] - closes[i - 1]
        cur = 1 if d > 0 else (-1 if d < 0 else 0)
        if cur == 0:
            break
        if s == 0:
            s = cur
        elif (s > 0 and cur > 0) or (s < 0 and cur < 0):
            s += cur
        else:
            break
    return s


def build_signals(futures_hist, spot_hist, oil_hist):
    """期货主连: 分位/连涨跌/多周期涨跌; 原油比价分位。"""
    out = {}
    # 原油三品种的多周期统计(用于传导对比)
    for code in ("SC", "BRENT", "WTI"):
        closes = [x["c"] for x in (futures_hist.get(code) or [])]
        if len(closes) < 2:
            continue
        out[code] = {
            "pct1": pct_change(closes, 1),
            "pct7": pct_change(closes, 7),
            "pct30": pct_change(closes, 30),
            "pos90": percentile(closes[-90:], closes[-1]),
        }
    for code in ("PP", "L", "V"):
        hist = futures_hist.get(code) or []
        closes = [x["c"] for x in hist]
        if len(closes) < 2:
            continue
        win90 = closes[-90:]
        rec = {
            "pct1": pct_change(closes, 1),
            "pct7": pct_change(closes, 7),
            "pct30": pct_change(closes, 30),
            "pos90": percentile(win90, closes[-1]),
            "pos30": percentile(closes[-30:], closes[-1]),
            "streak": calc_streak(closes),
            "high90": max(win90),
            "low90": min(win90),
        }
        # 原料/原油比价(同日期对齐)
        sc_map = {x["d"]: x["c"] for x in (oil_hist or [])}
        ratio_hist = []
        for x in hist:
            ov = sc_map.get(x["d"])
            if ov:
                ratio_hist.append((x["d"], x["c"] / ov))
        if ratio_hist:
            ratios = [r for _, r in ratio_hist[-90:]]
            cur = ratio_hist[-1][1]
            prev = ratio_hist[-2][1] if len(ratio_hist) > 1 else cur
            rec["ratio"] = round(cur, 3)
            rec["ratio_pct"] = round((cur / prev - 1) * 100, 2) if prev else None
            rec["ratio_pos90"] = percentile(ratios, cur)
        out[code] = rec
    # 现货信号(基于采样序列)
    for code in ("PP", "PE", "PVC"):
        hist = spot_hist.get(code) or []
        vals = [x["v"] for x in hist]
        if len(vals) < 2:
            continue
        out.setdefault(code, {})
        out[code]["spot_pos90"] = percentile(vals[-90:], vals[-1])
        out[code]["spot_pct_vs_last_sample"] = round((vals[-1] / vals[-2] - 1) * 100, 2)
    return out


def merge_spot_history(old_hist, new_hist):
    """合并旧现货历史与新采样, 按日期去重。"""
    merged = {}
    for x in (old_hist or []):
        if x.get("d") and x.get("v"):
            merged[x["d"]] = x["v"]
    for x in (new_hist or []):
        if x.get("d") and x.get("v"):
            merged[x["d"]] = x["v"]
    items = sorted(merged.items())
    return [{"d": d, "v": v} for d, v in items][-180:]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def load_old():
    try:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    warnings = []
    old = load_old()
    now = now_cst()

    data = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "generated_iso": now.isoformat(),
        "warnings": warnings,
        "stale": False,
    }

    # 1. 期货实时
    try:
        rt = fetch_sina_realtime()
        if not rt:
            raise RuntimeError("实时行情为空")
        data["futures"] = rt
    except Exception as e:  # noqa: BLE001
        warnings.append("期货实时行情抓取失败: %s" % e)
        data["futures"] = old.get("futures") or {}
        data["stale"] = True

    # 2. 期货历史
    try:
        data["klines"] = fetch_sina_klines()
    except Exception as e:  # noqa: BLE001
        warnings.append("期货历史K线抓取失败: %s" % e)
        data["klines"] = old.get("klines") or {}

    # 3. 现货: 现期表(PP/PE/PVC)
    spot = {}
    try:
        sf = fetch_ppi_sf()
        for code, rec in sf.items():
            spot[code] = rec
    except Exception as e:  # noqa: BLE001
        warnings.append("生意社现期表抓取失败: %s" % e)

    # 4. 现货历史
    spot_hist = {}
    for code, ppid in (("PP", 718), ("PE", 435), ("PVC", 107)):
        try:
            spot_hist[code] = fetch_ppi_spot_history(ppid)
        except Exception as e:  # noqa: BLE001
            warnings.append("现货历史抓取失败(%s): %s" % (code, e))
            old_hist = (old.get("spot") or {}).get(code, {}).get("history")
            spot_hist[code] = old_hist or []

    # 5. ABS / GPPS 报价
    try:
        spot["ABS"] = fetch_ppi_quotes("/mprice/plist-1-649-1.html")
        spot["ABS"].update({"name": "ABS", "unit": "元/吨"})
    except Exception as e:  # noqa: BLE001
        warnings.append("ABS报价抓取失败: %s" % e)
        if old.get("spot", {}).get("ABS"):
            spot["ABS"] = old["spot"]["ABS"]
            spot["ABS"]["stale"] = True
    try:
        spot["PS"] = fetch_ppi_quotes("/mprice/plist-1-2475-1.html")
        spot["PS"].update({"name": "GPPS(普通级聚苯乙烯)", "unit": "元/吨"})
    except Exception as e:  # noqa: BLE001
        warnings.append("GPPS报价抓取失败: %s" % e)
        if old.get("spot", {}).get("PS"):
            spot["PS"] = old["spot"]["PS"]
            spot["PS"]["stale"] = True

    if not spot:
        data["spot"] = old.get("spot") or {}
        data["stale"] = True
    else:
        # 合并现货历史(旧+新)
        for code in ("PP", "PE", "PVC"):
            old_hist = (old.get("spot") or {}).get(code, {}).get("history")
            hist = merge_spot_history(old_hist, spot_hist.get(code) or [])
            if code in spot:
                spot[code]["history"] = hist
                # 现货较上一次采样点的变化(采样间隔不固定, 仅作参考, 页面勿当日涨跌)
                if len(hist) >= 2:
                    spot[code]["change_vs_last_sample"] = round(hist[-1]["v"] - hist[-2]["v"], 2)
                    if hist[-2]["v"]:
                        spot[code]["pct_vs_last_sample"] = round(
                            (hist[-1]["v"] / hist[-2]["v"] - 1) * 100, 2)
            elif old.get("spot", {}).get(code):
                spot[code] = old["spot"][code]
                spot[code]["history"] = hist
                spot[code]["stale"] = True
        data["spot"] = spot

    # 6. 新闻
    try:
        news = fetch_ppi_news()
    except Exception as e:  # noqa: BLE001
        warnings.append("生意社资讯抓取失败: %s" % e)
        news = {"plastic": [], "crude": []}
    try:
        zb = fetch_sina_zhibo()
        news["plastic"].extend(zb["plastic"])
        news["crude"].extend(zb["crude"])
        news["macro"] = zb["macro"]
    except Exception as e:  # noqa: BLE001
        warnings.append("新浪快讯抓取失败: %s" % e)
        news.setdefault("macro", [])
        if not news["plastic"] and not news["crude"]:
            news = old.get("news") or news
    try:
        news["macro"] = (news.get("macro") or []) + fetch_ppi_macro_news()
    except Exception as e:  # noqa: BLE001
        warnings.append("生意社宏观新闻抓取失败: %s" % e)

    # 补导语(仅生意社条目; 新浪快讯自带摘要)
    old_news = old.get("news") or {}
    for cat in ("plastic", "crude"):
        try:
            fill_summaries(news.get(cat) or [], old_news.get(cat) or [])
        except Exception as e:  # noqa: BLE001
            warnings.append("新闻导语抓取失败: %s" % e)

    # 合并旧新闻(补足数量), 去重, 排序, 截断
    for cat in ("plastic", "crude", "macro"):
        seen = set()
        merged = []
        for item in (news.get(cat) or []) + (old_news.get(cat) or []):
            key = item.get("title", "").strip()
            if not key or key in seen:
                continue
            # 无有效原文链接的条目不展示, 避免点了没反应
            if not (item.get("url") or "").startswith("http"):
                continue
            seen.add(key)
            merged.append(item)
        merged.sort(key=lambda x: x.get("time", ""), reverse=True)
        news[cat] = merged[:15]
    data["news"] = news

    # 7. 信号
    try:
        data["signals"] = build_signals(
            data.get("klines") or {},
            {c: (spot.get(c) or {}).get("history") or [] for c in ("PP", "PE", "PVC")},
            data.get("klines", {}).get("SC") or [])
    except Exception as e:  # noqa: BLE001
        warnings.append("信号计算失败: %s" % e)
        data["signals"] = old.get("signals") or {}

    # 8. 写文件(原子替换)
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_PATH)

    print("[OK] %s 生成 %s" % (now.strftime("%Y-%m-%d %H:%M"), DATA_PATH))
    if warnings:
        print("[WARN] %d 条警告:" % len(warnings))
        for w in warnings:
            print("  - " + w)
    # 摘要
    for code in ("PP", "L", "V", "SC"):
        f = (data.get("futures") or {}).get("nf_" + code + "0") or {}
        if f:
            print("  期货 %-4s %8.2f  %+.2f%%  %s" % (
                code, f.get("price", 0), f.get("pct") or 0, f.get("date", "")))
    for code in ("PP", "PE", "PVC"):
        s = (data.get("spot") or {}).get(code)
        if s:
            print("  现货 %-4s %8.2f  %s" % (code, s.get("spot", 0), s.get("date", "")))
    for code in ("ABS", "PS"):
        s = (data.get("spot") or {}).get(code)
        if s:
            print("  现货 %-4s %8.2f  样本%d  %s" % (
                code, s.get("median", 0), s.get("count", 0), s.get("date", "")))
    for cat in ("plastic", "crude", "macro"):
        print("  新闻 %-7s %d 条" % (cat, len((data.get("news") or {}).get(cat) or [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
