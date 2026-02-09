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

def analyze_stock(stock_id):
    try:
        # 1. 處理代號
        ticker = stock_id.upper()
        if re.match(r'^\d{4}$', ticker):
            ticker += '.TW'
        
        # 2. 抓取數據
        df = yf.download(ticker, period="60d", auto_adjust=True)
        if df.empty:
            return f"❌ 找不到 {ticker} 的數據。"

        # 清洗數據
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if 'Close' not in df.columns:
            if 'Adj Close' in df.columns:
                df['Close'] = df['Adj Close']
            else:
                return "⚠️ 數據異常。"

        close = float(df['Close'].iloc[-1])
        change_pct = ((close - float(df['Close'].iloc[-2])) / float(df['Close'].iloc[-2])) * 100
        
        # 3. 測試模型 (關鍵診斷區)
        prompt = f"分析股票 {ticker}，現價 {close}，漲跌 {change_pct:.2f}%。請給出一句簡短點評。"
        
        # 嘗試使用 gemini-1.5-flash
        try:
            model = genai.GenerativeModel('gemini-1.5-flash')
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            # 如果失敗，嘗試列出所有可用模型
            error_msg = str(e)
            available_models = []
            try:
                for m in genai.list_models():
                    if 'generateContent' in m.supported_generation_methods:
                        available_models.append(m.name)
            except Exception as list_e:
                available_models = [f"無法讀取清單: {str(list_e)}"]
            
            # 將診斷結果回傳給 LINE
            return (f"⚠️ 模型調用失敗！\n"
                    f"錯誤原因: {error_msg}\n\n"
                    f"🔍 您的 API Key 可用模型清單:\n" + "\n".join(available_models))

    except Exception as e:
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
    if re.match(r'^[A-Za-z0-9.]+$', user_msg):
        reply_text = analyze_stock(user_msg)
    else:
        reply_text = "請輸入代號"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    app.run()
