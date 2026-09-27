import math

def check_item_eligibility(item_data):
    """
    仕入れ対象かどうかの判定を行う関数
    
    item_data = {
        "title": 商品タイトルや説明文,
        "buy_price": 仕入れ価格（買値）,
        "estimated_sell_price": 想定販売価格（売値相場）,
        "type": "solar" または "quartz",
        "description": 商品の補足説明（オプション）
    }
    """
    
    # --------------------------------------------------
    # 1. 状態NGキーワードチェック（即除外ロジック）
    # --------------------------------------------------
    ng_keywords = [
        "加水分解", "割れ", "ベタつき", "ベタツキ",  # 加水分解・劣化
        "ベゼル欠品", "ベゼル破損", "ベゼル割れ", "ベゼル不良", # ベゼル不良
        "遊環なし", "遊環欠品", "リング欠損",          # 遊環欠品
        "ガラス傷", "ガラスキズ", "風防欠け", "風防傷", # ガラス傷
        "黄ばみ", "色あせ", "変色", "日焼け",           # 日焼け・変色
        "CHG", "充電不足",                             # ソーラー点滅・消灯
        "不動", "ジャンク", "動作未確認"               # 不動品
    ]
    
    # タイトルと商品説明文を結合して検索対象にする
    text_to_check = item_data.get("title", "") + " " + item_data.get("description", "")
    
    found_ng_words = []
    for ng_word in ng_keywords:
        if ng_word in text_to_check:
            found_ng_words.append(ng_word)
            
    if found_ng_words:
        return {
            "result": False,
            "reason": f"NGキーワード検知: {', '.join(found_ng_words)}",
            "net_profit": 0,
            "min_sell_price": 0
        }

    # --------------------------------------------------
    # 2. 損益計算（相殺・最低利益ラインチェック）
    # --------------------------------------------------
    buy_price = item_data["buy_price"]
    estimated_sell_price = item_data["estimated_sell_price"]
    item_type = item_data.get("type", "quartz")  # デフォルトはクォーツ設定
    
    shipping_fee = 450  # 宅急便コンパクト送料
    fee_rate = 0.10     # メルカリ等の手数料10%
    
    # 種別ごとの目標最低利益設定
    target_profit = 100 if item_type == "solar" else 1500
    
    # ① 手元に残る純利益の計算式
    # 純利益 = (想定売値 × 0.9) - 送料 - 仕入れ価格
    net_profit = math.floor((estimated_sell_price * (1 - fee_rate)) - shipping_fee - buy_price)
    
    # ② 目標利益（＋原価全額相殺）を達成するために必要な最低販売価格の逆算
    # 最低販売価格 = (仕入れ価格 + 送料 + 目標利益) / 0.9
    min_sell_price = math.ceil((buy_price + shipping_fee + target_profit) / (1 - fee_rate))

    # 判定処理
    if net_profit >= target_profit:
        return {
            "result": True,
            "reason": f"合格 (判定基準: {item_type}利益 +{target_profit}円以上)",
            "net_profit": net_profit,
            "min_sell_price": min_sell_price
        }
    else:
        return {
            "result": False,
            "reason": f"利益不足 (見込み利益: {net_profit}円 / 必要最低利益: {target_profit}円)",
            "net_profit": net_profit,
            "min_sell_price": min_sell_price
        }


# ==================================================
# 動作テスト例
# ==================================================

# テスト1: 状態不良（加水分解あり）
item_1 = {
    "title": "G-SHOCK DW-5600 加水分解あり ジャンク",
    "buy_price": 1000,
    "estimated_sell_price": 3000,
    "type": "quartz"
}

# テスト2: クォーツ（仕入れ1,000円、売値想定 3,000円）
item_2 = {
    "title": "G-SHOCK DW-5600E 美品",
    "buy_price": 1000,
    "estimated_sell_price": 3000,
    "type": "quartz"
}

# テスト3: ソーラー（仕入れ1,000円、売値想定 1,800円）
item_3 = {
    "title": "G-SHOCK GW-M5610 タフソーラー 動作確認済み",
    "buy_price": 1000,
    "estimated_sell_price": 1800,
    "type": "solar"
}

print("--- テスト1 (NGキーワードあり) ---")
print(check_item_eligibility(item_1))

print("\n--- テスト2 (クォーツ判定) ---")
res2 = check_item_eligibility(item_2)
print(f"結果: {res2['result']} | 理由: {res2['reason']}")
print(f"見込み利益: {res2['net_profit']}円 | 基準達成に必要な最低売値: {res2['min_sell_price']}円")

print("\n--- テスト3 (ソーラー判定) ---")
res3 = check_item_eligibility(item_3)
print(f"結果: {res3['result']} | 理由: {res3['reason']}")
print(f"見込み利益: {res3['net_profit']}円 | 基準達成に必要な最低売値: {res3['min_sell_price']}円")
