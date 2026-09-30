import os
import json
import time
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import google.generativeai as genai

# --- 設定項目 ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash')

SEARCH_QUERIES = [
    "ZIPPO シリアル (ジャンク 着火未確認 動作未確認)",
    "ZIPPO スターリング (ジャンク 着火未確認 動作未確認 STERLING)"
]

SEEN_FILE = "seen_zippo.json"

def load_seen_ids():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen_ids(seen_ids):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen_ids), f)

def setup_driver():
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    driver = webdriver.Chrome(options=options)
    return driver

def analyze_zippo_with_gemini(title, price, image_urls):
    """
    Geminiにタイトル・価格・画像を渡して、シリアル/スターリングの有無と買い判定を依頼
    """
    prompt = f"""
    あなたはZIPPOライターのプロのバイヤーです。
    以下のヤフオク出品情報を解析し、仕入れ対象（転売利ざやが取れるか）を判定してください。

    【商品タイトル】: {title}
    【現在価格】: {price}円

    【判定基準】
    1. 画像やタイトルから「STERLING（スターリングシルバー）」または「シリアルナンバー（No.XXXX）」が確認できるか？
    2. ボトム刻印（底面）の文字や年代、限定モデルの特徴が読み取れるか？
    3. ヒンジ（蝶番）の大きな破損や致命的な外装凹みがないか？（「着火未確認」「オイル切れ」「軽微な汚れ」は問題なし）

    【出力フォーマット】
    ・種別: [スターリング / シリアル限定品 / その他]
    ・刻印・シリアル情報: [検出できた刻印やシリアル番号]
    ・状態評価: [OK / 注意（理由）]
    ・判定: [買い / スルー]
    ・コメント: [簡潔に理由を記載]
    """
    
    # 画像データの準備（先頭2〜3枚をダウンロードしてGeminiに渡す）
    contents = [prompt]
    for url in image_urls[:3]:
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                contents.append({
                    "mime_type": "image/jpeg",
                    "data": res.content
                })
        except Exception as e:
            print(f"画像取得エラー: {e}")

    try:
        response = gemini_model.generate_content(contents)
        return response.text
    except Exception as e:
        return f"Gemini解析エラー: {e}"

def send_discord_notification(title, price, url, image_url, gemini_result):
    embed = {
        "title": f"🔥 【ZIPPO仕入れ候補】{title}",
        "url": url,
        "color": 15105570, # オレンジゴールド系
        "fields": [
            {"name": "価格", "value": f"{price} 円", "inline": True},
            {"name": "🤖 Gemini解析結果", "value": gemini_result, "inline": False}
        ],
        "image": {"url": image_url} if image_url else {}
    }
    payload = {"embeds": [embed]}
    requests.post(DISCORD_WEBHOOK_URL, json=payload)

# (スクレイピングメイン処理・ルーティン実行部分は既存のヤフオクコードと同様に処理)
