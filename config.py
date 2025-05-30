MARGIN_COIN = "USDT"  # 保證金幣種
BITUNIX_API_KEY =""  # Bitunix API 金鑰
BITUNIX_SECRET_KEY =""  # Bitunix Secret 金鑰
DISCORD_WEBHOOK_URL =""  # DC通知(PC版:編輯頻道->整合->webhook->新webhook->複製網址)
TRADING_PAIR = "ETH/USDT"  # 交易對
SYMBOL = "ETHUSDT"  # 交易符號
LEVERAGE = 20# 槓桿
WALLET_PERCENTAGE = 0.8 # 每次下單使用錢包的%數->1.00=錢包的100%
LOOP_INTERVAL_SECONDS =20# 主循環執行間隔（秒）
# 技術指標參數
STOP_MULT = 1.0  # 停損倍數
LIMIT_MULT = 4 # 限制倍數
RSI_BUY = 47  # RSI 買入指標
RSI_LEN = 12 # RSI 長度
EXIT_RSI = 44 # 退出 RSI
BREAKOUT_LOOKBACK = 3# 突破回看
ATR_LEN = 12  # ATR 長度
ATR_MULT = 3.25  # ATR 倍數
TIMEFRAME = "4h"  # 時間框架
QUANTITY_PRECISION = 4 # 交易數量四捨五入小數位數
# === 空單參數 ===
rsiSell = 53  # RSI 空單進場閾值
exitRSI_short = 51  # RSI 空單平倉閾值
CONDITIONAL_ORDER_MAX_RETRIES = 3  # 條件單自動重試最大次數
CONDITIONAL_ORDER_RETRY_INTERVAL = 2  # 條件單重試間隔（秒）