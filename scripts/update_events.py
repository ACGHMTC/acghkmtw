import html as html_lib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()

DATA_PATH = "data/events.json"
HKT = timezone(timedelta(hours=8))

RSS_QUERIES = [
("香港", "香港 (同人 OR Rainbow Gala OR RG OR CON-CON OR COMIC CON OR 動漫節 OR Pure Love) -股價 -財報"),
    ("香港", "香港 (快閃店 OR 期間限定店 OR 展覽) (動漫 OR 遊戲) -股價 -財報"),
    ("澳門", "澳門 (動漫 OR 遊戲 OR 同人 OR 漫展 OR MCF OR LvUp ACG OR 澳門原創動漫作品展) -股價 -財報"),
    ("台灣", "台灣 (CWT OR FF OR 開拓動漫祭 OR 漫博 OR 同人活動) -股價 -財報"),
    ("深圳", "深圳 (漫展 OR 動漫展 OR 遊戲展 OR 同人展) -股價 -財報"),
]

DIRECT_RSS_FEEDS = [
    ("台灣", "https://gnn.gamer.com.tw/rss.xml?k=16"),
]


def to_clean_str(val) -> str:
    """安全轉換任何型別（包含 dict / list / None）為乾淨字串，徹底避免 AttributeError"""
    if val is None:
        return ""
    if isinstance(val, dict):
        parts = [to_clean_str(v) for v in val.values() if v is not None]
        return " ".join(p for p in parts if p).strip()
    if isinstance(val, list):
        parts = [to_clean_str(x) for x in val if x is not None]
        return " ".join(p for p in parts if p).strip()
    return str(val).strip()


def get_web_sources():
    now = datetime.now(HKT)
    ym_curr = now.strftime("%Y%m")
    next_month_date = now.replace(day=28) + timedelta(days=4)
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
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.in_script_or_style = False
        self.current_href = None
        self.current_link_text = []
        self.recent_texts = []
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
                self.current_link_text = []

    def handle_endtag(self, tag):
        tag_l = tag.lower()
        if tag_l in ("script", "style", "noscript", "svg"):
            self.in_script_or_style = False
            return
        if tag_l == "a" and self.current_href:
            raw_text = " ".join(self.current_link_text)
            clean_text = re.sub(r"\s+", " ", html_lib.unescape(raw_text)).strip()
            context_snippet = " | ".join(self.recent_texts[-5:])
            if len(clean_text) >= 4:
                self.candidates.append((self.current_href, clean_text, context_snippet))
            self.current_href = None
            self.current_link_text = []

    def handle_data(self, data):
        if self.in_script_or_style:
            return
        cleaned = re.sub(r"\s+", " ", html_lib.unescape(data)).strip()
        if cleaned:
            self.recent_texts.append(cleaned)
            if len(self.recent_texts) > 30:
                self.recent_texts.pop(0)
        if self.current_href is not None:
            self.current_link_text.append(data)


class PageTextExtractor(HTMLParser):
    """擷取詳情頁面的純文字與外部連結，讓 AI 讀到精準日期、完整地址與主辦官網"""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.skip = False
        self.texts = []
        self.ext_links = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self.skip = True
            return
        if t == "a":
            href = dict(attrs).get("href", "").strip()
            if href.startswith("http") and urllib.parse.urlparse(href).netloc != urllib.parse.urlparse(self.base_url).netloc:
                if not any(s in href.lower() for s in ("facebook.com/sharer", "twitter.com/intent", "line.me", "google.com/maps")):
                    self.ext_links.append(href)

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self.skip = False

    def handle_data(self, data):
        if not self.skip:
            txt = re.sub(r"\s+", " ", html_lib.unescape(data)).strip()
            if len(txt) >= 2:
                self.texts.append(txt)


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


def fetch_detail_snippet(detail_url: str) -> str:
    """針對活動情報子頁面抓出完整內文與主辦官網連結"""
    try:
        html_text = decode_html_bytes(fetch_url_bytes(detail_url, timeout=12))
        ext = PageTextExtractor(detail_url)
        ext.feed(html_text)
        body_text = " ".join(ext.texts)[:900]
        unique_ext = list(dict.fromkeys(ext.ext_links))[:4]
        links_info = f"頁面外部連結：{', '.join(unique_ext)}" if unique_ext else ""
        return f"{body_text}\n{links_info}".strip()
    except Exception:
        return ""


def parse_web_events(region_hint: str, source_name: str, url: str, limit: int = 12):
    items = []
    try:
        html_text = decode_html_bytes(fetch_url_bytes(url))
        parser = EventLinkParser(url)
        parser.feed(html_text)

        ignore_words = (
            "登入", "註冊", "私隱", "隱私", "條款", "聯絡我們", "關於我們",
            "首頁", "下一頁", "上一頁", "更多", "語言", "繁體", "简体", "English",
            "Facebook", "Instagram", "Twitter", "YouTube", "Cookie", "全部活動",
            "動漫活動日曆", "了解 ANIHK App", "Português", "ภาษาไทย", "Bahasa Indonesia",
            "無障礙旅遊指南", "智能行程規劃"
        )
        seen_local = set()
        for link, text, context in parser.candidates:
            if link == url or link in seen_local:
                continue
            if any(w.lower() == text.lower() for w in ignore_words) or len(text) > 160:
                continue
            if "暫未取得當屆官方圖片" in text:
                continue
            seen_local.add(link)
            items.append(
                {
                    "region_hint": region_hint,
                    "source": source_name,
                    "title": text[:100],
                    "link": link,
                    "pub_date": datetime.now(HKT).strftime("%Y-%m-%d"),
                    "desc": f"來源網站：{source_name}｜連結文字：{text}｜周邊資訊：{context[:200]}",
                    "fetch_detail": True,
                }
            )
            if len(items) >= limit:
                break
        print(f"[Web OK] {source_name}: 擷取到 {len(items)} 個候選項目")
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
            clean_desc = re.sub(r"\s+", " ", clean_desc).strip()[:250]
            if title and link:
                items.append(
                    {
                        "region_hint": region_hint,
                        "source": source_name,
                        "title": title,
                        "link": link,
                        "pub_date": pub_date,
                        "desc": clean_desc,
                        "fetch_detail": False,
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

    model = "@cf/meta/llama-3.1-8b-instruct"
    endpoint = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{model}"

    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是香港、澳門、台灣、深圳動漫、遊戲、同人與ACG文化活動情報整理助手。"
                    "請根據提供的『實際網頁內容』整理活動資訊，絕對不可憑空猜測或捏造年份與場館；若內文沒有寫明日期，start_date 請留空字串。"
                    "若為日本/歐美活動、純網站導覽按鈕、一般美食/賽車/宗教活動或純線上新聞，請只回覆 null。"
                    "若是明確的四地線下ACG活動，請只輸出一個合法 JSON 物件（所有欄位的值都必須是純字串，tags 為字串陣列）："
                    '{"region":"香港|澳門|台灣|深圳",'
                    '"category":"同人即賣會|綜合漫展|遊戲與快閃",'
                    '"title":"活動名稱（繁體中文）",'
                    '"start_date":"YYYY-MM-DD（必須依據內文，無則留空）",'
                    '"end_date":"YYYY-MM-DD",'
                    '"venue":"完整場館名稱與詳細地址（盡量保留樓層與街號）",'
                    '"official_url":"若內文有出現主辦單位官方網址（非新聞網站/非AniHK/非DoujinArk）請填入，否則留空字串",'
                    '"deadline_note":"重要提醒、入場費或售票資訊",'
                    '"tags":["標籤1","標籤2"]}'
                ),
            },
            {"role": "user", "content": str(prompt)[:1600]},
        ],
        "max_tokens": 500,
        "temperature": 0.1,
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
        return to_clean_str(ai_out.get("response"))
    return to_clean_str(ai_out)


def extract_json_object(text: str):
    cleaned = to_clean_str(text)
    if not cleaned or cleaned.lower() == "null":
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def normalize_title_key(title: str) -> str:
    t = to_clean_str(title).lower()
    t = re.sub(r"[\(（\[【].*?[\)）\]】]", "", t)
    for word in ("同人誌即賣會", "同人即賣會", "同人展", "動漫節", "快閃店", "期間限定店", "香港站", "台灣站", "澳門站"):
        t = t.replace(word, "")
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", t)


def find_matching_event(existing_events: list, region: str, title: str, start_date: str):
    new_key = normalize_title_key(title)
    if not new_key:
        return None

    for ev in existing_events:
        if ev.get("region") != region:
            continue
        old_key = normalize_title_key(ev.get("title", ""))
        if not old_key:
            continue

        if new_key == old_key:
            return ev
        if len(new_key) >= 4 and len(old_key) >= 4 and (new_key in old_key or old_key in new_key):
            return ev

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


def is_valid_official_url(url: str, source_link: str) -> bool:
    u = to_clean_str(url)
    if not u.startswith("http"):
        return False
    # 排除新聞媒體與情報聚合站本身，確保 official_url 真的是主辦單位官網
    aggregator_domains = ("hk01.com", "anihk.com", "doujinark.hk", "happymacao.com", "news.google.com", "gnn.gamer.com.tw")
    if any(d in u.lower() for d in aggregator_domains):
        return False
    return u != source_link


def merge_event_details(existing_ev: dict, parsed: dict, source_name: str, link: str):
    updated = False

    new_start = to_clean_str(parsed.get("start_date"))
    new_end = to_clean_str(parsed.get("end_date")) or new_start
    if new_start and existing_ev.get("start_date") in ("", "近期公布"):
        existing_ev["start_date"] = new_start
        existing_ev["end_date"] = new_end or new_start
        updated = True

    new_venue = to_clean_str(parsed.get("venue"))
    old_venue = to_clean_str(existing_ev.get("venue"))
    # 若新來源的地址更詳細（字數較長且包含具體場館），自動更新為更詳細的地址
    if new_venue and (old_venue in ("", existing_ev.get("region")) or len(new_venue) > len(old_venue) + 4):
        existing_ev["venue"] = new_venue
        updated = True

    new_official = to_clean_str(parsed.get("official_url"))
    if is_valid_official_url(new_official, link) and not existing_ev.get("official_url"):
        existing_ev["official_url"] = new_official
        updated = True

    new_note = to_clean_str(parsed.get("deadline_note"))
    old_note = to_clean_str(existing_ev.get("deadline_note"))
    if new_note and new_note != "無" and new_note not in old_note:
        existing_ev["deadline_note"] = f"{old_note} / {new_note}".strip(" /")
        updated = True

    raw_tags = parsed.get("tags")
    new_tags = [to_clean_str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
    old_tags = existing_ev.get("tags") if isinstance(existing_ev.get("tags"), list) else []
    for tag in new_tags:
        if tag and tag not in old_tags and len(old_tags) < 6:
            old_tags.append(tag)
            updated = True
    existing_ev["tags"] = old_tags

    sources = existing_ev.get("verified_sources", [])
    if source_name and source_name not in sources:
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
    for region_hint, source_name, web_url in get_web_sources():
        raw_items.extend(parse_web_events(region_hint, source_name, web_url, limit=12))

    for region_hint, feed_url in DIRECT_RSS_FEEDS:
        raw_items.extend(parse_rss_url(feed_url, region_hint, source_name="巴哈GNN", limit=8))

    for region_hint, query in RSS_QUERIES:
        raw_items.extend(fetch_rss_items(region_hint, query, limit=3))

    added_count = 0
    merged_count = 0

    for item in raw_items:
        if item["link"] in seen_urls:
            continue

        detail_text = ""
        if item.get("fetch_detail"):
            detail_text = fetch_detail_snippet(item["link"])

        prompt = (
            f"地區參考：{item['region_hint']}\n"
            f"情報來源：{item.get('source', '')}\n"
            f"列表標題：{item['title']}\n"
            f"摘要：{item.get('desc', '')}\n"
            f"詳情頁內文：\n{detail_text}\n"
            f"參考日期：{item['pub_date']}"
        )
        try:
            raw_ai = call_cloudflare_ai(prompt)
            parsed = extract_json_object(raw_ai)
        except Exception as e:
            print(f"[AI Warn] {item['title'][:30]}: {e}")
            time.sleep(1.2)
            continue

        if not parsed or not to_clean_str(parsed.get("title")):
            seen_urls.add(item["link"])
            time.sleep(1.0)
            continue

        raw_region = to_clean_str(parsed.get("region"))
        region = raw_region if raw_region in {"香港", "澳門", "台灣", "深圳"} else item["region_hint"]

        raw_cat = to_clean_str(parsed.get("category"))
        category = raw_cat if raw_cat in {"同人即賣會", "綜合漫展", "遊戲與快閃"} else "遊戲與快閃"

        clean_title = to_clean_str(parsed.get("title"))
        start_date = to_clean_str(parsed.get("start_date"))
        end_date = to_clean_str(parsed.get("end_date")) or start_date
        venue = to_clean_str(parsed.get("venue")) or region
        deadline_note = to_clean_str(parsed.get("deadline_note"))
        raw_official = to_clean_str(parsed.get("official_url"))
        official_url = raw_official if is_valid_official_url(raw_official, item["link"]) else ""

        raw_tags = parsed.get("tags")
        tags = [to_clean_str(t) for t in raw_tags if to_clean_str(t)] if isinstance(raw_tags, list) else [region]

        matched_ev = find_matching_event(existing_events, region, clean_title, start_date)
        if matched_ev:
            if merge_event_details(matched_ev, parsed, item.get("source", ""), item["link"]):
                merged_count += 1
                print(f"[Merged] [{region}] {matched_ev['title']} ← 補充自 {item.get('source')}")
            seen_urls.add(item["link"])
            time.sleep(1.0)
            continue

        new_event = {
            "id": f"auto-{int(time.time())}-{added_count}",
            "region": region,
            "category": category,
            "title": clean_title,
            "start_date": start_date or "近期公布",
            "end_date": end_date or start_date or "近期公布",
            "venue": venue,
            "deadline_note": deadline_note,
            "tags": tags[:6],
            "verified_sources": [item.get("source", "Web")],
            "official_url": official_url,
            "url": item["link"],
        }

        existing_events.append(new_event)
        seen_urls.add(item["link"])
        added_count += 1
        print(f"[Added] [{region}] {new_event['title']} ({item.get('source')})")

        time.sleep(1.2)

    data["last_updated"] = datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT")
    data["events"] = existing_events

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"完成更新：新增 {added_count} 筆活動，交叉比對合併 {merged_count} 筆資料。")


if __name__ == "__main__":
    main()
