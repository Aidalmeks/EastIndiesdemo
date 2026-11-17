import asyncio
asyncio.set_event_loop(asyncio.new_event_loop())
from ib_insync import *
from program_factory.port_listener import choose_connection

CLIENT_ID = 3
PREFERRED_PORT_ORDER = (4001, 4002)
PORT_ACCOUNT_MAP = {
    4001: "U18559662",
    4002: "DUO671947",
}
selection = choose_connection(
    port_account_map=PORT_ACCOUNT_MAP,
    preferred_order=PREFERRED_PORT_ORDER
)
CONNECT_PORT = selection.port
TARGET_ACCOUNT = selection.account


ib = IB()
ib.connect("127.0.0.1", CONNECT_PORT, clientId=CLIENT_ID)

print(f"✅ Connected to IB | port={CONNECT_PORT} | account={TARGET_ACCOUNT}")

# =============================
# 账户信息
# =============================
print("\n=== ACCOUNT SUMMARY ===")
for item in ib.accountSummary():
    if item.tag in ['NetLiquidation', 'AvailableFunds', 'BuyingPower']:
        print(item.tag, item.value, item.currency)

print("Accounts:", ib.managedAccounts())

# =============================
# 持仓
# =============================
print("\n=== POSITIONS ===")
for p in ib.positions():
    print(p)

# =============================
# 获取订单
# =============================
ib.reqAllOpenOrders()
ib.sleep(1)

print("\n=== OPEN ORDERS ===")
open_trades = []

for t in ib.trades():
    status = t.orderStatus.status
    if status not in ('Cancelled', 'Filled'):
        open_trades.append(t)
        print(f"OrderId: {t.order.orderId} | Status: {status} | Symbol: {t.contract.symbol}")

# =============================
# 用户输入决策
# =============================
print("\n=== INPUT MODE ===")
print("👉 输入股票代码取消该标的（例如 AAPL）")
print("👉 直接回车 = ⚠️ 全部取消")

user_input = input("Your choice: ").strip().upper()

# =============================
# 执行取消
# =============================
if user_input == "":
    print("⚠️ Global cancel ALL orders")
    ib.reqGlobalCancel()

else:
    target = user_input
    print(f"🎯 Cancel orders for: {target}")

    for t in open_trades:
        if t.contract.symbol == target and t.orderStatus.status in ['PreSubmitted', 'Submitted']:
            print(f"❌ Cancelling OrderId {t.order.orderId}")
            ib.cancelOrder(t.order)

# =============================
# 等待更新
# =============================
ib.sleep(2)

print("\n=== REMAINING ORDERS ===")
for t in ib.trades():
    if t.orderStatus.status not in ('Cancelled', 'Filled'):
        print(f"{t.order.orderId} | {t.contract.symbol} | {t.orderStatus.status}")

print("\n✅ Done")

ib.disconnect()
