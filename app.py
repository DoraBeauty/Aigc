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
        # 優先抓取中文名稱，抓不到再抓英文
        info = stock.info
        return info.get('longName') or info.get('shortName') or ticker
    except:
        return ticker

# SMC 分析核心
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

        # 3. 數據計算 (SMC 邏輯)
        closes = df['Close'].tolist()
        highs = df['High'].tolist()
        lows = df['Low'].tolist()
        opens = df['Open'].tolist()
        dates = df.index.strftime('%Y-%m-%d').tolist()

        current_price = float(closes[-1])
        change_pct = ((current_price - float(closes[-2])) / float(closes[-2])) * 100
        
        # 流動性高低點
        bsl = max(highs[-20:]) 
        ssl = min(lows[-20:])
        
        # P/D Zone (溢價/折價區)
        swing_high = max(highs[-60:])
        swing_low = min(lows[-60:])
        equilibrium = (swing_high + swing_low) / 2
        pd_zone = "Premium (溢價區)" if current_price > equilibrium else "Discount (折價區)"

        # K線型態
        candles_str = ""
        for i in range(-5, 0):
            candles_str += f"- {dates[i]}: O={opens[i]:.1f}, H={highs[i]:.1f}, L={lows[i]:.1f}, C={closes[i]:.1f}\n"

        # 4. 深度 SMC Prompt
        prompt = f"""
        你現在是 SMC (Smart Money Concepts) 頂尖量化操盤手。
        標的: {stock_name} ({ticker})
        現價: {current_price:.2f} ({change_pct:.2f}%)
        位階: {pd_zone} (EQ: {equilibrium:.2f})
        近期流動性: 上方 BSL {bsl:.2f} / 下方 SSL {ssl:.2f}

        【近 5 日 K 線數據】
        {candles_str}

        任務: 請根據 K 線型態(如實體大小、影線)與位階，給出專業 SMC 分析。
        請用 Markdown 表格格式輸出，包含「結構、位階、動能」，並明確標出建議觀察的「訂單塊(OB)」與「價值缺口(FVG)」價格區間。
        最後給出進場建議與失效點位。
        """

        # 【關鍵修正】使用你清單中明確存在的 gemini-flash-latest
        model = genai.GenerativeModel('gemini-flash-latest')
        response = model.generate_content(prompt)
        return response.text

    except Exception as e:
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
