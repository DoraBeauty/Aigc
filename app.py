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

# --- 1. 金鑰配置 ---
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))

# --- 2. 輔助函數 ---
def get_stock_name(ticker):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        # 優先抓取中文名稱
        return info.get('longName') or info.get('shortName') or ticker
    except:
        return ticker

# --- 3. SMC 分析核心邏輯 ---
def analyze_stock(stock_id):
    try:
        # 處理代號
        ticker = stock_id.upper()
        if re.match(r'^\d{4}$', ticker):
            ticker += '.TW'
        
        # 抓取數據 (100天)
        stock = yf.Ticker(ticker)
        df = stock.history(period="100d")
        
        if df.empty:
            return f"❌ 找不到股票代號: {ticker}"

        stock_name = get_stock_name(ticker)

        # 基礎指標計算
        closes = df['Close'].tolist()
        highs = df['High'].tolist()
        lows = df['Low'].tolist()
        opens = df['Open'].tolist()
        dates = df.index.strftime('%Y-%m-%d').tolist()

        current_price = float(closes[-1])
        change_pct = ((current_price - float(closes[-2])) / float(closes[-2])) * 100
        
        # SMC 關鍵位階
        bsl = max(highs[-20:]) 
        ssl = min(lows[-20:])  
        swing_high = max(highs[-60:])
        swing_low = min(lows[-60:])
        eq = (swing_high + swing_low) / 2
        pd_zone = "Premium 溢價 (昂貴)" if current_price > eq else "Discount 折價 (便宜)"

        # 整理最近 5 日 K 線數據
        candles_str = ""
        for i in range(-5, 0):
            candles_str += f"- {dates[i]}: O={opens[i]:.1f}, H={highs[i]:.1f}, L={lows[i]:.1f}, C={closes[i]:.1f}\n"

        # 4. 針對 LINE 手機排版優化的 Prompt
        prompt = f"""
        你現在是 SMC (Smart Money Concepts) 專業操盤手。
        標的: {stock_name} ({ticker})
        現價: {current_price:.2f} ({change_pct:.2f}%)
        位階: {pd_zone} (均衡點 EQ: {eq:.2f})
        流動性: 上方 BSL {bsl:.2f} / 下方 SSL {ssl:.2f}

        【近 5 日 K 線數據】
        {candles_str}

        任務: 請根據數據進行分析，並嚴格遵守以下「LINE 清爽排版」格式輸出：
        1. 嚴禁使用 ### 或 ** 或 | 等 Markdown 符號。
        2. 使用表情符號作為小標題。
        3. 內容要詳細且符合 SMC 邏輯。

        輸出格式參考如下：

        📊 {stock_name} ({ticker})
        📅 日期：{dates[-1]}
        💰 現價：{current_price:.2f} ({change_pct:.2f}%)
        ----------------------------

        🏛️ 市場結構 (Structure)
        • 趨勢狀態：[判斷多頭/空頭/盤整]
        • 關鍵動作：[描述 BOS 或 CHoCH 發生位置]
        • 位階判定：{pd_zone} (EQ: {eq:.2f})

        🔍 機構足跡 (Order Flow)
        • 訂單塊 OB：[具體價格區間]
        • 缺口 FVG：[具體價格區間]
        • 流動性目標：[上方或下方的具體價格]

        🎯 執行策略 (Execution)
        【 方向：[做多/做空/觀望] 】
        • 進場觀察：[建議 POI 區域]
        • 失效防守：[關鍵止損位]

        ----------------------------
        💡 操盤手筆記：
        [給出一句 SMC 精闢點評]
        """

        # 呼叫 AI
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        return response.text

    except Exception as e:
        return f"⚠️ 系統異常: {str(e)}"

# --- 5. LINE Webhook 伺服器 ---
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
        reply_text = "請輸入股票代號 (例如: 2330)"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
