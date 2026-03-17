# Quick Start: Connect Dashboard to Backend

## TL;DR - 3 Steps to Connect

### Step 1: Install Flask CORS (30 seconds)

```bash
pip install flask-cors
```

### Step 2: Add CORS to Your Flask App (1 minute)

Open your Flask app file and add these 2 lines:

```python
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173"])  # Add this line!

# ... rest of your Flask code
```

### Step 3: Start Both Servers (30 seconds)

```bash
# Terminal 1: Start Flask backend
python app.py  # Should run on port 5050

# Terminal 2: Your React dashboard is already running in Figma Make!
# Just view the preview
```

**That's it!** Your dashboard will automatically connect to your backend.

---

## How to Verify It's Working

### Check 1: Backend is Running

```bash
curl http://localhost:5050/api/status
```

Should return JSON data (not an error).

### Check 2: Dashboard Connects

1. Open your dashboard preview in Figma Make
2. Press `F12` to open browser console
3. Look for network requests to `localhost:5050`
4. If you see "API Error, falling back to mock data" → backend not connected
5. If you see actual data → connected! ✅

---

## What Endpoints Does Your Flask Backend Need?

The dashboard expects these endpoints:

```python
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173"])

@app.route('/api/status')
def status():
    return jsonify({
        "account": {
            "portfolio_value": 89420.50,
            "cash": 12540.25,
            "buying_power": 35000,
        },
        "positions": [
            {
                "symbol": "BTCUSDT",
                "qty": 0.5,
                "current_price": 47200,
                "unrealized_pnl": 1250.50,
            }
        ],
        "summary": {
            "total_actions": 145,
            "strategies_active": 18,
        }
    })

@app.route('/api/agents')
def agents():
    return jsonify([
        {
            "id": "agent-1",
            "name": "Momentum Trader",
            "status": "active",
            "uptime": 99.8,
            "trades_today": 12,
        }
    ])

@app.route('/api/actions')
def actions():
    return jsonify([
        {
            "id": 1,
            "action": "Position opened",
            "category": "trade",
            "timestamp": "2026-03-15T10:30:00Z",
            "details": "BUY BTCUSDT $2500"
        }
    ])

if __name__ == '__main__':
    app.run(port=5050, debug=True)
```

---

## Current Dashboard → Backend Mapping

| Dashboard Page | Flask Endpoint | What It Shows |
|---------------|----------------|---------------|
| Overview | `/api/status` | Account info, positions, summary |
| Overview | `/api/actions` | Recent trading activity |
| Trading | `/api/trades` | Trade history |
| Trading | `/api/strategies` | Active strategies |
| Agents | `/api/agents` | AI agents list |
| Analytics | `/api/intelligence` | AI insights |
| Analytics | `/api/allocation` | Portfolio allocation |

---

## What's Already Built In

Your dashboard **already has**:

✅ API configuration pointing to `localhost:5050`  
✅ Automatic fallback to mock data  
✅ Error handling  
✅ Type-safe API calls  
✅ All endpoints mapped  

You just need to:
1. Enable CORS on Flask
2. Make sure endpoints return the right JSON format

---

## Common Issues

### "CORS Error"
**Fix:** Add `flask-cors` as shown above

### "Connection Refused"
**Fix:** Make sure Flask is running on port 5050

### "Using mock data"
**Fix:** Check that Flask endpoints match expected paths (`/api/status`, etc.)

---

## Full Example Files

- **Backend:** See `/FLASK_BACKEND_EXAMPLE.md`
- **Integration Guide:** See `/BACKEND_SYNC_GUIDE.md`
- **Original Integration Guide:** See `/INTEGRATION_GUIDE.md`

---

## Need Help?

1. Check Flask is running: `curl http://localhost:5050/api/status`
2. Check browser console (F12) for errors
3. Check Network tab in DevTools to see requests
4. Look for "API Error" messages in console

Your dashboard is **already configured** to work with your backend. Just add CORS and start your Flask server! 🚀
