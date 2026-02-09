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
        
        # 2. 抓取數據 (抓取 100 天以計算更長期的結構)
        stock = yf.Ticker(ticker)
        df = stock.history(period="100d")
        
        if df.empty:
            return f"❌ 找不到 {ticker}，請確認代號。"

        stock_name = get_stock_name(ticker)

        # 3. 數據前處理
        # 轉成列表方便取出最後幾根 K 線
        closes = df['Close'].tolist()
        highs = df['High'].tolist()
        lows = df['Low'].tolist()
        opens = df['Open'].tolist()
        volumes = df['Volume'].tolist()
        dates = df.index.strftime('%Y-%m-%d').tolist()

        # SMC 關鍵數據計算
        current_price = closes[-1]
        
        # 尋找近期流動性 (Liquidity Pool) - 近 20 日高低點
        bsl = max(highs[-20:]) # Buy-Side Liquidity
        ssl = min(lows[-20:])  # Sell-Side Liquidity
        
        # 判斷溢價/折價區 (Premium / Discount)
        # 取近 60 日波段範圍
        swing_high = max(highs[-60:])
        swing_low = min(lows[-60:])
        equilibrium = (swing_high + swing_low) / 2
        pd_zone = "Premium (溢價區-找空點)" if current_price > equilibrium else "Discount (折價區-找買點)"

        # 準備最近 5 天的 K 線數據字串 (讓 AI "看" 到 K 線型態)
        # 格式: 日期 | 開 | 高 | 低 | 收 | 量
        candles_str = ""
        for i in range(-5, 0):
            candles_str += f"- {dates[i]}: O={opens[i]:.1f}, H={highs[i]:.1f}, L={lows[i]:.1f}, C={closes[i]:.1f}, Vol={volumes[i]}\n"

        # 4. 建構 SMC 專用 Prompt (極度詳細)
        prompt = f"""
        你現在是 ICT (Inner Circle Trader) 與 SMC 策略的頂尖量化分析師。
        
        【資產概況】
        標的: {stock_name} ({ticker})
        現價: {current_price:.2f}
        市場位階: {pd_zone} (50% 均衡點: {equilibrium:.2f})
        近期流動性目標: 上方 BSL {bsl:.2f} / 下方 SSL {ssl:.2f}

        【近 5 日價格行為 (Price Action)】
        {candles_str}

        【分析任務】
        請根據上述「具體的 K 線數據」，進行嚴謹的 SMC 邏輯推演：

        1. **識別訂單塊 (Order Block)**：
           - 觀察最近是否有「吞噬型態」或「強勢突破前的反向 K 線」。
           - 標出具體的 OB 價格範圍。

        2. **識別價值缺口 (FVG / Imbalance)**：
           - 觀察 K 線之間是否有未回補的缺口 (Gap) 或急漲急跌留下的流動性失衡。
           - 標出 FVG 價格範圍。

        3. **市場結構 (BOS / CHoCH)**：
           - 判斷最近一次是突破前高 (BOS) 還是跌破前低 (CHoCH)？
           - 目前是否發生流動性獵殺 (Liquidity Sweep)？(例如插針後收回)。

        【輸出格式】
        請直接輸出以下表格與結論 (不要廢話)：

        ### 🦁 {stock_name} ({ticker}) SMC 機構視角

        | 指標 | 狀態 | 關鍵價位 / 解讀 |
        | :--- | :--- | :--- |
        | **結構 (Structure)** | [多頭/空頭] | [BOS 或 CHoCH 發生位置] |
        | **位階 (P/D Array)** | {pd_zone} | 均衡點 {equilibrium:.2f} |
        | **動能 (Momentum)** | [強/弱] | [根據 K 線實體大小判斷] |

        **🎯 機構足跡 (Smart Money Footprint)**
        * **🧱 訂單塊 (OB)**: 觀察 [日期] 的 K 線，潛在支撐/壓力在 **[價格區間]**。
        * **⚡ 價值缺口 (FVG)**: 留意 **[價格區間]** 的失衡區，等待回補。
        * **🌊 流動性 (Liquidity)**: [上方/下方] 流動性目標為 **[價格]**。

        **📝 操盤劇本 (Execution)**
        > **方向**: [做多/做空/觀望]
        * **進場觸發**: 等待價格回測 **[OB或FVG區域]** 且出現 [反轉訊號] 時入場。
        * **失效防守**: 若收盤價越過 **[某個關鍵高低點]** 則分析失效。
        """

        # 使用 Gemini 2.5 Flash
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content(prompt)
        return response.text

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
