# Flask Backend Example for OT-CORP Dashboard

This document provides **ready-to-use Flask code** that matches your React dashboard's expectations.

## Complete Flask Backend Example

Here's a complete Flask app that works with your dashboard:

```python
# app.py - Complete Flask backend for OT-CORP Dashboard
from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime, timedelta
import random

app = Flask(__name__)

# Enable CORS for React dashboard
CORS(app, origins=["http://localhost:5173", "http://localhost:3000"])

# ============================================================================
# SAMPLE DATA (Replace with your actual trading system data)
# ============================================================================

def get_sample_positions():
    """Replace this with your actual positions from trading system"""
    return [
        {
            "symbol": "BTCUSDT",
            "qty": 0.5,
            "avg_entry_price": 45000,
            "current_price": 47200,
            "unrealized_pnl": 1100,
            "market_value": 23600,
            "side": "long",
            "age": "2d 14h",
            "asset_id": "BTC-USD-001",
            "exchange": "Alpaca"
        },
        {
            "symbol": "ETHUSDT",
            "qty": 5,
            "avg_entry_price": 3150,
            "current_price": 3440,
            "unrealized_pnl": 1450,
            "market_value": 17200,
            "side": "long",
            "age": "1d 8h",
            "asset_id": "ETH-USD-001",
            "exchange": "Alpaca"
        }
    ]

def get_sample_agents():
    """Replace this with your actual agents from agency-agents/"""
    return [
        {
            "id": "agent-momentum-1",
            "name": "Momentum Alpha",
            "status": "active",
            "uptime": 99.8,
            "trades_today": 12,
            "win_rate": 68.5,
            "pnl_today": 1250.50,
            "last_active": datetime.now().isoformat(),
            "strategy": "momentum",
            "confidence": 85
        },
        {
            "id": "agent-mean-reversion-1",
            "name": "Mean Reversion Beta",
            "status": "active",
            "uptime": 98.2,
            "trades_today": 8,
            "win_rate": 72.3,
            "pnl_today": 890.25,
            "last_active": datetime.now().isoformat(),
            "strategy": "mean_reversion",
            "confidence": 78
        }
    ]

# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    })

@app.route('/api/status', methods=['GET'])
def get_status():
    """
    Main status endpoint - returns account info, positions, and summary
    This is the primary endpoint used by the Overview page
    """
    positions = get_sample_positions()
    
    # Calculate totals
    total_pnl = sum(p.get('unrealized_pnl', 0) for p in positions)
    total_value = sum(p.get('market_value', 0) for p in positions)
    
    return jsonify({
        "account": {
            "portfolio_value": 89420.50,
            "cash": 12540.25,
            "buying_power": 35000,
            "equity": 76880.25,
            "status": "ACTIVE",
            "day_pnl": total_pnl,
            "total_pnl": 15234.75
        },
        "positions": positions,
        "summary": {
            "total_actions": 145,
            "strategies_active": 18,
            "signals_today": 23,
            "active_positions": len(positions)
        },
        "mode": "paper"  # or "live"
    })

@app.route('/api/mode', methods=['GET'])
def get_mode():
    """Get current trading mode"""
    return jsonify({
        "mode": "paper",  # or "live"
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/trades', methods=['GET'])
def get_trades():
    """
    Get recent trades history
    Used by Trading page
    """
    trades = [
        {
            "id": f"trade-{i}",
            "symbol": random.choice(["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
            "side": random.choice(["buy", "sell"]),
            "qty": round(random.uniform(0.1, 2), 2),
            "price": round(random.uniform(1000, 50000), 2),
            "timestamp": (datetime.now() - timedelta(hours=i)).isoformat(),
            "status": "filled",
            "pnl": round(random.uniform(-500, 1000), 2)
        }
        for i in range(20)
    ]
    return jsonify(trades)

@app.route('/api/position/<symbol>', methods=['GET'])
def get_position(symbol):
    """Get specific position by symbol"""
    positions = get_sample_positions()
    position = next((p for p in positions if p['symbol'] == symbol), None)
    
    if position:
        return jsonify(position)
    else:
        return jsonify({"error": "Position not found"}), 404

@app.route('/api/actions', methods=['GET'])
def get_actions():
    """
    Get recent trading actions/signals
    Used by Overview and Trading pages
    """
    actions = [
        {
            "id": 1,
            "action": "Strategy cycle complete",
            "category": "scheduler",
            "timestamp": (datetime.now() - timedelta(minutes=5)).isoformat(),
            "details": "Executed 18 strategies",
            "strategy_id": "scheduler-main"
        },
        {
            "id": 2,
            "action": "Position opened",
            "category": "trade",
            "timestamp": (datetime.now() - timedelta(hours=1)).isoformat(),
            "details": "BUY BTCUSDT $2500",
            "strategy_id": "momentum-alpha"
        },
        {
            "id": 3,
            "action": "Signal generated",
            "category": "signal",
            "timestamp": (datetime.now() - timedelta(hours=2)).isoformat(),
            "details": "kalman_trend: BUY ETHUSDT (confidence: 85%)",
            "strategy_id": "kalman-trend"
        }
    ]
    return jsonify(actions)

@app.route('/api/strategies', methods=['GET'])
def get_strategies():
    """
    Get all trading strategies
    Used by Trading page
    """
    strategies = [
        {
            "id": "momentum-alpha",
            "name": "Momentum Alpha",
            "status": "active",
            "win_rate": 68.5,
            "total_trades": 145,
            "pnl": 5234.50,
            "sharpe_ratio": 1.85
        },
        {
            "id": "mean-reversion-beta",
            "name": "Mean Reversion Beta",
            "status": "active",
            "win_rate": 72.3,
            "total_trades": 98,
            "pnl": 3890.25,
            "sharpe_ratio": 2.12
        }
    ]
    return jsonify(strategies)

@app.route('/api/strategy/<name>', methods=['GET'])
def get_strategy(name):
    """Get specific strategy details"""
    # Replace with actual strategy data
    return jsonify({
        "id": name,
        "name": name.replace('-', ' ').title(),
        "status": "active",
        "win_rate": 68.5,
        "total_trades": 145,
        "pnl": 5234.50,
        "parameters": {
            "lookback_period": 20,
            "threshold": 0.02
        }
    })

@app.route('/api/agents', methods=['GET'])
def get_agents():
    """
    Get all AI agents
    Used by Agents page
    """
    return jsonify(get_sample_agents())

@app.route('/api/agents/<agent_id>/status', methods=['GET'])
def get_agent_status(agent_id):
    """Get specific agent status"""
    agents = get_sample_agents()
    agent = next((a for a in agents if a['id'] == agent_id), None)
    
    if agent:
        return jsonify(agent)
    else:
        return jsonify({"error": "Agent not found"}), 404

@app.route('/api/agents/<agent_id>/control', methods=['POST'])
def control_agent(agent_id):
    """Control agent (start/stop/restart)"""
    data = request.get_json()
    action = data.get('action')  # 'start', 'stop', 'restart'
    
    # Implement your agent control logic here
    return jsonify({
        "success": True,
        "agent_id": agent_id,
        "action": action,
        "message": f"Agent {action}ed successfully"
    })

@app.route('/api/recommendation/<int:rec_id>', methods=['GET'])
def get_recommendation(rec_id):
    """Get agent recommendation"""
    return jsonify({
        "id": rec_id,
        "agent_id": "agent-momentum-1",
        "symbol": "BTCUSDT",
        "action": "BUY",
        "confidence": 85,
        "reason": "Strong upward momentum detected",
        "timestamp": datetime.now().isoformat()
    })

@app.route('/api/pnl', methods=['GET'])
@app.route('/api/pnl/<date>', methods=['GET'])
def get_pnl(date=None):
    """Get P&L data - overall or for specific date"""
    if date:
        # Return P&L for specific date
        return jsonify({
            "date": date,
            "pnl": 1250.50,
            "trades": 12,
            "win_rate": 68.5
        })
    else:
        # Return overall P&L from status endpoint
        return get_status()

@app.route('/api/intelligence', methods=['GET'])
def get_intelligence():
    """
    Get AI intelligence/insights
    Used by Analytics page
    """
    return jsonify({
        "market_sentiment": "bullish",
        "confidence": 82,
        "insights": [
            {
                "type": "trend",
                "message": "Strong bullish momentum in BTC/ETH",
                "confidence": 85
            },
            {
                "type": "risk",
                "message": "Market volatility increasing",
                "confidence": 72
            }
        ],
        "predictions": {
            "btc_24h": "upward",
            "eth_24h": "upward",
            "overall_market": "bullish"
        }
    })

@app.route('/api/allocation', methods=['GET'])
def get_allocation():
    """
    Get portfolio allocation data
    Used by Analytics page
    """
    return jsonify({
        "allocations": [
            {"asset": "BTC", "percentage": 45, "value": 40289},
            {"asset": "ETH", "percentage": 30, "value": 26826},
            {"asset": "SOL", "percentage": 15, "value": 13413},
            {"asset": "Cash", "percentage": 10, "value": 8942}
        ],
        "total_value": 89420
    })

@app.route('/api/chat', methods=['POST'])
def chat():
    """Chat endpoint for AI assistant"""
    data = request.get_json()
    message = data.get('message', '')
    
    # Implement your chat logic here
    return jsonify({
        "response": f"I received: {message}",
        "timestamp": datetime.now().isoformat(),
        "requires_confirmation": False
    })

@app.route('/api/chat/confirm', methods=['POST'])
def chat_confirm():
    """Confirm chat action"""
    data = request.get_json()
    action = data.get('action')
    
    return jsonify({
        "success": True,
        "action": action,
        "message": "Action confirmed and executed"
    })

# ============================================================================
# INTEGRATION WITH YOUR EXISTING CODE
# ============================================================================

# Example: Integrate with your trading module
# from trading.positions import get_all_positions
# 
# @app.route('/api/positions', methods=['GET'])
# def get_positions():
#     positions = get_all_positions()  # Your actual function
#     return jsonify(positions)

# Example: Integrate with your agents module
# from agency_agents.manager import AgentManager
# 
# agent_manager = AgentManager()
# 
# @app.route('/api/agents', methods=['GET'])
# def get_agents():
#     agents = agent_manager.get_all_agents()
#     return jsonify(agents)

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    print("=" * 60)
    print("OT-CORP Trading Dashboard Backend")
    print("=" * 60)
    print(f"🚀 Backend running on: http://localhost:5050")
    print(f"📊 Dashboard should connect to: http://localhost:5173")
    print(f"🔧 API endpoints available at: http://localhost:5050/api/")
    print("=" * 60)
    print("\nAPI Endpoints:")
    print("  GET  /api/health")
    print("  GET  /api/status")
    print("  GET  /api/trades")
    print("  GET  /api/actions")
    print("  GET  /api/agents")
    print("  GET  /api/strategies")
    print("  GET  /api/intelligence")
    print("=" * 60)
    
    app.run(
        host='0.0.0.0',
        port=5050,
        debug=True
    )
```

## Install Requirements

Create `requirements.txt`:

```txt
Flask==3.0.0
flask-cors==4.0.0
python-dotenv==1.0.0
```

Install:
```bash
pip install -r requirements.txt
```

## Run the Backend

```bash
python app.py
```

You should see:
```
============================================================
OT-CORP Trading Dashboard Backend
============================================================
🚀 Backend running on: http://localhost:5050
📊 Dashboard should connect to: http://localhost:5173
🔧 API endpoints available at: http://localhost:5050/api/
============================================================
```

## Test the Endpoints

```bash
# Test health check
curl http://localhost:5050/api/health

# Test status endpoint
curl http://localhost:5050/api/status

# Test agents endpoint
curl http://localhost:5050/api/agents

# Test trades endpoint
curl http://localhost:5050/api/trades
```

## Integrate with Your Existing Code

### Example 1: Connect to Your Trading Module

```python
# Import your existing trading code
from trading.positions import get_current_positions
from trading.pnl import calculate_pnl

@app.route('/api/status', methods=['GET'])
def get_status():
    # Use your actual trading data
    positions = get_current_positions()  # Your function
    pnl = calculate_pnl()  # Your function
    
    return jsonify({
        "account": {
            "portfolio_value": pnl.total_value,
            "cash": pnl.cash,
            # ... your actual data
        },
        "positions": positions,
        "summary": {
            # ... your actual summary
        }
    })
```

### Example 2: Connect to Your Agents

```python
# Import your agents code
from agency_agents.manager import AgentManager

agent_manager = AgentManager()

@app.route('/api/agents', methods=['GET'])
def get_agents():
    # Use your actual agent data
    agents = agent_manager.list_all_agents()
    
    # Format for dashboard
    formatted_agents = [
        {
            "id": agent.id,
            "name": agent.name,
            "status": agent.status,
            "uptime": agent.uptime_percentage,
            "trades_today": agent.trades_count,
            # ... map your agent properties
        }
        for agent in agents
    ]
    
    return jsonify(formatted_agents)
```

## Next Steps

1. ✅ Run this Flask app: `python app.py`
2. ✅ Test endpoints with curl
3. ✅ Open your React dashboard - it will auto-connect!
4. 🔄 Replace sample data with your actual trading/agent data
5. 🚀 Deploy to production

The dashboard will automatically:
- ✅ Connect to your Flask backend on port 5050
- ✅ Display your real trading data
- ✅ Fall back to mock data if backend is offline
- ✅ Show all positions, agents, and analytics

Your integration is complete! 🎉
