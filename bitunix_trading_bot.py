import ccxt
import numpy as np
import requests
import hashlib
import uuid
import time
import json
import random
import discord
from discord.ext import tasks
import os
import pandas as pd
from discord.ext import commands
from config import BITUNIX_API_KEY, BITUNIX_SECRET_KEY, DISCORD_WEBHOOK_URL, STOP_MULT, LIMIT_MULT, RSI_BUY, RSI_LEN, EXIT_RSI, BREAKOUT_LOOKBACK, ATR_LEN, ATR_MULT, TIMEFRAME, LEVERAGE, TRADING_PAIR, SYMBOL, MARGIN_COIN, LOOP_INTERVAL_SECONDS, QUANTITY_PRECISION
from config import rsiSell, exitRSI_short, CONDITIONAL_ORDER_MAX_RETRIES, CONDITIONAL_ORDER_RETRY_INTERVAL
import threading


# === 全域變數與統計檔案設定 ===
STATS_FILE = os.path.join(os.path.dirname(__file__), "stats.json")
win_count = 0
loss_count = 0

# === 移動止損相關全域變數 ===
current_pos_entry_type = None # 記錄持倉的進場信號類型 ('rsi' 或 'breakout')
current_stop_loss_price = None # 記錄當前持倉的止損價格
current_position_id_global = None # 記錄當前持倉的 positionId
last_checked_kline_time = None  # 新增：記錄上一次檢查的K棒時間

# === 新增：本地已通知平倉單ID記錄 ===
NOTIFIED_ORDERS_FILE = os.path.join(os.path.dirname(__file__), "notified_orders.json")
notified_orders_lock = threading.Lock()

def load_stats():
    global win_count, loss_count
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                stats = json.load(f)
                win_count = stats.get('win_count', 0)
                loss_count = stats.get('loss_count', 0)
            print(f"載入統計數據: 勝 {win_count}, 負 {loss_count}")
        except (IOError, json.JSONDecodeError):
            print(f"統計數據讀取失敗，初始化為 0")
            win_count = 0
            loss_count = 0
    else:
        print("未找到統計數據檔案，初始化為 0")
        win_count = 0
        loss_count = 0

def save_stats():
    global win_count, loss_count
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump({'win_count': win_count, 'loss_count': loss_count}, f)
        print(f"儲存統計數據: 勝 {win_count}, 負 {loss_count}")
    except IOError:
        print(f"無法儲存統計數據")

def load_notified_order_ids():
    if os.path.exists(NOTIFIED_ORDERS_FILE):
        try:
            with open(NOTIFIED_ORDERS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_notified_order_ids(order_ids):
    with notified_orders_lock:
        try:
            with open(NOTIFIED_ORDERS_FILE, 'w') as f:
                json.dump(order_ids, f)
        except Exception as e:
            print(f"寫入已通知平倉單ID失敗: {e}")





# === Bitunix API 函數 === #
def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


# 完全按照ccc.py中的get_signed_params函數實現

print(f"[Config Check] SYMBOL from config: {SYMBOL}")
print(f"[Config Check] TRADING_PAIR from config: {TRADING_PAIR}")

def get_signed_params(api_key, secret_key, query_params: dict = None, body: dict = None, path: str = None, method: str = None):
    """
    按照 Bitunix 官方雙重 SHA256 簽名方式對請求參數進行簽名。
    
    參數:
        api_key (str): 用戶 API Key
        secret_key (str): 用戶 Secret Key
        query_params (dict): 查詢參數 (GET 方法)
        body (dict or None): 請求 JSON 主體 (POST 方法)
    
    返回:
        headers (dict): 包含簽名所需的請求頭（api-key, sign, nonce, timestamp 等）
    """
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))

    # 構造 query string: 將參數按鍵名 ASCII 升序排序後，鍵名與鍵值依次拼接
    if query_params:
        params_str = {k: str(v) for k, v in query_params.items()}
        sorted_items = sorted(params_str.items(), key=lambda x: x[0])
        query_str = "".join([f"{k}{v}" for k, v in sorted_items])
    else:
        query_str = ""

    # 構造 body string: 將 JSON 體壓縮成字符串 (無空格)
    if body is not None:
        if isinstance(body, (dict, list)):
            body_str = json.dumps(body, separators=(',', ':'), ensure_ascii=False)
        else:
            body_str = str(body)
    else:
        body_str = ""

    # 根據 method 決定簽名內容
    if method == "GET":
        digest_input = nonce + timestamp + api_key + query_str
    else:
        digest_input = nonce + timestamp + api_key + body_str
    # 第一次 SHA256
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    # 第二次 SHA256
    sign = hashlib.sha256((digest + secret_key).encode('utf-8')).hexdigest()

  

    # 構造標頭
    headers = {
        "api-key": api_key,
        "sign": sign,
        "nonce": nonce,
        "timestamp": timestamp,
        "language": "en-US",
        "Content-Type": "application/json"
    }
    return nonce, timestamp, sign, headers

# === 日誌紀錄函數 ===
def log_event(event_type, message):
    log_file = os.path.join(os.path.dirname(__file__), "log.txt")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{event_type}] {message}\n")

def send_order(api_key, secret_key, symbol, margin_coin, side, size, leverage=LEVERAGE, position_id=None):
    # 直接下單，不再自動設置槓桿/槓桿
    # 正確的API端點路徑
    path = "/api/v1/futures/trade/place_order"
    url = f"https://fapi.bitunix.com{path}"
    
    # 根據cc.py中的格式調整請求參數
    # 將side轉換為適當的side和tradeSide參數
    if side == "open_long":
        api_side = "BUY"
        trade_side = "OPEN"
    elif side == "close_long":
        api_side = "SELL"
        trade_side = "CLOSE"
    elif side == "open_short":
        api_side = "SELL"
        trade_side = "OPEN"
    elif side == "close_short":
        api_side = "BUY"
        trade_side = "CLOSE"
    else:
        print(f"錯誤：不支持的交易方向 {side}")
        return {"error": f"不支持的交易方向: {side}"}
    
    body = {
        "symbol": symbol,
        "marginCoin": margin_coin,  # 新增保證金幣種參數
        "qty": str(size),  # API要求數量為字符串
        "side": api_side,
        "tradeSide": trade_side,
        "orderType": "MARKET",  # 市價單
        "effect": "GTC",  # 訂單有效期
        "leverage": leverage  # 新增：自動帶入 config 設定的槓桿
    }

    if position_id and (side == "close_long" or side == "close_short"):
        body["positionId"] = position_id

    print(f"準備發送訂單: {body}")
    log_event("下單請求", f"{body}")
    
    try:
        # 使用更新後的get_signed_params獲取完整的headers
        _, _, _, headers = get_signed_params(BITUNIX_API_KEY, BITUNIX_SECRET_KEY, {}, body)
        
        response = requests.post(url, headers=headers, data=json.dumps(body, separators=(',', ':'), ensure_ascii=False))
        response.raise_for_status()  # 檢查HTTP錯誤
        result = response.json()
        print(f"API響應: {result}")
        log_event("下單回應", f"{result}")
        return result
    except requests.exceptions.HTTPError as e:
        error_msg = f"HTTP錯誤: {e}, 響應: {response.text if 'response' in locals() else '無響應'}"
        print(error_msg)
        log_event("下單錯誤", error_msg)
        send_discord_message(f"🔴 **下單錯誤**: {error_msg} 🔴", api_key, secret_key)
        return {"error": error_msg}
    except requests.exceptions.RequestException as e:
        error_msg = f"請求錯誤: {e}"
        print(error_msg)
        log_event("下單錯誤", error_msg)
        send_discord_message(f"🔴 **下單錯誤**: {error_msg} 🔴", api_key, secret_key)
        return {"error": error_msg}
    except Exception as e:
        error_msg = f"未知錯誤: {e}"
        print(error_msg)
        log_event("下單錯誤", error_msg)
        send_discord_message(f"🔴 **下單錯誤**: {error_msg} 🔴", api_key, secret_key)
        return {"error": error_msg}
# === 新增：根據 orderId 查詢 positionId 的輔助函數 ===
def get_position_id_by_order_id(api_key, secret_key, symbol, order_id, max_retries=3, retry_interval=2):
    """
    根據 orderId 查詢 positionId，輪詢持倉列表，找到最新持倉。
    """
    for attempt in range(max_retries):
        try:
            url = "https://fapi.bitunix.com/api/v1/futures/position/get_pending_positions"
            params = {"symbol": symbol}
            nonce = uuid.uuid4().hex
            timestamp = str(int(time.time() * 1000))
            sorted_items = sorted((k, str(v)) for k, v in params.items())
            query_string = "".join(f"{k}{v}" for k, v in sorted_items)
            digest_input = nonce + timestamp + api_key + query_string
            digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
            sign = hashlib.sha256((digest + secret_key).encode('utf-8')).hexdigest()
            headers = {
                "api-key": api_key,
                "sign": sign,
                "nonce": nonce,
                "timestamp": timestamp,
                "Content-Type": "application/json"
            }
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            if data.get("code") == 0 and data.get("data"):
                for pos in data["data"]:
                    # 只找有數量的持倉
                    if float(pos.get("qty", 0)) > 0:
                        # 這裡假設最新的持倉就是剛剛下單的（Bitunix API 沒有直接 orderId 對應 positionId）
                        # 可根據 avgOpenPrice、side、qty 等進一步比對
                        return pos.get("positionId")
            time.sleep(retry_interval)
        except Exception as e:
            print(f"查詢 positionId 失敗: {e}")
            time.sleep(retry_interval)
    return None

def place_conditional_orders(api_key, secret_key, symbol, margin_coin, position_id, stop_price=None, limit_price=None, max_retries=CONDITIONAL_ORDER_MAX_RETRIES, retry_interval=CONDITIONAL_ORDER_RETRY_INTERVAL):
    """
    Place Stop Loss and Take Profit orders for a given position using Bitunix API.
    自動重試設置條件單，最多 max_retries 次。
    """
    path = "/api/v1/futures/tpsl/position/place_order"
    url = f"https://fapi.bitunix.com{path}"

    body = {
        "symbol": symbol,
        "positionId": position_id,
    }

    if stop_price is not None:
        body["slPrice"] = str(stop_price) # API requires price as string
        body["slStopType"] = "LAST_PRICE" # Use LAST_PRICE as trigger type

    if limit_price is not None:
        body["tpPrice"] = str(limit_price) # API requires price as string
        body["tpStopType"] = "LAST_PRICE" # Use LAST_PRICE as trigger type

    # Ensure at least one of TP or SL is provided
    if stop_price is None and limit_price is None:
        print(f"[Conditional Orders] 警告: 未提供止損或止盈價格，不設置條件訂單 for position {position_id} on {symbol}")
        return {"error": "未提供止損或止盈價格"}

    for attempt in range(1, max_retries + 1):
        print(f"[Conditional Orders] 嘗試第 {attempt} 次設置條件單: {body}")
        try:
            _, _, _, headers = get_signed_params(api_key, secret_key, {}, body, path, method="POST")
            response = requests.post(url, headers=headers, data=json.dumps(body, separators=(',', ':'), ensure_ascii=False))
            response.raise_for_status()
            result = response.json()
            print(f"[Conditional Orders] API 響應: {result}")
            if result.get("code") == 0:
                print(f"[Conditional Orders] 成功為持倉 {position_id} 設置條件訂單（第 {attempt} 次）")
                return result
            else:
                error_msg = f"[Conditional Orders] API 返回錯誤: {result.get('msg', '未知錯誤')} (第 {attempt} 次)"
                print(error_msg)
                if attempt == max_retries:
                    send_discord_message(f"🔴 **條件訂單設置失敗（重試{max_retries}次）** 🔴", api_key, secret_key, operation_details={
                        "type": "error",
                        "details": error_msg,
                        "force_send": True
                    })
                else:
                    time.sleep(retry_interval)
        except Exception as e:
            error_msg = f"[Conditional Orders] 未知錯誤: {e} (第 {attempt} 次)"
            print(error_msg)
            if attempt == max_retries:
                send_discord_message(f"🔴 **條件訂單設置失敗（重試{max_retries}次）** 🔴", api_key, secret_key, operation_details={
                    "type": "error",
                    "details": error_msg,
                    "force_send": True
                })
            else:
                time.sleep(retry_interval)
    return {"error": f"設置條件單失敗，已重試{max_retries}次"}

# Note: As of current information, automatic trailing stop placement for breakout entries is not implemented due to lack of specific API details.

def modify_position_tpsl(api_key, secret_key, symbol, position_id, stop_price=None, limit_price=None):
    """
    Modify Stop Loss and/or Take Profit orders for a given position using Bitunix API.
    Endpoint: /api/v1/futures/tpsl/modify_position_tp_sl_order
    """
    path = "/api/v1/futures/tpsl/modify_position_tp_sl_order"
    url = f"https://fapi.bitunix.com{path}"

    body = {
        "symbol": symbol,
        "positionId": position_id,
    }

    if stop_price is not None:
        body["slPrice"] = str(stop_price) # API requires price as string
        body["slStopType"] = "LAST_PRICE" # Use LAST_PRICE as trigger type

    if limit_price is not None:
        body["tpPrice"] = str(limit_price) # API requires price as string
        body["tpStopType"] = "LAST_PRICE" # Use LAST_PRICE as trigger type

    # Ensure at least one of TP or SL is provided
    if stop_price is None and limit_price is None:
        print(f"[Modify Conditional Orders] 警告: 未提供止損或止盈價格，不修改條件訂單 for position {position_id} on {symbol}")
        return {"error": "未提供止損或止盈價格"}

    print(f"[Modify Conditional Orders] 準備為持倉 {position_id} 在 {symbol} 上修改條件訂單: {body}")

    try:
        # 使用 get_signed_params 獲取完整的 headers
        _, _, _, headers = get_signed_params(api_key, secret_key, {}, body, path, method="POST")

        response = requests.post(url, headers=headers, data=json.dumps(body, separators=(',', ':'), ensure_ascii=False))
        response.raise_for_status()  # 檢查HTTP錯誤
        result = response.json()
        print(f"[Modify Conditional Orders] API 響應: {result}")

        if result.get("code") == 0:
            print(f"[Modify Conditional Orders] 成功為持倉 {position_id} 修改條件訂單")
            return result
        else:
            error_msg = f"[Modify Conditional Orders] API 返回錯誤: {result.get('msg', '未知錯誤')}"
            print(error_msg)
            send_discord_message(f"🔴 **修改條件訂單失敗** 🔴", api_key, secret_key, operation_details={
                "type": "error",
                "details": error_msg,
                "force_send": True
            })
            return {"error": error_msg}

    except requests.exceptions.HTTPError as e:
        error_msg = f"[Modify Conditional Orders] HTTP 錯誤: {e}, 響應: {response.text if 'response' in locals() else '無響應'}"
        print(error_msg)
        send_discord_message(f"🔴 **修改條件訂單失敗** 🔴", api_key, secret_key, operation_details={
            "type": "error",
            "details": error_msg,
            "force_send": True
        })
        return {"error": error_msg}
    except requests.exceptions.RequestException as e:
        error_msg = f"[Modify Conditional Orders] 請求錯誤: {e}"
        print(error_msg)
        send_discord_message(f"🔴 **修改條件訂單失敗** 🔴", api_key, secret_key, operation_details={
            "type": "error",
            "details": error_msg,
            "force_send": True
        })
        return {"error": error_msg}
    except Exception as e:
        error_msg = f"[Modify Conditional Orders] 未知錯誤: {e}"
        print(error_msg)
        send_discord_message(f"🔴 **修改條件訂單失敗** 🔴", api_key, secret_key, operation_details={
            "type": "error",
            "details": error_msg,
            "force_send": True
        })
        return {"error": error_msg}


# === Discord 提醒設定 === #
# DISCORD_WEBHOOK_URL = 'https://discordapp.com/api/webhooks/1366780723864010813/h_CPbJX3THcOElVVHYOeJPR4gTgZGHJ1ehSeXuOAceGTNz3abY0XlljPzzxkaimAcE77'

# 消息緩衝區和計時器設置
message_buffer = []
last_send_time = 0
BUFFER_TIME_LIMIT = 180  # 3分鐘 = 180秒

# 記錄上一次的餘額，用於比較變化
last_balance = None

# 修改函數簽名以包含 operation_details
def send_discord_message(core_message, api_key=None, secret_key=None, operation_details=None):
    global message_buffer, last_send_time, win_count, loss_count # 確保能訪問全域勝敗計數
    current_time = time.time()

    # 預設顏色與 emoji
    embed_color = 0x3498db  # 藍色
    title_emoji = "ℹ️"
    if operation_details:
        op_type = operation_details.get("type")
        if op_type == "close_success":
            embed_color = 0xf39c12  # 橘色
            title_emoji = "🟠"
        elif op_type == "open_success":
            embed_color = 0x2ecc71  # 綠色
            title_emoji = "🟢"
        elif op_type == "error":
            embed_color = 0xe74c3c  # 紅色
            title_emoji = "🔴"
        elif op_type == "status_update":
            embed_color = 0xf1c40f  # 黃色
            title_emoji = "⚠️"
        else:
            embed_color = 0x3498db
            title_emoji = "ℹ️"
    else:
        embed_color = 0x3498db
        title_emoji = "ℹ️"

    # 構造勝率字符串
    total_trades = win_count + loss_count
    win_rate_str = f"{win_count / total_trades * 100:.2f}% ({win_count}勝/{loss_count}負)" if total_trades > 0 else "N/A (尚無已完成交易)"

    # 主要內容區塊
    action_specific_msg = core_message
    current_pos_status_for_discord = ""
    current_pos_pnl_msg = ""
    if api_key and secret_key:
        actual_pos_side, actual_pos_qty_str, _, actual_unrealized_pnl = get_current_position_details(api_key, secret_key, SYMBOL, MARGIN_COIN)
        if actual_pos_side in ["long", "short"] and actual_unrealized_pnl is not None:
            current_pos_pnl_msg = f"{actual_unrealized_pnl:.4f} USDT"
    if operation_details:
        op_type = operation_details.get("type")
        if op_type == "close_success":
            side_closed_display = "多單" if operation_details.get("side_closed") == "long" else "空單"
            closed_qty = operation_details.get("qty", "N/A")
            pnl = operation_details.get("pnl", 0.0)
            margin = operation_details.get("margin", None)
            pnl_display = f"{pnl:.4f}" if pnl is not None else "N/A"
            margin_display = f"{margin:.4f}" if margin is not None else "N/A"
            action_specific_msg = f"**{title_emoji} 平倉成功**\n\n**平倉類型：**{side_closed_display}\n**數量：**{closed_qty}\n**本金：**`{margin_display} USDT`\n**本次已實現盈虧（已扣本金與手續費）：**`{pnl_display} USDT`"
            signal_info = operation_details.get("signal")
            if signal_info:
                action_specific_msg += f"\n**平倉信號：**{signal_info}"
            current_pos_status_for_discord = "🔄 無持倉"
            current_pos_pnl_msg = ""
        elif op_type == "open_success":
            side_opened_display = "多單" if operation_details.get("side_opened") == "long" else "空單"
            opened_qty = operation_details.get("qty", "N/A")
            entry_price_display = f"{operation_details.get('entry_price', 'N/A'):.2f}"
            action_specific_msg = f"**{title_emoji} 開倉成功**\n\n**開倉類型：**{side_opened_display}\n**數量：**{opened_qty}\n**進場價格：**`{entry_price_display} USDT`"
            signal_info = operation_details.get("signal")
            if signal_info:
                action_specific_msg += f"\n**開倉信號：**{signal_info}"
        elif op_type == "error":
            action_specific_msg = f"**{title_emoji} 錯誤**\n\n{core_message}\n{operation_details.get('details', '')}"
            signal_info = operation_details.get("signal")
            if signal_info:
                action_specific_msg += f"\n**相關信號：**{signal_info}"
        elif op_type == "status_update":
            action_specific_msg = f"**{title_emoji} 狀態更新**\n\n{core_message}"
    # 決定最終的持倉狀態顯示
    if not (operation_details and operation_details.get("type") == "close_success"):
        if api_key and secret_key:
            actual_pos_side, actual_pos_qty_str, _, _ = get_current_position_details(api_key, secret_key, SYMBOL, MARGIN_COIN)
            if actual_pos_side == "long":
                current_pos_status_for_discord = f"📈 多單 (數量: {actual_pos_qty_str})"
            elif actual_pos_side == "short":
                current_pos_status_for_discord = f"📉 空單 (數量: {actual_pos_qty_str})"
            else:
                current_pos_status_for_discord = "🔄 無持倉"
    # 構造 Discord Embed
    embed = discord.Embed(
        title=f"{title_emoji} {SYMBOL} 交易通知",
        description=action_specific_msg,
        color=embed_color
    )
    embed.add_field(name="🏆 勝率統計", value=win_rate_str, inline=True)
    embed.add_field(name="📊 目前持倉", value=current_pos_status_for_discord, inline=True)
    if current_pos_pnl_msg:
        embed.add_field(name="💰 未實現盈虧", value=f"`{current_pos_pnl_msg}`", inline=True)
    embed.add_field(name="🕒 時間", value=time.strftime('%Y-%m-%d %H:%M:%S'), inline=False)
    # 發送訊息
    data_payload = {"embeds": [embed.to_dict()]}
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=data_payload)
        response.raise_for_status()
    except Exception as e:
        print(f"Discord 發送失敗: {e}")

# 強制發送緩衝區中的所有消息，不管時間限制
def flush_discord_messages():
    # 由於 send_discord_message 已改為直接發送 Embed，此函數暫時不需要實現複雜的緩衝區處理
    # 如果未來需要緩衝多個 Embeds，需要重新設計此函數
    print("flush_discord_messages 函數被呼叫，但目前不執行任何操作 (Embeds 直接發送)")
    pass





# === 策略邏輯 === #
def fetch_ohlcv(api_key=None, secret_key=None): # 移除了未使用的 symbol 參數
    """獲取指定交易對的K線數據，並添加錯誤處理"""
    try:
        # 使用ccxt庫連接到Binance交易所
        exchange = ccxt.binance()
        # 獲取指定交易對的4小時K線數據，限制為最近100根
        # 這將確保我們總是獲取最新的市場數據
        ohlcv = exchange.fetch_ohlcv(TRADING_PAIR, timeframe=TIMEFRAME, limit=100) # 使用 TRADING_PAIR
        return np.array(ohlcv)
    except Exception as e:
        error_msg = f"獲取 {TRADING_PAIR} K線數據失敗: {e}"
        print(f"錯誤：{error_msg}")
        return None




def compute_indicators(df, rsi_len, atr_len, breakout_len, api_key=None, secret_key=None, symbol=None):
    """計算技術指標，並添加錯誤處理"""
    try:
        # 確保 talib 庫已安裝並導入
        try:
            import talib
        except ImportError:
            error_msg = "錯誤：TA-Lib 未正確安裝。請按照以下步驟操作：\n1. 確保虛擬環境已激活\n2. 檢查是否已安裝 TA-Lib C 函式庫\n3. 執行 'pip install TA_Lib‑*.whl' 安裝 Python 套件\n詳細安裝指引請參考 README.md"
            print(error_msg)
            return None # 返回 None 表示計算失敗

        df["rsi"] = talib.RSI(df["close"], timeperiod=rsi_len)
        df["atr"] = talib.ATR(df["high"], df["low"], df["close"], timeperiod=atr_len)
        # 使用 shift(1) 確保不包含當前 K 線的最高價
        df["highest_break"] = df["high"].shift(1).rolling(window=breakout_len).max()
        # 新增：空單指標
        df["lowest_break"] = df["low"].shift(1).rolling(window=breakout_len).min()
        return df
    except Exception as e:
        error_msg = f"計算指標失敗: {e}"
        print(f"錯誤：{error_msg}")
        return None # 返回 None 表示計算失敗

def calculate_trade_size(api_key, secret_key, symbol, wallet_percentage, leverage, current_price):
    available_balance = check_wallet_balance(api_key, secret_key)
    if available_balance is None or available_balance <= 0:
        print("錯誤：無法獲取錢包餘額或餘額不足")
        return 0

    # 只用 config 裡的 WALLET_PERCENTAGE，不再乘以 0.95
    trade_capital = available_balance * wallet_percentage
    contract_value = trade_capital * leverage
    if current_price > 0:
        quantity = contract_value / current_price
        quantity = round(quantity, QUANTITY_PRECISION)
        print(f"計算下單數量: 可用餘額={available_balance:.4f}, 使用比例={wallet_percentage}, 槓桿={leverage}, 合約價值={contract_value:.4f}, 當前價格={current_price:.2f}, 計算數量={quantity:.3f}")
        return quantity
    else:
        print("錯誤：當前價格無效")
        return 0

# === 交易策略核心邏輯 === #
def execute_trading_strategy(api_key, secret_key, symbol, margin_coin, wallet_percentage, leverage, rsi_buy_signal, breakout_lookback, atr_multiplier):
    global win_count, loss_count, current_pos_entry_type, current_stop_loss_price, current_position_id_global
    global last_checked_kline_time
    print(f"執行交易策略: {symbol}")

    try:
        ohlcv_data = fetch_ohlcv(api_key, secret_key)
        df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        # 新增：計算 RSI/ATR/突破等指標
        df = compute_indicators(df, RSI_LEN, ATR_LEN, BREAKOUT_LOOKBACK, api_key, secret_key, symbol)

        latest_kline_time = df['timestamp'].iloc[-1]
        latest_close = df['close'].iloc[-1]
        latest_rsi = df['rsi'].iloc[-1] if 'rsi' in df.columns else None
        latest_highest_break = df['highest_break'].iloc[-1] if 'highest_break' in df.columns and pd.notna(df['highest_break'].iloc[-1]) else None
        latest_atr = df['atr'].iloc[-1] if 'atr' in df.columns else None
        # 新增：空單指標
        lowest_break = df['lowest_break'].iloc[-1] if 'lowest_break' in df.columns and pd.notna(df['lowest_break'].iloc[-1]) else None

        # 新增：終端機輸出 RSI
        if latest_rsi is not None:
            print(f"RSI: {latest_rsi:.2f}")
        else:
            print("RSI: 無法取得")

        # 檢查當前持倉狀態
        current_pos_side, current_pos_qty_str, current_position_id, current_unrealized_pnl = get_current_position_details(api_key, secret_key, symbol, margin_coin)
        current_pos_qty = float(current_pos_qty_str) if current_pos_qty_str else 0.0

        # 只允許同時一張單
        if current_pos_side is None:
            # RSI 多單進場
            if latest_rsi is not None and latest_rsi < RSI_BUY:
                trade_size = calculate_trade_size(api_key, secret_key, symbol, wallet_percentage, leverage, latest_close)
                if trade_size > 0:
                    log_event("策略判斷", f"觸發RSI多單條件，RSI={latest_rsi:.2f} < {RSI_BUY}")
                    order_result = send_order(api_key, secret_key, symbol, margin_coin, "open_long", trade_size, leverage)
                    if order_result and order_result.get('code') == 0:
                        # 嘗試取得 positionId
                        new_position_id = order_result.get("data", {}).get("positionId")
                        if not new_position_id:
                            # 若沒有，則用 orderId 查詢
                            order_id = order_result.get("data", {}).get("orderId")
                            new_position_id = get_position_id_by_order_id(api_key, secret_key, symbol, order_id)
                        current_position_id_global = new_position_id
                        current_pos_entry_type = "rsi"
                        stop_loss = latest_close - latest_atr * STOP_MULT
                        take_profit = latest_close + latest_atr * LIMIT_MULT
                        if new_position_id:
                            place_conditional_orders(api_key, secret_key, symbol, margin_coin, new_position_id, stop_price=stop_loss, limit_price=take_profit)
                        else:
                            log_event("條件單設置失敗", f"無法取得 positionId，條件單未設置。orderId={order_id}")
                        current_stop_loss_price = stop_loss
                        log_event("開倉成功", f"多單 RSI, 數量={trade_size}, 價格={latest_close}, 止損={stop_loss}, 止盈={take_profit}")
                        send_discord_message("🟢 **RSI 多單開倉成功** 🟢", api_key, secret_key, operation_details={"type": "open_success", "side_opened": "long", "qty": trade_size, "entry_price": latest_close, "signal": "RSI", "force_send": True})
                    else:
                        log_event("開倉失敗", f"多單 RSI, 數量={trade_size}, 價格={latest_close}, 錯誤={order_result}")
                        send_discord_message("🔴 **RSI 多單開倉失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": order_result.get("msg", order_result.get("error", "未知錯誤")), "signal": "RSI", "force_send": True})
                else:
                    log_event("策略判斷", f"RSI多單條件成立但下單數量為0，RSI={latest_rsi:.2f}")
            # Breakout 多單進場
            elif latest_highest_break is not None and latest_close > latest_highest_break:
                trade_size = calculate_trade_size(api_key, secret_key, symbol, wallet_percentage, leverage, latest_close)
                if trade_size > 0:
                    log_event("策略判斷", f"觸發突破多單條件，close={latest_close} > highestBreak={latest_highest_break}")
                    order_result = send_order(api_key, secret_key, symbol, margin_coin, "open_long", trade_size, leverage)
                    if order_result and order_result.get('code') == 0:
                        new_position_id = order_result.get("data", {}).get("positionId")
                        current_position_id_global = new_position_id
                        current_pos_entry_type = "breakout"
                        current_stop_loss_price = latest_close - latest_atr * ATR_MULT
                        log_event("開倉成功", f"多單 Breakout, 數量={trade_size}, 價格={latest_close}, 初始移動止損={current_stop_loss_price}")
                        send_discord_message("🟢 **突破多單開倉成功** 🟢", api_key, secret_key, operation_details={"type": "open_success", "side_opened": "long", "qty": trade_size, "entry_price": latest_close, "signal": "Breakout", "force_send": True})
                    else:
                        log_event("開倉失敗", f"多單 Breakout, 數量={trade_size}, 價格={latest_close}, 錯誤={order_result}")
                        send_discord_message("🔴 **突破多單開倉失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": order_result.get("msg", order_result.get("error", "未知錯誤")), "signal": "Breakout", "force_send": True})
                else:
                    log_event("策略判斷", f"突破多單條件成立但下單數量為0，close={latest_close}")
            # RSI 空單進場
            elif latest_rsi is not None and latest_rsi > rsiSell:
                trade_size = calculate_trade_size(api_key, secret_key, symbol, wallet_percentage, leverage, latest_close)
                if trade_size > 0:
                    log_event("策略判斷", f"觸發RSI空單條件，RSI={latest_rsi:.2f} > {rsiSell}")
                    order_result = try_place_order_with_auto_reduce(api_key, secret_key, symbol, margin_coin, "open_short", trade_size, leverage)
                    if order_result and order_result.get('code') == 0:
                        # 嘗試取得 positionId
                        new_position_id = order_result.get("data", {}).get("positionId")
                        if not new_position_id:
                            order_id = order_result.get("data", {}).get("orderId")
                            new_position_id = get_position_id_by_order_id(api_key, secret_key, symbol, order_id)
                        if new_position_id:
                            current_position_id_global = new_position_id
                            current_pos_entry_type = "rsi_short"
                            stop_loss = latest_close + latest_atr * STOP_MULT
                            take_profit = latest_close - latest_atr * LIMIT_MULT
                            place_conditional_orders(api_key, secret_key, symbol, margin_coin, new_position_id, stop_price=stop_loss, limit_price=take_profit)
                            current_stop_loss_price = stop_loss
                            log_event("開倉成功", f"空單 RSI, 數量={trade_size}, 價格={latest_close}, 止損={stop_loss}, 止盈={take_profit}")
                            send_discord_message("🟢 **RSI 空單開倉成功** 🟢", api_key, secret_key, operation_details={"type": "open_success", "side_opened": "short", "qty": trade_size, "entry_price": latest_close, "signal": "RSI 空", "force_send": True})
                        else:
                            log_event("條件單設置失敗", f"無法取得 positionId，條件單未設置。orderId={order_id}")
                            send_discord_message("🔴 **RSI 空單開倉成功但無法取得 positionId，條件單設置失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": "無法取得 positionId，條件單未設置。", "signal": "RSI 空", "force_send": True})
                    else:
                        log_event("開倉失敗", f"空單 RSI, 數量={trade_size}, 價格={latest_close}, 錯誤={order_result}")
                        send_discord_message("🔴 **RSI 空單開倉失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": order_result.get("msg", order_result.get("error", "未知錯誤")), "signal": "RSI 空", "force_send": True})
                else:
                    log_event("策略判斷", f"RSI空單條件成立但下單數量為0，RSI={latest_rsi:.2f}")
            # Breakout 空單進場
            elif lowest_break is not None and latest_close < lowest_break:
                trade_size = calculate_trade_size(api_key, secret_key, symbol, wallet_percentage, leverage, latest_close)
                if trade_size > 0:
                    log_event("策略判斷", f"觸發突破空單條件，close={latest_close} < lowestBreak={lowest_break}")
                    order_result = try_place_order_with_auto_reduce(api_key, secret_key, symbol, margin_coin, "open_short", trade_size, leverage)
                    if order_result and order_result.get('code') == 0:
                        new_position_id = order_result.get("data", {}).get("positionId")
                        if not new_position_id:
                            order_id = order_result.get("data", {}).get("orderId")
                            new_position_id = get_position_id_by_order_id(api_key, secret_key, symbol, order_id)
                        if new_position_id:
                            current_position_id_global = new_position_id
                            current_pos_entry_type = "breakout_short"
                            current_stop_loss_price = latest_close + latest_atr * ATR_MULT
                            log_event("開倉成功", f"空單 Breakout, 數量={trade_size}, 價格={latest_close}, 初始移動止損={current_stop_loss_price}")
                            send_discord_message("🟢 **突破空單開倉成功** 🟢", api_key, secret_key, operation_details={"type": "open_success", "side_opened": "short", "qty": trade_size, "entry_price": latest_close, "signal": "Breakout 空", "force_send": True})
                        else:
                            log_event("條件單設置失敗", f"無法取得 positionId，條件單未設置。orderId={order_id}")
                            send_discord_message("🔴 **突破空單開倉成功但無法取得 positionId，條件單設置失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": "無法取得 positionId，條件單未設置。", "signal": "Breakout 空", "force_send": True})
                    else:
                        log_event("開倉失敗", f"空單 Breakout, 數量={trade_size}, 價格={latest_close}, 錯誤={order_result}")
                        send_discord_message("🔴 **突破空單開倉失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": order_result.get("msg", order_result.get("error", "未知錯誤")), "signal": "Breakout 空", "force_send": True})
                else:
                    log_event("策略判斷", f"突破空單條件成立但下單數量為0，close={latest_close}")
            else:
                log_event("策略判斷", f"無進場條件觸發，RSI={latest_rsi}, close={latest_close}")

        # RSI 多單平倉（只在新K棒結束時檢查）
        if current_pos_side == "long" and current_pos_entry_type == "rsi":
            if last_checked_kline_time is None or latest_kline_time != last_checked_kline_time:
                # 新K棒結束，檢查 RSI > EXIT_RSI
                if latest_rsi is not None and latest_rsi > EXIT_RSI:
                    if current_pos_qty > 0 and current_position_id:
                        balance_before_close = check_wallet_balance(api_key, secret_key)
                        # 查詢平倉前的本金（margin）
                        margin_before_close = None
                        try:
                            url = "https://fapi.bitunix.com/api/v1/futures/position/get_pending_positions"
                            params = {"symbol": symbol}
                            nonce = uuid.uuid4().hex
                            timestamp = str(int(time.time() * 1000))
                            sorted_items = sorted((k, str(v)) for k, v in params.items())
                            query_string = "".join(f"{k}{v}" for k, v in sorted_items)
                            digest_input = nonce + timestamp + api_key + query_string
                            digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
                            sign = hashlib.sha256((digest + secret_key).encode('utf-8')).hexdigest()
                            headers = {
                                "api-key": api_key,
                                "sign": sign,
                                "nonce": nonce,
                                "timestamp": timestamp,
                                "Content-Type": "application/json"
                            }
                            res = requests.get(url, headers=headers, params=params)
                            data = res.json()
                            if data.get("code") == 0 and data.get("data"):
                                for pos in data["data"]:
                                    if pos.get("positionId") == current_position_id:
                                        margin_before_close = float(pos.get("margin", 0))
                                        break
                        except Exception as e:
                            print(f"查詢平倉前本金失敗: {e}")
                        order_result = send_order(api_key, secret_key, symbol, margin_coin, "close_long", current_pos_qty, position_id=current_position_id)
                        if order_result and order_result.get('code') == 0:
                            # === 新增：直接查詢 Bitunix 歷史訂單的 profit 欄位 ===
                            order_info = query_last_closed_order(api_key, secret_key, symbol, current_position_id)
                            profit = None
                            if order_info:
                                profit = order_info.get('profit', None)
                            log_event("平倉成功", f"多單 RSI, 數量={current_pos_qty}, 價格={latest_close}, 本金={margin_before_close}, 實際盈虧={profit}")
                            send_discord_message(
                                "🟠 **RSI 多單平倉成功** 🟠",
                                api_key, secret_key,
                                operation_details={
                                    "type": "close_success",
                                    "side_closed": "long",
                                    "qty": current_pos_qty,
                                    "pnl": profit,
                                    "margin": margin_before_close,
                                    "force_send": True
                                }
                            )
                            if profit is not None:
                                if profit > 0:
                                    win_count += 1
                                else:
                                    loss_count += 1
                                save_stats()
                            current_pos_entry_type = None
                            current_stop_loss_price = None
                            current_position_id_global = None
                        else:
                            log_event("平倉失敗", f"多單 RSI, 數量={current_pos_qty}, 價格={latest_close}, 錯誤={order_result}")
                            send_discord_message("🔴 **RSI 多單平倉失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": order_result.get("msg", order_result.get("error", "未知錯誤")), "force_send": True})
                last_checked_kline_time = latest_kline_time
        # RSI 空單平倉（只在新K棒結束時檢查）
        if current_pos_side == "short" and current_pos_entry_type == "rsi_short":
            if last_checked_kline_time is None or latest_kline_time != last_checked_kline_time:
                # 新K棒結束，檢查 RSI < exitRSI_short
                if latest_rsi is not None and latest_rsi < exitRSI_short:
                    if current_pos_qty > 0 and current_position_id:
                        balance_before_close = check_wallet_balance(api_key, secret_key)
                        # 查詢平倉前的本金（margin）
                        margin_before_close = None
                        try:
                            url = "https://fapi.bitunix.com/api/v1/futures/position/get_pending_positions"
                            params = {"symbol": symbol}
                            nonce = uuid.uuid4().hex
                            timestamp = str(int(time.time() * 1000))
                            sorted_items = sorted((k, str(v)) for k, v in params.items())
                            query_string = "".join(f"{k}{v}" for k, v in sorted_items)
                            digest_input = nonce + timestamp + api_key + query_string
                            digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
                            sign = hashlib.sha256((digest + secret_key).encode('utf-8')).hexdigest()
                            headers = {
                                "api-key": api_key,
                                "sign": sign,
                                "nonce": nonce,
                                "timestamp": timestamp,
                                "Content-Type": "application/json"
                            }
                            res = requests.get(url, headers=headers, params=params)
                            data = res.json()
                            if data.get("code") == 0 and data.get("data"):
                                for pos in data["data"]:
                                    if pos.get("positionId") == current_position_id:
                                        margin_before_close = float(pos.get("margin", 0))
                                        break
                        except Exception as e:
                            print(f"查詢平倉前本金失敗: {e}")
                        order_result = send_order(api_key, secret_key, symbol, margin_coin, "close_short", current_pos_qty, position_id=current_position_id)
                        if order_result and order_result.get('code') == 0:
                            # === 新增：直接查詢 Bitunix 歷史訂單的 profit 欄位 ===
                            order_info = query_last_closed_order(api_key, secret_key, symbol, current_position_id)
                            profit = None
                            if order_info:
                                profit = order_info.get('profit', None)
                            log_event("平倉成功", f"空單 RSI, 數量={current_pos_qty}, 價格={latest_close}, 本金={margin_before_close}, 實際盈虧={profit}")
                            send_discord_message(
                                "🟠 **RSI 空單平倉成功** 🟠",
                                api_key, secret_key,
                                operation_details={
                                    "type": "close_success",
                                    "side_closed": "short",
                                    "qty": current_pos_qty,
                                    "pnl": profit,
                                    "margin": margin_before_close,
                                    "force_send": True
                                }
                            )
                            if profit is not None:
                                if profit > 0:
                                    win_count += 1
                                else:
                                    loss_count += 1
                                save_stats()
                            current_pos_entry_type = None
                            current_stop_loss_price = None
                            current_position_id_global = None
                        else:
                            log_event("平倉失敗", f"空單 RSI, 數量={current_pos_qty}, 價格={latest_close}, 錯誤={order_result}")
                            send_discord_message("🔴 **RSI 空單平倉失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": order_result.get("msg", order_result.get("error", "未知錯誤")), "force_send": True})
                last_checked_kline_time = latest_kline_time
        # Breakout 多單移動止損（每次循環都檢查）
        if current_pos_side == "long" and current_pos_entry_type == "breakout" and current_position_id_global:
            new_trailing_stop = latest_close - latest_atr * ATR_MULT
            if current_stop_loss_price is not None and new_trailing_stop > current_stop_loss_price:
                modify_result = modify_position_tpsl(api_key, secret_key, symbol, current_position_id_global, stop_price=new_trailing_stop)
                if modify_result and modify_result.get('code') == 0:
                    log_event("移動止損調整", f"多單 Breakout, positionId={current_position_id_global}, 新止損={new_trailing_stop}")
                    current_stop_loss_price = new_trailing_stop
                    send_discord_message(f"⬆️ **突破多單移動止損上調** ⬆️ 新止損: {new_trailing_stop:.4f}", api_key, secret_key, operation_details={"type": "status_update", "details": f"新止損: {new_trailing_stop:.4f}", "force_send": True})
                else:
                    log_event("移動止損失敗", f"多單 Breakout, positionId={current_position_id_global}, 嘗試新止損={new_trailing_stop}, 錯誤={modify_result}")
                    send_discord_message(f"🔴 **突破多單移動止損調整失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": modify_result.get("msg", modify_result.get("error", "未知錯誤")), "force_send": True})
        # Breakout 空單移動止損（每次循環都檢查）
        if current_pos_side == "short" and current_pos_entry_type == "breakout_short" and current_position_id_global:
            new_trailing_stop = latest_close + latest_atr * ATR_MULT
            if current_stop_loss_price is not None and new_trailing_stop < current_stop_loss_price:
                modify_result = modify_position_tpsl(api_key, secret_key, symbol, current_position_id_global, stop_price=new_trailing_stop)
                if modify_result and modify_result.get('code') == 0:
                    log_event("移動止損調整", f"空單 Breakout, positionId={current_position_id_global}, 新止損={new_trailing_stop}")
                    current_stop_loss_price = new_trailing_stop
                    send_discord_message(f"⬇️ **突破空單移動止損下調** ⬇️ 新止損: {new_trailing_stop:.4f}", api_key, secret_key, operation_details={"type": "status_update", "details": f"新止損: {new_trailing_stop:.4f}", "force_send": True})
                else:
                    log_event("移動止損失敗", f"空單 Breakout, positionId={current_position_id_global}, 嘗試新止損={new_trailing_stop}, 錯誤={modify_result}")
                    send_discord_message(f"🔴 **突破空單移動止損調整失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": modify_result.get("msg", modify_result.get("error", "未知錯誤")), "force_send": True})

        # RSI 多單動態止盈止損自動更新（先查詢、取消、再設置）
        if current_pos_side == "long" and current_pos_entry_type == "rsi" and current_position_id_global:
            new_stop_loss = latest_close - latest_atr * STOP_MULT
            new_take_profit = latest_close + latest_atr * LIMIT_MULT
            # 僅當止損或止盈價格有變動才更新
            if (current_stop_loss_price is None or abs(new_stop_loss - current_stop_loss_price) > 1e-6):
                tpsl_order_ids = get_pending_tpsl_orders(api_key, secret_key, symbol, current_position_id_global)
                for oid in tpsl_order_ids:
                    cancel_tpsl_order(api_key, secret_key, symbol, oid)
                place_result = place_conditional_orders(api_key, secret_key, symbol, margin_coin, current_position_id_global, stop_price=new_stop_loss, limit_price=new_take_profit)
                if place_result and place_result.get('code') == 0:
                    log_event("RSI多單動態止損/止盈調整", f"多單 RSI, positionId={current_position_id_global}, 新止損={new_stop_loss}, 新止盈={new_take_profit}")
                    current_stop_loss_price = new_stop_loss
                else:
                    log_event("RSI多單動態止損/止盈調整失敗", f"多單 RSI, positionId={current_position_id_global}, 嘗試新止損={new_stop_loss}, 新止盈={new_take_profit}, 錯誤={place_result}")

        # RSI 空單動態止盈止損自動更新（先查詢、取消、再設置）
        if current_pos_side == "short" and current_pos_entry_type == "rsi_short" and current_position_id_global:
            new_stop_loss = latest_close + latest_atr * STOP_MULT
            new_take_profit = latest_close - latest_atr * LIMIT_MULT
            if (current_stop_loss_price is None or abs(new_stop_loss - current_stop_loss_price) > 1e-6):
                tpsl_order_ids = get_pending_tpsl_orders(api_key, secret_key, symbol, current_position_id_global)
                for oid in tpsl_order_ids:
                    cancel_tpsl_order(api_key, secret_key, symbol, oid)
                place_result = place_conditional_orders(api_key, secret_key, symbol, margin_coin, current_position_id_global, stop_price=new_stop_loss, limit_price=new_take_profit)
                if place_result and place_result.get('code') == 0:
                    log_event("RSI空單動態止損/止盈調整", f"空單 RSI, positionId={current_position_id_global}, 新止損={new_stop_loss}, 新止盈={new_take_profit}")
                    current_stop_loss_price = new_stop_loss
                else:
                    log_event("RSI空單動態止損/止盈調整失敗", f"空單 RSI, positionId={current_position_id_global}, 嘗試新止損={new_stop_loss}, 新止盈={new_take_profit}, 錯誤={place_result}")

    except Exception as e:
        error_msg = f"執行交易策略時發生未知錯誤: {e}"
        print(f"錯誤：{error_msg}")
        log_event("策略錯誤", error_msg)

# === 查詢錢包餘額 === #
def check_wallet_balance(api_key, secret_key):
    query_params = {"marginCoin": MARGIN_COIN}
    path = "/api/v1/futures/account"
    url = f"https://fapi.bitunix.com{path}?marginCoin={MARGIN_COIN}"
    _, _, _, headers = get_signed_params(api_key, secret_key, query_params, method="GET")
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        print(f"Response from API: {response.text}")
        balance_info = response.json()
        current_balance = None
        if "data" in balance_info and balance_info["data"] is not None:
            print(f"完整的數據結構: {balance_info['data']}")
            if isinstance(balance_info["data"], dict):
                account_data = balance_info["data"]
                available_balance = float(account_data.get("available", 0))
                margin_balance = float(account_data.get("margin", 0))
                cross_unrealized_pnl = float(account_data.get("crossUnrealizedPNL", 0))
                isolation_unrealized_pnl = float(account_data.get("isolationUnrealizedPNL", 0))
                total_unrealized_pnl = cross_unrealized_pnl + isolation_unrealized_pnl
                total_asset = available_balance + margin_balance + total_unrealized_pnl
                print(f"已獲取並發送餘額信息: 可用 {available_balance}, 保證金 {margin_balance}, 未實現盈虧 {total_unrealized_pnl}, 總資產 {total_asset}")
                current_wallet_balance = available_balance
                return available_balance
            else:
                error_message = "餘額數據格式不正確"
                print(f"餘額查詢錯誤: {error_message}, 原始數據: {balance_info['data']}")
                return current_wallet_balance
        else:
            error_message = balance_info.get("message", "無法獲取餘額信息")
            return current_wallet_balance
    except requests.exceptions.HTTPError as err:
        print(f"HTTP Error: {err}")
        return current_wallet_balance
    except requests.exceptions.RequestException as err:
        print(f"Request Exception: {err}")
        return current_wallet_balance
    except Exception as e:
        error_msg = f"執行交易策略時發生未知錯誤: {e}"
        print(f"錯誤：{error_msg}")

# === 查詢持倉狀態 === #
def get_current_position_details(api_key, secret_key, symbol, margin_coin=MARGIN_COIN): # 使用 MARGIN_COIN from config as default
    """查詢目前持倉的詳細信息，包括方向、數量、positionId 和未實現盈虧。"""
    import hashlib, uuid, time, requests

    url = "https://fapi.bitunix.com/api/v1/futures/position/get_pending_positions"
    params = {"symbol": symbol}
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    
    sorted_items = sorted((k, str(v)) for k, v in params.items())
    query_string = "".join(f"{k}{v}" for k, v in sorted_items)

    digest_input = nonce + timestamp + api_key + query_string
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    sign = hashlib.sha256((digest + secret_key).encode('utf-8')).hexdigest()

    headers = {
        "api-key": api_key,
        "sign": sign,
        "nonce": nonce,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }
    try:
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        if data.get("code") == 0 and data.get("data"):
            for pos_detail in data["data"]:
                pos_qty_str = pos_detail.get("qty", "0")
                position_id = pos_detail.get("positionId")
                unrealized_pnl = float(pos_detail.get("unrealizedPNL", 0.0)) # 獲取未實現盈虧
                
                if float(pos_qty_str) > 0: # 只處理有實際數量的倉位
                    if pos_detail.get("side") == "BUY":
                        print(f"API偵測到多單持倉: qty={pos_qty_str}, positionId={position_id}, PNL={unrealized_pnl}")
                        return "long", pos_qty_str, position_id, unrealized_pnl
                    if pos_detail.get("side") == "SELL":
                        print(f"API偵測到空單持倉: qty={pos_qty_str}, positionId={position_id}, PNL={unrealized_pnl}")
                        return "short", pos_qty_str, position_id, unrealized_pnl
        # print("API未偵測到有效持倉或回傳數據格式問題。") # 可以根據需要取消註釋
        return None, None, None, 0.0  # 無持倉或錯誤，PNL返回0.0
    except Exception as e:
        print(f"查詢持倉詳細失敗: {e}")
        return None, None, None, 0.0

def get_recent_closed_orders(api_key, secret_key, symbol, page_size=10):
    url = "https://fapi.bitunix.com/api/v1/futures/order/history"
    params = {"symbol": symbol, "pageSize": page_size}
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    sorted_items = sorted((k, str(v)) for k, v in params.items())
    query_string = "".join(f"{k}{v}" for k, v in sorted_items)
    digest_input = nonce + timestamp + api_key + query_string
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    sign = hashlib.sha256((digest + secret_key).encode('utf-8')).hexdigest()
    headers = {
        "api-key": api_key,
        "sign": sign,
        "nonce": nonce,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }
    try:
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        if data.get("code") == 0 and data.get("data"):
            return data["data"]
    except Exception as e:
        print(f"查詢歷史訂單失敗: {e}")
    return []
# === 新增：查詢最近平倉訂單的輔助函數 ===
def query_last_closed_order(api_key, secret_key, symbol, prev_pos_id):
    """
    查詢最近的平倉訂單，並判斷是TP還是SL
    """
    url = "https://fapi.bitunix.com/api/v1/futures/order/history"
    params = {"symbol": symbol, "pageSize": 5}
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    sorted_items = sorted((k, str(v)) for k, v in params.items())
    query_string = "".join(f"{k}{v}" for k, v in sorted_items)
    digest_input = nonce + timestamp + api_key + query_string
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    sign = hashlib.sha256((digest + secret_key).encode('utf-8')).hexdigest()
    headers = {
        "api-key": api_key,
        "sign": sign,
        "nonce": nonce,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }
    try:
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        if data.get("code") == 0 and data.get("data"):
            for order in data["data"]:
                # 根據 positionId 或其他欄位比對
                if str(order.get("positionId")) == str(prev_pos_id) and order.get("status") == "FILLED":
                    trigger_type = order.get("triggerType", "")
                    close_price = order.get("avgPrice", order.get("price", ""))
                    profit = order.get("profit", 0)
                    return {"trigger_type": trigger_type, "close_price": close_price, "profit": profit}
    except Exception as e:
        print(f"查詢歷史訂單失敗: {e}")
    return None

def send_profit_loss_to_discord(api_key, secret_key, symbol_param, message): # Renamed symbol to symbol_param
    position = get_current_position(api_key, secret_key, symbol_param)
    if position in ['long', 'short']:
        url = "https://fapi.bitunix.com/api/v1/futures/position/get_pending_positions"
        params = {"symbol": symbol_param} # Use symbol_param
        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time() * 1000))
        digest_input = nonce + timestamp + api_key + "symbol" + symbol_param # Use symbol_param
        digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
        sign = hashlib.sha256((digest + secret_key).encode('utf-8')).hexdigest()
        headers = {
            "api-key": api_key,
            "sign": sign,
            "nonce": nonce,
            "timestamp": timestamp,
            "Content-Type": "application/json"
        }
        try:
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            if data.get("code") == 0 and data.get("data"):
                for pos in data["data"]:
                    if ((position == "long" and pos.get("side") == "BUY") or
                        (position == "short" and pos.get("side") == "SELL")):
                        pnl = float(pos.get("unrealizedPNL", 0))
                        margin = float(pos.get("margin", 0))
                        if margin:
                            profit_pct = (pnl / margin) * 100
                            message += f"\n💰 盈虧: {pnl:.4f} USDT｜收益率: {profit_pct:.2f}%"
                        else:
                            message += f"\n💰 盈虧: {pnl:.4f} USDT"
        except Exception as e:
            message += f"\n查詢盈虧失敗: {e}"
    
    # 根據需求，移除持倉和盈虧更新的 Discord 通知
    pass

def get_pending_tpsl_orders(api_key, secret_key, symbol, position_id):
    """
    查詢目前持倉的 TP/SL 單，回傳 orderId list。
    參考官方文件：https://openapidoc.bitunix.com/doc/tp_sl/cancel_tp_sl_order.html
    """
    url = "https://fapi.bitunix.com/api/v1/futures/tpsl/get_pending_tp_sl_order"
    params = {"symbol": symbol}
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    sorted_items = sorted((k, str(v)) for k, v in params.items())
    query_string = "".join(f"{k}{v}" for k, v in sorted_items)
    digest_input = nonce + timestamp + api_key + query_string
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    sign = hashlib.sha256((digest + secret_key).encode('utf-8')).hexdigest()
    headers = {
        "api-key": api_key,
        "sign": sign,
        "nonce": nonce,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }
    try:
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        order_ids = []
        if data.get("code") == 0 and data.get("data"):
            for order in data["data"]:
                # 只抓對應 positionId 的 TP/SL 單
                if str(order.get("positionId")) == str(position_id):
                    if order.get("orderId"):
                        order_ids.append(order["orderId"])
        return order_ids
    except Exception as e:
        print(f"查詢 TP/SL 單失敗: {e}")
        return []

def cancel_tpsl_order(api_key, secret_key, symbol, order_id):
    """
    取消指定 TP/SL 單。
    參考官方文件：https://openapidoc.bitunix.com/doc/tp_sl/cancel_tp_sl_order.html
    """
    url = "https://fapi.bitunix.com/api/v1/futures/tpsl/cancel_order"
    body = {"symbol": symbol, "orderId": order_id}
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(',', ':'), ensure_ascii=False)
    digest_input = nonce + timestamp + api_key + body_str
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    sign = hashlib.sha256((digest + secret_key).encode('utf-8')).hexdigest()
    headers = {
        "api-key": api_key,
        "sign": sign,
        "nonce": nonce,
        "timestamp": timestamp,
        "Content-Type": "application/json"
    }
    try:
        res = requests.post(url, headers=headers, data=body_str)
        data = res.json()
        if data.get("code") == 0:
            print(f"成功取消 TP/SL 單: {order_id}")
            return True
        else:
            print(f"取消 TP/SL 單失敗: {data}")
            return False
    except Exception as e:
        print(f"取消 TP/SL 單失敗: {e}")
        return False

def main():
    global win_count, loss_count
    global current_pos_entry_type, current_stop_loss_price, current_position_id_global, last_checked_kline_time
    load_stats() # 啟動時載入統計數據
    order_points = []  # 新增：初始化 order_points 以避免 NameError

    # 用戶參數
    from config import TRADING_PAIR, SYMBOL, MARGIN_COIN, LEVERAGE, WALLET_PERCENTAGE, RSI_LEN, ATR_LEN, BREAKOUT_LOOKBACK, STOP_MULT, LIMIT_MULT, RSI_BUY, EXIT_RSI, ATR_MULT, TIMEFRAME
    api_key = BITUNIX_API_KEY # 從 config 導入
    secret_key = BITUNIX_SECRET_KEY # 從 config 導入
    # trading_pair 變數不再需要在 main 中單獨定義，直接使用導入的 TRADING_PAIR 或 SYMBOL
    symbol = SYMBOL # SYMBOL 已經從 config 導入
    margin_coin = MARGIN_COIN # 從 config 導入
    leverage = LEVERAGE
    wallet_percentage = WALLET_PERCENTAGE

    current_pos_side = None
    current_pos_qty = None
    # win_count 和 loss_count 由 load_stats() 初始化，此處無需重置為0
    # win_count = 0
    # loss_count = 0
    last_upper_band = None
    last_lower_band = None
    last_middle_band = None
    
    print("交易機器人啟動，開始載入初始K線數據並準備生成啟動圖表...")
    # 原啟動訊息已移除，將由包含圖表的訊息替代

    # 獲取初始K線數據用於繪圖
    ohlcv_data = fetch_ohlcv(api_key, secret_key)

    # 新增：查詢目前錢包餘額
    balance = check_wallet_balance(api_key, secret_key)

    min_data_len = max(RSI_LEN, ATR_LEN, BREAKOUT_LOOKBACK + 1) + 5 # +1 for shift, +5 for buffer
    if ohlcv_data is None or len(ohlcv_data) < min_data_len:
        error_detail_msg = f"需要至少 {min_data_len} 條數據，實際獲取 {len(ohlcv_data) if ohlcv_data is not None else 0} 條。"
        send_discord_message(f"🔴 啟動失敗：無法獲取足夠的初始K線數據繪製圖表。{error_detail_msg}", api_key, secret_key, operation_details={"type": "error", "details": f"Insufficient initial K-line data for chart. {error_detail_msg}", "force_send": True})
        return

    df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    # 可選擇截取最近一部分數據進行繪圖，避免過長的歷史數據影響圖表可讀性
    # df = df.iloc[-min_data_len*2:] 

    df_for_plot = compute_indicators(df.copy(), RSI_LEN, ATR_LEN, BREAKOUT_LOOKBACK, api_key, secret_key, symbol)
    if df_for_plot is None or df_for_plot.empty:
        send_discord_message("🔴 啟動失敗：計算初始指標失敗，無法繪製圖表。", api_key, secret_key, operation_details={"type": "error", "details": "Failed to compute initial indicators for chart", "force_send": True})
        return

    if df_for_plot['rsi'].isnull().all() or df_for_plot['atr'].isnull().all():
        send_discord_message("🔴 啟動失敗：計算出的初始指標包含過多無效值 (NaN)，無法繪製圖表。", api_key, secret_key, operation_details={"type": "error", "details": "Computed initial indicators are mostly NaN, cannot plot chart.", "force_send": True})
        return

    latest_close = df_for_plot['close'].iloc[-1]
    latest_rsi = df_for_plot['rsi'].iloc[-1]
    latest_highest_break = df_for_plot['highest_break'].iloc[-1] if 'highest_break' in df_for_plot.columns and pd.notna(df_for_plot['highest_break'].iloc[-1]) else None
    latest_atr = df_for_plot['atr'].iloc[-1]
    
    if pd.isna(latest_close) or pd.isna(latest_rsi) or pd.isna(latest_atr):
        send_discord_message("🔴 啟動失敗：獲取的最新指標數據包含無效值 (NaN)，無法繪製圖表。", api_key, secret_key, operation_details={"type": "error", "details": "Latest indicator data contains NaN, cannot plot chart.", "force_send": True})
        return

    print(f"[Main Startup] 準備繪製啟動圖表... 最新收盤價: {latest_close:.2f}, RSI: {latest_rsi:.2f}, ATR: {latest_atr:.4f}")
    # 使用 df_for_plot 進行繪圖
    send_discord_message(f"🚀 交易機器人啟動 🚀\n策略參數:\nSTOP_MULT: {STOP_MULT}\nLIMIT_MULT: {LIMIT_MULT}\nRSI_BUY: {RSI_BUY}\nRSI_LEN: {RSI_LEN}\nEXIT_RSI: {EXIT_RSI}\nrsiSell: {rsiSell}（空單進場RSI）\nexitRSI_short: {exitRSI_short}（空單平倉RSI）\nBREAKOUT_LOOKBACK: {BREAKOUT_LOOKBACK}\nATR_LEN: {ATR_LEN}\nATR_MULT: {ATR_MULT}\nTIMEFRAME: {TIMEFRAME}\nWALLET_PERCENTAGE: {wallet_percentage}（每次下單佔錢包比例）\nLOOP_INTERVAL_SECONDS: {LOOP_INTERVAL_SECONDS}（主循環間隔秒數）\n**目前錢包餘額: {balance:.2f} USDT**\n\n**最新 RSI: {latest_rsi:.2f}**", api_key, secret_key, operation_details={"type": "custom_message", "force_send": True})

    last_kline_len = len(ohlcv_data)

    # 在主循環開始前，獲取一次當前持倉狀態 (返回四個值)
    current_pos_side, current_pos_qty_str, current_pos_id, current_unrealized_pnl = get_current_position_details(api_key, secret_key, SYMBOL, MARGIN_COIN)
    print(f"啟動時持倉狀態: side={current_pos_side}, qty={current_pos_qty_str}, positionId={current_pos_id}, PNL={current_unrealized_pnl}")
    
    # 啟動時自動補上現有持倉點 (這部分邏輯如果存在，需要確保 order_points 的更新)
    import numpy as np
    from typing import Any
    def get_entry_price_and_side(api_key: str, secret_key: str, symbol: str) -> Any:
        url = "https://fapi.bitunix.com/api/v1/futures/position/get_pending_positions"
        params = {"symbol": symbol}
        nonce = uuid.uuid4().hex
        timestamp = str(int(time.time() * 1000))
        api_key_ = api_key
        secret_key_ = secret_key
        sorted_items = sorted((k, str(v)) for k, v in params.items())
        query_string = "".join(f"{k}{v}" for k, v in sorted_items)
        digest_input = nonce + timestamp + api_key_ + query_string
        digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
        sign = hashlib.sha256((digest + secret_key_).encode('utf-8')).hexdigest()
        headers = {
            "api-key": api_key_,
            "sign": sign,
            "nonce": nonce,
            "timestamp": timestamp,
            "Content-Type": "application/json"
        }
        try:
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            if data.get("code") == 0 and data.get("data"):
                for pos in data["data"]:
                    side = None
                    if pos.get("side") == "BUY" and float(pos.get("qty", 0)) > 0:
                        side = "long"
                    elif pos.get("side") == "SELL" and float(pos.get("qty", 0)) > 0:
                        side = "short"
                    if side:
                        entry_price = float(pos.get("avgOpenPrice", pos.get("entryValue", 0)))
                        return entry_price, side
            return None
        except Exception as e:
            print(f"查詢持倉失敗: {e}")
            return None

    entry = get_entry_price_and_side(api_key, secret_key, symbol)
    if entry:
        entry_price, side = entry
        # 使用 df_for_plot 中的 'close' 數據
        close_prices = df_for_plot['close'].values
        idx = int(np.argmin(np.abs(close_prices - entry_price)))
        order_points.append({'idx': idx, 'price': close_prices[idx], 'side': side})
        print(f"DEBUG: 啟動自動補標註現有持倉點: {order_points[-1]}")

    global last_checked_kline_time
    last_checked_kline_time = df['timestamp'].iloc[-1]

    # === 新增：持倉消失自動偵測與條件單觸發通知 ===
    last_cycle_pos_side = None
    last_cycle_pos_id = None
    last_cycle_entry_price = None
    
    # === 冷啟動自動補發未通知的條件單平倉通知 ===
    notified_ids = load_notified_order_ids()
    current_pos_side, current_pos_qty_str, current_pos_id, _ = get_current_position_details(api_key, secret_key, SYMBOL, MARGIN_COIN)
    recent_orders = get_recent_closed_orders(api_key, secret_key, SYMBOL, page_size=10)
    new_notify = False
    if current_pos_side is None:
        for order in recent_orders:
            order_id = str(order.get("orderId"))
            if order_id not in notified_ids and order.get("status") == "FILLED":
                trigger_type = order.get("triggerType", "")
                close_price = order.get("avgPrice", order.get("price", ""))
                profit = order.get("profit", 0)
                pos_side = "long" if order.get("side") == "BUY" else "short"
                if trigger_type == "TAKE_PROFIT":
                    msg = f"🟢 **止盈觸發自動平倉**\n平倉價格：{close_price}\n本次盈虧：{profit} USDT"
                    log_event("止盈觸發(冷啟動)", msg)
                    send_discord_message(msg, api_key, secret_key, operation_details={"type": "close_success", "side_closed": pos_side, "pnl": profit, "force_send": True})
                elif trigger_type == "STOP_LOSS":
                    msg = f"🔴 **止損觸發自動平倉**\n平倉價格：{close_price}\n本次盈虧：{profit} USDT"
                    log_event("止損觸發(冷啟動)", msg)
                    send_discord_message(msg, api_key, secret_key, operation_details={"type": "close_success", "side_closed": pos_side, "pnl": profit, "force_send": True})
                else:
                    msg = f"⚠️ **自動平倉（未知觸發類型）**\n平倉價格：{close_price}\n本次盈虧：{profit} USDT"
                    log_event("自動平倉(冷啟動)", msg)
                    send_discord_message(msg, api_key, secret_key, operation_details={"type": "close_success", "side_closed": pos_side, "pnl": profit, "force_send": True})
                notified_ids.append(order_id)
                new_notify = True
    if new_notify:
        save_notified_order_ids(notified_ids)

    # === 冷啟動自動還原全域狀態 ===
    current_position_id_global = current_pos_id
    # 取得最新K線與ATR
    ohlcv_data = fetch_ohlcv(api_key, secret_key)
    df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    last_checked_kline_time = df['timestamp'].iloc[-1]
    latest_close = df['close'].iloc[-1]
    # 重新計算ATR
    df_ind = compute_indicators(df, RSI_LEN, ATR_LEN, BREAKOUT_LOOKBACK, api_key, secret_key, symbol)
    latest_atr = df_ind['atr'].iloc[-1] if 'atr' in df_ind.columns else 0
    # 根據持倉自動還原狀態
    if current_pos_side == "long":
        # 預設為 rsi 進場（如需更精細可根據進場價格與策略判斷）
        current_pos_entry_type = "rsi"
        current_stop_loss_price = latest_close - latest_atr * STOP_MULT
    elif current_pos_side == "short":
        current_pos_entry_type = "rsi_short"
        current_stop_loss_price = latest_close + latest_atr * STOP_MULT
    else:
        current_pos_entry_type = None
        current_stop_loss_price = None
        current_position_id_global = None

    while True:
        # 檢查錢包餘額並獲取當前餘額
        balance = check_wallet_balance(api_key, secret_key)
        # 計算下單數量 (錢包餘額的30%*槓桿/當前BTC價格)
        btc_price = None
        # 執行交易策略
        execute_trading_strategy(api_key, secret_key, symbol, margin_coin, wallet_percentage, leverage, RSI_BUY, BREAKOUT_LOOKBACK, ATR_MULT)

        # === 新增：持倉消失自動偵測 ===
        prev_pos_side = last_cycle_pos_side
        prev_pos_id = last_cycle_pos_id
        prev_entry_price = last_cycle_entry_price
        # 查詢本輪持倉
        current_pos_side, current_pos_qty_str, current_pos_id, _ = get_current_position_details(api_key, secret_key, SYMBOL, MARGIN_COIN)
        # 檢查上一輪有持倉，這一輪沒持倉
        if prev_pos_side in ["long", "short"] and current_pos_side is None and prev_pos_id:
            result = query_last_closed_order(api_key, secret_key, SYMBOL, prev_pos_id)
            if result:
                trigger_type, close_price, profit = result["trigger_type"], result["close_price"], result["profit"]
                if trigger_type == "TAKE_PROFIT":
                    msg = f"🟢 **止盈觸發自動平倉**\n平倉價格：{close_price}\n本次盈虧：{profit} USDT"
                    log_event("止盈觸發", msg)
                    send_discord_message(msg, api_key, secret_key, operation_details={"type": "close_success", "side_closed": prev_pos_side, "pnl": profit, "force_send": True})
                elif trigger_type == "STOP_LOSS":
                    msg = f"🔴 **止損觸發自動平倉**\n平倉價格：{close_price}\n本次盈虧：{profit} USDT"
                    log_event("止損觸發", msg)
                    send_discord_message(msg, api_key, secret_key, operation_details={"type": "close_success", "side_closed": prev_pos_side, "pnl": profit, "force_send": True})
                else:
                    msg = f"⚠️ **自動平倉（未知觸發類型）**\n平倉價格：{close_price}\n本次盈虧：{profit} USDT"
                    log_event("自動平倉", msg)
                    send_discord_message(msg, api_key, secret_key, operation_details={"type": "close_success", "side_closed": prev_pos_side, "pnl": profit, "force_send": True})
        # 更新本輪持倉狀態
        last_cycle_pos_side = current_pos_side
        last_cycle_pos_id = current_pos_id
        last_cycle_entry_price = None  # 如需可查詢 entry price

        # 計算下單數量 (錢包餘額的30%*槓桿/當前BTC價格)
        btc_price = None
        # 檢查錢包餘額並獲取當前餘額 (用於下一次循環的數量計算)
        balance = check_wallet_balance(api_key, secret_key)
        if balance is None or balance <= 0:
            print("餘額為0或無法獲取餘額，退出程序")
            send_discord_message("🛑 **程序終止**: 餘額為0或無法獲取餘額，交易機器人已停止運行 🛑", SYMBOL, api_key, secret_key)
            # 在退出前強制發送所有緩衝區中的消息
            flush_discord_messages()
            print("程序已終止運行")
            return # 直接退出main函數而不是繼續循環

        # 休眠1分鐘後再次執行策略
        # 休眠指定時間後再次執行策略
        next_strategy_time = time.strftime('%H:%M:%S', time.localtime(time.time() + LOOP_INTERVAL_SECONDS))
        print(f"休眠中，將在 {next_strategy_time} 再次執行交易策略 (間隔 {LOOP_INTERVAL_SECONDS} 秒)...")
        # 在休眠前強制發送所有緩衝區中的消息
        flush_discord_messages()
        time.sleep(LOOP_INTERVAL_SECONDS) # 休眠1分鐘  # 每 1 分鐘檢查一次


if __name__ == "__main__":
    try:
        main()
    finally:
        # 確保程序結束時發送所有緩衝區中的消息
        flush_discord_messages()
