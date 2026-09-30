import os
import time
import random
import re
import json
import requests
from io import BytesIO
from PIL import Image
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types

# --------------------------------------------------
# 1. 設定値・定数 & API設定
# --------------------------------------------------
API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

client = genai.Client(api_key=API_KEY)

# 検索上限価格（ZIPPOの狙い目・15,000円以下）
MAX_PRICE_LIMIT = 15000

# ★ URLを修正: カテゴリ制限(auccat)を外し、ZIPPO全般から検索
# ① オークション専用URL（最高15,000円 / 競売のみ / 残り時間の短い順）
URL_AUCTION = f"https://auctions.yahoo.co.jp/search/search?p=ZIPPO&max={MAX_PRICE_LIMIT}&is_auction=1&s1=end&o1=a"

# ② 定額/フリマ専用URL（最高15,000円 / 定額即決のみ / 新着順）
URL_FIXED = f"https://auctions.yahoo.co.jp/search/search?p=ZIPPO&max={MAX_PRICE_LIMIT}&is_buynow=1&s1=bids&o1=a"

SEEN_FILE = "seen_zippo.json"

# 件数上限
MAX_AUCTION_ITEMS = 20  # オークション上限20件
MAX_FIXED_ITEMS = 10    # 定額上限10件

# ① 狙い目キーワード（素材・シリアル・限定・人気モチーフ）
TARGET_KEYWORDS = [
    # 素材・限定刻印
    "STERLING", "スターリング", "925", "銀製", "純銀",
    "COPPER", "カッパー", "銅", "チタン", "TITANIUM",
    "シリアル", "NUMBERED", "NO.", "限定", "LIMITED",
    # 年代・仕様
    "1932", "1933", "1941", "レプリカ", "REPLICA", "VINTAGE", "ビンテージ", "ヴィンテージ",
    "ARMOR", "アーマー", "SLIM", "スリム",
    # コラボ・モチーフ
    "ジブリ", "ハーレー", "HARLEY", "アニメ", "コラボ", "メタル", "貼り"
]

# ② NGキーワード（致命的な破損・社外品）
NG_KEYWORDS = [
    "ヒンジ破損", "ヒンジ外れ", "ヒンジ取れ",
    "蓋閉まらない", "変形大", "大きな凹み", "潰れ",
    "社外インサイドユニット", "インナー社外",
    "コピー", "偽物", "レプリカ品（非純正）"
]

# --------------------------------------------------
# 2. ヘルパー関数（既読管理・通知）
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
    """Geminiの目利き結果を含めてDiscordへ通知"""
    if not DISCORD_WEBHOOK_URL:
        print("❌ Discord Webhook URLが未設定です")
        return

    risk_text = " / ".join(g_result.get('risk_flags', [])) if g_result.get('risk_flags') else "特筆なし"

    message = f"""
🔥 **【ヤフオク】仕入れ候補 ZIPPO 発見！** 🔥
----------------------------------------
🏪 **出品者**: {seller_name}
📌 **商品名**: {item['title']}
💰 **現在/即決価格**: {item['price']:,}円 ({item['sale_type']})
🏷️ **ヒット属性**: {item['matched_keyword']}
🔗 **URL**: {item['url']}

💎 **素材判定**: {g_result.get('material_type', '不明')}
🔢 **シリアル/限定**: {g_result.get('serial_or_edition', 'なし/不明')}
🔍 **底面刻印(ボトム)**: {g_result.get('bottom_stamp', '確認不可')}
📊 **総合評価**: **{g_result.get('condition_score', '-')}**
⚠️ **状態フラグ**: {risk_text}

💵 **想定売価**: {g_result.get('estimated_resale_normal', '-')}円
🎯 **推奨購入上限額**: **{g_result.get('max_bid_price_target', '-')}円**
💡 **目利き添削理由**: {g_result.get('reasoning', '-')}
----------------------------------------
"""
    payload = {"content": message}
    headers = {"Content-Type": "application/json"}
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, data=json.dumps(payload), headers=headers, timeout=10)
        if res.status_code in [200, 204]:
            print(f"✅ Discord通知送信完了: {item['title'][:20]}")
        else:
            print(f"❌ Discord通知エラー: {res.status_code}, {res.text}")
    except Exception as e:
        print(f"❌ Discord送信例外: {e}")

# --------------------------------------------------
# 3. Gemini 2.5 Flash による画像・刻印・相場解析
# --------------------------------------------------
def analyze_zippo_with_gemini(title, description, images, price):
    prompt = f"""
あなたはZIPPO（ジッポー）ライターのヴィンテージ・限定品の鑑定および転売目利き専門家です。
添付された商品画像（特に底面のボトム刻印やシリアル刻印）と商品タイトル・説明文を詳細に解析し、仕入れ判定を行ってください。

【確認・査定ポイント】
1. 底面刻印（ボトム刻印）の読み取り:
   - "STERLING" や "925" の刻印があるか（スターリングシルバー判定）。
   - シリアルナンバー（例: 0123/1000 や NO. 0450 等）の刻印があるか。
   - 年代刻印（ローマ字 I〜L、月、年数など）を特定。
2. 状態・外観チェック:
   - ケースの著しい凹みやヒンジの破損、ヒンジピンの脱落がないか。
   - インサイドユニット（中身）が純正か。

【利益判定の計算】
- 現在の出品価格は【 {price} 円 】です。
- メルカリ等での想定売価から、手数料10%、送料梱包代350円、仕入れ送料500円を一律コストとし、仕入れ推奨上限額を計算してください。
- 現在価格（{price}円）が推奨上限額を超えている場合、または利益が出ない（赤字）場合は、絶対に「C（不可）」と判定してください。

以下のJSON形式でのみ回答してください：
{{
  "brand": "ZIPPO",
  "material_type": "スターリングシルバー / 銅(COPPER) / チタン / 真鍮・レギュラー / 不明",
  "serial_or_edition": "シリアル刻印あり / 限定モデル / レギュラー",
  "bottom_stamp": "底面刻印の判別結果（例: STERLING 1995年製 など）",
  "risk_flags": ["キズあり", "小凹みあり", "インサイドユニット純正", "着火未確認" などの状態フラグ],
  "condition_score": "A（推奨） / B（慎重） / C（不可）",
  "estimated_resale_normal": 12000,
  "max_bid_price_target": 7500,
  "reasoning": "刻印判定・素材・外観状態と仕入れ判定理由（100文字以内）"
}}

【商品タイトル】: {title}
【商品説明文】: {description}
"""

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=images + [prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1
                )
            )
            return json.loads(response.text)
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 3
                print(f"  ⚠️ Gemini一時的エラー。{wait_time}秒後に再試行します... ({attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print(f"❌ Gemini解析失敗: {e}")
                return None

# --------------------------------------------------
# 4. 詳細ページ情報・画像取得
# --------------------------------------------------
def fetch_detail_page(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=15000)
    time.sleep(1)
    
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    
    seller_tag = soup.find("a", class_=re.compile("Seller__name|Seller__link")) or soup.find("p", class_=re.compile("Seller"))
    seller_name = seller_tag.text.strip() if seller_tag else "不明出品者"
    
    desc_tag = soup.find("div", class_="ProductExplanation__commentArea") or soup.find("section", class_="ProductExplanation")
    description = desc_tag.text.strip() if desc_tag else "説明文なし"
    
    img_urls = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "auctions.c.yimg.jp" in src:
            clean_url = src.split("?")[0]
            if clean_url not in img_urls and not clean_url.endswith(".gif"):
                img_urls.append(clean_url)
                
    img_urls = img_urls[:5] # 底面刻印含む画像取得のため上限5枚
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
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
# 5. 商品処理の共通関数
# --------------------------------------------------
def process_target_list(page, target_url, max_limit, sale_type_label, seen_items):
    print(f"\n🔍 【{sale_type_label}】検索URLへアクセス中: {target_url}")
    page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
    
    # スクロールして要素を確実に読み込ませる
    for _ in range(3):
        page.evaluate("window.scrollBy(0, 1000)")
        time.sleep(0.5)
        
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    
    # ★ 複数のセレクターに対応させて取得漏れを防止
    items = soup.select(".Product") or soup.select("li.Product") or soup.select(".Product__item")
    print(f"📦 検出件数: {len(items)}件（上位{max_limit}件を精査）")

    processed_count = 0

    for item in items:
        if processed_count >= max_limit:
            print(f"⏱️ 【{sale_type_label}】の上限{max_limit}件に達したため完了。")
            break

        title_tag = item.select_one(".Product__titleLink") or item.select_one("a[data-auction-title]") or item.select_one("h3 a")
        price_tag = item.select_one(".Product__priceValue") or item.select_one(".Product__price") or item.select_one("span.Price__value")

        if not (title_tag and price_tag):
            continue

        title = title_tag.get_text(strip=True)
        price_raw = price_tag.get_text(strip=True)
        
        # 数字のみ抽出
        price_digits = re.sub(r"[^\d]", "", price_raw)
        if not price_digits:
            continue
        price = int(price_digits)

        # 価格上限ガード
        if price > MAX_PRICE_LIMIT:
            continue

        url = title_tag.get("href", "")
        item_id = item.get("data-auction-id") or (url.split("/")[-1] if url else None)

        if not item_id:
            continue

        # 既読スキップ
        if item_id in seen_items:
            continue

        # ① NGキーワードチェック（タイトル）
        if any(ng.lower() in title.lower() for ng in NG_KEYWORDS):
            continue

        # ② ターゲットキーワードのチェック
        matched_keyword = None
        for kw in TARGET_KEYWORDS:
            if kw.lower() in title.lower():
                matched_keyword = kw
                break

        if not matched_keyword:
            continue

        processed_count += 1
        print(f"🎯 狙い目 [{sale_type_label} {processed_count}/{max_limit}]: [{matched_keyword}] | 価格: {price}円 | {title[:25]}...")

        # ③ 詳細ページ取得＆Gemini解析
        try:
            seller_name, description, images = fetch_detail_page(page, url)
            
            text_to_check = f"{title} {description}".lower()
            found_ng = [ng for ng in NG_KEYWORDS if ng.lower() in text_to_check]
            if found_ng:
                print(f"  ⏩ 本文NGワード検出のためスキップ: {', '.join(found_ng)}")
                seen_items.add(item_id)
                save_seen_items(seen_items)
                continue

            print("🤖 Gemini 2.5 Flashで底面刻印・利益試算中...")
            g_result = analyze_zippo_with_gemini(title, description, images, price)

            if g_result:
                score = g_result.get("condition_score", "")
                material = g_result.get("material_type", "")
                
                try:
                    max_target = int(g_result.get('max_bid_price_target', 0))
                except Exception:
                    max_target = 0

                # 赤字判定補正
                if price >= max_target and max_target > 0:
                    print(f"  ⚠️ 赤字判定補正: 現在価格({price}円) >= 推奨上限額({max_target}円)")
                    score = "C（不可）"

                print(f"  └ 最終判定: {score} | 素材: {material} | 推奨上限: {max_target}円")
                print(f"  └ 添削理由: {g_result.get('reasoning')}")

                # 判定が A または B のみ Discord通知
                if "A" in score or "B" in score:
                    item_data = {
                        "id": item_id,
                        "title": title,
                        "price": price,
                        "sale_type": sale_type_label,
                        "matched_keyword": matched_keyword,
                        "url": url
                    }
                    send_discord_notification(item_data, g_result, seller_name)
                else:
                    print("  ⏩ スルー（赤字/不適格のため通知なし）")

            seen_items.add(item_id)
            save_seen_items(seen_items)

        except Exception as e:
            print(f"❌ 詳細解析エラー: {e}")

        time.sleep(1)

# --------------------------------------------------
# 6. メイン実行処理
# --------------------------------------------------
def main():
    print("🚀 ヤフオク ZIPPO仕入れリサーチ（Gemini 2.5 Flash版）を開始します...")
    seen_items = load_seen_items()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # 1. オークション（残り時間短い順：20件）
        process_target_list(page, URL_AUCTION, MAX_AUCTION_ITEMS, "オークション(終了間近)", seen_items)

        # 2. 定額/フリマ（新着順：10件）
        process_target_list(page, URL_FIXED, MAX_FIXED_ITEMS, "定額/フリマ(新着順)", seen_items)

        browser.close()

    print("\n✨ すべてのリサーチが正常に完了しました。")

if __name__ == "__main__":
    main()
