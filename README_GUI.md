# PyQt6 GUI（仪表盘控制版）

## 启动

先复制 `.env.example` 为 `.env`，填写本机数据库、Redis 与（如使用）IBKR 配置；`.env` 不会进入 Git。

```bash
pip install -r requirements-gui.txt
python quant_gui.py
```

## 标签页

- `dashboard`：服务总控面板（绿灯=运行中，灰灯=停止）。
- `simple_dashboard`：内嵌 `app.py` 页面（`http://127.0.0.1:8000/`）。
- `quant_dashboard`：内嵌 Streamlit 页面（默认 `http://127.0.0.1:8501`）。

## dashboard 控制项（顺序）

1. `simple_dashboard (app.py)`  
2. `quant_dashboard`  
3. `schedule (scheduler.pyw)`  
4. `livedata`  
5. `flagship_board`  
6. `waiting_entry`  
7. `fill_db`  

## 行为规则

- GUI 启动时会确保默认两项已运行（有则不重复启动）：
  - `app.py`
  - `scheduler.pyw`
- `quant_dashboard_fixed_beta.py` 是按需启动项：从首页的 `Quant Dashboard`
  服务卡启动后，才会运行 Streamlit 进程。
- `Public Dashboard Tunnel` 由 `scheduler.pyw` 在启动时自动确保运行，不依赖 GUI，并持续提供服务器上的只读实时页面。
- 关闭默认服务时会弹确认框。
- `livedata/flagship_board/waiting_entry/fill_db` 点亮后在独立 PowerShell 窗口运行（保留 rich 效果）。
- 你手动关闭外部 PowerShell 后，仪表盘会自动变灰。
- 你在仪表盘点击绿灯也可以关闭对应进程。

## 编码

- GUI 及子进程统一使用 UTF-8 环境：
  - `PYTHONUTF8=1`
  - `PYTHONIOENCODING=utf-8`

## 环境变量

- `QUANT_API_HOST`（默认 `127.0.0.1`）
- `QUANT_API_PORT`（默认 `8000`）
- `QUANT_DASHBOARD_PORT`（默认 `8501`）
- `QUANT_DB_PASSWORD`（必填；不得写回 Python 源码）
- `QUANT_IB_PORT_ACCOUNTS`（仅启动 IBKR 服务时必填；JSON 格式的端口到账户映射）
