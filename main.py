import os
import time
import random
import re
import requests
import json
from bs4 import BeautifulSoup
from io import BytesIO
from PIL import Image
from google import genai
from google.genai import types

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ==================================================
# 1. 環境変数からの設定読み込み & ストア設定
# ==================================================
API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

client = genai.Client(api_key=API_KEY)

# 巡回対象のストアリスト
STORES = [
    {"name": "ブランドHavana", "id": "4m1MXp8rnM1kYKAYHvHVULadMvgMD"},
    {"name": "日本貴金属", "id": "BaLo7yetbAXTrZxWNL5iV2ESPpPb6"}
]
SEARCH_KEYWORD = "ソーラー"

# 除外したいブランドキーワード（タイトルに含まれる場合スキップ）
EXCLUDE_KEYWORDS = ["ELGIN", "Elgin", "elgin", "エルジン"]

def send_discord_notify(store_name, item, result):
    """Discordに判定結果を通知する"""
    if not DISCORD_WEBHOOK_URL:
        print("⚠️ DISCORD_WEBHOOK_URLが未設定のため、通知をスキップします。")
        return
        
    message = f"""
🔔 **【仕入れチャンス到来！】**
----------------------------------------
🏪 **店舗**: {store_name}
📌 **タイトル**: {item['title']}
⏰ **残り時間**: {item['time']}
🔗 **URL**: {item['url']}

🏷️ **ブランド/型番**: {result.get('brand', '不明')} / {result.get('model', '不明')}
📊 **評価**: {result.get('condition_score', '-')}
💰 **稼働時想定売価**: {result.get('estimated_resale_normal', '-')}
⚠️ **ジャンク時想定売価**: {result.get('estimated_resale_junk', '-')}
🎯 **推奨落札上限 (目標利益確保)**: **{result.get('max_bid_price_target', '-')}**
🛡️ **ジャンク防衛ライン (利益±0)**: {result.get('max_bid_price_break_even', '-')}
💡 **理由**: {result.get('reasoning', '-')}
----------------------------------------
"""
    payload = {"content": message}
    headers = {"Content-Type": "application/json"}
    try:
        requests.post(DISCORD_WEBHOOK_URL, data=json.dumps(payload), headers=headers, timeout=10)
    except Exception as e:
        print(f"❌ Discord通知エラー: {e}")

# ==================================================
# 2. Chromeブラウザ起動（Bot検知回避対策強化）
# ==================================================
def create_browser():
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    
    # 対策1: 一般的なPC用User-Agentを偽装
    options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
    
    # 対策2: 自動化検出フラグ（Bot判定）を消去
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    
    # JavaScriptのnavigator.webdriverを偽装
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    return driver

# 人間らしい動きを演出するためのランダムスリープ
def human_sleep(min_sec=3, max_sec=6):
    time.sleep(random.uniform(min_sec, max_sec))

# ==================================================
# 3. 残り1時間未満の出品物URLを自動抽出
# ==================================================
def get_urgent_auction_urls(driver, store_name, search_url):
    print(f"🔍 【{store_name}】 内を検索中...\nURL: {search_url}\n", flush=True)
    driver.get(search_url)
    human_sleep(4, 7)
    
    # ランダムなタイミングでスクロール（人間の操作を疑似再現）
    for _ in range(4):
        scroll_height = random.randint(600, 1000)
        driver.execute_script(f"window.scrollBy(0, {scroll_height});")
        human_sleep(1, 2)
    
    soup = BeautifulSoup(driver.page_source, "html.parser")
    urgent_items = []
    
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/auction/" in href:
            clean_url = href.split("?")[0]
            if not clean_url.startswith("http"):
                clean_url = "https://page.auctions.yahoo.co.jp" + clean_url
                
            parent = a.find_parent(["li", "div", "article"])
            parent_text = parent.get_text(" ", strip=True) if parent else ""
            
            if ("分" in parent_text or "秒" in parent_text) and not ("日" in parent_text or "時間" in parent_text):
                time_match = re.search(r'(\d+分|\d+秒)', parent_text)
                time_str = time_match.group(0) if time_match else "1時間未満"
                
                title = a.get_text().strip()
                if not title or len(title) < 5:
                    title = parent_text[:35] if parent_text else "タイトル不明"
                
                # --- 除外ブランドの判定 ---
                if any(keyword in title for keyword in EXCLUDE_KEYWORDS):
                    print(f"🚫 除外対象ブランドのためスキップ: {title[:20]}...", flush=True)
                    continue
                # --------------------------
                    
                if not any(x['clean_url'] == clean_url for x in urgent_items):
                    urgent_items.append({
                        "title": title,
                        "url": clean_url,
                        "clean_url": clean_url,
                        "time": time_str
                    })
                    
    return urgent_items

# ==================================================
# 4. 詳細情報 & 主要画像取得
# ==================================================
def fetch_auction_details(driver, url):
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    human_sleep(3, 5)  # ページ取得ごとにランダム待機
    
    soup = BeautifulSoup(driver.page_source, "html.parser")
    
    title_tag = soup.find("h1") or soup.find("meta", property="og:title")
    title = title_tag.get("content") if title_tag and title_tag.name == "meta" else (title_tag.text.strip() if title_tag else "不明")
    
    desc_tag = soup.find("div", class_="ProductExplanation__commentArea") or soup.find("section", class_="ProductExplanation")
    description = desc_tag.text.strip() if desc_tag else "説明文なし"
    
    img_urls = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if "auctions.c.yimg.jp" in src:
            clean_img_url = src.split("?")[0]
            if clean_img_url not in img_urls and not clean_img_url.endswith(".gif"):
                img_urls.append(clean_img_url)
                
    img_urls = img_urls[:8]
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
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
            
    return title, description, images

# ==================================================
# 5. Gemini 3.6 Flash による目利き（リトライ機能付き）
# ==================================================
def analyze_watch(title, description, images):
    prompt = f"""
あなたは中古ソーラー時計の転売・仕入れ目利き専門家です。
提供された「商品画像」と「タイトル・商品説明文」を分析し、仕入れ判定と落札上限額（送料込み総額）を試算してください。

【計算ロジックの設定】
1. 想定相場（2パターン）:
   - ①「稼働品（正常動作品）」としての想定販売相場
   - ②「不動・ジャンク（パーツ取り）」としての想定販売相場

2. コスト前提：
   - 販売手数料（メルカリ等）：10%
   - 販売時発送送料（メルカリ等）：210円
   - ヤフオク仕入れ時送料：990円（一律）

3. 仕入れ判定と上限額計算：
   - 実質仕入原価 ＝ 落札価格 ＋ 仕入れ送料(990円)
   - 利益 ＝ 販売相場 - 手数料(10%) - 販売送料(210円) - (落札価格 + 990円)
   - 推奨落札上限額（max_bid_price_target）は、上記計算で希望利益が得られる「ヤフオクでの本体落札の上限価格」として算出してください。

出力は以下のJSON形式のみで回答してください：
{{
  "brand": "ブランド名",
  "model": "型番/キャリバー",
  "condition_score": "A（推奨）/ B（慎重）/ C（不可）",
  "estimated_resale_normal": "稼働想定売価",
  "estimated_resale_junk": "ジャンク想定売価",
  "max_bid_price_target": "推奨落札上限額（目標利益確保）",
  "max_bid_price_break_even": "ジャンク時トントン上限（利益±0円）",
  "reasoning": "判定理由と上限額の根拠（80文字以内）"
}}

【商品タイトル】: {title}
【商品説明文】: {description}
"""

    max_retries = 3
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
                print(f"  ⚠️ Gemini一時的エラー発生（{e}）。5秒後に再試行します... ({attempt + 1}/{max_retries})", flush=True)
                time.sleep(5)
            else:
                raise e

# ==================================================
# 6. メイン実行処理
# ==================================================
if __name__ == "__main__":
    driver = None
    try:
        print("🚀 自動リサーチプログラムを起動します...", flush=True)
        driver = create_browser()
        
        # 設定された各ストアを順番に巡回
        for store in STORES:
            store_name = store["name"]
            seller_id = store["id"]
            
            target_search_url = f"https://auctions.yahoo.co.jp/seller/{seller_id}?p={SEARCH_KEYWORD}&select=22&is_auction=1&s1=end&o1=a"
            
            print(f"\n========================================", flush=True)
            print(f"🏪 巡回開始: 【 {store_name} 】", flush=True)
            print(f"========================================", flush=True)
            
            target_items = get_urgent_auction_urls(driver, store_name, target_search_url)
            
            # 各ストアごとに最大15件まで処理
            MAX_ITEMS = 15
            target_items = target_items[:MAX_ITEMS]
            
            print(f"⏰ 残り時間が短い上位【 {len(target_items)} 件 】を厳選してチェックします。\n", flush=True)
            
            if not target_items:
                print(f"【{store_name}】に該当する商品は見つかりませんでした。", flush=True)
                continue
                
            for i, item in enumerate(target_items, 1):
                print(f"────────────────────────────────────────", flush=True)
                print(f"[{store_name}] 【{i}/{len(target_items)}】残り時間: {item['time']} | {item['title'][:25]}...", flush=True)
                
                try:
                    title, description, images = fetch_auction_details(driver, item['url'])
                    print("🤖 Gemini 3.6 Flashで目利き試算中...", flush=True)
                    
                    res = analyze_watch(title, description, images)
                    
                    score = res.get("condition_score", "")
                    print(f"  └ 判定結果: {score} | 通常上限: {res.get('max_bid_price_target')} | 防衛線: {res.get('max_bid_price_break_even')}")
                    
                    # 判定結果が「A（推奨）」または「B（慎重）」のときだけDiscordへ通知！
                    if "A" in score or "B" in score:
                        print(f"🎯 利益見込み案件を発見！Discordへ通知します。", flush=True)
                        send_discord_notify(store_name, item, res)
                    else:
                        print(f"⏩ スルー（評価Cのため通知なし）", flush=True)
                        
                except Exception as e:
                    print(f"❌ 解析エラー: {e}", flush=True)
                    
                # アクセス制限回避のため、次の商品取得までにランダムで3〜6秒待機
                human_sleep(3, 6)
                
        print("\n🎉 すべてのストアの自動処理が正常に終了しました！", flush=True)

    except Exception as e:
        print(f"❌ 全体エラー: {e}", flush=True)
    finally:
        if driver:
            driver.quit()
