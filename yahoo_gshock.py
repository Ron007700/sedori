import os
import json
import math
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# --------------------------------------------------
# 設定値・定数
# --------------------------------------------------
# ヤフオク検索URL（G-SHOCK ＆ 最高8,000円で絞り込み）
TARGET_URL = "https://auctions.yahoo.co.jp/search/search?p=G-SHOCK&max=8000"
SEEN_FILE = "seen_items_yahoo.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# ① 狙い目キーワード（セカスト条件を継承）
TARGET_KEYWORDS = [
    "TWIN SENSOR", "TRIPLE SENSOR", "ALTI", "SURF",
    "オレンジ", "ピンク", "ネイビー", "グリーン", "イエロー",
    "迷彩", "カモフラ", "マーブル", "グラデーション",
    "フロッグマン", "FROGMAN", "マッドマン", "ガルフマン", "レイズマン",
    "スカイフォース", "DW-6700", "DW-002", "DW-003", "DW-004", "DW-8800", "DW-9000",
    "ラバーズコレクション", "ラバコレ", "イルクジ", "コラボ",
    "タフソーラー", "電波ソーラー"
]

# ② NGキーワード（除外条件）
NG_KEYWORDS = [
    "加水分解", "割れ", "ベタつき", "ベタツキ",
    "ベゼル欠品", "ベゼル破損", "ベゼル割れ", "ベゼル不良",
    "遊環なし", "遊環欠品", "リング欠損",
    "ガラス傷", "ガラスキズ", "風防欠け", "風防傷",
    "黄ばみ", "色あせ", "変色", "日焼け",
    "CHG", "充電不足",
    "不動", "ジャンク", "動作未確認"
]

# --------------------------------------------------
# ヘルパー関数
# --------------------------------------------------
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

def check_item_eligibility(title, buy_price, description=""):
    """仕入れ条件・NGワード・利益計算の判定関数"""
    text_to_check = f"{title} {description}".lower()

    # 1. NGキーワードチェック
    found_ng_words = [kw for kw in NG_KEYWORDS if kw.lower() in text_to_check]
    if found_ng_words:
        return {"result": False, "reason": f"NGワード検知: {', '.join(found_ng_words)}"}

    # 2. 狙い目キーワードチェック
    matched_keyword = None
    for kw in TARGET_KEYWORDS:
        if kw.lower() in text_to_check:
            matched_keyword = kw
            break
            
    if not matched_keyword:
        return {"result": False, "reason": "ターゲットキーワード不一致"}

    # 3. 種別判定 (ソーラーかクォーツか)
    is_solar = ("ソーラー" in text_to_check) or ("solar" in text_to_check)
    item_type = "solar" if is_solar else "quartz"
    target_profit = 100 if is_solar else 1500

    # 4. 損益計算（相殺計算）
    # ※ヤフオクでの想定売値を「仕入れ価格に対して利益が出る相場値」として最低相場ラインを試算
    shipping_fee = 450  # 宅急便コンパクト
    fee_rate = 0.10     # 手数料10%

    # 利益＋原価相殺に必要な最低想定販売価格
    min_required_sell_price = math.ceil((buy_price + shipping_fee + target_profit) / (1 - fee_rate))

    return {
        "result": True,
        "item_type": item_type,
        "matched_keyword": matched_keyword,
        "target_profit": target_profit,
        "min_required_sell_price": min_required_sell_price
    }

def send_discord_notification(item):
    if not DISCORD_WEBHOOK_URL:
        print("❌ Discord Webhook URL is missing!")
        return

    payload = {
        "content": f"🚨 **【ヤフオク】利益候補 G-SHOCK 発見！** 🚨\n\n"
                   f"**商品名**: {item['title']}\n"
                   f"**現在価格**: {item['price']:,}円\n"
                   f"**ヒット属性**: {item['matched_keyword']} ({item['item_type']})\n"
                   f"**目安最低売値**: {item['min_required_sell_price']:,}円以上で利益獲得\n"
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

# --------------------------------------------------
# メイン処理
# --------------------------------------------------
def get_html_with_playwright():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
        html = page.content()
        browser.close()
        return html

def main():
    print("🚀 ヤフオク スクレイピングを開始します...")
    seen_items = load_seen_items()

    try:
        html = get_html_with_playwright()
    except Exception as e:
        print(f"Error fetching page with Playwright: {e}")
        return

    soup = BeautifulSoup(html, "html.parser")
    # ヤフオクの商品リスト要素を取得
    items = soup.select(".Product") or soup.select("li.Product")

    print(f"📦 取得した商品件数: {len(items)}件")
    new_matches = []

    for item in items:
        title_tag = item.select_one(".Product__titleLink")
        price_tag = item.select_one(".Product__priceValue")

        if not (title_tag and price_tag):
            continue

        title = title_tag.get_text(strip=True)
        price_str = price_tag.get_text(strip=True).replace("円", "").replace(",", "").replace("即決", "").strip()

        try:
            price = int(price_str)
        except ValueError:
            continue

        url = title_tag.get("href", "")
        # ヤフオクの商品ID抽出（オークションID）
        item_id = item.get("data-auction-id") or url.split("/")[-1]

        if item_id in seen_items:
            continue

        # 8,000円以下チェック ＋ NG判定 ＋ ターゲットキーワード判定 ＋ 損益チェック
        if price <= 8000:
            eligibility = check_item_eligibility(title, price)
            if eligibility["result"]:
                new_matches.append({
                    "id": item_id,
                    "title": title,
                    "price": price,
                    "matched_keyword": eligibility["matched_keyword"],
                    "item_type": eligibility["item_type"],
                    "min_required_sell_price": eligibility["min_required_sell_price"],
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
