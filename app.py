import os
import re
import time
import yfinance as yf
import pandas as pd
import google.generativeai as genai
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage

app = Flask(__name__)

# 設定金鑰
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))

# 獲取股票名稱
def get_stock_name(ticker):
    try:
        stock = yf.Ticker(ticker)
        return stock.info.get('shortName', ticker)
    except:
        return ticker

# 呼叫 Gemini 的核心函數 (調整為高額度模型優先)
def call_gemini_with_fallback(prompt):
    # 優先順序調整：
    # 1. gemini-2.0-flash (主力: 額度高、速度快)
    # 2. gemini-flash-latest (備援: 指向當前最穩定的 Flash 版本)
    # 3. gemini-pro-latest (最後防線)
    model_priority = ['gemini-2.0-flash', 'gemini-flash-latest', 'gemini-pro-latest']
    
    error_log = [] # 記錄錯誤以便除錯

    for model_name in model_priority:
        try:
            print(f"嘗試使用模型: {model_name} ...")
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            error_msg = str(e)
            print(f"⚠️ 模型 {model_name} 失敗: {error_msg}")
            error_log.append(f"{model_name}: {error_msg}")
            # 繼續嘗試下一個模型
            continue
    
    # 如果全部失敗，回傳詳細錯誤給使用者
    return f"⚠️ 系統忙碌 (所有模型皆額滿)。\n除錯紀錄:\n" + "\n".join(error_log)

# SMC 深度分析核心
def analyze_stock(stock_id):
    try:
        # 1. 處理代號
        ticker = stock_id.upper()
        if re.match(r'^\d{4}$', ticker):
            ticker += '.TW'
        
        # 2. 抓取數據
        stock = yf.Ticker(ticker)
        df = stock.history(period="100d")
        
        if df.empty:
            return f"❌ 找不到 {ticker}，請確認代號。"

        stock_name = get_stock_name(ticker)

        # 3. 數據前處理
        closes = df['Close'].tolist()
        highs = df['High'].tolist()
        lows = df['Low'].tolist()
        opens = df['Open'].tolist()
        dates = df.index.strftime('%Y-%m-%d').tolist()

        current_price = closes[-1]
        
        # 流動性與位階計算
        bsl = max(highs[-20:]) 
        ssl = min(lows[-20:])
        swing_high = max(highs[-60:])
        swing_low = min(lows[-60:])
        equilibrium = (swing_high + swing_low) / 2
        pd_zone = "Premium (溢價區-找空點)" if current_price > equilibrium else "Discount (折價區-找買點)"

        # K 線數據字串
        candles_str = ""
        # 取最後 5 天
        for i in range(-5, 0):
            candles_str += f"- {dates[i]}: O={opens[i]:.1f}, H={highs[i]:.1f}, L={lows[i]:.1f}, C={closes[i]:.1f}\n"

        # 4. Prompt
        prompt = f"""
        你現在是 ICT (Inner Circle Trader) 與 SMC 策略的頂尖量化分析師。
        
        【資產概況】
        標的: {stock_name} ({ticker})
        現價: {current_price:.2f}
        位階: {pd_zone} (50% EQ: {equilibrium:.2f})
        近期流動性: BSL {bsl:.2f} / SSL {ssl:.2f}

        【近 5 日價格行為】
        {candles_str}

        【任務】
        請根據 K 線數據進行 SMC 分析，並用以下 Markdown 表格輸出：

        ### 🦁 {stock_name} ({ticker}) SMC 機構視角

        | 指標 | 狀態 | 關鍵價位 / 解讀 |
        | :--- | :--- | :--- |
        | **結構** | [多頭/空頭] | [BOS 或 CHoCH] |
        | **位階** | {pd_zone} | 均衡點 {equilibrium:.2f} |
        | **動能** | [強/弱] | [K線實體力度] |

        **🎯 機構足跡 (Smart Money)**
        * **🧱 訂單塊 (OB)**: 觀察 [日期] K線，支撐/壓力在 **[價格區間]**。
        * **⚡ 價值缺口 (FVG)**: 留意 **[價格區間]** 失衡。
        * **🌊 流動性**: [上方/下方] 目標 **[價格]**。

        **📝 操盤劇本**
        > **方向**: [做多/做空/觀望]
        * **進場**: 回測 **[OB/FVG]** 且 [反轉訊號] 時入場。
        * **防守**: 收盤跌破 **[價格]** 則失效。
        """

        return call_gemini_with_fallback(prompt)

    except Exception as e:
        print(f"Error: {e}")
        return f"⚠️ 分析失敗: {str(e)}"

# Webhook 設定
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    if re.match(r'^[A-Za-z0-9.-]+$', user_msg):
        reply_text = analyze_stock(user_msg)
    else:
        reply_text = "請輸入代號 (例如: 2330)"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    app.run()
