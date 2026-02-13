import os
import re
import yfinance as yf
import pandas as pd
import google.generativeai as genai
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import TextSendMessage, MessageEvent, TextMessage
from datetime import datetime
import pytz

app = Flask(__name__)

# --- 1. 金鑰配置 ---
line_bot_api = LineBotApi(os.environ.get('LINE_CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('LINE_CHANNEL_SECRET'))
genai.configure(api_key=os.environ.get('GEMINI_API_KEY'))

# --- 2. 狀態管理 ---
# 儲存使用者狀態: {user_id: {'state': 'WAITING_MODE_SELECTION', 'ticker': '2330.TW'}}
USER_STATE = {}

# --- 3. 輔助函數 ---
def get_stock_name(ticker):
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        # 優先抓取中文名稱
        return info.get('longName') or info.get('shortName') or ticker
    except:
        return ticker

def get_market_data():
    """抓取大盤關鍵數據 (VIX, 美債, 台股/美股指數)"""
    try:
        # VIX 恐慌指數, 美國 10 年期公債殖利率, 標普 500, 台灣加權指數
        tickers = ['^VIX', '^TNX', '^GSPC', '^TWII']
        data = yf.download(tickers, period="5d", progress=False)['Close']

        # 取得最新一筆非空值數據 (針對每個 ticker 獨立取最後一筆有效值)
        def get_last_valid(ticker):
            if ticker in data and not data[ticker].dropna().empty:
                return data[ticker].dropna().iloc[-1]
            return 0

        last_vix = get_last_valid('^VIX')
        last_tnx = get_last_valid('^TNX')
        last_gspc = get_last_valid('^GSPC')
        last_twii = get_last_valid('^TWII')

        return {
            "vix": f"{last_vix:.2f}",
            "tnx": f"{last_tnx:.2f}%",
            "gspc": f"{last_gspc:.0f}",
            "twii": f"{last_twii:.0f}"
        }
    except Exception as e:
        print(f"Error fetching market data: {e}")
        return {"vix": "N/A", "tnx": "N/A", "gspc": "N/A", "twii": "N/A"}

def get_stock_news(ticker, limit=3):
    """抓取個股相關新聞標題"""
    try:
        stock = yf.Ticker(ticker)
        news = stock.news
        if not news:
            return "暫無相關新聞"

        news_summary = ""
        count = 0
        for item in news:
            if count >= limit: break

            # Try new structure first
            content = item.get('content', {})
            title = content.get('title')

            link = None
            if content:
                click_through = content.get('clickThroughUrl')
                if click_through:
                    link = click_through.get('url')
                if not link:
                    canonical = content.get('canonicalUrl')
                    if canonical:
                        link = canonical.get('url')

            # Fallback to old structure
            if not title:
                title = item.get('title', '')
            if not link:
                link = item.get('link', '')

            news_summary += f"- {title} ({link})\n"
            count += 1
        return news_summary
    except Exception as e:
        return f"無法取得新聞: {str(e)}"

def resolve_ticker(stock_id):
    """
    解析股票代號，處理台股 .TW/.TWO 邏輯。
    回傳: (resolved_ticker, history_df) 或 (None, None)
    """
    ticker = stock_id.upper()

    # 判斷是否為 4 位數代號 (台灣股票)
    if re.match(r'^\d{4}$', ticker):
        # 優先嘗試 .TW (上市)
        test_ticker = ticker + '.TW'
        stock = yf.Ticker(test_ticker)
        # 用較短的 period 檢查是否存在，節省資源，但 analyze_stock 需要 100d
        # 這裡為了統一，我們取 100d，如果不夠長也沒關係，重點是有沒有資料
        df = stock.history(period="100d")

        if not df.empty:
            return test_ticker, df
        else:
            # 若無資料，嘗試 .TWO (上櫃)
            test_ticker = ticker + '.TWO'
            stock = yf.Ticker(test_ticker)
            df = stock.history(period="100d")

            if not df.empty:
                return test_ticker, df
    else:
        # 非 4 位數代號 (如美股或其他輸入)，直接查詢
        stock = yf.Ticker(ticker)
        df = stock.history(period="100d")
        if not df.empty:
            return ticker, df

    return None, None

def call_gemini_api(prompt):
    """統一呼叫 Gemini API 的函數"""
    model_list = [
        'gemma-3-27b-it',
        'gemma-3-12b-it',
        'gemma-3-4b-it',
        'gemma-3-1b-it',
        # 備援模型
        'gemini-2.0-flash-exp',
        'gemini-1.5-flash-8b'
    ]

    last_error = None
    for model_name in model_list:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text
        except Exception as e:
            error_msg = str(e)
            last_error = error_msg
            print(f"Model {model_name} failed: {error_msg}")
            continue

    return f"⚠️ 系統異常 (所有模型皆忙碌或無法使用): {last_error}"


# --- 4. 分析邏輯 ---
def analyze_stock(ticker, df):
    """
    SMC 分析核心邏輯 (Mode 1: SMC)
    注意：這裡傳入的 ticker 已經是 resolve 過的，df 也是抓好的
    """
    try:
        stock_name = get_stock_name(ticker)
        market_data = get_market_data()
        stock_news = get_stock_news(ticker, limit=3)

        # 基礎指標計算
        closes = df['Close'].tolist()
        highs = df['High'].tolist()
        lows = df['Low'].tolist()
        opens = df['Open'].tolist()
        volumes = df['Volume'].tolist()
        dates = df.index.strftime('%Y-%m-%d').tolist()

        current_price = float(closes[-1])
        # 避免除以零或索引錯誤，但前面 df check 過了應該還好
        change_pct = 0
        if len(closes) >= 2:
            change_pct = ((current_price - float(closes[-2])) / float(closes[-2])) * 100
        
        # SMC 關鍵位階
        bsl = max(highs[-20:]) if len(highs) >= 20 else max(highs)
        ssl = min(lows[-20:]) if len(lows) >= 20 else min(lows)
        swing_high = max(highs[-60:]) if len(highs) >= 60 else max(highs)
        swing_low = min(lows[-60:]) if len(lows) >= 60 else min(lows)
        eq = (swing_high + swing_low) / 2
        pd_zone = "Premium 溢價 (昂貴)" if current_price > eq else "Discount 折價 (便宜)"

        # 簡單 FVG 偵測
        fvg_msg = "無明顯 FVG"
        if len(highs) >= 3:
            if highs[-3] < lows[-1]:
                fvg_msg = f"潛在看漲 FVG ({highs[-3]:.1f} - {lows[-1]:.1f})"
            elif lows[-3] > highs[-1]:
                fvg_msg = f"潛在看跌 FVG ({highs[-1]:.1f} - {lows[-3]:.1f})"

        # 整理最近 5 日 K 線
        candles_str = ""
        loop_range = range(-5, 0) if len(dates) >= 5 else range(-len(dates), 0)
        for i in loop_range:
            candles_str += f"- {dates[i]}: O={opens[i]:.1f}, H={highs[i]:.1f}, L={lows[i]:.1f}, C={closes[i]:.1f}, V={volumes[i]}\n"

        current_time = datetime.now(pytz.timezone('Asia/Taipei')).strftime("%Y-%m-%d %H:%M")

        prompt = f"""
        你現在是 SMC (Smart Money Concepts) 專業操盤手與金融分析師。
        請根據以下提供的實時數據，為使用者撰寫一份詳細的股票分析報告。

        【基本資訊】
        - 標的: {stock_name} ({ticker})
        - 時間: {current_time}
        - 現價: {current_price:.2f} (漲跌幅 {change_pct:.2f}%)
        - 市場位階: {pd_zone} (EQ 均衡點: {eq:.2f})
        - 流動性目標: 上方 BSL {bsl:.2f} / 下方 SSL {ssl:.2f}
        - FVG 狀態: {fvg_msg}

        【大盤環境數據】(用於模式 0)
        - VIX 恐慌指數: {market_data['vix']}
        - 美國 10 年期公債殖利率: {market_data['tnx']}
        - 標普 500 指數: {market_data['gspc']}
        - 台灣加權指數: {market_data['twii']}

        【近 5 日 K 線數據】
        {candles_str}

        【最新新聞頭條】(用於模式 3)
        {stock_news}

        任務: 請根據數據進行分析，並嚴格遵守以下「LINE 清爽排版」格式輸出：
        1. 必須包含 4 個模式 (Mode 0 ~ Mode 3)。
        2. 使用表情符號作為小標題，版面要整潔易讀。
        3. 內容要詳細且符合 SMC 邏輯 (Order Block, FVG, Liquidity)。
        4. 針對新聞內容進行摘要與影響評估。

        輸出格式參考如下 (請直接套用數據，不要照抄範例文字)：

        ⚠️ 資料時間：{current_time}
        ----------------------------

        🌡️ 模式 0：大盤環境儀表板
        > 市場溫度計：[請根據 VIX 與指數判斷市場情緒，例如：恐慌/貪婪/觀望]
        > 資金流向：[請根據台股/美股指數判斷大致趨勢]
        > 📊 關鍵數據：
        >  * VIX 指數：{market_data['vix']} ([判斷高/低/持平])
        >  * US 10Y 殖利率：{market_data['tnx']} ([判斷趨勢])
        >  * 加權指數：{market_data['twii']} (台股) / S&P500：{market_data['gspc']} (美股)

        ----------------------------
        模式 1：個股與趨勢分析
        {stock_name} ({ticker})
        💰 報價：{current_price:.2f} ({change_pct:.2f}%)
        📈 趨勢：[判斷短線與長線趨勢，例如：多頭回調/空頭反彈/區間震盪]
        🛠️ 關鍵價位 (SMC)：
         * 訂單塊 OB：[根據 K 線推測可能的支撐/壓力區]
         * 缺口 FVG：{fvg_msg}
         * 流動性 BSL/SSL：上方 {bsl:.2f} / 下方 {ssl:.2f}
        📝 SMC 短評：
        > [請用 SMC 術語描述價格行為，例如：是否獵殺流動性 (Liquidity Sweep)? 是否出現結構破壞 (BOS)? 目前價格處於 Premium 還是 Discount?]

        ----------------------------
        模式 2：風險評估 (Risk Radar)
         * 市場風險：[根據 VIX 與大盤判斷]
         * 技術風險：[根據與 EQ 的距離判斷是否過熱或超賣]
         * 籌碼/消息風險：[根據成交量與新聞判斷]

        ----------------------------
        模式 3：今日觀盤重點 (Daily Brief)
         * 新聞摘要：[請摘要上方提供的新聞重點，若無新聞則分析成交量變化]
         * 觀察重點：[給出 1-2 個具體的看盤重點，例如：留意 xxx 價位是否守住]

        ----------------------------
        💡 專業策略建議：
        [給出具體的操作建議，例如：等待回測 OB 做多，或反彈至 FVG 做空]

        ----------------------------
        📚 術語小教室 (請簡單解釋報告中出現的 3-5 個關鍵術語，例如)：
         * SMC (Smart Money Concepts): 聰明錢概念，追蹤法人大戶的資金流向。
         * FVG (Fair Value Gap): 價格急速波動產生的缺口，常作為支撐或壓力。
         * OB (Order Block): 訂單塊，大戶介入的關鍵價位區。
         * BSL/SSL (Buy/Sell Side Liquidity): 買方/賣方流動性，通常是止損單聚集的位置。
         * EQ (Equilibrium): 均衡點，價格區間的中間值。
         * VIX: 恐慌指數，數值越高代表市場越恐慌。
        """

        return call_gemini_api(prompt)

    except Exception as e:
        return f"⚠️ 系統異常: {str(e)}"

def analyze_industry(ticker):
    """
    產業分析核心邏輯 (Mode 2: Industry)
    """
    try:
        stock = yf.Ticker(ticker)
        # Try to get info, but proceed even if some fields missing
        try:
            info = stock.info
        except:
            info = {}

        stock_name = get_stock_name(ticker)
        stock_news = get_stock_news(ticker, limit=5)

        sector = info.get('sector', 'N/A')
        industry = info.get('industry', 'N/A')
        summary = info.get('longBusinessSummary', 'N/A')

        current_time = datetime.now(pytz.timezone('Asia/Taipei')).strftime("%Y-%m-%d %H:%M")

        prompt = f"""
        你現在是頂尖的產業分析師與供應鏈專家。
        請針對以下公司撰寫一份「供應鏈深度偵查報告」。

        【目標公司】
        - 公司: {stock_name} ({ticker})
        - 產業: {industry} ({sector})
        - 業務簡介: {summary[:500]}... (摘要)
        - 最新新聞與傳言線索:
        {stock_news}

        【報告格式要求】
        請嚴格按照以下架構輸出，語氣專業且深入，需包含推論信心度與具體查證結果。

        🎯 {stock_name} ({ticker}) 供應鏈深度偵查報告

        1. 核心結論 (BLUF)
        • 推論信心度：⭐⭐⭐⭐⭐ (請自行評估 1-5 星)
        • 一句話總結：[請用精煉的語言總結該公司在供應鏈中的關鍵地位與轉型成果]

        2. 關鍵題材與傳言驗證 (請根據新聞或已知產業知識，列出 3 點市場關注的傳言或題材並驗證)
        • 傳言 A：[例如：切入某大廠供應鏈 / 新產品量產時間]
        • 查證結果：[✅ 已證實 / ❓ 待觀察 / ❌ 已否認] [請簡述查證依據]
        • 傳言 B：[同上]
        • 查證結果：[同上]
        • 傳言 C：[同上]
        • 查證結果：[同上]

        3. 六步推論鏈 (Supply Chain Logic) (請建立一個邏輯嚴密的推論過程，解釋為何該公司會受惠/受害)
        1. 事實：[產業趨勢或技術瓶頸]
        2. 事實：[主要客戶或競爭對手的動向]
        3. 事實：[該公司的關鍵技術或產品優勢]
        4. 推論：[連接上述事實的邏輯]
        5. 事實：[佐證推論的市場數據或合作案]
        6. 結論：[最終對該公司的影響判定]

        4. 技術規格對決表 (Spec Match) (請列出該公司與競爭對手的關鍵規格對比，若無直接對手則列出前後代產品對比)
        | 項目 | {stock_name} | [競爭對手/舊產品] | 備註 |
        |---|---|---|---|
        | [關鍵指標 1] | ... | ... | ... |
        | [關鍵指標 2] | ... | ... | ... |
        | [關鍵指標 3] | ... | ... | ... |

        請確保內容「完整正確」，若無確切數據請根據產業常識進行合理推估並註明。
        """

        return call_gemini_api(prompt)

    except Exception as e:
        return f"⚠️ 產業分析異常: {str(e)}"

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
    user_id = event.source.user_id

    # 1. 檢查使用者是否處於「選擇模式」狀態
    if user_id in USER_STATE:
        state_data = USER_STATE[user_id]
        ticker = state_data['ticker']

        if user_msg == "1":
            # 模式 1: SMC
            # 這裡需要傳入 df，我們可以在 resolve 時 cache，或這裡重抓
            # 為了簡單，這裡重新 resolve 一次 (成本是一次 history call)，或者我們在 state 裡不存 df (因為不能序列化太好)
            # 因為 yfinance 有 cache，短時間重複呼叫應該還好
            # 但我們已經知道正確的 ticker 是什麼了

            # 重新抓取資料以供 analyze_stock 使用
            try:
                stock = yf.Ticker(ticker)
                df = stock.history(period="100d")
                if df.empty:
                    reply_text = f"❌ 無法取得 {ticker} 的數據，請重新查詢。"
                else:
                    reply_text = analyze_stock(ticker, df)
            except Exception as e:
                 reply_text = f"❌ 發生錯誤: {str(e)}"

            # 清除狀態
            del USER_STATE[user_id]
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            return

        elif user_msg == "2":
            # 模式 2: 產業分析
            reply_text = analyze_industry(ticker)

            # 清除狀態
            del USER_STATE[user_id]
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
            return

        else:
            # 輸入無效，如果看起來像是新的股票代號，則重新開始流程
             if re.match(r'^[A-Za-z0-9.-]+$', user_msg) and user_msg not in ["1", "2"]:
                 # Fall through to the ticker handling logic below
                 # First clear old state
                 del USER_STATE[user_id]
             else:
                reply_text = "請輸入數字選擇模式：\n1. SMC 分析\n2. 產業分析\n(或輸入新的股票代號)"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
                return

    # 2. 處理股票代號輸入 (或新的查詢)
    if re.match(r'^[A-Za-z0-9.-]+$', user_msg):
        # 嘗試解析股票代號
        resolved_ticker, df = resolve_ticker(user_msg)

        if resolved_ticker:
            # 找到股票，進入選擇模式
            USER_STATE[user_id] = {
                'ticker': resolved_ticker,
                'timestamp': datetime.now()
            }
            stock_name = get_stock_name(resolved_ticker)

            reply_text = f"已找到：{stock_name} ({resolved_ticker})\n請選擇分析模式：\n1. SMC 技術分析\n2. 產業供應鏈分析\n(請輸入數字 1 或 2)"
        else:
            reply_text = f"❌ 找不到股票代號: {user_msg}"
    else:
        reply_text = "請輸入股票代號 (例如: 2330)"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
