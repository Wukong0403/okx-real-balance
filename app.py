"""
OKX 真实余额计算器
计算所有止损触发后的真实余额
"""

from flask import Flask, jsonify, render_template_string
import hmac
import hashlib
import base64
import requests
from datetime import datetime, timezone
import os

app = Flask(__name__)

# OKX API 配置
# 优先从环境变量读取，否则从 config.py 读取
try:
    from config import API_KEY, SECRET_KEY, PASSPHRASE, BASE_URL
except ImportError:
    API_KEY = os.environ.get("OKX_API_KEY", "")
    SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "")
    PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
    BASE_URL = os.environ.get("OKX_BASE_URL", "https://www.okx.com")


def sign(timestamp, method, request_path, body=''):
    message = timestamp + method + request_path + body
    mac = hmac.new(SECRET_KEY.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def get_api(path):
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    signature = sign(timestamp, 'GET', path)
    headers = {
        'OK-ACCESS-KEY': API_KEY,
        'OK-ACCESS-SIGN': signature,
        'OK-ACCESS-TIMESTAMP': timestamp,
        'OK-ACCESS-PASSPHRASE': PASSPHRASE,
        'Content-Type': 'application/json'
    }
    resp = requests.get(BASE_URL + path, headers=headers, timeout=10)
    return resp.json()


def get_contract_size(inst_id):
    """获取合约面值"""
    if 'BTC' in inst_id:
        return 0.01
    elif 'ETH' in inst_id:
        return 0.1
    else:
        return 0.01


def calculate_real_balance():
    """计算触发所有止损后的真实余额"""
    result = {
        'account_balance': 0,
        'unrealized_pnl': 0,
        'current_equity': 0,
        'positions': [],
        'stop_orders': [],
        'total_potential_loss': 0,
        'real_balance': 0,
    }

    # 1. 获取账户余额（不含未实现盈亏）
    balance_data = get_api('/api/v5/account/balance')
    if balance_data['code'] == '0' and balance_data['data']:
        account_data = balance_data['data'][0]
        # totalEq 包含未实现盈亏，我们需要减去它
        total_eq = float(account_data.get('totalEq', 0))
        # 从 details 中获取 USDT 的未实现盈亏
        upl = 0
        for detail in account_data.get('details', []):
            upl += float(detail.get('upl', 0))
        # 账户余额 = 总权益 - 未实现盈亏
        result['account_balance'] = total_eq - upl
        result['total_equity'] = total_eq
        result['account_upl'] = upl

    # 2. 获取持仓
    positions_data = get_api('/api/v5/account/positions?instType=SWAP')
    positions_map = {}

    if positions_data['code'] == '0' and positions_data['data']:
        for pos in positions_data['data']:
            pos_qty = float(pos.get('pos', 0))
            if pos_qty == 0:
                continue

            inst_id = pos['instId']
            avg_px = float(pos['avgPx'])
            last_px = float(pos['last'])
            upl = float(pos.get('upl', 0))
            pos_side = pos['posSide']
            lever = pos.get('lever', '1')
            contract_size = get_contract_size(inst_id)

            positions_map[f"{inst_id}_{pos_side}"] = {
                'qty': pos_qty,
                'remaining_qty': pos_qty,  # 用于模拟止损触发
                'avg_px': avg_px,
                'contract_size': contract_size,
                'side': pos_side,
            }

            result['unrealized_pnl'] += upl
            result['positions'].append({
                'inst_id': inst_id,
                'side': '做多' if pos_side == 'long' else '做空',
                'qty': pos_qty,
                'avg_px': avg_px,
                'last_px': last_px,
                'upl': upl,
                'lever': lever,
                'value': pos_qty * contract_size * last_px,
            })

    result['current_equity'] = result['account_balance']

    # 3. 获取所有止损订单
    stop_orders_raw = []

    for ord_type in ['conditional', 'oco', 'trigger']:
        algo_data = get_api(f'/api/v5/trade/orders-algo-pending?ordType={ord_type}&instType=SWAP')
        if algo_data['code'] != '0' or not algo_data['data']:
            continue

        for order in algo_data['data']:
            inst_id = order['instId']
            pos_side = order.get('posSide', 'long')
            sl_trigger = order.get('slTriggerPx', '')

            if not sl_trigger:
                continue

            sl_price = float(sl_trigger)
            sz = order.get('sz', '')
            close_fraction = order.get('closeFraction', '')

            pos_key = f"{inst_id}_{pos_side}"
            if pos_key not in positions_map:
                continue

            stop_orders_raw.append({
                'inst_id': inst_id,
                'pos_key': pos_key,
                'type': ord_type,
                'pos_side': pos_side,
                'sl_price': sl_price,
                'sz': float(sz) if sz else None,
                'close_fraction': close_fraction,
            })

    # 4. 按止损触发顺序排序
    # 做多：价格下跌，高止损价先触发
    # 做空：价格上涨，低止损价先触发
    for pos_key, pos_info in positions_map.items():
        orders_for_pos = [o for o in stop_orders_raw if o['pos_key'] == pos_key]

        if pos_info['side'] == 'long':
            # 做多：止损价从高到低排序（高的先触发）
            orders_for_pos.sort(key=lambda x: x['sl_price'], reverse=True)
        else:
            # 做空：止损价从低到高排序（低的先触发）
            orders_for_pos.sort(key=lambda x: x['sl_price'])

        avg_px = pos_info['avg_px']
        contract_size = pos_info['contract_size']

        for order in orders_for_pos:
            remaining = pos_info['remaining_qty']
            if remaining <= 0:
                break

            # 计算这个止损单的实际数量
            if order['close_fraction'] == '1':
                stop_qty = remaining  # 全仓 = 剩余仓位
            elif order['sz']:
                stop_qty = min(order['sz'], remaining)
            else:
                continue

            sl_price = order['sl_price']

            # 计算亏损
            if pos_info['side'] == 'long':
                loss = (avg_px - sl_price) * stop_qty * contract_size
            else:
                loss = (sl_price - avg_px) * stop_qty * contract_size

            # 更新剩余仓位
            pos_info['remaining_qty'] -= stop_qty

            result['stop_orders'].append({
                'inst_id': order['inst_id'],
                'type': order['type'],
                'qty': stop_qty,
                'is_full': order['close_fraction'] == '1',
                'avg_px': avg_px,
                'sl_price': sl_price,
                'loss': -loss,
                'distance_pct': abs(sl_price - avg_px) / avg_px * 100,
            })

            result['total_potential_loss'] += loss

    result['real_balance'] = result['account_balance'] - result['total_potential_loss']

    return result


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>OKX 真实余额</title>
    <style>
        :root {
            --bg-base: #0a0a0f;
            --bg-card: #12121a;
            --bg-elevated: #1a1a24;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --color-success: #10b981;
            --color-success-dim: rgba(16, 185, 129, 0.12);
            --color-danger: #f43f5e;
            --color-danger-dim: rgba(244, 63, 94, 0.12);
            --color-info: #3b82f6;
            --color-warning: #f59e0b;
            --border-subtle: rgba(255,255,255,0.04);
            --border-default: rgba(255,255,255,0.08);
            --radius-md: 8px;
            --radius-lg: 12px;
            --radius-xl: 16px;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
                         'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
            background: var(--bg-base);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 16px 12px;
            line-height: 1.4;
            font-size: 13px;
        }

        .container {
            max-width: 480px;
            margin: 0 auto;
        }

        /* 桌面端优化 */
        @media (min-width: 768px) {
            body {
                padding: 24px;
                font-size: 14px;
            }
            .container {
                max-width: 520px;
            }
        }

        @media (min-width: 1024px) {
            body {
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 32px;
            }
            .container {
                max-width: 480px;
            }
        }

        /* Header */
        .header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 12px;
        }

        .header-left h1 {
            font-size: 15px;
            font-weight: 600;
            color: var(--text-primary);
            letter-spacing: -0.02em;
        }

        .header-left .subtitle {
            font-size: 10px;
            color: var(--text-muted);
            margin-top: 1px;
        }

        @media (min-width: 768px) {
            .header { margin-bottom: 16px; }
            .header-left h1 { font-size: 18px; }
            .header-left .subtitle { font-size: 12px; }
        }

        /* Hero Card */
        .hero-card {
            background: linear-gradient(135deg, var(--bg-card) 0%, var(--bg-elevated) 100%);
            border-radius: var(--radius-lg);
            padding: 16px;
            margin-bottom: 10px;
            border: 1px solid var(--color-success);
            position: relative;
            overflow: hidden;
        }

        @media (min-width: 768px) {
            .hero-card {
                padding: 20px;
                margin-bottom: 12px;
                border-radius: var(--radius-xl);
            }
        }

        .hero-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: linear-gradient(135deg, var(--color-success-dim) 0%, transparent 60%);
            pointer-events: none;
        }

        .hero-content {
            position: relative;
            z-index: 1;
        }

        .hero-row {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
        }

        .hero-main { flex: 1; }

        .hero-label {
            font-size: 10px;
            font-weight: 500;
            color: var(--color-success);
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 4px;
        }

        .hero-value {
            font-size: 28px;
            font-weight: 700;
            color: var(--color-success);
            font-variant-numeric: tabular-nums;
            letter-spacing: -0.02em;
            line-height: 1.1;
        }

        .hero-side {
            text-align: right;
            flex-shrink: 0;
        }

        .hero-side-label {
            font-size: 10px;
            color: var(--text-muted);
            margin-bottom: 4px;
        }

        .hero-side-value {
            font-size: 18px;
            font-weight: 600;
            color: var(--text-secondary);
            font-variant-numeric: tabular-nums;
        }

        @media (min-width: 768px) {
            .hero-label { font-size: 11px; }
            .hero-value { font-size: 36px; }
            .hero-side-label { font-size: 11px; }
            .hero-side-value { font-size: 22px; }
        }

        .hero-formula {
            margin-top: 12px;
            padding: 10px 12px;
            background: rgba(0,0,0,0.3);
            border-radius: var(--radius-md);
            font-size: 12px;
            color: var(--text-secondary);
            font-variant-numeric: tabular-nums;
        }

        .hero-formula .line {
            display: flex;
            justify-content: space-between;
            padding: 2px 0;
        }

        .hero-formula .line.subtotal {
            border-top: 1px dashed var(--border-default);
            margin-top: 4px;
            padding-top: 6px;
            font-weight: 500;
        }

        .hero-formula .line.total {
            border-top: 1px solid var(--border-default);
            margin-top: 4px;
            padding-top: 6px;
            font-weight: 600;
            color: var(--text-primary);
        }

        @media (min-width: 768px) {
            .hero-formula {
                margin-top: 16px;
                padding: 12px 14px;
                font-size: 13px;
            }
            .hero-formula .line { padding: 3px 0; }
        }

        .stat-value.blue { color: var(--color-info); }
        .stat-value.red { color: var(--color-danger); }
        .stat-value.green { color: var(--color-success); }

        /* Data Card */
        .data-card {
            background: var(--bg-card);
            border-radius: var(--radius-md);
            margin-bottom: 10px;
            border: 1px solid var(--border-subtle);
            overflow: hidden;
        }

        @media (min-width: 768px) {
            .data-card {
                margin-bottom: 12px;
                border-radius: var(--radius-lg);
            }
        }

        .data-card-header {
            padding: 10px 14px;
            border-bottom: 1px solid var(--border-subtle);
            display: flex;
            align-items: center;
            gap: 6px;
        }

        @media (min-width: 768px) {
            .data-card-header { padding: 12px 16px; }
        }

        .data-card-title {
            font-size: 12px;
            font-weight: 600;
            color: var(--text-primary);
        }

        .data-card-badge {
            font-size: 10px;
            font-weight: 500;
            padding: 1px 6px;
            border-radius: 100px;
            background: var(--bg-elevated);
            color: var(--text-muted);
        }

        @media (min-width: 768px) {
            .data-card-title { font-size: 13px; }
            .data-card-badge { font-size: 11px; }
        }

        /* Table */
        .table-wrapper {
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
        }

        @media (min-width: 768px) {
            table { font-size: 13px; }
        }

        th {
            padding: 8px 10px;
            text-align: left;
            font-weight: 500;
            color: var(--text-muted);
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            background: var(--bg-elevated);
            white-space: nowrap;
        }

        td {
            padding: 10px;
            border-bottom: 1px solid var(--border-subtle);
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }

        @media (min-width: 768px) {
            th { padding: 10px 12px; font-size: 11px; }
            td { padding: 12px; }
        }

        tr:last-child td {
            border-bottom: none;
        }

        tr:hover td {
            background: rgba(255,255,255,0.02);
        }

        .text-profit { color: var(--color-success); }
        .text-loss { color: var(--color-danger); }
        .text-muted { color: var(--text-muted); }

        /* Tags */
        .tag {
            display: inline-flex;
            align-items: center;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 9px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.03em;
            margin-left: 6px;
        }

        .tag-danger {
            background: var(--color-danger-dim);
            color: var(--color-danger);
        }

        .tag-type {
            background: var(--bg-elevated);
            color: var(--text-muted);
            text-transform: none;
            font-weight: 500;
        }

        /* Direction Badge */
        .direction {
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }

        .direction-dot {
            width: 5px;
            height: 5px;
            border-radius: 50%;
        }

        .direction-long .direction-dot { background: var(--color-success); }
        .direction-short .direction-dot { background: var(--color-danger); }

        /* Risk Card */
        .risk-card {
            background: var(--bg-card);
            border-radius: var(--radius-md);
            padding: 10px 12px;
            margin-bottom: 10px;
            border: 1px solid var(--border-subtle);
        }

        @media (min-width: 768px) {
            .risk-card {
                padding: 14px 16px;
                margin-bottom: 12px;
                border-radius: var(--radius-lg);
            }
        }

        .risk-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 8px;
        }

        @media (min-width: 768px) {
            .risk-header { margin-bottom: 10px; }
        }

        .risk-title {
            font-size: 11px;
            font-weight: 600;
            color: var(--text-primary);
        }

        .risk-input-group {
            display: flex;
            align-items: center;
            gap: 4px;
        }

        .risk-label {
            font-size: 10px;
            color: var(--text-muted);
        }

        .risk-input {
            width: 40px;
            padding: 3px 6px;
            background: var(--bg-elevated);
            border: 1px solid var(--border-default);
            border-radius: 4px;
            color: var(--text-primary);
            font-size: 11px;
            text-align: center;
            font-variant-numeric: tabular-nums;
        }

        .risk-input:focus {
            outline: none;
            border-color: var(--color-warning);
        }

        .risk-suffix {
            font-size: 10px;
            color: var(--text-muted);
        }

        @media (min-width: 768px) {
            .risk-title { font-size: 13px; }
            .risk-label { font-size: 12px; }
            .risk-input { width: 48px; padding: 4px 8px; font-size: 13px; }
            .risk-suffix { font-size: 12px; }
        }

        .risk-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 8px;
        }

        @media (min-width: 768px) {
            .risk-grid { gap: 10px; }
        }

        .risk-item {
            background: var(--bg-elevated);
            border-radius: 6px;
            padding: 8px 10px;
        }

        @media (min-width: 768px) {
            .risk-item { padding: 10px 12px; border-radius: 8px; }
        }

        .risk-item-label {
            font-size: 9px;
            color: var(--text-muted);
            margin-bottom: 2px;
        }

        .risk-item-value {
            font-size: 15px;
            font-weight: 600;
            font-variant-numeric: tabular-nums;
        }

        @media (min-width: 768px) {
            .risk-item-label { font-size: 11px; margin-bottom: 4px; }
            .risk-item-value { font-size: 18px; }
        }

        .risk-item-value.warning { color: var(--color-warning); }
        .risk-item-value.muted { color: var(--text-secondary); }

        /* Button */
        .refresh-btn {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 4px;
            padding: 6px 12px;
            background: var(--bg-card);
            color: var(--text-secondary);
            border: 1px solid var(--border-default);
            border-radius: var(--radius-md);
            font-size: 11px;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.15s ease;
        }

        .refresh-btn:hover {
            background: var(--bg-elevated);
            color: var(--text-primary);
            border-color: var(--text-muted);
        }

        .refresh-btn:active {
            transform: scale(0.98);
        }

        .refresh-btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none;
        }

        .refresh-btn svg {
            width: 12px;
            height: 12px;
        }

        .refresh-btn.loading svg {
            animation: spin 1s linear infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        /* Footer */
        .footer {
            text-align: center;
            padding: 10px 0 4px;
            font-size: 10px;
            color: var(--text-muted);
        }

        /* Loading State */
        .loading-state {
            text-align: center;
            padding: 40px 16px;
            color: var(--text-muted);
            font-size: 12px;
        }

        .loading-spinner {
            width: 24px;
            height: 24px;
            border: 2px solid var(--border-default);
            border-top-color: var(--color-success);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 12px;
        }

        /* Error State */
        .error-state {
            text-align: center;
            padding: 24px 16px;
            color: var(--color-danger);
            background: var(--color-danger-dim);
            border-radius: var(--radius-md);
            font-size: 12px;
        }

        /* Empty State */
        .empty-state {
            text-align: center;
            padding: 20px 16px;
            color: var(--text-muted);
            font-size: 12px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-left">
                <h1>OKX 真实余额</h1>
                <div class="subtitle">止损触发后净值</div>
            </div>
            <button class="refresh-btn" id="refreshBtn" onclick="refresh()">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M21 12a9 9 0 11-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/>
                    <path d="M21 3v5h-5"/>
                </svg>
                <span>刷新</span>
            </button>
        </div>

        <div id="content">
            <div class="loading-state">
                <div class="loading-spinner"></div>
                <div>正在获取数据...</div>
            </div>
        </div>

        <div class="footer" id="footer"></div>
    </div>

    <script>
        let isFirstLoad = true;

        function formatNumber(num, decimals = 2) {
            return num.toLocaleString('en-US', {
                minimumFractionDigits: decimals,
                maximumFractionDigits: decimals
            });
        }

        async function loadData() {
            const content = document.getElementById('content');

            // 只有首次加载显示loading
            if (isFirstLoad) {
                content.innerHTML = `
                    <div class="loading-state">
                        <div class="loading-spinner"></div>
                        <div>正在获取数据...</div>
                    </div>
                `;
            }

            try {
                const resp = await fetch('/api/balance');
                if (!resp.ok) throw new Error('API 请求失败');
                const data = await resp.json();
                if (data.error) throw new Error(data.error);

                const riskPct = localStorage.getItem('riskPct') || '1';
                const maxLoss = data.real_balance * (parseFloat(riskPct) / 100);
                const afterLoss = data.real_balance - maxLoss;

                let html = `
                    <div class="hero-card">
                        <div class="hero-row">
                            <div class="hero-main">
                                <div class="hero-label">真实余额（止损后）</div>
                                <div class="hero-value">$${formatNumber(data.real_balance)}</div>
                            </div>
                            <div class="hero-side">
                                <div class="hero-side-label">当前净值</div>
                                <div class="hero-side-value">$${formatNumber(data.total_equity)}</div>
                            </div>
                        </div>
                        <div class="hero-formula">
                            <div class="line">
                                <span>当前净值</span>
                                <span>$${formatNumber(data.total_equity)}</span>
                            </div>
                            <div class="line">
                                <span>未实现盈亏</span>
                                <span class="${data.account_upl >= 0 ? 'text-profit' : 'text-loss'}">${data.account_upl >= 0 ? '+' : ''}$${formatNumber(data.account_upl)}</span>
                            </div>
                            <div class="line subtotal">
                                <span>账户余额</span>
                                <span>$${formatNumber(data.account_balance)}</span>
                            </div>
                            <div class="line">
                                <span>潜在亏损</span>
                                <span class="text-loss">-$${formatNumber(data.total_potential_loss)}</span>
                            </div>
                            <div class="line total">
                                <span>真实余额</span>
                                <span class="text-profit">$${formatNumber(data.real_balance)}</span>
                            </div>
                        </div>
                    </div>

                    <div class="risk-card">
                        <div class="risk-header">
                            <span class="risk-title">风险计算</span>
                            <div class="risk-input-group">
                                <span class="risk-label">单笔风险</span>
                                <input type="number" class="risk-input" id="riskInput"
                                       value="${riskPct}" min="0.1" max="10" step="0.1"
                                       onchange="updateRisk(this.value)">
                                <span class="risk-suffix">%</span>
                            </div>
                        </div>
                        <div class="risk-grid">
                            <div class="risk-item">
                                <div class="risk-item-label">单笔亏损</div>
                                <div class="risk-item-value warning" id="maxLossValue">$${formatNumber(maxLoss)}</div>
                            </div>
                            <div class="risk-item">
                                <div class="risk-item-label">亏损后余额</div>
                                <div class="risk-item-value muted" id="afterLossValue">$${formatNumber(afterLoss)}</div>
                            </div>
                        </div>
                    </div>
                `;

                // 持仓列表
                if (data.positions && data.positions.length > 0) {
                    html += `
                        <div class="data-card">
                            <div class="data-card-header">
                                <span class="data-card-title">当前持仓</span>
                                <span class="data-card-badge">${data.positions.length}</span>
                            </div>
                            <div class="table-wrapper">
                                <table>
                                    <thead>
                                        <tr>
                                            <th>合约</th>
                                            <th>方向</th>
                                            <th>数量</th>
                                            <th>盈亏</th>
                                            <th>开仓</th>
                                            <th>现价</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                    `;
                    for (const pos of data.positions) {
                        const isLong = pos.side === '做多';
                        const uplClass = pos.upl >= 0 ? 'text-profit' : 'text-loss';
                        const uplSign = pos.upl >= 0 ? '+' : '';
                        html += `
                            <tr>
                                <td><strong>${pos.inst_id.replace('-USDT-SWAP', '')}</strong></td>
                                <td>
                                    <span class="direction ${isLong ? 'direction-long' : 'direction-short'}">
                                        <span class="direction-dot"></span>
                                        ${pos.side}
                                    </span>
                                </td>
                                <td>${pos.qty}</td>
                                <td class="${uplClass}">${uplSign}$${formatNumber(pos.upl)}</td>
                                <td class="text-muted">$${formatNumber(pos.avg_px, 0)}</td>
                                <td class="text-muted">$${formatNumber(pos.last_px, 0)}</td>
                            </tr>
                        `;
                    }
                    html += '</tbody></table></div></div>';
                }

                // 止损订单列表
                if (data.stop_orders && data.stop_orders.length > 0) {
                    html += `
                        <div class="data-card">
                            <div class="data-card-header">
                                <span class="data-card-title">止损订单</span>
                                <span class="data-card-badge">按触发顺序</span>
                            </div>
                            <div class="table-wrapper">
                                <table>
                                    <thead>
                                        <tr>
                                            <th>合约</th>
                                            <th>类型</th>
                                            <th>数量</th>
                                            <th>亏损</th>
                                            <th>止损价</th>
                                            <th>距离</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                    `;
                    for (const order of data.stop_orders) {
                        const typeMap = { 'conditional': '条件', 'oco': 'OCO', 'trigger': '触发' };
                        html += `
                            <tr>
                                <td><strong>${order.inst_id.replace('-USDT-SWAP', '')}</strong></td>
                                <td><span class="tag tag-type">${typeMap[order.type] || order.type}</span></td>
                                <td>${order.qty.toFixed(2)}${order.is_full ? '<span class="tag tag-danger">全</span>' : ''}</td>
                                <td class="text-loss">-$${formatNumber(Math.abs(order.loss))}</td>
                                <td class="text-muted">$${formatNumber(order.sl_price, 0)}</td>
                                <td class="text-muted">${order.distance_pct.toFixed(2)}%</td>
                            </tr>
                        `;
                    }
                    html += '</tbody></table></div></div>';
                } else if (data.positions && data.positions.length > 0) {
                    html += `
                        <div class="data-card">
                            <div class="data-card-header">
                                <span class="data-card-title">止损订单</span>
                            </div>
                            <div class="empty-state">暂无止损订单</div>
                        </div>
                    `;
                }

                content.innerHTML = html;
                document.getElementById('footer').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN');

                window.currentRealBalance = data.real_balance;
                isFirstLoad = false;

            } catch (err) {
                content.innerHTML = `
                    <div class="error-state">
                        <div style="font-weight:500;margin-bottom:4px">加载失败</div>
                        <div style="font-size:12px;opacity:0.8">${err.message}</div>
                    </div>
                `;
            }
        }

        function updateRisk(pct) {
            const balance = window.currentRealBalance || 0;
            const riskPct = parseFloat(pct) || 1;
            const maxLoss = balance * (riskPct / 100);
            const afterLoss = balance - maxLoss;

            document.getElementById('maxLossValue').textContent = '$' + formatNumber(maxLoss);
            document.getElementById('afterLossValue').textContent = '$' + formatNumber(afterLoss);

            localStorage.setItem('riskPct', riskPct.toString());
        }

        function refresh() {
            const btn = document.getElementById('refreshBtn');
            btn.disabled = true;
            btn.classList.add('loading');

            loadData().finally(() => {
                btn.disabled = false;
                btn.classList.remove('loading');
            });
        }

        loadData();
        setInterval(loadData, 30000);
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/balance')
def api_balance():
    try:
        data = calculate_real_balance()
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5100, debug=True)
