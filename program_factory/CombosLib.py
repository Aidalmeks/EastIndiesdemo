import math
import time

from ib_insync import MarketOrder, Stock

from program_factory.execution import cap_target_gross_to_total_exposure

FAILED_ORDER_STATUSES = {"Cancelled", "Inactive", "ApiCancelled"}


def get_action(qty):
    if qty > 0:
        return "BUY", qty
    if qty < 0:
        return "SELL", abs(qty)
    return None, 0


def pair_target_quantities(position_type, beta, qty_y, qty_x, single_leg_mode=False):
    """Return signed target shares for Y/X from a spread direction.

    The modelled spread is ``Y - beta * X``.  A negative beta therefore
    requires same-direction legs: long spread buys both legs and short spread
    sells both legs.  Keep that rule explicit here because waiting-entry uses
    these targets to submit the two entry orders.
    """
    position_type = str(position_type or "").upper()
    if position_type not in {"LONG_SPREAD", "SHORT_SPREAD"}:
        raise ValueError(f"Unsupported position type: {position_type!r}")

    y_sign = 1 if position_type == "LONG_SPREAD" else -1
    if single_leg_mode:
        return y_sign * int(qty_y), 0

    x_sign = -y_sign if float(beta) > 0 else y_sign
    return y_sign * int(qty_y), x_sign * abs(int(qty_x))


def wait_for_order_trades(ib, trades, pair_name, label, timeout=120):
    start = time.time()

    while True:
        all_filled = True
        failures = []

        for symbol, trade in trades:
            status = str(trade.orderStatus.status or "")
            filled = trade.orderStatus.filled
            remaining = trade.orderStatus.remaining
            print(f"[{label} STATUS] {symbol} | Status: {status} | Filled: {filled} | Remaining: {remaining}")

            if status in FAILED_ORDER_STATUSES:
                failures.append(f"{symbol}: {status}")
            if status != "Filled":
                all_filled = False

        if failures:
            raise RuntimeError("; ".join(failures))

        if all_filled:
            print(f"[{label}] all legs filled")
            return

        if time.time() - start > timeout:
            raise RuntimeError(f"Timeout waiting {label.lower()} fills for {pair_name}")

        ib.sleep(1)


def place_pair_order(ib, pair_name, cfg, net_liq_usd, account):
    print(f"\nplace_pair_order: {pair_name}")

    beta = cfg["beta"]
    exposure_pct = cfg["exposure_pct"]
    mode_direction = cfg["position_type"]

    y, x = pair_name.split("_")
    symbol_y = y.upper()
    symbol_x = x.upper()

    gross_target = 1.6 * net_liq_usd * (exposure_pct / 100)

    contract_y = Stock(symbol_y, "SMART", "USD")
    contract_x = Stock(symbol_x, "SMART", "USD")

    ib.qualifyContracts(contract_y, contract_x)

    ticker_y = ib.reqMktData(contract_y, "", False, False)
    ticker_x = ib.reqMktData(contract_x, "", False, False)

    while math.isnan(ticker_y.marketPrice()) or math.isnan(ticker_x.marketPrice()):
        ib.sleep(0.2)

    price_y = ticker_y.marketPrice()
    price_x = ticker_x.marketPrice()

    single_leg_mode = math.isclose(abs(beta), 0.0, abs_tol=1e-12)
    pair_gross_per_share = price_y + abs(beta) * price_x
    if pair_gross_per_share <= 0:
        raise ValueError(f"Invalid gross-per-share for {pair_name}: {pair_gross_per_share}")

    positions = ib.positions()
    pos_y, pos_x = 0, 0

    for p in positions:
        if getattr(p, "account", None) != account:
            continue
        if p.contract.symbol == symbol_y:
            pos_y = p.position
        elif p.contract.symbol == symbol_x:
            pos_x = p.position

    capped_gross_target, risk_info = cap_target_gross_to_total_exposure(
        ib_client=ib,
        account=account,
        requested_gross=gross_target,
        net_liq_usd=net_liq_usd,
        target_symbols=[symbol_y, symbol_x],
        target_prices={symbol_y: price_y, symbol_x: price_x},
        positions=positions,
    )
    print(
        "[RISK] current gross="
        f"{risk_info['current_total_gross']:.2f} USD "
        f"({risk_info['current_total_pct']:.2f}%) | "
        f"allowed target={risk_info['allowed_target_gross']:.2f} USD"
    )
    if risk_info["capped"]:
        print(
            f"[RISK] gross target capped: {gross_target:.2f} -> "
            f"{capped_gross_target:.2f} USD"
        )
        gross_target = capped_gross_target

    qty_y = int(gross_target / pair_gross_per_share)
    qty_x = int(qty_y * abs(beta))
    gross_actual = qty_y * price_y + abs(qty_x) * price_x

    print(f"Gross target={gross_target:.2f} | actual={gross_actual:.2f}")
    if single_leg_mode:
        print(f"beta=0 detected -> single-leg mode on {symbol_y}; {symbol_x} target forced to flat")

    if qty_y <= 0 or (not single_leg_mode and abs(qty_x) <= 0):
        raise ValueError(f"Target share size too small for {pair_name}: {symbol_y}={qty_y}, {symbol_x}={qty_x}")

    mode = "OPEN" if abs(pos_y) < 1 and abs(pos_x) < 1 else "TARGET"
    print("Mode:", mode)

    target_y, target_x = pair_target_quantities(
        mode_direction,
        beta,
        qty_y,
        qty_x,
        single_leg_mode=single_leg_mode,
    )
    if beta < 0 and not single_leg_mode and target_y * target_x <= 0:
        raise RuntimeError(
            f"Negative-beta entry must use same-direction legs: "
            f"{symbol_y}={target_y}, {symbol_x}={target_x}"
        )

    order_y = int(target_y - pos_y)
    order_x = int(target_x - pos_x)

    action_y, qty_y_final = get_action(order_y)
    action_x, qty_x_final = get_action(order_x)

    print(f"Target -> {symbol_y}={target_y}, {symbol_x}={target_x}")
    print(f"Delta  -> {symbol_y}={order_y}, {symbol_x}={order_x}")

    trades = []
    if action_y:
        order_y_obj = MarketOrder(action_y, qty_y_final)
        order_y_obj.account = account
        print(f"[SUBMIT] {symbol_y}: {action_y} {qty_y_final}")
        trades.append((symbol_y, ib.placeOrder(contract_y, order_y_obj)))
    else:
        print(f"[SKIP] {symbol_y}: already at target")

    if action_x:
        order_x_obj = MarketOrder(action_x, qty_x_final)
        order_x_obj.account = account
        print(f"[SUBMIT] {symbol_x}: {action_x} {qty_x_final}")
        trades.append((symbol_x, ib.placeOrder(contract_x, order_x_obj)))
    else:
        print(f"[SKIP] {symbol_x}: already at target")

    if not trades:
        raise RuntimeError(f"No valid share quantity to place for {pair_name}")

    wait_for_order_trades(ib, trades, pair_name, mode)
