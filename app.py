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
        return info.get('longName') or info.get('shortName') or ticker
    except:
        return ticker

def call_gemini(prompt):
    # 優先使用 flash-latest (目前最穩、額度高且速度快的代號)
    try:
        model = genai.GenerativeModel('gemini-flash-latest')
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"⚠️ AI 服務暫時無法回應，請稍後再試。({str(e)})"

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
        bsl = max(highs[-20:]) # Buy-side Liquidity
        ssl = min(lows[-20:])  # Sell-side Liquidity
        swing_high = max(highs[-60:])
        swing_low = min(lows[-60:])
        eq = (swing_high + swing_low) / 2
        pd_zone = "Premium (溢價/昂貴)" if current_price > eq else "Discount (折價/便宜)"

        # 整理最近 5 日 K 線數據給 AI
        candles_str = ""
        for i in range(-5, 0):
            candles_str += f"- {dates[i]}: O={opens[i]:.1f}, H={highs[i]:.1f}, L={lows[i]:.1f}, C={closes[i]:.1f}\n"

        # 構建針對手機優化的 Prompt
        prompt = f"""
        你現在是專精 ICT 與 SMC (Smart Money Concepts) 的量化操盤手。
        標的: {stock_name} ({ticker})
        現價: {current_price:.2f} ({change_pct:.2f}%)
        位階: {pd_zone} (均衡點 EQ: {eq:.2f})
        流動性池: BSL {bsl:.2f} / SSL {ssl:.2f}

        【近 5 日價格行為數據】
        {candles_str}

        任務: 根據數據進行專業分析，並嚴格遵守以下 LINE 手機版排版格式：

        ---
        ### 📊 **{stock_name} ({ticker}) SMC 戰報**
        **📅 日期**: {dates[-1]}
        **現價**: {current_price:.2f} ({change_pct:.2f}%)

        ---
        #### **1️⃣ 市場結構 | Structure**
        * **趨勢狀態**: [判斷多頭/空頭/盤整]
        * **結構動作**: [描述最近是否發生 BOS 或 CHoCH]
        * **位階判定**: {pd_zone} (EQ: {eq:.2f})

        #### **2️⃣ 機構足跡 | Order Flow**
        * **🧱 訂單塊 (OB)**: 建議觀察 **[價格區間]** (支撐/壓力)。
        * **⚡ 價值缺口 (FVG)**: 留意 **[價格區間]** 的回補狀況。
        * **🌊 流動性**: [上方/下方] 目標為 **[價格]**。

        #### **3️⃣ 執行策略 | Execution**
        > **🎯 偏好方向**: [做多/做空/觀望]
        * **關注進場**: 回測 **[POI價格區間]** 時觀察。
        * **失效防守**: [跌破/突破] **[價格]** 則分析失效。

        ---
        #### **💡 操盤手筆記**
        [給出一句基於 SMC 邏輯的精闢總結，例如：在溢價區不追多，耐心等待回測。]
        """

        return call_gemini(prompt)

    except Exception as e:
        return f"⚠️ 分析系統錯誤: {str(e)}"

# --- 4. LINE Webhook 伺服器 ---
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
    
    # 判斷是否為代號 (英文、數字、點、橫槓)
    if re.match(r'^[A-Za-z0-9.-]+$', user_msg):
        # 收到代號後開始分析
        reply_text = analyze_stock(user_msg)
    else:
        reply_text = "請輸入股票代號 (例如: 2330, TSLA, BTC-USD)"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    # Render 環境建議不用設定 port，它會自動抓
    app.run()
