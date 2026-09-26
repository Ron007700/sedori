import os
import json
import time
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TARGET_URL = "https://www.2ndstreet.jp/search?category=900001&keyword=G-SHOCK"
SEEN_FILE = "seen_items_2ndstreet.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL_2NDSTREET")

KEYWORDS = [
    "TWIN SENSOR", "TRIPLE SENSOR", "ALTI", "SURF",
    "オレンジ", "ピンク", "ネイビー", "グリーン", "イエロー",
    "迷彩", "カモフラ", "マーブル", "グラデーション",
    "フロッグマン", "FROGMAN", "マッドマン", "ガルフマン", "レイズマン",
    "スカイフォース", "DW-6700", "DW-002", "DW-003", "DW-004", "DW-8800", "DW-9000",
    "ラバーズコレクション", "ラバコレ", "イルクジ", "コラボ",
    "タフソーラー", "電波ソーラー"
]

def load_seen_items():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Error loading seen items: {e}")
            return set()
    return set()

def save_seen_items(seen):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving seen items: {e}")

def send_discord_notification(item):
    if not DISCORD_WEBHOOK_URL:
        print("❌ Discord Webhook URL is missing from Secrets!")
        return

    payload = {
        "content": f"🚨 **【セカスト】利益候補 G-SHOCK 発見！** 🚨\n\n"
                   f"**商品名**: {item['title']}\n"
                   f"**価格**: {item['price']:,}円\n"
                   f"**キーワード**: {item['matched_keyword']}\n"
                   f"**URL**: {item['url']}"
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        if res.status_code == 204:
            print(f"✅ Discordへ通知送信完了: {item['title']}")
        else:
            print(f"❌ Discord通知エラー: {res.status_code}, {res.text}")
    except Exception as e:
        print(f"Error sending Discord notification: {e}")

def get_html_with_playwright():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)
        html = page.content()
        browser.close()
        return html

def main():
    print("🚀 スクレイピングを開始します...")
    seen_items = load_seen_items()

    try:
        html = get_html_with_playwright()
    except Exception as e:
        print(f"Error fetching page with Playwright: {e}")
        return

    soup = BeautifulSoup(html, "html.parser")
    
    # 🔍 デバッグ情報の出力
    print(f"📄 ページタイトル: {soup.title.string if soup.title else 'タイトルなし'}")
    all_links = soup.find_all("a", href=True)
    print(f"🔗 検出した全リンク数: {len(all_links)}件")
    
    sample_hrefs = [a['href'] for a in all_links[:10]]
    print(f"👀 リンクのサンプル（先頭10件）: {sample_hrefs}")

    # 商品カードの汎用判定
    items = []
    for a in all_links:
        href = a['href']
        if any(path in href for path in ["/goods/", "/item/", "/search/"]):
            parent = a.find_parent("li") or a.find_parent("div")
            if parent and parent not in items:
                items.append(parent)

    print(f"📦 取得した商品件数: {len(items)}件")

    new_matches = []
    for item in items:
        link_tag = item if item.name == "a" else item.find("a", href=True)
        if not link_tag:
            continue

        title = item.get_text(" ", strip=True)
        
        import re
        price_match = re.search(r"[￥¥]\s*([\d,]+)", title) or re.search(r"([\d,]+)\s*円", title)
        if not price_match:
            continue

        price_str = price_match.group(1).replace(",", "")
        try:
            price = int(price_str)
        except ValueError:
            continue

        href = link_tag.get("href", "")
        url = href if href.startswith("http") else "https://www.2ndstreet.jp" + href
        item_id = url.split("?")[0].split("/")[-1]

        if item_id in seen_items:
            continue

        matched = None
        for kw in KEYWORDS:
            if kw.lower() in title.lower():
                matched = kw
                break

        if price <= 8000 and matched:
            new_matches.append({
                "id": item_id,
                "title": title[:50] + "..." if len(title) > 50 else title,
                "price": price,
                "matched_keyword": matched,
                "url": url
            })

    print(f"🎯 条件に合致した新着商品: {len(new_matches)}件")

    for match in new_matches:
        send_discord_notification(match)
        seen_items.add(match["id"])

    save_seen_items(seen_items)
    print("✨ 処理が正常に完了しました。")

if __name__ == "__main__":
    main()
