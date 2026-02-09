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

# 從環境變數讀取金鑰
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))

# SMC 分析核心邏輯
def analyze_stock(stock_id):
    try:
        # 1. 處理代號
        ticker = stock_id.upper()
        if re.match(r'^\d{4}$', ticker):
            ticker += '.TW'
        
        # 2. 抓取數據 (修正 yfinance 格式)
        print(f"正在抓取: {ticker}")
        df = yf.download(ticker, period="60d", auto_adjust=True)
        
        if df.empty:
            return f"❌ 找不到 {ticker} 的數據，請確認代號是否正確。"

        # 【清洗數據】處理 MultiIndex (避免格式錯誤)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # 確保有收盤價
        if 'Close' not in df.columns:
            if 'Adj Close' in df.columns:
                df['Close'] = df['Adj Close']
            else:
                return "⚠️ 數據異常，無法讀取收盤價。"

        # 3. 計算技術指標
        # 強制轉型 float，避免 numpy/pandas 格式造成報錯
        close = float(df['Close'].iloc[-1])
        prev_close = float(df['Close'].iloc[-2])
        change_pct = ((close - prev_close) / prev_close) * 100
        
        sma_20 = float(df['Close'].rolling(20).mean().iloc[-1])
        sma_60 = float(df['Close'].rolling(60).mean().iloc[-1])
        
        # RSI 計算
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        
        loss_val = float(loss.iloc[-1])
        if loss_val == 0:
            rsi = 100.0
        else:
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs)).iloc[-1]
        rsi = float(rsi)

        # 高低點
        high_20 = float(df['High'].tail(20).max())
        low_20 = float(df['Low'].tail(20).min())

        # 4. 組裝 Prompt
        prompt = f"""
        你現在是 SMC (Smart Money Concepts) 頂尖交易員。
        
        【市場數據】
        股票代號: {ticker}
        現價: {close:.2f} (漲跌幅: {change_pct:.2f}%)
        SMA20: {sma_20:.2f}
        SMA60: {sma_60:.2f}
        RSI(14): {rsi:.2f}
        前波高點(近20日): {high_20:.2f}
        前波低點(近20日): {low_20:.2f}

        【任務】
        請用「SMC 機構訂單流」風格，撰寫約 150 字的短評。
        1. 判斷趨勢 (SMA排列)。
        2. 尋找流動性 (Liquidity) 與 FVG 潛在區。
        3. 給出操作建議 (做多/做空/觀望)。
        4. 語氣要專業、冷靜。
        """

        # 【關鍵修正】這裡改用最新的 gemini-1.5-flash
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        return response.text

    except Exception as e:
        print(f"Error: {e}")
        return f"⚠️ 系統錯誤: {str(e)}"

# LINE Webhook
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
    
    # 允許代號輸入 (含數字與英文)
    if re.match(r'^[A-Za-z0-9.]+$', user_msg):
        reply_text = analyze_stock(user_msg)
    else:
        reply_text = "請輸入股票代號 (例如: 2330, 0050)"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    app.run()
