import os
import re
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
        
        # SMC 數據計算
        bsl = max(highs[-20:]) 
        ssl = min(lows[-20:])
        swing_high = max(highs[-60:])
        swing_low = min(lows[-60:])
        equilibrium = (swing_high + swing_low) / 2
        pd_zone = "Premium (溢價區-找空點)" if current_price > equilibrium else "Discount (折價區-找買點)"

        # K 線數據字串 (給 AI 看型態)
        candles_str = ""
        for i in range(-5, 0):
            candles_str += f"- {dates[i]}: O={opens[i]:.1f}, H={highs[i]:.1f}, L={lows[i]:.1f}, C={closes[i]:.1f}\n"

        # 4. Prompt (針對 1.5 Flash 優化，讓它更聽話)
        prompt = f"""
        你現在是 SMC (Smart Money Concepts) 專業交易員。
        
        【資產數據】
        標的: {stock_name} ({ticker})
        現價: {current_price:.2f}
        位階: {pd_zone} (EQ: {equilibrium:.2f})
        近20日流動性: 上方BSL {bsl:.2f} / 下方SSL {ssl:.2f}

        【近 5 日 K 線】
        {candles_str}

        【任務】
        請直接用 Markdown 表格輸出分析 (不要廢話)：

        ### 🦁 {stock_name} ({ticker}) SMC 分析

        | 項目 | 狀態 | 關鍵價位/解讀 |
        | :--- | :--- | :--- |
        | **結構** | [多頭/空頭] | [描述 BOS 或 CHoCH] |
        | **動能** | [強/弱] | [根據 K 線實體判斷] |
        | **位階** | {pd_zone} | 均衡點 {equilibrium:.2f} |

        **🎯 關鍵區域 (POI)**
        * **🧱 訂單塊 (OB)**: 關注 **[價格區間]** (支撐/壓力)。
        * **⚡ 缺口 (FVG)**: 關注 **[價格區間]** (失衡區)。
        * **🌊 流動性**: 目標 **[價格]**。

        **📝 操盤建議**
        > **方向**: [做多/做空/觀望]
        * **進場**: 回測 **[POI]** 且出現反轉訊號時。
        * **止損**: 收盤跌破/突破 **[價格]**。
        """

        # 【關鍵】使用 gemini-1.5-flash (最穩定、額度最高)
        # 只要步驟一的 requirements.txt 有更新，這裡絕對不會 404
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        return response.text

    except Exception as e:
        # 如果還是錯，印出詳細原因
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
