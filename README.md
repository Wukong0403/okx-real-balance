# OKX 真实余额计算器

计算所有止损订单触发后的真实余额，帮助交易者了解最坏情况下的账户净值。

## 功能

- **真实余额计算** - 当前净值 - 未实现盈亏 - 潜在止损亏损 = 真实余额
- **止损模拟** - 按触发顺序计算每个止损订单的潜在亏损
- **风险计算器** - 可配置单笔风险百分比，计算单笔最大亏损
- **实时刷新** - 30秒自动刷新，手动刷新不闪烁
- **响应式设计** - 适配桌面端和移动端

## 计算逻辑

```
当前净值 (totalEq)
- 未实现盈亏 (upl)
= 账户余额

账户余额
- 潜在止损亏损 (按触发顺序计算)
= 真实余额
```

止损触发顺序：
- 做多：价格下跌，高止损价先触发
- 做空：价格上涨，低止损价先触发

## 部署

### 1. 安装依赖

```bash
pip install flask requests
```

### 2. 配置 API

编辑 `app.py`，填入你的 OKX API 密钥：

```python
API_KEY = "your-api-key"
SECRET_KEY = "your-secret-key"
PASSPHRASE = "your-passphrase"
```

### 3. 运行

```bash
python app.py
```

访问 http://localhost:5100

### 4. Systemd 服务 (可选)

```bash
sudo cp okx-balance.service /etc/systemd/system/
sudo systemctl enable okx-balance
sudo systemctl start okx-balance
```

## 截图

桌面端和移动端均支持，深色主题。

## 注意事项

- 仅支持 SWAP 永续合约
- 需要 OKX API 的读取权限
- 不会执行任何交易操作
