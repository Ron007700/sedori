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

# ヤフオク検索URL（G-SHOCK ＆ 最高8,000円 ＆ オークション＋定額/フリマの両方を対象 ＆ 終了が近い順）
TARGET_URL = "https://auctions.yahoo.co.jp/search/search?p=G-SHOCK&max=8000&auccat=23140&select=22&s1=end&o1=a"
SEEN_FILE = "seen_items_yahoo.json"

# ① 狙い目キーワード
TARGET_KEYWORDS = [
    "TWIN SENSOR", "TRIPLE SENSOR", "ALTI", "SURF",
    "オレンジ", "ピンク", "ネイビー", "グリーン", "イエロー",
    "迷彩", "カモフラ", "マーブル", "グラデーション",
    "フロッグマン", "FROGMAN", "マッドマン", "ガルフマン", "レイズマン",
    "スカイフォース", "DW-6700", "DW-002", "DW-003", "DW-004", "DW-8800", "DW-9000",
    "ラバーズコレクション", "ラバコレ", "イルクジ", "コラボ",
    "タフソーラー", "電波ソーラー", "ソーラー"
]

# ② NGキーワード（CHG・充電不足・不動・ジャンク・動作未確認・破損等を排除）
NG_KEYWORDS = [
    "CHG", "充電不足",
    "不動", "ジャンク", "動作未確認",
    "加水分解", "割れ", "ベタつき", "ベタツキ",
    "ベゼル欠品", "ベゼル破損", "ベゼル割れ", "ベゼル不良",
    "遊環なし", "遊環欠品", "リング欠損",
    "ガラス傷", "ガラスキズ", "風防欠け", "風防傷",
    "黄ばみ", "色あせ", "変色", "日焼け"
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
    """Geminiの添削結果を含めてDiscordへ通知"""
    if not DISCORD_WEBHOOK_URL:
        print("❌ Discord Webhook URLが未設定です")
        return

    risk_text = " / ".join(g_result.get('risk_flags', [])) if g_result.get('risk_flags') else "なし"

    message = f"""
🚨 **【ヤフオク】仕入れ候補 G-SHOCK 発見！** 🚨
----------------------------------------
🏪 **出品者/ストア**: {seller_name}
📌 **商品名**: {item['title']}
💰 **現在/即決価格**: {item['price']:,}円 ({item['sale_type']})
🏷️ **ヒット属性**: {item['matched_keyword']}
🔗 **URL**: {item['url']}

🏷️ **モデル特定**: {g_result.get('brand', 'CASIO')} / {g_result.get('model', '不明')}
🛡️ **純正性判定**: {g_result.get('authenticity_status', '不明')}
📊 **総合評価**: **{g_result.get('condition_score', '-')}**
🏷️ **状態・注記フラグ**: {risk_text}

💵 **想定売価**: {g_result.get('estimated_resale_normal', '-')}円
🎯 **推奨落札/購入上限額**: **{g_result.get('max_bid_price_target', '-')}円**
💡 **目利き添削・状態感**: {g_result.get('reasoning', '-')}
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
# 3. Gemini 3.6 Flash による画像添削・真贋・相場推論
# --------------------------------------------------
def analyze_gshock_with_gemini(title, description, images, price):
    prompt = f"""
あなたはG-SHOCKおよびブランドウォッチの転売・仕入れ目利き専門家です。
添付された商品画像と商品タイトル・説明文を詳細に添削・解析し、社外品や偽物を排除した上で、利益が見込めるか仕入れ判定を行ってください。

【厳格排除ルール：社外品・MOD・偽物の排除】
- 社外パーツ・不審点のチェック：社外メタルベゼル、社外ベゼル/ベルト（ベゼル刻印のフォント違和感・粗悪印刷）、カスタムMOD品でないか画像で厳密に確認。
- 偽物チェック：裏蓋刻印の浅さ、ボタンの配置・形状違和感、液晶表示の不自然さをチェック。
- 社外パーツ使用（純正でない）や偽物の疑いがある場合は、判定を「C（不可）」にしてください。

【外観ダメージ・状態の添削】
- ベゼル/ベルトの加水分解（割れ・ベタつき・ひび割れ）の有無。
- 風防（ガラス）の目立つキズ、液晶漏れ・文字盤ダメージの有無。

【売価リサーチと推奨価格算出（現在価格/即決価格: {price}円）】
- メルカリ・ヤフオク等の中古相場を基に「想定売価」を算出してください。
- 手数料10%、送料梱包代450円、仕入れ送料990円を一律コストとし、目標利益が残る仕入れ上限価格を計算してください。

以下のJSON形式でのみ回答してください：
{{
  "brand": "CASIO",
  "model": "型番（例: DW-6900B-9 / GW-M5610等）",
  "authenticity_status": "純正品 / 社外パーツあり / 偽物・MODの疑い",
  "risk_flags": ["目立つキズなし", "小傷あり", "純正パーツ" などの状態フラグ],
  "condition_score": "A（推奨） / B（慎重） / C（不可）",
  "estimated_resale_normal": 8500,
  "max_bid_price_target": 5500,
  "reasoning": "真贋・社外品チェック・外観状態の添削理由（100文字以内）"
}}

【商品タイトル】: {title}
【商品説明文】: {description}
"""

    max_retries = 5
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=images + [prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1
                )
            )
            return json.loads(response.text)
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 8
                print(f"  ⚠️ Gemini一時的エラー。{wait_time}秒後に再試行します... ({attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print(f"❌ Gemini解析失敗: {e}")
                return None

# --------------------------------------------------
# 4. 詳細ページ情報・画像取得
# --------------------------------------------------
def fetch_detail_page(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    time.sleep(2)
    
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
                
    img_urls = img_urls[:8]
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    images = []
    for img_url in img_urls:
        try:
            res = requests.get(img_url, headers=headers, timeout=5)
            if res.status_code == 200:
                img = Image.open(BytesIO(res.content))
                img.thumbnail((800, 800))
                images.append(img)
        except Exception:
            continue
            
    return seller_name, description, images

# --------------------------------------------------
# 5. メイン実行処理
# --------------------------------------------------
def main():
    print("🚀 ヤフオク G-SHOCK仕入れリサーチ（オークション＆フリマ対応 / Gemini 3.6 Flash）を開始します...")
    seen_items = load_seen_items()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        print(f"🔍 検索URLへアクセス中（競売＋定額）: {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="networkidle", timeout=30000)
        
        for _ in range(3):
            page.evaluate("window.scrollBy(0, 800)")
            time.sleep(1)
            
        html = page.content()
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select(".Product") or soup.select("li.Product")
        print(f"📦 検出商品数: {len(items)}件（オークション・定額混在）")

        for item in items:
            title_tag = item.select_one(".Product__titleLink")
            price_tag = item.select_one(".Product__priceValue")

            if not (title_tag and price_tag):
                continue

            title = title_tag.get_text(strip=True)
            price_raw = price_tag.get_text(strip=True)
            
            sale_type = "定額/即決" if "即決" in price_raw else "オークション"
            price_str = price_raw.replace("円", "").replace(",", "").replace("即決", "").strip()

            try:
                price = int(price_str)
            except ValueError:
                continue

            url = title_tag.get("href", "")
            item_id = item.get("data-auction-id") or url.split("/")[-1]

            # 既読スキップ
            if item_id in seen_items:
                continue

            # ① NGキーワードチェック（タイトルで簡易フィルタ）
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

            print(f"\n🎯 狙い目キーワード検出: [{matched_keyword}] | 価格: {price}円 ({sale_type}) | {title[:25]}...")

            # ③ 詳細ページを取得して画像・説明文・出品者をスクレイピング
            try:
                seller_name, description, images = fetch_detail_page(page, url)
                
                # 詳細本文でのNGキーワードチェック
                text_to_check = f"{title} {description}".lower()
                found_ng = [ng for ng in NG_KEYWORDS if ng.lower() in text_to_check]
                if found_ng:
                    print(f"  ⏩ NGワード検出のためスキップ: {', '.join(found_ng)}")
                    seen_items.add(item_id)
                    save_seen_items(seen_items)
                    continue

                print("🤖 Gemini 3.6 Flashで目利き（真贋・社外品・画像添削・売価計算）試算中...")
                g_result = analyze_gshock_with_gemini(title, description, images, price)

                if g_result:
                    score = g_result.get("condition_score", "")
                    auth = g_result.get("authenticity_status", "")
                    print(f"  └ 判定: {score} | 純正性: {auth} | 仕入れ推奨上限: {g_result.get('max_bid_price_target')}円")
                    print(f"  └ 添削理由: {g_result.get('reasoning')}")

                    # 評価AまたはBの場合にDiscord通知
                    if "A" in score or "B" in score:
                        item_data = {
                            "id": item_id,
                            "title": title,
                            "price": price,
                            "sale_type": sale_type,
                            "matched_keyword": matched_keyword,
                            "url": url
                        }
                        send_discord_notification(item_data, g_result, seller_name)
                    else:
                        print("  ⏩ スルー（社外品/偽物疑い/利益薄のため通知なし）")

                seen_items.add(item_id)
                save_seen_items(seen_items)

            except Exception as e:
                print(f"❌ 詳細解析エラー: {e}")

            time.sleep(random.uniform(2, 4))

        browser.close()
    print("\n✨ すべてのリサーチ・目利き処理が正常に完了しました。")

if __name__ == "__main__":
    main()
