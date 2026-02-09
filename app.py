import os
import re
import yfinance as yf
import google.generativeai as genai
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage

app = Flask(__name__)

# 從環境變數讀取金鑰 (等等在 Render 設定)
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))

# SMC 分析核心邏輯
def analyze_stock(stock_id):
    try:
        # 1. 處理代號 (台股加 .TW)
        ticker = stock_id.upper()
        if re.match(r'^\d{4}$', ticker): # 如果是4位數字
            ticker += '.TW'
        
        # 2. 抓取數據 (yfinance)
        df = yf.download(ticker, period="60d")
        if df.empty:
            return "❌ 找不到此股票數據，請確認代號。"
        
        # 3. 計算技術指標 (Python 硬運算)
        close = df['Close'].iloc[-1]
        prev_close = df['Close'].iloc[-2]
        change_pct = ((close - prev_close) / prev_close) * 100
        
        sma_20 = df['Close'].rolling(20).mean().iloc[-1]
        sma_60 = df['Close'].rolling(60).mean().iloc[-1]
        
        # 簡單 RSI 計算
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs)).iloc[-1]

        # 4. 組裝 Prompt 給 Gemini
        prompt = f"""
        你現在是 SMC (Smart Money Concepts) 頂尖交易員。
        
        【市場數據】
        股票代號: {ticker}
        現價: {close:.2f} (漲跌幅: {change_pct:.2f}%)
        SMA20: {sma_20:.2f}
        SMA60: {sma_60:.2f}
        RSI(14): {rsi:.2f}
        前波高點(近20日): {df['High'].tail(20).max():.2f}
        前波低點(近20日): {df['Low'].tail(20).min():.2f}

        【任務】
        請用「SMC 機構訂單流」風格，撰寫約 150 字的短評。
        1. 判斷趨勢 (SMA排列)。
        2. 尋找流動性 (Liquidity) 與 FVG 潛在區。
        3. 給出操作建議 (做多/做空/觀望)。
        4. 語氣要專業、冷靜。
        """

        # 5. 呼叫 Gemini
        model = genai.GenerativeModel('gemini-pro')
        response = model.generate_content(prompt)
        return response.text

    except Exception as e:
        return f"⚠️ 系統錯誤: {str(e)}"

# LINE Webhook 入口
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# 處理訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    # 簡單判斷：如果是股票代號格式 (例如 2330 或 TSLA)
    if re.match(r'^[A-Za-z0-9]+$', user_msg):
        reply_text = analyze_stock(user_msg)
    else:
        reply_text = "請輸入股票代號 (例如: 2330, 0050, TSLA)"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    app.run()
