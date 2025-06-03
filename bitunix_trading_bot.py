import ccxt
import numpy as np
import requests
import hashlib
import uuid
import time
import json
import random
import discord
import asyncio
from discord.ext import tasks
import os
import pandas as pd
from discord.ext import commands
from config import BITUNIX_API_KEY, BITUNIX_SECRET_KEY, DISCORD_WEBHOOK_URL, STOP_MULT, LIMIT_MULT, RSI_BUY, RSI_LEN, EXIT_RSI, BREAKOUT_LOOKBACK, ATR_LEN, ATR_MULT, TIMEFRAME, LEVERAGE, TRADING_PAIR, SYMBOL, MARGIN_COIN, LOOP_INTERVAL_SECONDS, QUANTITY_PRECISION
from config import rsiSell, exitRSI_short, CONDITIONAL_ORDER_MAX_RETRIES, CONDITIONAL_ORDER_RETRY_INTERVAL
import threading
import re
import logging
import sys
from config import DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID
import traceback

# 設定 logging，寫入 log.txt
logging.basicConfig(
    filename='log.txt',
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

# 全域未捕捉異常也寫入日誌
def log_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = log_uncaught_exception

# === 全域變數與統計檔案設定 ===
STATS_FILE = os.path.join(os.path.dirname(__file__), "stats.json")
win_count = 0
loss_count = 0

# === 移動止損相關全域變數 ===
current_pos_entry_type = None # 記錄持倉的進場信號類型 ('rsi' 或 'breakout')
current_stop_loss_price = None # 記錄當前持倉的止損價格
current_position_id_global = None # 記錄當前持倉的 positionId
last_checked_kline_time = None  # 新增：記錄上一次檢查的K棒時間
# === 新增：記錄開倉價 ===
current_entry_price_long = None
current_entry_price_short = None

# === 新增：本地已通知平倉單ID記錄 ===
NOTIFIED_ORDERS_FILE = os.path.join(os.path.dirname(__file__), "notified_orders.json")
notified_orders_lock = threading.Lock()

POSITION_ENTRY_TYPE_FILE = "position_entry_type.json"
try:
    with open(POSITION_ENTRY_TYPE_FILE, "r", encoding="utf-8") as f:
        position_entry_type_map = json.load(f)
except Exception:
    position_entry_type_map = {}

def save_position_entry_type_map():
    with open(POSITION_ENTRY_TYPE_FILE, "w", encoding="utf-8") as f:
        json.dump(position_entry_type_map, f)

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
    import re
    log_file = os.path.join(os.path.dirname(__file__), "log.txt")
    if event_type == "RSI多單動態止損/止盈調整":
        # 從 message 取出 positionId
        match = re.search(r"positionId=([0-9]+)", message)
        if match:
            position_id = match.group(1)
            # 讀取所有行
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except FileNotFoundError:
                lines = []
            # 過濾掉同 event_type 且同 positionId 的舊紀錄
            pattern = re.compile(r"\[RSI多單動態止損/止盈調整\].*positionId=" + position_id)
            lines = [line for line in lines if not pattern.search(line)]
            # 加入新紀錄
            lines.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{event_type}] {message}\n")
            # 寫回檔案
            with open(log_file, "w", encoding="utf-8") as f:
                f.writelines(lines)
        else:
            # 若沒抓到 positionId，則直接追加
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{event_type}] {message}\n")
    else:
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
        pos_info = get_current_position_details(api_key, secret_key, SYMBOL, MARGIN_COIN)
        long_pos = pos_info["long"]
        short_pos = pos_info["short"]
        # 優先顯示多單，若無多單則顯示空單，否則顯示無持倉
        if long_pos is not None:
            current_pos_status_for_discord = f"📈 多單 (數量: {long_pos['qty']})"
            if long_pos.get("unrealized_pnl") is not None:
                current_pos_pnl_msg = f"{long_pos['unrealized_pnl']:.4f} USDT"
        elif short_pos is not None:
            current_pos_status_for_discord = f"📉 空單 (數量: {short_pos['qty']})"
            if short_pos.get("unrealized_pnl") is not None:
                current_pos_pnl_msg = f"{short_pos['unrealized_pnl']:.4f} USDT"
        else:
            current_pos_status_for_discord = "🔄 無持倉"
            current_pos_pnl_msg = ""
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
            pos_info = get_current_position_details(api_key, secret_key, SYMBOL, MARGIN_COIN)
            long_pos = pos_info["long"]
            short_pos = pos_info["short"]
            if long_pos is not None:
                current_pos_status_for_discord = f"📈 多單 (數量: {long_pos['qty']})"
            elif short_pos is not None:
                current_pos_status_for_discord = f"📉 空單 (數量: {short_pos['qty']})"
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

    # 新增：記錄本K棒是否已經有多單平倉行為
    if not hasattr(execute_trading_strategy, "long_action_taken_on_kline_time"):
        execute_trading_strategy.long_action_taken_on_kline_time = {}

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

        # 檢查是否新K棒，若是則重置
        if (last_checked_kline_time is None) or (latest_kline_time != last_checked_kline_time):
            execute_trading_strategy.long_action_taken_on_kline_time[latest_kline_time] = False
            last_checked_kline_time = latest_kline_time

        # 檢查當前持倉狀態
        pos_info = get_current_position_details(api_key, secret_key, symbol, margin_coin)
        long_pos = pos_info["long"]
        short_pos = pos_info["short"]
        # 多單資訊
        if long_pos is not None:
            current_pos_side = "long"
            current_pos_qty_str = long_pos["qty"]
            current_position_id = long_pos["positionId"]
            current_unrealized_pnl = long_pos["unrealized_pnl"]
        elif short_pos is not None:
            current_pos_side = "short"
            current_pos_qty_str = short_pos["qty"]
            current_position_id = short_pos["positionId"]
            current_unrealized_pnl = short_pos["unrealized_pnl"]
        else:
            current_pos_side = None
            current_pos_qty_str = None
            current_position_id = None
            current_unrealized_pnl = None
        current_pos_qty = float(current_pos_qty_str) if current_pos_qty_str else 0.0

        # 只允許同時一張單
        if current_pos_side is None:
            # 若本K棒已經有多單平倉行為，則禁止RSI多單開倉
            rsi_long_blocked = execute_trading_strategy.long_action_taken_on_kline_time.get(latest_kline_time, False)
            # RSI 多單進場
            if not rsi_long_blocked and latest_rsi is not None and latest_rsi < RSI_BUY:
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
                        position_entry_type_map[str(new_position_id)] = "RSI"
                        save_position_entry_type_map()
                        # 在 execute_trading_strategy() 新開倉時，開倉後自動查詢並記錄開倉價
                        # 新開倉多單
                        pos_info = get_current_position_details(api_key, secret_key, symbol, margin_coin)
                        long_pos = pos_info["long"]
                        if long_pos is not None:
                            current_entry_price_long = long_pos.get("avgOpenPrice")
                        # 止損止盈計算改用 current_entry_price_long
                        if current_entry_price_long is not None:
                            stop_loss = current_entry_price_long - latest_atr * STOP_MULT
                            take_profit = current_entry_price_long + latest_atr * LIMIT_MULT
                        else:
                            log_event("止損止盈錯誤", "無法取得多單開倉價，跳過止損止盈計算")
                            stop_loss = None
                            take_profit = None
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
            elif rsi_long_blocked and latest_rsi is not None and latest_rsi < RSI_BUY:
                print("本K棒已多單平倉，禁止RSI多單開倉")
            # Breakout 多單進場（不受限制）
            elif latest_highest_break is not None and latest_close > latest_highest_break:
                trade_size = calculate_trade_size(api_key, secret_key, symbol, wallet_percentage, leverage, latest_close)
                if trade_size > 0:
                    log_event("策略判斷", f"觸發突破多單條件，close={latest_close} > highestBreak={latest_highest_break}")
                    order_result = send_order(api_key, secret_key, symbol, margin_coin, "open_long", trade_size, leverage)
                    if order_result and order_result.get('code') == 0:
                        new_position_id = order_result.get("data", {}).get("positionId")
                        current_position_id_global = new_position_id
                        current_pos_entry_type = "breakout"
                        position_entry_type_map[str(new_position_id)] = "Breakout"
                        save_position_entry_type_map()
                        current_stop_loss_price = latest_close - latest_atr * ATR_MULT
                        log_event("開倉成功", f"多單 Breakout, 數量={trade_size}, 價格={latest_close}, 初始移動止損={current_stop_loss_price}")
                        send_discord_message("🟢 **突破多單開倉成功** 🟢", api_key, secret_key, operation_details={"type": "open_success", "side_opened": "long", "qty": trade_size, "entry_price": latest_close, "signal": "Breakout", "force_send": True})
                    else:
                        log_event("開倉失敗", f"多單 Breakout, 數量={trade_size}, 價格={latest_close}, 錯誤={order_result}")
                        send_discord_message("🔴 **突破多單開倉失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": order_result.get("msg", order_result.get("error", "未知錯誤")), "signal": "Breakout", "force_send": True})
                else:
                    log_event("策略判斷", f"突破多單條件成立但下單數量為0，close={latest_close}")
            # 空單進場不受限制
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
                            position_entry_type_map[str(new_position_id)] = "RSI"
                            save_position_entry_type_map()
                            # 在 execute_trading_strategy() 新開倉時，開倉後自動查詢並記錄開倉價
                            # 新開倉空單
                            pos_info = get_current_position_details(api_key, secret_key, symbol, margin_coin)
                            short_pos = pos_info["short"]
                            if short_pos is not None:
                                current_entry_price_short = short_pos.get("avgOpenPrice")
                            if current_entry_price_short is not None:
                                stop_loss = current_entry_price_short + latest_atr * STOP_MULT
                                take_profit = current_entry_price_short - latest_atr * LIMIT_MULT
                            else:
                                log_event("止損止盈錯誤", "無法取得空單開倉價，跳過止損止盈計算")
                                stop_loss = None
                                take_profit = None
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
                            position_entry_type_map[str(new_position_id)] = "Breakout"
                            save_position_entry_type_map()
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
                            # 標記本K棒已多單平倉
                            execute_trading_strategy.long_action_taken_on_kline_time[latest_kline_time] = True
                            save_long_action_flag(execute_trading_strategy.long_action_taken_on_kline_time)
                            if current_position_id:
                                position_entry_type_map.pop(str(current_position_id), None)
                                save_position_entry_type_map()
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
                            if current_position_id:
                                position_entry_type_map.pop(str(current_position_id), None)
                                save_position_entry_type_map()
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
                    log_event("移動止損調整", f"多單 Breakout, positionId={current_position_id_global}, 新止損={new_trailing_stop}, ATR={latest_atr}, RSI={latest_rsi}")
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
                    log_event("移動止損調整", f"空單 Breakout, positionId={current_position_id_global}, 新止損={new_trailing_stop}, ATR={latest_atr}, RSI={latest_rsi}")
                    current_stop_loss_price = new_trailing_stop
                    send_discord_message(f"⬇️ **突破空單移動止損下調** ⬇️ 新止損: {new_trailing_stop:.4f}", api_key, secret_key, operation_details={"type": "status_update", "details": f"新止損: {new_trailing_stop:.4f}", "force_send": True})
                else:
                    log_event("移動止損失敗", f"空單 Breakout, positionId={current_position_id_global}, 嘗試新止損={new_trailing_stop}, 錯誤={modify_result}")
                    send_discord_message(f"🔴 **突破空單移動止損調整失敗** 🔴", api_key, secret_key, operation_details={"type": "error", "details": modify_result.get("msg", modify_result.get("error", "未知錯誤")), "force_send": True})

        # RSI 多單動態止盈止損自動更新（先查詢、取消、再設置）
        if current_pos_side == "long" and current_pos_entry_type == "rsi" and current_position_id_global:
            if current_entry_price_long is not None:
                new_stop_loss = current_entry_price_long - latest_atr * STOP_MULT
                new_take_profit = current_entry_price_long + latest_atr * LIMIT_MULT
            else:
                log_event("止損止盈錯誤", "無法取得多單開倉價，跳過動態止損止盈計算")
                new_stop_loss = None
                new_take_profit = None
            # 僅當止損或止盈價格有變動才更新
            if (current_stop_loss_price is None or abs(new_stop_loss - current_stop_loss_price) > 1e-6):
                tpsl_order_ids = get_pending_tpsl_orders(api_key, secret_key, symbol, current_position_id_global)
                for oid in tpsl_order_ids:
                    cancel_tpsl_order(api_key, secret_key, symbol, oid)
                place_result = place_conditional_orders(api_key, secret_key, symbol, margin_coin, current_position_id_global, stop_price=new_stop_loss, limit_price=new_take_profit)
                if place_result and place_result.get('code') == 0:
                    log_event("RSI多單動態止損/止盈調整", f"多單 RSI, positionId={current_position_id_global}, 新止損={new_stop_loss}, 新止盈={new_take_profit}, ATR={latest_atr}, RSI={latest_rsi}")
                    current_stop_loss_price = new_stop_loss
                else:
                    log_event("RSI多單動態止損/止盈調整失敗", f"多單 RSI, positionId={current_position_id_global}, 嘗試新止損={new_stop_loss}, 新止盈={new_take_profit}, 錯誤={place_result}")

        # RSI 空單動態止盈止損自動更新（先查詢、取消、再設置）
        if current_pos_side == "short" and current_pos_entry_type == "rsi_short" and current_position_id_global:
            if current_entry_price_short is not None:
                new_stop_loss = current_entry_price_short + latest_atr * STOP_MULT
                new_take_profit = current_entry_price_short - latest_atr * LIMIT_MULT
            else:
                log_event("止損止盈錯誤", "無法取得空單開倉價，跳過動態止損止盈計算")
                new_stop_loss = None
                new_take_profit = None
            if (current_stop_loss_price is None or abs(new_stop_loss - current_stop_loss_price) > 1e-6):
                tpsl_order_ids = get_pending_tpsl_orders(api_key, secret_key, symbol, current_position_id_global)
                for oid in tpsl_order_ids:
                    cancel_tpsl_order(api_key, secret_key, symbol, oid)
                place_result = place_conditional_orders(api_key, secret_key, symbol, margin_coin, current_position_id_global, stop_price=new_stop_loss, limit_price=new_take_profit)
                if place_result and place_result.get('code') == 0:
                    log_event("RSI空單動態止損/止盈調整", f"空單 RSI, positionId={current_position_id_global}, 新止損={new_stop_loss}, 新止盈={new_take_profit}, ATR={latest_atr}, RSI={latest_rsi}")
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

# === 查詢持倉狀態（新版：同時回傳多單與空單） === #
def get_current_position_details(api_key, secret_key, symbol, margin_coin=MARGIN_COIN):
    """
    回傳 {'long': {...}, 'short': {...}} 結構，分別包含 qty, positionId, unrealized_pnl, avgOpenPrice。
    """
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
    result = {"long": None, "short": None}
    try:
        res = requests.get(url, headers=headers, params=params)
        data = res.json()
        if data.get("code") == 0 and data.get("data"):
            for pos_detail in data["data"]:
                pos_qty_str = pos_detail.get("qty", "0")
                position_id = pos_detail.get("positionId")
                unrealized_pnl = float(pos_detail.get("unrealizedPNL", 0.0))
                avg_open_price = float(pos_detail.get("avgOpenPrice", 0.0)) if pos_detail.get("avgOpenPrice") else None
                if float(pos_qty_str) > 0:
                    if pos_detail.get("side") == "BUY":
                        result["long"] = {"qty": pos_qty_str, "positionId": position_id, "unrealized_pnl": unrealized_pnl, "avgOpenPrice": avg_open_price}
                    elif pos_detail.get("side") == "SELL":
                        result["short"] = {"qty": pos_qty_str, "positionId": position_id, "unrealized_pnl": unrealized_pnl, "avgOpenPrice": avg_open_price}
        return result
    except Exception as e:
        print(f"查詢持倉詳細失敗: {e}")
        return {"long": None, "short": None}

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
def query_last_closed_order(api_key, secret_key, symbol, prev_pos_id, max_retries=3, retry_interval=1):
    """
    查詢最近的平倉訂單，並判斷是TP還是SL，增加debug print與重試機制。
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
    for attempt in range(max_retries):
        try:
            res = requests.get(url, headers=headers, params=params)
            data = res.json()
            print(f"[DEBUG] 歷史訂單查詢結果 (第{attempt+1}次): {data}")
            if data.get("code") == 0 and data.get("data"):
                for order in data["data"]:
                    if str(order.get("positionId")) == str(prev_pos_id) and order.get("status") == "FILLED":
                        trigger_type = order.get("triggerType", "")
                        close_price = order.get("avgPrice", order.get("price", ""))
                        profit = order.get("profit", None)
                        return {"trigger_type": trigger_type, "close_price": close_price, "profit": profit}
        except Exception as e:
            print(f"查詢歷史訂單失敗: {e}")
        time.sleep(retry_interval)
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

def set_leverage_to_config():
    """
    使用 Bitunix API 將槓桿設為 config.py 的 LEVERAGE
    """
    url = "https://fapi.bitunix.com/api/v1/futures/account/change_leverage"
    body = {
        "symbol": SYMBOL,
        "leverage": LEVERAGE,
        "marginCoin": MARGIN_COIN
    }
    import uuid, time, hashlib, json
    nonce = uuid.uuid4().hex
    timestamp = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(',', ':'), ensure_ascii=False)
    digest_input = nonce + timestamp + BITUNIX_API_KEY + body_str
    digest = hashlib.sha256(digest_input.encode('utf-8')).hexdigest()
    sign = hashlib.sha256((digest + BITUNIX_SECRET_KEY).encode('utf-8')).hexdigest()
    headers = {
        "api-key": BITUNIX_API_KEY,
        "sign": sign,
        "nonce": nonce,
        "timestamp": timestamp,
        "language": "en-US",
        "Content-Type": "application/json"
    }
    try:
        res = requests.post(url, headers=headers, data=body_str)
        print(f"[DEBUG] 槓桿設定API回應: {res.text}")
        data = res.json()
        if data.get("code") == 0:
            print(f"[INFO] 槓桿已設為 {LEVERAGE}")
        else:
            print(f"[WARNING] 槓桿設定失敗: {data}")
            log_event("槓桿設定失敗", str(data))
    except Exception as e:
        print(f"[ERROR] 設定槓桿時發生錯誤: {e}")
        log_event("槓桿設定異常", str(e))

class BitunixBot(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(intents=discord.Intents.default())
        self.position_message_id = None
        self.last_position_status = None
        self.bg_task = None

    async def on_ready(self):
        print(f'Logged in as {self.user}')
        self.bg_task = asyncio.create_task(self.trading_loop())

    async def trading_loop(self):
        global win_count, loss_count, current_pos_entry_type, current_stop_loss_price, current_position_id_global, last_checked_kline_time
        load_stats()
        from config import TRADING_PAIR, SYMBOL, MARGIN_COIN, LEVERAGE, WALLET_PERCENTAGE, RSI_LEN, ATR_LEN, BREAKOUT_LOOKBACK, STOP_MULT, LIMIT_MULT, RSI_BUY, EXIT_RSI, ATR_MULT, TIMEFRAME
        api_key = BITUNIX_API_KEY
        secret_key = BITUNIX_SECRET_KEY
        symbol = SYMBOL
        margin_coin = MARGIN_COIN
        leverage = LEVERAGE
        wallet_percentage = WALLET_PERCENTAGE
        print("交易機器人啟動，開始載入初始K線數據...")
        ohlcv_data = fetch_ohlcv(api_key, secret_key)
        balance = check_wallet_balance(api_key, secret_key)
        min_data_len = max(RSI_LEN, ATR_LEN, BREAKOUT_LOOKBACK + 1) + 5
        if ohlcv_data is None or len(ohlcv_data) < min_data_len:
            await self.send_status(f"🔴 啟動失敗：無法獲取足夠的初始K線數據。需要至少 {min_data_len} 條數據，實際獲取 {len(ohlcv_data) if ohlcv_data is not None else 0} 條。")
            return
        df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df_ind = compute_indicators(df, RSI_LEN, ATR_LEN, BREAKOUT_LOOKBACK, api_key, secret_key, symbol)
        if df_ind is None or df_ind.empty or df_ind['rsi'].isnull().all() or df_ind['atr'].isnull().all():
            await self.send_status("🔴 啟動失敗：計算指標失敗。")
            return
        latest_close = df_ind['close'].iloc[-1]
        latest_rsi = df_ind['rsi'].iloc[-1]
        latest_atr = df_ind['atr'].iloc[-1]
        print(f"[Main Startup] 最新收盤價: {latest_close:.2f}, RSI: {latest_rsi:.2f}, ATR: {latest_atr:.4f}")
        await self.send_status("", balance=balance, rsi=latest_rsi)
        # 冷啟動時立即同步持倉訊息
        # === 新增：手動補 entry_type ===
        pos_info = get_current_position_details(api_key, secret_key, symbol, margin_coin)
        for pos in [pos_info["long"], pos_info["short"]]:
            if pos is not None:
                pid = str(pos.get("positionId"))
                if pid not in position_entry_type_map:
                    print(f"偵測到未知進場方式的持倉：positionId={pid} 進場價={pos.get('avgOpenPrice')} 數量={pos.get('qty')}")
                    entry_type = input(f"請輸入 positionId={pid} 的進場方式（RSI/Breakout）：").strip().upper()
                    if entry_type in ["RSI", "BREAKOUT"]:
                        position_entry_type_map[pid] = "RSI" if entry_type == "RSI" else "Breakout"
                        save_position_entry_type_map()
                        print(f"已補 entry_type: {pid} → {position_entry_type_map[pid]}")
                    else:
                        print("輸入無效，請下次重啟時再補。")
        await self.update_discord_position_message(api_key, secret_key, symbol, margin_coin, latest_rsi, latest_atr)
        while True:
            ohlcv_data = fetch_ohlcv(api_key, secret_key)
            df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df_ind = compute_indicators(df, RSI_LEN, ATR_LEN, BREAKOUT_LOOKBACK, api_key, secret_key, symbol)
            latest_rsi = df_ind['rsi'].iloc[-1]
            latest_atr = df_ind['atr'].iloc[-1]
            execute_trading_strategy(api_key, secret_key, symbol, margin_coin, wallet_percentage, leverage, RSI_BUY, BREAKOUT_LOOKBACK, ATR_MULT)
            await self.update_discord_position_message(api_key, secret_key, symbol, margin_coin, latest_rsi, latest_atr)
            await asyncio.sleep(LOOP_INTERVAL_SECONDS)

    async def send_status(self, msg, balance=None, rsi=None):
        try:
            from config import STOP_MULT, LIMIT_MULT, RSI_BUY, RSI_LEN, EXIT_RSI, rsiSell, exitRSI_short, BREAKOUT_LOOKBACK, ATR_LEN, ATR_MULT, TIMEFRAME, WALLET_PERCENTAGE, LOOP_INTERVAL_SECONDS
            channel = self.get_channel(DISCORD_CHANNEL_ID)
            if channel:
                now_str = time.strftime('%Y-%m-%d %H:%M:%S')
                param_text = (
                    f"STOP_MULT: {STOP_MULT}\n"
                    f"LIMIT_MULT: {LIMIT_MULT}\n"
                    f"RSI_BUY: {RSI_BUY}\n"
                    f"RSI_LEN: {RSI_LEN}\n"
                    f"EXIT_RSI: {EXIT_RSI}\n"
                    f"rsiSell: {rsiSell}\n"
                    f"exitRSI_short: {exitRSI_short}\n"
                    f"BREAKOUT_LOOKBACK: {BREAKOUT_LOOKBACK}\n"
                    f"ATR_LEN: {ATR_LEN}\n"
                    f"ATR_MULT: {ATR_MULT}\n"
                    f"TIMEFRAME: {TIMEFRAME}\n"
                    f"WALLET_PERCENTAGE: {WALLET_PERCENTAGE}\n"
                    f"LOOP_INTERVAL_SECONDS: {LOOP_INTERVAL_SECONDS}\n"
                )
                embed = discord.Embed(
                    title="🚀 交易機器人啟動 🚀",
                    description=(
                        f"```{param_text}```"
                        f"目前錢包餘額: `{balance:.2f} USDT`\n"
                        f"最新 RSI: `{rsi:.2f}`\n"
                        f"🕒 啟動時間: {now_str}"
                    ),
                    color=0x3498db
                )
                await channel.send(embed=embed)
        except Exception as e:
            print(f"send_status 發生錯誤: {e}")
            logger.error(f"send_status 發生錯誤: {e}\n{traceback.format_exc()}")

    async def update_discord_position_message(self, api_key, secret_key, symbol, margin_coin, latest_rsi, latest_atr):
        try:
            from config import WALLET_PERCENTAGE  # 確保變數可用
            channel = self.get_channel(DISCORD_CHANNEL_ID)
            if not channel:
                print("找不到指定的 Discord 頻道")
                logger.error("找不到指定的 Discord 頻道")
                return
            pos_info = get_current_position_details(api_key, secret_key, symbol, margin_coin)
            long_pos = pos_info["long"]
            short_pos = pos_info["short"]
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            rsi_str = f"{latest_rsi:.2f}" if latest_rsi is not None else "N/A"
            # Embed 標題與顏色
            embed_title = f"{SYMBOL} 交易通知"
            embed_color = 0x3498db  # 預設藍色
            show_param = False
            if long_pos or short_pos:
                embed_color = 0x2ecc71  # 綠色（有持倉）
                show_param = False
            else:
                embed_color = 0xf1c40f  # 黃色（無持倉）
                show_param = True
            # Embed 內容
            if show_param:
                param_text = (
                    f"STOP_MULT: {STOP_MULT}\n"
                    f"LIMIT_MULT: {LIMIT_MULT}\n"
                    f"RSI_BUY: {RSI_BUY}\n"
                    f"RSI_LEN: {RSI_LEN}\n"
                    f"EXIT_RSI: {EXIT_RSI}\n"
                    f"rsiSell: {rsiSell}\n"
                    f"exitRSI_short: {exitRSI_short}\n"
                    f"BREAKOUT_LOOKBACK: {BREAKOUT_LOOKBACK}\n"
                    f"ATR_LEN: {ATR_LEN}\n"
                    f"ATR_MULT: {ATR_MULT}\n"
                    f"TIMEFRAME: {TIMEFRAME}\n"
                    f"WALLET_PERCENTAGE: {WALLET_PERCENTAGE}\n"
                    f"LOOP_INTERVAL_SECONDS: {LOOP_INTERVAL_SECONDS}\n"
                    f"目前錢包餘額: {check_wallet_balance(api_key, secret_key):.2f} USDT\n"
                )
                embed = discord.Embed(
                    title=embed_title,
                    description=f"🚀 交易機器人啟動 🚀\n```\n{param_text}```\n**最新 RSI:** `{rsi_str}`",
                    color=embed_color
                )
            else:
                embed = discord.Embed(
                    title=embed_title,
                    description=f"**最新 RSI:** `{rsi_str}`",
                    color=embed_color
                )
            # 勝率
            total_trades = win_count + loss_count
            win_rate_str = f"{win_count / total_trades * 100:.2f}% ({win_count}勝/{loss_count}負)" if total_trades > 0 else "N/A(尚無已完成交易)"
            embed.add_field(name="🏆 勝率統計", value=win_rate_str, inline=True)
            # 持倉與盈虧
            if long_pos is not None:
                entry_type = position_entry_type_map.get(str(long_pos.get("positionId")), "未知")
                entry_price = long_pos.get("avgOpenPrice")
                stop_loss = None
                take_profit = None
                if entry_type == "RSI" and entry_price is not None:
                    stop_loss = entry_price - latest_atr * STOP_MULT
                    take_profit = entry_price + latest_atr * LIMIT_MULT
                elif entry_type == "Breakout" and entry_price is not None:
                    stop_loss = long_pos.get("stop_loss") or (entry_price - latest_atr * ATR_MULT)
                pnl = long_pos.get("unrealized_pnl")
                entry_price_str = f"{entry_price:.2f}" if entry_price is not None else "N/A"
                stop_loss_str = f"{stop_loss:.2f}" if stop_loss is not None else "N/A"
                take_profit_str = f"{take_profit:.2f}" if take_profit is not None else "N/A"
                pnl_str = f"{pnl:.2f}" if pnl is not None else "N/A"
                embed.add_field(name="📊 目前持倉", value=f"多單 (數量: `{long_pos['qty']}`)", inline=True)
                embed.add_field(name="💰 未實現盈虧", value=f"`{pnl_str} USDT`", inline=True)
                embed.add_field(name="🔑 進場方式", value=f"{entry_type}", inline=True)
                embed.add_field(name="💵 進場價", value=f"`{entry_price_str}`", inline=True)
                embed.add_field(name="🛡️ 止損", value=f"`{stop_loss_str}`", inline=True)
                embed.add_field(name="🎯 止盈", value=f"`{take_profit_str}`", inline=True)
            elif short_pos is not None:
                entry_type = position_entry_type_map.get(str(short_pos.get("positionId")), "未知")
                entry_price = short_pos.get("avgOpenPrice")
                stop_loss = None
                take_profit = None
                if entry_type == "RSI" and entry_price is not None:
                    stop_loss = entry_price + latest_atr * STOP_MULT
                    take_profit = entry_price - latest_atr * LIMIT_MULT
                elif entry_type == "Breakout" and entry_price is not None:
                    stop_loss = short_pos.get("stop_loss") or (entry_price + latest_atr * ATR_MULT)
                pnl = short_pos.get("unrealized_pnl")
                entry_price_str = f"{entry_price:.2f}" if entry_price is not None else "N/A"
                stop_loss_str = f"{stop_loss:.2f}" if stop_loss is not None else "N/A"
                take_profit_str = f"{take_profit:.2f}" if take_profit is not None else "N/A"
                pnl_str = f"{pnl:.2f}" if pnl is not None else "N/A"
                embed.add_field(name="📊 目前持倉", value=f"空單 (數量: `{short_pos['qty']}`)", inline=True)
                embed.add_field(name="💰 未實現盈虧", value=f"`{pnl_str} USDT`", inline=True)
                embed.add_field(name="🔑 進場方式", value=f"{entry_type}", inline=True)
                embed.add_field(name="💵 進場價", value=f"`{entry_price_str}`", inline=True)
                embed.add_field(name="🛡️ 止損", value=f"`{stop_loss_str}`", inline=True)
                embed.add_field(name="🎯 止盈", value=f"`{take_profit_str}`", inline=True)
            else:
                embed.add_field(name="📊 目前持倉", value="無持倉", inline=True)
                embed.add_field(name="💰 未實現盈虧", value="N/A", inline=True)
            # 時間
            embed.add_field(name="🕒 時間", value=now_str, inline=False)
            # 發送或編輯訊息
            if not self.position_message_id:
                msg_obj = await channel.send(embed=embed)
                self.position_message_id = msg_obj.id
            else:
                try:
                    msg_obj = await channel.fetch_message(self.position_message_id)
                    await msg_obj.edit(embed=embed)
                except Exception as e:
                    print(f"編輯訊息失敗: {e}，改為發送新訊息")
                    logger.error(f"編輯訊息失敗: {e}\n{traceback.format_exc()}")
                    msg_obj = await channel.send(embed=embed)
                    self.position_message_id = msg_obj.id
        except Exception as e:
            print(f"update_discord_position_message 發生錯誤: {e}")
            logger.error(f"update_discord_position_message 發生錯誤: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    while True:
        try:
            print("主循環執行中...")
            logger.info("主循環執行中...")
            bot = BitunixBot()
            bot.run(DISCORD_BOT_TOKEN)
            break  # 成功啟動就跳出
        except Exception as e:
            print(f"啟動 Discord Bot 失敗：{e}")
            logger.error(f"啟動 Discord Bot 失敗：{e}\n{traceback.format_exc()}")
            print("3 秒後自動重試...")
            import time
            time.sleep(3)
        finally:
            flush_discord_messages()
