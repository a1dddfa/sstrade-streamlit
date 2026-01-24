
# Streamlit 交易控制台（扫描 + 阶梯下单）

## 1) 依赖安装
```bash
pip install streamlit pyyaml pandas
```

## 2) 启动
把以下文件放在同一目录：
- streamlit_app.py
- binance_exchange.py
- config.yaml

然后执行：
```bash
streamlit run streamlit_app.py
```

## 3) 功能
- 扫描：按 24h 涨跌幅 abs(pct) >= 阈值筛 USDT 永续交易对（默认 30%）
- 手动阶梯：
  - 输入交易对、方向(long/short)、步长%、每次下单数量
  - 第一单限价，后续按“价格触发就下单，并更新下一档触发价”
  - 可选：每次触发加仓后自动撤销旧 TP 并重挂新 TP

## 4) 安全建议（重要）
建议不要把 api_key/api_secret 明文写在 config.yaml。
更安全的做法是改成从环境变量读取（例如 BINANCE_API_KEY / BINANCE_API_SECRET）。
