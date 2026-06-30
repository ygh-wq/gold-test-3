#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
════════════════════════════════════════════════════════════════
  黄金价格智能监控系统 (中国版) — GitHub Actions 云端版
  Gold Price Smart Monitor — China Edition for GitHub Actions

  运行在 GitHub 服务器上，国内可直接访问，完全免费 24/7
════════════════════════════════════════════════════════════════
"""

import os
import json
import smtplib
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import quote

# ╔═══════════════════════════════════════════════════════════════╗
# ║   📧 配置区 — 改这里                                            ║
# ╚═══════════════════════════════════════════════════════════════╝

# ── 投资信息 ──
GOLD_GRAMS = 69             # 持有黄金克数
INVESTED_AMOUNT = 72000     # 投入总金额 (¥)

# ── 邮箱配置 (QQ邮箱 或 Gmail 二选一) ──
EMAIL_CONFIG = {
    "sender_email": "1137206138@qq.com",
    "sender_name": "黄金智能监控",
    "smtp_server": "smtp.qq.com",
    "smtp_port": 587,
     "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
 "receiver_emails": [
        "1137206138@qq.com", 
         "648148661@qq.com", 
# ── 提醒阈值 ──
DAILY_ALERT_PCT = 1.5   # 日波动超 ±1.5% 即时提醒
WEEKLY_ALERT_PCT = 3.0  # 周波动超 ±3% 即时提醒


# ════════════════════════════════════════════════════════════════
#  ⛏️ 第一部分：金价数据获取
# ════════════════════════════════════════════════════════════════

def http_get(url, headers=None):
    """带重试的 HTTP GET"""
    if headers is None:
        headers = {"User-Agent": "Mozilla/5.0 GoldMonitor/1.0"}
    for attempt in range(3):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1)


def get_current_gold_price():
    """
    获取实时金价 (人民币/克)
    策略: 国际金价 USD/oz → CNY 汇率 → CNY/g
    """
    result = {
        "price_cny_gram": None,
        "price_usd_oz": None,
        "usd_cny_rate": None,
        "source": "",
        "success": False,
    }

    # ── 数据源1: metals.live (免费) ──
    try:
        data = http_get("https://api.metals.live/v1/spot/gold")
        items = json.loads(data)
        if isinstance(items, list) and len(items) > 0:
            result["price_usd_oz"] = float(items[0]["price"])
            result["source"] = "metals.live"
    except Exception as e:
        print(f"[数据源1] metals.live 失败: {e}")

    # ── 数据源2 备选: goldprice.org ──
    if not result["price_usd_oz"]:
        try:
            data = http_get("https://data-asg.goldprice.org/dbXRates/USD")
            obj = json.loads(data)
            if obj.get("items") and len(obj["items"]) > 0:
                result["price_usd_oz"] = float(obj["items"][0].get("xauPrice", 0))
                result["source"] = "goldprice.org"
        except Exception as e:
            print(f"[数据源2] goldprice.org 失败: {e}")

    # ── 数据源3: Yahoo Finance GC=F ──
    if not result["price_usd_oz"]:
        try:
            data = http_get(
                "https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            obj = json.loads(data)
            meta = obj["chart"]["result"][0]["meta"]
            result["price_usd_oz"] = meta["regularMarketPrice"]
            result["source"] = "Yahoo Finance"
        except Exception as e:
            print(f"[数据源3] Yahoo Finance 失败: {e}")

    if not result["price_usd_oz"]:
        return result

    # ── 汇率: USD → CNY ──
    try:
        data = http_get("https://api.frankfurter.app/latest?from=USD&to=CNY")
        obj = json.loads(data)
        result["usd_cny_rate"] = obj["rates"]["CNY"]
    except Exception as e:
        print(f"汇率获取失败: {e}")

    if not result["usd_cny_rate"]:
        result["usd_cny_rate"] = 7.25  # 降级近似值

    # 转换: USD/oz → CNY/g (1 troy oz = 31.1035g)
    result["price_cny_gram"] = round(
        result["price_usd_oz"] * result["usd_cny_rate"] / 31.1035, 2
    )
    result["success"] = True
    return result


def get_historical_close(target_date_str):
    """
    从 Yahoo Finance 获取指定日期的黄金期货收盘价 (USD/oz)
    target_date_str: "2026-06-29"
    返回 USD/oz 价格或 None
    """
    try:
        target_dt = datetime.strptime(target_date_str, "%Y-%m-%d")
        # 查询前后 3 天范围以防非交易日
        start_dt = target_dt - timedelta(days=3)
        end_dt = target_dt + timedelta(days=1)

        period1 = int(start_dt.timestamp())
        period2 = int(end_dt.timestamp())

        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/GC=F"
            f"?period1={period1}&period2={period2}&interval=1d"
        )
        data = http_get(url, {"User-Agent": "Mozilla/5.0"})
        obj = json.loads(data)

        result_arr = obj["chart"]["result"][0]
        timestamps = result_arr.get("timestamp", [])
        closes = result_arr["indicators"]["quote"][0].get("close", [])

        if not timestamps or not closes:
            return None

        # 找最接近目标日期的有效收盘价
        target_ts = int(target_dt.timestamp())
        best_close = None
        best_diff = float("inf")

        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            diff = abs(ts - target_ts)
            if diff < best_diff and diff < 86400 * 3:  # 3 天内
                best_diff = diff
                best_close = close

        return best_close
    except Exception as e:
        print(f"历史数据获取失败 ({target_date_str}): {e}")
        return None


def get_previous_trading_day(dt, days_back=1):
    """获取前 N 个交易日"""
    count = 0
    while count < days_back:
        dt = dt - timedelta(days=1)
        if dt.weekday() < 5:  # 周一至周五
            count += 1
    return dt


def get_last_friday(dt):
    """获取最近一个周五"""
    while dt.weekday() != 4:  # 周五 = 4
        dt = dt - timedelta(days=1)
    return dt


# ════════════════════════════════════════════════════════════════
#  📰 第二部分：新闻与事件
# ════════════════════════════════════════════════════════════════

def get_gold_news():
    """抓取黄金相关财经新闻"""
    news_items = []
    try:
        query = quote("黄金 金价")
        url = (
            f"https://news.google.com/rss/search?q={query}"
            f"&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        )
        xml_data = http_get(url)
        root = ET.fromstring(xml_data)

        for item in root.iter("item"):
            title = item.find("title")
            link = item.find("link")
            pub_date = item.find("pubDate")
            source = item.find("source")

            if title is not None and title.text:
                news_items.append({
                    "title": title.text,
                    "link": link.text if link is not None else "",
                    "source": source.text if source is not None else "",
                    "pub_date": pub_date.text if pub_date is not None else "",
                })

            if len(news_items) >= 6:
                break
    except Exception as e:
        print(f"新闻获取失败: {e}")

    if not news_items:
        news_items.append({
            "title": "（新闻数据源暂不可用，仅展示价格分析）",
            "link": "", "source": "", "pub_date": ""
        })

    return news_items


def analyze_price_drivers(current_price, prev_close):
    """分析涨跌影响事件"""
    if prev_close is None:
        return ["初始数据积累中，暂无对比分析"]

    change = (current_price - prev_close) / prev_close * 100
    direction = "上涨" if change >= 0 else "下跌"
    drivers = [
        f"📊 价格变动: {direction} {abs(change):.2f}%"
    ]

    if change >= 0:
        drivers += [
            "🔍 可能推动金价上涨的因素：",
            "  • 美元走弱预期 / 美联储降息预期升温",
            "  • 地缘政治紧张局势升级",
            "  • 全球央行持续增持黄金储备",
            "  • 通胀预期上升 / 避险需求增加",
        ]
    else:
        drivers += [
            "🔍 可能推动金价下跌的因素：",
            "  • 美元走强 / 美联储鹰派表态",
            "  • 风险偏好回升，资金流向股市",
            "  • 美国国债收益率上升",
            "  • 地缘局势缓和 / 避险需求下降",
        ]

    return drivers


def get_event_calendar():
    """重大经济事件日历 (需定期更新)"""
    return [
        {"date": "2026-07-01", "event": "美国ISM制造业PMI", "impact": "中",
         "gold_direction": "PMI弱于预期 → 金价↑", "probability": "40%",
         "note": "制造业萎缩利好黄金避险"},
        {"date": "2026-07-02", "event": "美国6月非农就业报告", "impact": "🔴高",
         "gold_direction": "数据弱于预期 → 金价↑", "probability": "45%↑",
         "note": "就业疲软加大降息预期"},
        {"date": "2026-07-10", "event": "美国6月CPI通胀数据", "impact": "🔴高",
         "gold_direction": "通胀回落可能短期打压金价", "probability": "55%↑",
         "note": "核心CPI为关键观察指标"},
        {"date": "2026-07-15", "event": "美联储褐皮书发布", "impact": "中",
         "gold_direction": "经济放缓信号 → 金价↑", "probability": "50%",
         "note": "各地区经济状况综合评估"},
        {"date": "2026-07-17", "event": "美国6月零售销售数据", "impact": "中",
         "gold_direction": "消费疲软 → 金价↑", "probability": "45%",
         "note": "反映消费者支出趋势"},
        {"date": "2026-07-29", "event": "美联储FOMC利率决议", "impact": "🔴高",
         "gold_direction": "若暗示降息 → 金价大幅上涨", "probability": "60%↑",
         "note": "全年最重要的货币政策事件之一"},
        {"date": "2026-07-30", "event": "美国Q2 GDP初值", "impact": "高",
         "gold_direction": "GDP低于预期 → 金价↑", "probability": "50%",
         "note": "经济增速影响降息预期"},
        {"date": "2026-08-01", "event": "美国7月非农就业报告", "impact": "🔴高",
         "gold_direction": "数据弱于预期 → 金价↑", "probability": "45%↑",
         "note": ""},
        {"date": "2026-08-12", "event": "美国7月CPI通胀数据", "impact": "🔴高",
         "gold_direction": "通胀数据影响降息节奏", "probability": "55%",
         "note": ""},
        {"date": "2026-08-21", "event": "Jackson Hole全球央行年会", "impact": "🔴高",
         "gold_direction": "鲍威尔讲话定调 → 大幅波动", "probability": "60%",
         "note": "全球央行政策信号集中释放"},
    ]


def get_upcoming_events():
    """获取未来 30 天内的事件"""
    today = datetime.now()
    events = []
    for evt in get_event_calendar():
        evt_date = datetime.strptime(evt["date"], "%Y-%m-%d")
        days_away = (evt_date - today).days
        if 0 <= days_away <= 30:
            evt["days_away"] = days_away
            events.append(evt)
    return events


# ════════════════════════════════════════════════════════════════
#  📧 第三部分：邮件构建
# ════════════════════════════════════════════════════════════════

def build_email_html(prices, prev_close, last_friday_close,
                     news_items, drivers, upcoming_events):
    """构建 HTML 邮件"""
    now = datetime.now(timezone(timedelta(hours=8)))
    time_str = now.strftime("%Y-%m-%d %H:%M")

    day_change = None
    week_change = None
    if prev_close:
        day_change = (prices["price_cny_gram"] - prev_close) / prev_close * 100
    if last_friday_close:
        week_change = (prices["price_cny_gram"] - last_friday_close) / last_friday_close * 100

    def change_color(val):
        if val is None:
            return "#888"
        return "#e74c3c" if val >= 0 else "#27ae60"

    def change_icon(val):
        if val is None:
            return "➖"
        return "📈" if val >= 0 else "📉"

    # 持仓计算
    current_value = prices["price_cny_gram"] * GOLD_GRAMS
    profit = current_value - INVESTED_AMOUNT if INVESTED_AMOUNT > 0 else None

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f5f5f5; margin:0; padding:20px; }}
  .container {{ max-width:640px; margin:0 auto; background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 2px 12px rgba(0,0,0,.08); }}
  .header {{ background:linear-gradient(135deg,#1a1a2e,#16213e,#0f3460); color:#ffd700; padding:28px 24px; text-align:center; }}
  .header h1 {{ margin:0; font-size:22px; letter-spacing:2px; }}
  .header .time {{ font-size:12px; color:#aaa; margin-top:8px; }}
  .price-section {{ padding:24px; text-align:center; background:#fafafa; border-bottom:1px solid #eee; }}
  .current-price {{ font-size:48px; font-weight:700; color:#1a1a2e; margin:0; }}
  .current-price .unit {{ font-size:16px; color:#888; font-weight:400; }}
  .sub-info {{ font-size:12px; color:#999; margin-top:4px; }}
  .comparison {{ display:flex; justify-content:center; gap:40px; padding:20px 0 0; }}
  .comp-item {{ text-align:center; }}
  .comp-label {{ font-size:12px; color:#999; margin-bottom:4px; }}
  .comp-price {{ font-size:18px; font-weight:600; color:#333; }}
  .comp-change {{ font-size:13px; font-weight:600; margin-top:2px; }}
  .section {{ padding:20px 24px; border-bottom:1px solid #f0f0f0; }}
  .section:last-child {{ border-bottom:none; }}
  .section-title {{ font-size:15px; font-weight:700; color:#1a1a2e; margin:0 0 12px; padding-left:8px; border-left:3px solid #ffd700; }}
  .drivers {{ list-style:none; padding:0; margin:0; }}
  .drivers li {{ padding:4px 0; font-size:13px; color:#555; line-height:1.6; }}
  .news-item {{ padding:10px 0; border-bottom:1px dashed #f0f0f0; }}
  .news-item:last-child {{ border-bottom:none; }}
  .news-title {{ font-size:13px; color:#333; line-height:1.5; }}
  .news-title a {{ color:#1a5276; text-decoration:none; }}
  .news-source {{ font-size:11px; color:#aaa; margin-top:2px; }}
  .event-table {{ width:100%; border-collapse:collapse; font-size:12px; }}
  .event-table th {{ background:#f8f8f8; padding:8px; text-align:left; font-weight:600; color:#555; border-bottom:2px solid #eee; }}
  .event-table td {{ padding:8px; border-bottom:1px solid #f5f5f5; color:#555; line-height:1.5; }}
  .high-impact {{ color:#e74c3c; font-weight:600; }}
  .probability {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }}
  .prob-up {{ background:#fdecea; color:#e74c3c; }}
  .prob-down {{ background:#e8f8f0; color:#27ae60; }}
  .prob-neutral {{ background:#f5f5f5; color:#888; }}
  .footer {{ padding:16px 24px; background:#fafafa; text-align:center; font-size:11px; color:#bbb; }}
  .summary-box {{ background:linear-gradient(135deg,#fff9e6,#fff3cd); border:1px solid #ffe082; border-radius:8px; padding:14px 16px; margin-top:12px; font-size:13px; color:#856404; line-height:1.6; }}
  .holdings {{ display:flex; justify-content:center; gap:30px; padding:8px 0 16px; }}
  .holding-item {{ text-align:center; }}
  .holding-val {{ font-size:16px; font-weight:600; color:#1a1a2e; }}
  .holding-label {{ font-size:11px; color:#999; }}
</style></head><body>
<div class="container">
<div class="header">
  <h1>🥇 黄金价格智能监控报告</h1>
  <div class="time">⏰ {time_str} (北京时间)</div>
</div>

<div class="price-section">
  <p class="current-price">¥{prices['price_cny_gram']:.2f}<span class="unit"> /克</span></p>
  <p class="sub-info">📡 {prices['source']} | 国际: ${prices['price_usd_oz']:.2f}/oz | 汇率: ¥{prices['usd_cny_rate']:.2f}</p>
"""

    # 持仓信息
    if GOLD_GRAMS > 0:
        html += f"""
  <div class="holdings">
    <div class="holding-item">
      <div class="holding-val">{GOLD_GRAMS}g</div>
      <div class="holding-label">持仓重量</div>
    </div>
    <div class="holding-item">
      <div class="holding-val">¥{current_value:,.0f}</div>
      <div class="holding-label">当前市值</div>
    </div>"""
        if profit is not None:
            p_color = "#e74c3c" if profit >= 0 else "#27ae60"
            p_icon = "📈" if profit >= 0 else "📉"
            html += f"""
    <div class="holding-item">
      <div class="holding-val" style="color:{p_color}">{p_icon} ¥{profit:,.0f}</div>
      <div class="holding-label">浮动盈亏</div>
    </div>"""
        html += "\n  </div>"

    # 历史对比
    if prev_close or last_friday_close:
        html += '<div class="comparison">'
        if prev_close:
            dc = day_change
            html += f"""
    <div class="comp-item">
      <div class="comp-label">📅 前日收盘</div>
      <div class="comp-price">¥{prev_close:.2f}</div>
      <div class="comp-change" style="color:{change_color(dc)}">{change_icon(dc)} {dc:+.2f}%</div>
    </div>"""
        if last_friday_close:
            wc = week_change
            html += f"""
    <div class="comp-item">
      <div class="comp-label">📅 上周五收盘</div>
      <div class="comp-price">¥{last_friday_close:.2f}</div>
      <div class="comp-change" style="color:{change_color(wc)}">{change_icon(wc)} {wc:+.2f}%</div>
    </div>"""
        html += "</div>"

    html += "</div>"

    # 涨跌因素
    html += """
<div class="section">
  <p class="section-title">📊 涨跌主要影响因素</p>
  <ul class="drivers">"""
    for d in drivers:
        html += f"<li>{d}</li>"
    html += "</ul></div>"

    # 最新资讯
    if news_items and news_items[0].get("link"):
        html += """
<div class="section">
  <p class="section-title">📰 最新相关资讯</p>"""
        for n in news_items[:5]:
            src = f"{n['source']} · {n['pub_date']}" if n.get("source") else ""
            html += f"""
  <div class="news-item">
    <div class="news-title"><a href="{n['link']}" target="_blank">{n['title']}</a></div>
    {f'<div class="news-source">{src}</div>' if src else ''}
  </div>"""
        html += "</div>"

    # 未来事件
    if upcoming_events:
        html += """
<div class="section">
  <p class="section-title">🔮 未来可能影响金价的重要事件</p>
  <table class="event-table">
    <tr><th>日期</th><th>事件</th><th>影响</th><th>金价走向</th><th>概率</th></tr>"""
        for evt in upcoming_events[:8]:
            prob_class = "prob-up" if "↑" in evt["probability"] else \
                         "prob-down" if "↓" in evt["probability"] else "prob-neutral"
            impact_class = "high-impact" if "高" in evt["impact"] else ""
            html += f"""
    <tr>
      <td>{evt['date']} ({evt['days_away']}天后)</td>
      <td>{evt['event']}</td>
      <td class="{impact_class}">{evt['impact']}</td>
      <td style="font-size:12px">{evt['gold_direction']}</td>
      <td><span class="probability {prob_class}">{evt['probability']}</span></td>
    </tr>"""
            if evt.get("note"):
                html += f"""
    <tr><td></td><td colspan="4" style="color:#aaa;font-size:11px;padding-top:0;">💡 {evt['note']}</td></tr>"""
        html += """
  </table>
  <div class="summary-box">
    <strong>📋 综合研判：</strong>未来30天金价走势大概率受美联储政策预期主导。建议重点关注 <strong>非农就业、CPI通胀、FOMC利率决议</strong> 三大核心数据。<br><br>
    ⚠️ <em>以上分析仅供参考，不构成投资建议。金价受多重因素影响，概率评估存在不确定性。</em>
  </div>
</div>"""

    html += f"""
<div class="footer">
  ⚡ 运行在 GitHub Actions 云端 | 每15分钟自动检测 | 完全免费<br>
  📧 由黄金智能监控系统自动生成 · {time_str}
</div>
</div>
</body></html>"""
    return html


def send_email(subject, html_body):
    """发送 HTML 邮件"""
    config = EMAIL_CONFIG
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((Header(config['sender_name'], "utf-8").encode(), config['sender_email']))
    msg["To"] = config["sender_email"]
    msg["Subject"] = subject

    plain = "这是一封黄金价格监控报告，请使用支持HTML的邮件客户端查看完整内容。\n\n— 黄金智能监控系统 (GitHub Actions)"
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(config["smtp_server"], config["smtp_port"], timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(config["sender_email"], config["smtp_password"])
            server.sendmail(config["sender_email"], config["sender_email"], msg.as_string())
        print(f"✅ 邮件已发送: {subject}")
        return True
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")
        return False


# ════════════════════════════════════════════════════════════════
#  🎯 第四部分：主逻辑
# ════════════════════════════════════════════════════════════════

def main():
    tz_cn = timezone(timedelta(hours=8))
    now = datetime.now(tz_cn)
    print(f"════════ 检测开始 {now.strftime('%Y-%m-%d %H:%M')} ════════")

    # 1. 获取实时金价
    prices = get_current_gold_price()
    if not prices["success"]:
        print("❌ 无法获取金价")
        return

    print(f"💰 国际金价: ${prices['price_usd_oz']:.2f}/oz")
    print(f"💱 汇率: {prices['usd_cny_rate']:.4f}")
    print(f"🥇 人民币: ¥{prices['price_cny_gram']:.2f}/g")

    # 2. 获取历史收盘价
    tz_cn = timezone(timedelta(hours=8))
    now = datetime.now(tz_cn)

    prev_trading_day = get_previous_trading_day(now)
    last_friday = get_last_friday(now)

    prev_close_usd = get_historical_close(prev_trading_day.strftime("%Y-%m-%d"))
    last_friday_close_usd = get_historical_close(last_friday.strftime("%Y-%m-%d"))

    # 转换为 CNY/g
    prev_close = None
    last_friday_close = None
    rate = prices["usd_cny_rate"]

    if prev_close_usd:
        prev_close = round(prev_close_usd * rate / 31.1035, 2)
        print(f"📅 前日收盘({prev_trading_day.strftime('%m-%d')}): ¥{prev_close}/g")
    if last_friday_close_usd:
        last_friday_close = round(last_friday_close_usd * rate / 31.1035, 2)
        print(f"📅 上周五收盘({last_friday.strftime('%m-%d')}): ¥{last_friday_close}/g")

    # 3. 判断是否发送报告
    hour = now.hour
    minute = now.minute
    should_send = False
    alert_reason = ""

    # 每日 18:00-18:15 定时报告
    if hour == 18 and minute < 15:
        should_send = True
        alert_reason = "⏰ 每日18:00定时报告"

    # 涨跌幅超限即时提醒
    day_change_abs = 0
    week_change_abs = 0
    if prev_close:
        day_change_abs = abs((prices["price_cny_gram"] - prev_close) / prev_close * 100)
    if last_friday_close:
        week_change_abs = abs((prices["price_cny_gram"] - last_friday_close) / last_friday_close * 100)

    if day_change_abs >= DAILY_ALERT_PCT:
        should_send = True
        direction = "📈 急涨" if prices["price_cny_gram"] >= prev_close else "📉 急跌"
        alert_reason += (", " if alert_reason else "") + \
                        f"{direction} 日波动 {day_change_abs:.2f}% (超{DAILY_ALERT_PCT}%阈值)"

    if week_change_abs >= WEEKLY_ALERT_PCT:
        should_send = True
        alert_reason += (", " if alert_reason else "") + \
                        f"周波动 {week_change_abs:.2f}% (超{WEEKLY_ALERT_PCT}%阈值)"

    # 通过环境变量强制发送 (GitHub Actions 手动触发时)
    if os.environ.get("FORCE_SEND", "").lower() == "true":
        should_send = True
        if not alert_reason:
            alert_reason = "🔧 手动触发"

    if should_send:
        print(f"📤 触发原因: {alert_reason}")

        news_items = get_gold_news()
        drivers = analyze_price_drivers(prices["price_cny_gram"], prev_close)
        upcoming_events = get_upcoming_events()

        html_body = build_email_html(
            prices, prev_close, last_friday_close,
            news_items, drivers, upcoming_events
        )

        subject = (
            f"🥇 黄金监控 {now.strftime('%Y-%m-%d')} "
            f"— ¥{prices['price_cny_gram']:.2f}/g"
        )
        if alert_reason:
            subject += f" | {alert_reason}"

        send_email(subject, html_body)
    else:
        print(f"⏭️ 未触发发送条件 ({now.strftime('%H:%M')})")

    print("════════ 检测完成 ════════")


if __name__ == "__main__":
    main()
