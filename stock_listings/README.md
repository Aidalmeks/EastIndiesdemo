# 独立美股名录

这是一个完全静态、只读的展示页面，不占用端口，也不修改量化系统文件。

直接双击 **`stock_listings.html`**，或在文件管理器中用 Firefox 打开它即可。
这是包含样式、数据和功能的单一文件，不依赖其他本地资源。

页面数据来自生成时的：

- `/home/quant/Downloads/nasdaqlisted.txt`
- `/home/quant/Downloads/otherlisted.txt`
- `data/All stocks_2026-07.csv`
- `data/ETF vault_2026-07.csv`
- `quant_data.us_info`

TradingView 的两份 CSV 用证券代码精确匹配到 Nasdaq Trader 官方名录。
页面使用一个月成交额，并只保留有月成交额的证券。Cboe BZX 上市、名称明确
标注为 1.25X/1.5X/2X/3X/4X、Leveraged 或 ProShares Ultra 系列的杠杆
ETF 也会从名录和排名中排除。普通 Cboe ETF 不受影响。

排名分为两个口径：

- `全`：在全部有成交额的证券中生成的固定排名。
- `筛`：按当前搜索、市场、类型、行情和注册状态筛选结果重新计算的临时排名。

清单或数据库变化后，重新生成数据：

```bash
cd /var/www/html/finance/stock_listings
/var/www/html/finance/quant/bin/python build_static.py
```

如以后下载了新文件，可用环境变量指定文件名：

```bash
TRADINGVIEW_STOCKS_FILE="/path/to/monthly-stocks.csv" \
TRADINGVIEW_ETFS_FILE="/path/to/monthly-etfs.csv" \
/var/www/html/finance/quant/bin/python build_static.py
```

生成过程只读取上述数据，并更新本目录下的 `data.js` 和
`stock_listings.html`。
