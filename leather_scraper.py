import json
import os
import re
import time
from io import BytesIO

from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from google.genai.errors import APIError
from PIL import Image
from playwright.sync_api import sync_playwright
import requests

# --------------------------------------------------
# 1. 設定値・定数 & API設定
# --------------------------------------------------
API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# APIキー未設定時の安全化
client = genai.Client(api_key=API_KEY) if API_KEY else None

# 検索条件
MIN_PRICE_LIMIT = 1000   # 下限価格（1,000円）
MAX_PRICE_LIMIT = 10000  # 上限価格（10,000円）
MAX_AUCTION_ITEMS = 30   # 1回の巡回でチェックする上限件数

# ヤフオク検索キーワード（「スレ」のみに設定）
SEARCH_QUERIES = [
    "スレ",
]

SEEN_FILE = "seen_leather.json"

# レザー小物用のターゲットキーワード（タイトル部分一致）
TARGET_KEYWORDS = [
    "スレ",
    "角スレ",
    "擦れ",
    "色落ち",
    "キズ",
    "傷",
    "汚れ",
    "ジャンク",
]

# リペア不可・困難なNGキーワード（タイトル・本文共通）
NG_KEYWORDS = [
    "破れ",
    "ちぎれ",
    "べたつき",
    "ベタつき",
    "エナメル",
    "スエード",
    "ヌメ革",
    "生地破れ",
    "パイピング飛び出し",
]


# --------------------------------------------------
# 2. ヘルパー関数
# --------------------------------------------------
def load_seen_items():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"⚠️ 既読ファイルの読み込みエラー: {e}")
            return set()
    return set()


def save_seen_items(seen):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 既読ファイルの保存エラー: {e}")


def send_discord_notification(item, g_result, seller_name):
    if not DISCORD_WEBHOOK_URL:
        print("❌ Discord Webhook URLが未設定です")
        return

    risk_text = (
        " / ".join(g_result.get("risk_flags", []))
        if g_result.get("risk_flags")
        else "特筆なし"
    )

    message = f"""
🟢 **【ヤフオク】仕入れ候補（黒革小物）発見！** 🟢
----------------------------------------
🏪 **出品者**: {seller_name}
📌 **商品名**: {item['title']}
💰 **現在価格**: {item['price']:,}円 ({item['sale_type']})
⏰ **残り時間**: {item['time_left']}
🏷 **ヒット属性**: {item['matched_keyword']}
🔗 **URL**: {item['url']}

🎨 **検出カラー**: {g_result.get('color', '不明')}
🧱 **素材タイプ**: {g_result.get('material_type', '不明')}
📊 **リペア評価**: **{g_result.get('condition_score', '-')}**
⚠ **状態フラグ**: {risk_text}

🛠 **推奨リペア手順**: {g_result.get('repair_method', '特になし')}
💵 **想定売価**: {g_result.get('estimated_resale_normal', '-')}円
🎯 **推奨購入上限額**: **{g_result.get('max_bid_price_target', '-')}円**
💡 **目利き添削理由**: {g_result.get('reasoning', '-')}
----------------------------------------
"""
    payload = {"content": message}
    headers = {"Content-Type": "application/json"}
    try:
        res = requests.post(
            DISCORD_WEBHOOK_URL,
            data=json.dumps(payload),
            headers=headers,
            timeout=10,
        )
        if res.status_code in [200, 204]:
            print(f"✅ Discord通知送信完了: {item['title'][:20]}")
        else:
            print(f"❌ Discord通知エラー: {res.status_code}, {res.text}")
    except Exception as e:
        print(f"❌ Discord送信例外: {e}")


# --------------------------------------------------
# 3. Gemini 解析 (429エラーハンドリング付き)
# --------------------------------------------------
def analyze_leather_with_gemini(title, description, images, price):
    if not client:
        print("❌ Gemini APIキーが読み込めていません")
        return None

    prompt = f"""
あなたはブランド革小物のリペア転売プロフェッショナルです。
添付された商品画像（最大8枚）と商品タイトル・説明文を詳細に解析し、仕入れ判定を行ってください。

【確認・査定ポイント】
1. カラー・素材判定:
   - メインカラーが「ブラック（黒）」または「極めて濃いダークブラウン」であるか？（赤・パステル・ベージュ・柄物はNG）
   - 補色クリームが馴染むツヤ革・型押し革（スムースレザー、エピ、タイガ等）か？（布・スエード・エナメルはNG）
2. ダメージ判定:
   - 買い: ダメージが「四隅（角）のスレ」「フチの色落ち」「表面の小傷・乾燥」程度で、サフィールやシュプリームクリームで数分で綺麗になるレベル。
   - 不可: 革が破れて内部の芯材・布が見えている、ちぎれている、表面全体が著しくハゲている、内部の重度ベタつき。

【利益判定の計算】
- 現在の出品価格は【 {price} 円 】です。
- メルカリ等での想定売価から、手数料10%、送料梱包代350円、仕入れ送料500円、リペア資材代200円を一律コストとし、仕入れ推奨上限額を計算してください。
- 現在価格（{price}円）が推奨上限額を超えている場合、または利益が出ない（赤字）場合は、絶対に「C（不可）」と判定してください。

以下のJSON形式でのみ回答してください：
{{
  "color": "ブラック / ダークブラウン / その他",
  "material_type": "スムースレザー / 型押しレザー / エナメル・布地など(不可)",
  "risk_flags": ["角スレあり", "フチ色落ち", "破れあり", "金属サビ" などの状態フラグ],
  "condition_score": "A（推奨） / B（慎重） / C（不可）",
  "repair_method": "例: サフィール黒での角補色＋シュプリームでの全体栄養補給",
  "estimated_resale_normal": 8500,
  "max_bid_price_target": 4500,
  "reasoning": "判定理由とリペア容易性のコメント（100文字以内）"
}}

【商品タイトル】: {title}
【商品説明文】: {description}
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=images + [prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0.1
            ),
        )
        return json.loads(response.text)

    except APIError as e:
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            print("⚠ 【Quota上限検知】Gemini APIの無料枠(1日あたりの上限)に達しました。")
            print("   以降の解析を中断し、スクリプトを安全に終了します。")
            return "QUOTA_EXCEEDED"
        else:
            print(f"❌ Gemini APIエラー: {e}")
            return None
    except Exception as e:
        print(f"❌ Gemini解析例外: {e}")
        return None


# --------------------------------------------------
# 4. 詳細ページ取得（最大8枚の画像取得）
# --------------------------------------------------
def fetch_detail_page(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=15000)
    time.sleep(1)

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    seller_tag = soup.select_one(".Seller__name, .Seller__link, [class*='Seller']")
    seller_name = seller_tag.get_text(strip=True) if seller_tag else "不明出品者"

    desc_tag = soup.select_one(".ProductExplanation__commentArea, .ProductExplanation")
    description = desc_tag.get_text(strip=True) if desc_tag else "説明文なし"

    img_urls = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "auctions.c.yimg.jp" in src:
            clean_url = src.split("?")[0]
            if clean_url not in img_urls and not clean_url.endswith(".gif"):
                img_urls.append(clean_url)

    img_urls = img_urls[:8]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
    }
    images = []
    for img_url in img_urls:
        try:
            res = requests.get(img_url, headers=headers, timeout=5)
            if res.status_code == 200:
                img = Image.open(BytesIO(res.content))
                img.thumbnail((600, 600))
                images.append(img)
        except Exception:
            continue

    return seller_name, description, images


# --------------------------------------------------
# 5. 商品処理のメイン関数
# --------------------------------------------------
def process_auction_list(page, query, max_limit, seen_items):
    # auccat=23004 : ファッション小物
    # s1=end&o1=a : 残り時間の短い順
    # min=1000, max=10000 : 1,000円〜10,000円
    target_url = (
        f"https://auctions.yahoo.co.jp/search/search?"
        f"p={requests.utils.quote(query)}"
        f"&auccat=23004"
        f"&min={MIN_PRICE_LIMIT}"
        f"&max={MAX_PRICE_LIMIT}"
        f"&s1=end&o1=a"
        f"&n=50"
    )

    print(f"\n🔍 【検索ワード: {query}】アクセス中: {target_url}")

    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"❌ ページ移動エラー: {e}")
        return True

    time.sleep(2)

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("li.Product") or soup.select(".Product")

    if not items:
        print("⚠️ 商品要素が見つかりませんでした。")
        return True

    print(f"📦 検出件数: {len(items)}件")

    processed_count = 0

    for idx, item in enumerate(items, 1):
        if processed_count >= max_limit:
            print(f"⏱ 上限{max_limit}件に達したため本キーワードの処理完了。")
            break

        title_tag = item.select_one(".Product__titleLink") or item.select_one("a")
        if not title_tag:
            continue
        title = title_tag.get_text(strip=True)
        url = title_tag.get("href", "")

        price_tag = item.select_one(".Product__priceValue") or item.select_one("[class*='price']")
        if not price_tag:
            continue
        price_digits = re.sub(r"[^\d]", "", price_tag.get_text(strip=True))
        if not price_digits:
            continue
        price = int(price_digits)

        time_tag = item.select_one(".Product__time") or item.select_one("[class*='time']")
        time_text = (
            time_tag.get_text(strip=True)
            if time_tag
            else item.get_text(" ", strip=True)
        )

        item_id = item.get("data-auction-id")
        if not item_id and url:
            match = re.search(r"/auction/([a-zA-Z0-9]+)", url)
            item_id = match.group(1) if match else url

        if item_id in seen_items:
            continue

        matched_keyword = None
        for kw in TARGET_KEYWORDS:
            if kw.lower() in title.lower():
                matched_keyword = kw
                break

        if not matched_keyword:
            continue

        if "日" in time_text:
            continue

        minutes_left = None
        hour_match = re.search(r"(\d+)時間(?:(\d+)分)?", time_text)
        min_match = re.search(r"^(\d+)分|[\s](\d+)分|(\d+)分", time_text)

        if hour_match:
            hours = int(hour_match.group(1))
            mins = int(hour_match.group(2)) if hour_match.group(2) else 0
            minutes_left = hours * 60 + mins
        elif min_match:
            mins_val = next(g for g in min_match.groups() if g is not None)
            minutes_left = int(mins_val)

        if minutes_left is None or not (10 <= minutes_left < 180):
            continue

        if minutes_left >= 60:
            time_left_str = f"{minutes_left // 60}時間{minutes_left % 60}分"
        else:
            time_left_str = f"{minutes_left}分"

        if any(ng.lower() in title.lower() for ng in NG_KEYWORDS):
            continue

        processed_count += 1
        print(
            f"\n🎯 ターゲット検知 [{processed_count}/{max_limit}]:"
            f" 残り{time_left_str} | [{matched_keyword}] | {price}円 |"
            f" {title[:25]}..."
        )

        try:
            seller_name, description, images = fetch_detail_page(page, url)

            text_to_check = f"{title} {description}".lower()
            found_ng = [ng for ng in NG_KEYWORDS if ng.lower() in text_to_check]
            if found_ng:
                print(f"  ⏩ 本文NGワードのためスキップ: {', '.join(found_ng)}")
                seen_items.add(item_id)
                save_seen_items(seen_items)
                continue

            print("🤖 Geminiで黒革・リペア適性AI査定中...")
            g_result = analyze_leather_with_gemini(title, description, images, price)

            if g_result == "QUOTA_EXCEEDED":
                print("⛔ 制限のため処理をここで安全に停止します。")
                return False

            if g_result:
                score = g_result.get("condition_score", "")
                color = g_result.get("color", "")

                try:
                    max_target = int(g_result.get("max_bid_price_target", 0))
                except Exception:
                    max_target = 0

                if price >= max_target and max_target > 0:
                    print(
                        f"  ⚠ 赤字判定補正: 現在価格({price}円) >= 推奨上限額({max_target}円)"
                    )
                    score = "C（不可）"

                print(
                    f"  └ 最終判定: {score} | 検出色: {color} | 推奨上限:"
                    f" {max_target}円"
                )
                print(f"  └ 添削理由: {g_result.get('reasoning')}")

                if "A" in score or "B" in score:
                    item_data = {
                        "id": item_id,
                        "title": title,
                        "price": price,
                        "sale_type": "ヤフオク",
                        "matched_keyword": matched_keyword,
                        "time_left": time_left_str,
                        "url": url,
                    }
                    send_discord_notification(item_data, g_result, seller_name)
                else:
                    print("  ⏩ スルー（利益条件またはリペア条件未達）")

            seen_items.add(item_id)
            save_seen_items(seen_items)

        except Exception as e:
            print(f"❌ 詳細解析エラー: {e}")

        time.sleep(1)

    return True


# --------------------------------------------------
# 6. メイン実行処理
# --------------------------------------------------
def main():
    print("🚀 ヤフオク 黒革小物リペア仕入れリサーチを開始します...")
    seen_items = load_seen_items()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="ja-JP",
        )
        page = context.new_page()

        for query in SEARCH_QUERIES:
            continue_flag = process_auction_list(
                page, query, MAX_AUCTION_ITEMS, seen_items
            )
            if not continue_flag:
                break

        browser.close()

    print("\n✨ すべてのリサーチが正常に完了しました。")


if __name__ == "__main__":
    main()
