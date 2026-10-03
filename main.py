import json
import os
import re
import time
import warnings
from io import BytesIO

from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from google.genai.errors import APIError
from PIL import Image
from playwright.sync_api import sync_playwright
import requests

# 警告ログの非表示化
warnings.filterwarnings("ignore")

# ==================================================
# 1. 環境変数からの設定読み込み & ストア設定
# ==================================================
API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# APIキー未設定時でもクラッシュしない安全化
client = genai.Client(api_key=API_KEY) if API_KEY else None

# 巡回対象のストアリスト（全4店舗）
STORES = [
    {"name": "日本貴金属", "id": "BaLo7yetbAXTrZxWNL5iV2ESPpPb6"},
    {"name": "Fii", "id": "88qw1vitqMALJSEUEqiZDDTmNSoiQ"},
    {"name": "TAKARAYA", "id": "A9ajxEQZ34DuDBbQoJKDh4bFYA74N"},
    {"name": "TVCストア1号店", "id": "GHXf6dTmnn7SLWbqAEeiBU1rrJHM6"},
]
SEARCH_KEYWORD = "ソーラー"
EXCLUDE_KEYWORDS = ["ELGIN", "Elgin", "elgin", "エルジン"]

SEEN_FILE = "seen_solar.json"


# ==================================================
# 2. ヘルパー関数
# ==================================================
def load_seen_items():
  if os.path.exists(SEEN_FILE):
    try:
      with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))
    except Exception as e:
      print(f"⚠️ 既読ファイルの読み込みエラー: {e}", flush=True)
      return set()
  return set()


def save_seen_items(seen):
  try:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
      json.dump(list(seen), f, ensure_ascii=False, indent=2)
  except Exception as e:
    print(f"⚠️ 既読ファイルの保存エラー: {e}", flush=True)


def parse_price(price_val):
  if not price_val:
    return 0
  digits = re.sub(r"[^\d]", "", str(price_val))
  return int(digits) if digits else 0


def send_discord_notify(store_name, item, result, current_price=0):
  if not DISCORD_WEBHOOK_URL:
    print(
        "⚠️ DISCORD_WEBHOOK_URLが未設定のため、通知をスキップします。",
        flush=True,
    )
    return

  price_str = f"{current_price:,}円" if current_price > 0 else "不明"

  message = f"""
🔔 **【ソーラー時計 仕入れチャンス到来！】**
----------------------------------------
🏪 **店舗**: {store_name}
📌 **タイトル**: {item['title']}
💰 **現在価格**: {price_str}
⏰ **残り時間**: {item['time']}
🔗 **URL**: {item['url']}

🏷 **ブランド/型番**: {result.get('brand', '不明')} / {result.get('model', '不明')}
📊 **評価**: **{result.get('condition_score', '-')}**
💰 **稼働時想定売価**: {result.get('estimated_resale_normal', '-')}円
⚠ **ジャンク時想定売価**: {result.get('estimated_resale_junk', '-')}円
🎯 **推奨落札上限 (目標利益確保)**: **{result.get('max_bid_price_target', '-')}円**
🛡️ **ジャンク防衛ライン (利益±0)**: {result.get('max_bid_price_break_even', '-')}円
💡 **理由・状態感**: {result.get('reasoning', '-')}
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
      print(f"✅ Discord通知送信完了: {item['title'][:20]}", flush=True)
    else:
      print(
          f"❌ Discord通知エラー: {res.status_code}, {res.text}", flush=True
      )
  except Exception as e:
    print(f"❌ Discord通知例外: {e}", flush=True)


# ==================================================
# 3. リスト取得（「残り時間の短い順」切り替え＆上位20件取得）
# ==================================================
def get_urgent_auction_urls(page, store_name, search_url, seen_items):
  print(
      f"🔍 【{store_name}】 内を検索中...\nURL: {search_url}\n", flush=True
  )

  try:
    page.goto(search_url, wait_until="networkidle", timeout=30000)
  except Exception:
    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)

  time.sleep(1.5)

  # 画面上のプルダウンで「残り時間の短い順」へ物理切り替えを試みる
  try:
    sort_text = page.get_by_text("新着順").or_(
        page.get_by_text("おすすめ順")
    )
    if sort_text.is_visible():
      sort_text.click()
      time.sleep(0.5)
      target_option = page.get_by_text("残り時間の短い順").or_(
          page.get_by_text("終了時間の短い順")
      )
      if target_option.is_visible():
        target_option.click()
        page.wait_for_load_state("networkidle", timeout=10000)
        time.sleep(1.5)
        print(
            "  🔄 画面操作により『残り時間の短い順』に切り替えました。",
            flush=True,
        )
  except Exception:
    pass

  # 画面をスクロールして要素をロード
  for i in range(1, 3):
    page.evaluate(f"window.scrollTo(0, {i * 800});")
    time.sleep(0.8)

  soup = BeautifulSoup(page.content(), "html.parser")
  raw_items = []

  # 1. ページ内のオークション商品を抽出
  for a in soup.find_all("a", href=True):
    href = a["href"]
    if "/auction/" in href:
      clean_url = href.split("?")[0]
      if not clean_url.startswith("http"):
        clean_url = "https://page.auctions.yahoo.co.jp" + clean_url

      match = re.search(r"/auction/([a-zA-Z0-9]+)", clean_url)
      item_id = match.group(1) if match else clean_url.split("/")[-1]

      parent = a.find_parent(["li", "div", "article"])
      parent_text = parent.get_text(" ", strip=True) if parent else ""

      title = a.get_text().strip()
      if not title or len(title) < 5:
        title = parent_text[:35] if parent_text else "タイトル不明"

      if not any(x["item_id"] == item_id for x in raw_items):
        raw_items.append({
            "item_id": item_id,
            "title": title,
            "url": clean_url,
            "parent_text": parent_text,
        })

  # 上位20件のみをスキャン対象とする
  raw_items = raw_items[:20]
  print(
      f"📦 【{store_name}】の上位{len(raw_items)}件をスキャン中...", flush=True
  )

  urgent_items = []

  # 2. 上位20件の中で条件判定（未チェック＆残り3時間未満）
  for item in raw_items:
    item_id = item["item_id"]
    parent_text = item["parent_text"]

    if item_id in seen_items:
      continue

    # 「日」が含まれるものは除外（1日以上の残り時間）
    if "日" in parent_text:
      continue

    minutes_left = None

    # 残り時間表現のパース
    hour_match = re.search(r"(\d+)時間(?:(\d+)分)?", parent_text)
    min_match = re.search(r"^(\d+)分|[\s](\d+)分", parent_text)

    if hour_match:
      hours = int(hour_match.group(1))
      mins = int(hour_match.group(2)) if hour_match.group(2) else 0
      minutes_left = hours * 60 + mins
    elif min_match:
      minutes_left = int(
          min_match.group(1) if min_match.group(1) else min_match.group(2)
      )

    # 残り3時間未満（180分未満）を対象にする
    if minutes_left is not None and minutes_left < 180:
      if minutes_left >= 60:
        time_str = f"{minutes_left // 60}時間{minutes_left % 60}分"
      else:
        time_str = f"{minutes_left}分"

      if any(keyword in item["title"] for keyword in EXCLUDE_KEYWORDS):
        print(
            f"🚫 除外対象ブランドのためスキップ: {item['title'][:20]}...",
            flush=True,
        )
        continue

      urgent_items.append({
          "item_id": item_id,
          "title": item["title"],
          "url": item["url"],
          "time": time_str,
      })

  return urgent_items


# ==================================================
# 4. 詳細情報・画像・現在価格取得
# ==================================================
def fetch_auction_details(page, url):
  page.goto(url, wait_until="domcontentloaded", timeout=15000)
  time.sleep(1)

  soup = BeautifulSoup(page.content(), "html.parser")

  title_tag = soup.find("h1") or soup.find("meta", property="og:title")
  title = (
      title_tag.get("content")
      if title_tag and title_tag.name == "meta"
      else (title_tag.text.strip() if title_tag else "不明")
  )

  desc_tag = soup.find(
      "div", class_="ProductExplanation__commentArea"
  ) or soup.find("section", class_="ProductExplanation")
  description = desc_tag.text.strip() if desc_tag else "説明文なし"

  current_price = 0
  price_tag = (
      soup.find("dd", class_="Price__value")
      or soup.find("span", class_="Price__value")
      or soup.find("p", class_="Price__value")
  )
  if price_tag:
    price_digits = re.sub(r"[^\d]", "", price_tag.get_text())
    if price_digits:
      current_price = int(price_digits)

  img_urls = []
  for img in soup.find_all("img"):
    src = img.get("src") or img.get("data-src") or ""
    if "auctions.c.yimg.jp" in src:
      clean_img_url = src.split("?")[0]
      if not clean_img_url.endswith(".gif") and clean_img_url not in img_urls:
        img_urls.append(clean_img_url)

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
        img.thumbnail((800, 800))
        images.append(img)
    except Exception:
      continue

  return title, description, images, current_price


# ==================================================
# 5. Gemini 3.8 Flash 解析
# ==================================================
def analyze_watch(title, description, images):
  if not client:
    print("❌ Gemini APIキーが読み込めていません", flush=True)
    return None

  prompt = f"""
あなたは中古ソーラー時計の転売・仕入れ目利き専門家です。
添付された商品画像とタイトル・商品説明文を細部まで精査し、外観ダメージ（特に風防キズ）および二次電池（ソーラー充電）の状態を厳しく見極めた上で仕入れ判定を行ってください。

【確認・査定基準】
1. 風防（ガラス面）チェック:
   - 線キズ、深い傷、ヒビ割れ、欠け、内部の曇り・カビがないか厳重に確認。
   - 風防に明確なキズ・擦れがある場合は、研磨・手間に見合うか考慮し評価を降格（BまたはC）してください。
2. 二次電池・動作状況チェック:
   - 「充電しても動かない」「ソーラー非稼働」「ジャンク」表記がないか。
3. 外観ダメージ:
   - ケース・ベゼルの大きな打痕、メッキ剥げ、著しい錆・汚れの有無。

【計算ロジックの設定】
1. 想定相場（2パターン）:
   - ①「稼働品（正常動作品）」としての想定販売相場
   - ②「不動・ジャンク（パーツ取り）」としての想定販売相場
2. コスト前提：
   - 販売手数料（メルカリ等）：10%
   - 販売時発送送料：210円
   - ヤフオク仕入れ時送料：990円（一律）

以下のJSON形式でのみ回答してください：
{{
  "brand": "ブランド名",
  "model": "型番/キャリバー",
  "condition_score": "A（推奨） / B（慎重） / C（不可）",
  "estimated_resale_normal": 12000,
  "estimated_resale_junk": 3000,
  "max_bid_price_target": 6500,
  "max_bid_price_break_even": 1500,
  "reasoning": "風防・動作状態と仕入れ判定理由（80文字以内）"
}}

【商品タイトル】: {title}
【商品説明文】: {description}
"""

  try:
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=images + [prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.1
        ),
    )
    return json.loads(response.text)

  except APIError as e:
    if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
      print(
          "⚠️ 【Quota上限検知】Gemini APIの制限枠に達しました。",
          flush=True,
      )
      print(
          "   以降の解析を中断し、スクリプトを安全に終了します。",
          flush=True,
      )
      return "QUOTA_EXCEEDED"
    else:
      print(f"❌ Gemini APIエラー: {e}", flush=True)
      return None
  except Exception as e:
    print(f"❌ Gemini解析例外: {e}", flush=True)
    return None


# ==================================================
# 6. メイン実行処理
# ==================================================
def main():
  print(
      "🚀 ヤフオク"
      " ソーラー時計仕入れリサーチ（ソート修正・上位20件厳選版）を開始します...",
      flush=True,
  )
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
            " (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="ja-JP",
    )
    page = context.new_page()

    for store in STORES:
      store_name = store["name"]
      seller_id = store["id"]

      # 残り時間の短い順パラメータ（select=05 & s1=end & o1=a）を付与
      target_search_url = f"https://auctions.yahoo.co.jp/seller/{seller_id}?p={SEARCH_KEYWORD}&category_id=23140&is_auction=1&select=05&s1=end&o1=a"

      print("\n========================================", flush=True)
      print(f"🏪 巡回開始: 【 {store_name} 】", flush=True)
      print("========================================", flush=True)

      target_items = get_urgent_auction_urls(
          page, store_name, target_search_url, seen_items
      )

      print(
          f"⏰ 残り時間の短い順"
          f" 上位20件中、残り3時間未満の対象商品【 {len(target_items)} 件 】をチェックします。\n",
          flush=True,
      )

      if not target_items:
        print(
            f"【{store_name}】該当する未チェック対象商品（残り3時間未満）は見つかりませんでした。",
            flush=True,
        )
        continue

      for i, item in enumerate(target_items, 1):
        print("────────────────────────────────────────", flush=True)
        print(
            f"[{store_name}] 【{i}/{len(target_items)}】残り時間:"
            f" {item['time']} | {item['title'][:25]}...",
            flush=True,
        )

        try:
          title, description, images, current_price = fetch_auction_details(
              page, item["url"]
          )
          print(f"  💰 現在価格: {current_price:,}円", flush=True)
          print(
              "🤖 Gemini 3.8 Flashで目利き試算中（風防・外観厳密チェック）...",
              flush=True,
          )

          g_result = analyze_watch(title, description, images)

          if g_result == "QUOTA_EXCEEDED":
            print("⛔ API制限に達したため処理を安全に停止します。", flush=True)
            break

          if g_result:
            score = g_result.get("condition_score", "")
            max_bid_num = parse_price(g_result.get("max_bid_price_target"))

            # 赤字判定補正
            if (
                current_price > 0
                and max_bid_num > 0
                and current_price >= max_bid_num
            ):
              print(
                  f"  ⚠️ 赤字判定補正: 現在価格({current_price}円) >="
                  f" 推奨上限額({max_bid_num}円)",
                  flush=True,
              )
              score = "C（不可）"

            print(
                f"  └ 最終判定: {score} | 通常上限:"
                f" {g_result.get('max_bid_price_target')}円 | 防衛線:"
                f" {g_result.get('max_bid_price_break_even')}円",
                flush=True,
            )
            print(
                f"  └ 添削理由: {g_result.get('reasoning')}", flush=True
            )

            if "A" in score or "B" in score:
              send_discord_notify(store_name, item, g_result, current_price)
            else:
              print("  ⏩ スルー（評価Cのため通知なし）", flush=True)

          seen_items.add(item["item_id"])
          save_seen_items(seen_items)

        except Exception as e:
          print(f"❌ 解析エラー: {e}", flush=True)

        time.sleep(1)

    browser.close()

  print(
      "\n🎉 すべてのストアの自動処理が正常に終了しました！",
      flush=True,
  )


if __name__ == "__main__":
  main()
