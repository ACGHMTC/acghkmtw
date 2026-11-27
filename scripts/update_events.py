import html as html_lib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()

DATA_PATH = "data/events.json"
HKT = timezone(timedelta(hours=8))

# 1. Google News 關鍵字搜尋
RSS_QUERIES = [
    ("香港", "香港 (同人 OR Rainbow Gala OR RG OR CON-CON OR COMIC CON OR 動漫節 OR ONLY OR Pure Love) -股價 -財報"),
    ("香港", "香港 (快閃店 OR 期間限定店 OR 展覽) (動漫 OR 遊戲) -股價 -財報"),
    ("澳門", "澳門 (動漫 OR 遊戲 OR 同人 OR 漫展 OR MCF OR LvUp ACG OR 澳門原創動漫作品展) -股價 -財報"),
    ("台灣", "台灣 (CWT OR FF OR 開拓動漫祭 OR 漫博 OR 同人活動) -股價 -財報"),
    ("深圳", "深圳 (漫展 OR 動漫展 OR 遊戲展 OR 同人展) -股價 -財報"),
]

# 2. 官方 RSS 來源
DIRECT_RSS_FEEDS = [
    ("台灣", "https://gnn.gamer.com.tw/rss.xml?k=16"),
]


def get_web_sources():
    """動態產生包含當前月份與下個月的網頁活動來源清單"""
    now = datetime.now(HKT)
    ym_curr = now.strftime("%Y%m")
    # 計算下個月的 YYYYMM
    next_month_date = (now.replace(day=28) + timedelta(days=4))
    ym_next = next_month_date.strftime("%Y%m")

    sources = [
        ("香港", "DoujinArk", "https://doujinark.hk/event"),
        ("香港", "AniHK", "https://anihk.com/events/region/hk"),
        ("台灣", "AniHK", "https://anihk.com/events/region/tw"),
        ("澳門", "HappyMacao", "https://event.happymacao.com/cms62/e/action/ListInfo/?classid=76"),
        ("澳門", "澳門旅遊局", f"https://www.macaotourism.gov.mo/zh-hant/events/whatson?month={ym_curr}"),
    ]
    if ym_next != ym_curr:
        sources.append(
            ("澳門", "澳門旅遊局", f"https://www.macaotourism.gov.mo/zh-hant/events/whatson?month={ym_next}")
        )
    return sources


class EventLinkParser(HTMLParser):
    """從活動列表 HTML 中擷取包含活動標題與連結的候選區塊"""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.in_script_or_style = False
        self.current_href = None
        self.current_text = []
        self.candidates = []

    def handle_starttag(self, tag, attrs):
        tag_l = tag.lower()
        if tag_l in ("script", "style", "noscript", "svg"):
            self.in_script_or_style = True
            return
        if tag_l == "a":
            attr_dict = dict(attrs)
            href = (attr_dict.get("href") or "").strip()
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self.current_href = urllib.parse.urljoin(self.base_url, href)
                self.current_text = []

    def handle_endtag(self, tag):
        tag_l = tag.lower()
        if tag_l in ("script", "style", "noscript", "svg"):
            self.in_script_or_style = False
            return
        if tag_l == "a" and self.current_href:
            raw_text = " ".join(self.current_text)
            clean_text = re.sub(r"\s+", " ", html_lib.unescape(raw_text)).strip()
            if len(clean_text) >= 6:
                self.candidates.append((self.current_href, clean_text))
            self.current_href = None
            self.current_text = []

    def handle_data(self, data):
        if self.in_script_or_style:
            return
        if self.current_href is not None:
            self.current_text.append(data)


def fetch_url_bytes(url: str, timeout: int = 18) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "zh-TW,zh-HK;q=0.9,zh;q=0.8,en;q=0.7",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def decode_html_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "big5", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def parse_web_events(region_hint: str, source_name: str, url: str, limit: int = 8):
    items = []
    try:
        raw_bytes = fetch_url_bytes(url)
        html_text = decode_html_bytes(raw_bytes)
        parser = EventLinkParser(url)
        parser.feed(html_text)

        # 過濾常見導覽列/頁尾無關字眼
        ignore_words = (
            "登入", "註冊", "私隱", "隱私", "條款", "聯絡我們", "關於我們",
            "首頁", "下一頁", "上一頁", "更多", "語言", "繁體", "简体", "English",
            "Facebook", "Instagram", "Twitter", "YouTube", "Cookie"
        )
        seen_local = set()
        for link, text in parser.candidates:
            if link == url or link in seen_local:
                continue
            if any(w.lower() == text.lower() for w in ignore_words) or len(text) > 160:
                continue
            seen_local.add(link)
            items.append(
                {
                    "region_hint": region_hint,
                    "source": source_name,
                    "title": text[:100],
                    "link": link,
                    "pub_date": datetime.now(HKT).strftime("%Y-%m-%d"),
                    "desc": f"來源網站：{source_name}｜擷取文字：{text}",
                }
            )
            if len(items) >= limit:
                break
    except Exception as e:
        print(f"[Web Warn] {source_name} ({url}): {e}")
    return items


def parse_rss_url(url: str, region_hint: str, source_name: str = "RSS", limit: int = 5):
    items = []
    try:
        xml_data = fetch_url_bytes(url, timeout=15)
        root = ET.fromstring(xml_data)
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "").strip()
            clean_desc = re.sub(r"<[^>]+>", " ", html_lib.unescape(desc))
            clean_desc = re.sub(r"\s+", " ", clean_desc).strip()[:200]
            if title and link:
                items.append(
                    {
                        "region_hint": region_hint,
                        "source": source_name,
                        "title": title,
                        "link": link,
                        "pub_date": pub_date,
                        "desc": clean_desc,
                    }
                )
    except Exception as e:
        print(f"[RSS Warn] {url}: {e}")
    return items


def fetch_rss_items(region_hint: str, query: str, limit: int = 3):
    encoded_q = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_q}&hl=zh-TW&gl=HK&ceid=HK:zh-Hant"
    return parse_rss_url(url, region_hint, source_name="GoogleNews", limit=limit)


def call_cloudflare_ai(prompt: str) -> str:
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise RuntimeError("缺少 CF_ACCOUNT_ID 或 CF_API_TOKEN 環境變數")

    # 改成目前可用的模型
    model = "@cf/meta/llama-3.1-8b-instruct"   # ← 這裡改掉
    # 或 model = "@cf/qwen/qwen2.5-coder-32b-instruct"

    endpoint = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{model}"
    )
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是香港、澳門、台灣、深圳動漫、遊戲與同人活動情報整理助手。"
                    "請判斷輸入是否為明確的『香港、澳門、台灣或深圳』線下活動、漫展、同人場、遊戲活動或快閃店。"
                    "若為日本/歐美活動、純線上活動或導覽按鈕，請只回覆 null。"
                    "若是明確的四地線下活動，請只輸出合法 JSON 物件，不要 markdown 包裹："
                    '{"region":"香港|澳門|台灣|深圳",'
                    '"category":"同人即賣會|綜合漫展|遊戲與快閃",'
                    '"title":"活動名稱（繁體中文）",'
                    '"start_date":"YYYY-MM-DD",'
                    '"end_date":"YYYY-MM-DD",'
                    '"venue":"場地或地區",'
                    '"deadline_note":"重要提醒或售票資訊",'
                    '"tags":["標籤1","標籤2"]}'
                ),
            },
            {"role": "user", "content": str(prompt)[:1000]},
        ],
        "max_tokens": 400,
        "temperature": 0.15,
    }

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {CF_API_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code}: {err_body[:250]}")

    if not result.get("success"):
        raise RuntimeError(f"Cloudflare AI API 失敗: {result}")

    ai_out = result.get("result", {})
    if isinstance(ai_out, dict):
        return (ai_out.get("response") or "").strip()
    return str(ai_out).strip()


def extract_json_object(text: str):
    cleaned = text.strip()
    if not cleaned or cleaned.lower() == "null":
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def normalize_title_key(title: str) -> str:
    """標準化活動標題以便進行同地區交叉比對"""
    t = title.lower()
    # 移除括號內補充字眼如（範例資料）、[香港場]
    t = re.sub(r"[\(（\[【].*?[\)）\]】]", "", t)
    # 移除常見泛用後綴詞，保留核心品牌/活動名
    for word in ("同人誌即賣會", "同人即賣會", "同人展", "動漫節", "快閃店", "期間限定店", "香港站", "台灣站", "澳門站"):
        t = t.replace(word, "")
    # 僅保留中英文與數字
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
    return t


def find_matching_event(existing_events: list, region: str, title: str, start_date: str):
    """同地區交叉比對：檢查現有清單中是否已有同一活動"""
    new_key = normalize_title_key(title)
    if not new_key:
        return None

    for ev in existing_events:
        if ev.get("region") != region:
            continue
        old_key = normalize_title_key(ev.get("title", ""))
        if not old_key:
            continue

        # 規則 1：核心名稱完全一致或高度包含（長度至少 4 個字元）
        if new_key == old_key:
            return ev
        if len(new_key) >= 4 and len(old_key) >= 4 and (new_key in old_key or old_key in new_key):
            return ev

        # 規則 2：同地區 + 起始日期相同 + 標題有明顯重疊（至少 3 個連續字元相同）
        if (
            start_date
            and start_date != "近期公布"
            and ev.get("start_date") == start_date
            and len(new_key) >= 3
            and len(old_key) >= 3
        ):
            for i in range(len(new_key) - 2):
                if new_key[i : i + 3] in old_key:
                    return ev
    return None


def merge_event_details(existing_ev: dict, parsed: dict, source_name: str, link: str):
    """交叉比對命中後，用新來源補全舊資料缺漏的欄位"""
    updated = False

    new_start = (parsed.get("start_date") or "").strip()
    new_end = (parsed.get("end_date") or new_start).strip()
    if new_start and existing_ev.get("start_date") in ("", "近期公布"):
        existing_ev["start_date"] = new_start
        existing_ev["end_date"] = new_end or new_start
        updated = True

    new_venue = (parsed.get("venue") or "").strip()
    if new_venue and existing_ev.get("venue") in ("", existing_ev.get("region")):
        existing_ev["venue"] = new_venue
        updated = True

    new_note = (parsed.get("deadline_note") or "").strip()
    old_note = (existing_ev.get("deadline_note") or "").strip()
    if new_note and new_note not in old_note:
        existing_ev["deadline_note"] = f"{old_note} / {new_note}".strip(" /")
        updated = True

    new_tags = parsed.get("tags") if isinstance(parsed.get("tags"), list) else []
    old_tags = existing_ev.get("tags") if isinstance(existing_ev.get("tags"), list) else []
    for tag in new_tags:
        if tag and tag not in old_tags and len(old_tags) < 6:
            old_tags.append(tag)
            updated = True
    existing_ev["tags"] = old_tags

    # 紀錄已交叉驗證的來源（不影響前端顯示，方便追蹤資料可信度）
    sources = existing_ev.get("verified_sources", [])
    if source_name not in sources:
        sources.append(source_name)
        existing_ev["verified_sources"] = sources
        updated = True

    return updated


def main():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"last_updated": "", "events": []}

    existing_events = data.get("events", [])
    seen_urls = {e.get("url", "") for e in existing_events}

    raw_items = []
    # 1. 抓取五個指定活動網站（含動態月份澳門旅遊局）
    for region_hint, source_name, web_url in get_web_sources():
        raw_items.extend(parse_web_events(region_hint, source_name, web_url, limit=8))

    # 2. 抓取官方 RSS（巴哈姆特 GNN）
    for region_hint, feed_url in DIRECT_RSS_FEEDS:
        raw_items.extend(parse_rss_url(feed_url, region_hint, source_name="巴哈GNN", limit=8))

    # 3. 抓取 Google News 關鍵字補充
    for region_hint, query in RSS_QUERIES:
        raw_items.extend(fetch_rss_items(region_hint, query, limit=3))

    added_count = 0
    merged_count = 0

    for item in raw_items:
        if item["link"] in seen_urls:
            continue

        prompt = (
            f"地區參考：{item['region_hint']}\n"
            f"情報來源：{item.get('source', '')}\n"
            f"標題：{item['title']}\n"
            f"摘要：{item.get('desc', '')}\n"
            f"參考日期：{item['pub_date']}"
        )
        try:
            raw_ai = call_cloudflare_ai(prompt)
            parsed = extract_json_object(raw_ai)
        except Exception as e:
            print(f"[AI Warn] {item['title'][:30]}: {e}")
            time.sleep(1.5)
            continue

        if not parsed or not parsed.get("title"):
            seen_urls.add(item["link"])
            time.sleep(1.2)
            continue

        region = (
            parsed.get("region")
            if parsed.get("region") in {"香港", "澳門", "台灣", "深圳"}
            else item["region_hint"]
        )
        category = (
            parsed.get("category")
            if parsed.get("category") in {"同人即賣會", "綜合漫展", "遊戲與快閃"}
            else "遊戲與快閃"
        )
        clean_title = parsed["title"].strip()
        start_date = (parsed.get("start_date") or "").strip()
        end_date = (parsed.get("end_date") or start_date).strip()

        # 同地區交叉比對
        matched_ev = find_matching_event(existing_events, region, clean_title, start_date)
        if matched_ev:
            if merge_event_details(matched_ev, parsed, item.get("source", ""), item["link"]):
                merged_count += 1
                print(f"[Merged] [{region}] {matched_ev['title']} ← 補充自 {item.get('source')}")
            seen_urls.add(item["link"])
            time.sleep(1.2)
            continue

        new_event = {
            "id": f"auto-{int(time.time())}-{added_count}",
            "region": region,
            "category": category,
            "title": clean_title,
            "start_date": start_date or "近期公布",
            "end_date": end_date or start_date or "近期公布",
            "venue": (parsed.get("venue") or region).strip(),
            "deadline_note": (parsed.get("deadline_note") or "").strip(),
            "tags": parsed.get("tags") if isinstance(parsed.get("tags"), list) else [region],
            "verified_sources": [item.get("source", "Web")],
            "url": item["link"],
        }

        existing_events.append(new_event)
        seen_urls.add(item["link"])
        added_count += 1
        print(f"[Added] [{region}] {new_event['title']} ({item.get('source')})")

        time.sleep(1.5)

    data["last_updated"] = datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT")
    data["events"] = existing_events

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"完成更新：新增 {added_count} 筆活動，交叉比對合併 {merged_count} 筆資料。")


if __name__ == "__main__":
    main()
