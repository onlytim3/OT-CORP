# Quick Reference Card

Keep this handy while integrating your dashboard with Flask backend.

## 📋 3-Step Setup

```bash
# 1. Install CORS
pip install flask-cors

# 2. Add to Flask
from flask_cors import CORS
CORS(app, origins=["http://localhost:5173"])

# 3. Start Flask
python app.py
```

**Done!** Dashboard auto-connects.

---

## 🔌 Connection Info

| Component | URL | Port |
|-----------|-----|------|
| **React Dashboard** | `http://localhost:5173` | 5173 |
| **Flask Backend** | `http://localhost:5050` | 5050 |
| **API Endpoint** | `http://localhost:5050/api/...` | 5050 |

---

## 📡 Required Flask Endpoints

| Endpoint | Method | Used By |
|----------|--------|---------|
| `/api/status` | GET | Overview |
| `/api/trades` | GET | Trading |
| `/api/agents` | GET | Agents |
| `/api/actions` | GET | Overview |
| `/api/strategies` | GET | Trading |
| `/api/intelligence` | GET | Analytics |
| `/api/allocation` | GET | Analytics |

---

## 🧪 Testing Commands

```bash
# Test backend is running
curl http://localhost:5050/api/status

# Test with CORS headers
curl -H "Origin: http://localhost:5173" \
     http://localhost:5050/api/status

# Pretty print JSON response
curl http://localhost:5050/api/status | python -m json.tool

# Check what's using port 5050
lsof -i :5050  # Mac/Linux
netstat -ano | findstr :5050  # Windows
```

---

## 🔍 Debugging Checklist

**Browser Console (F12 → Console):**
```
✅ No errors = Connected!
❌ "API Error, falling back to mock data" = Not connected
❌ "CORS policy" = CORS not enabled
❌ "ERR_CONNECTION_REFUSED" = Flask not running
```

**Network Tab (F12 → Network):**
```
✅ Requests to localhost:5050 = Connecting
✅ Status 200 = Success
❌ Status 404 = Endpoint missing
❌ Status 500 = Flask error
❌ (failed) = Flask not running
```

**Flask Terminal:**
```
✅ "GET /api/status HTTP/1.1" 200 = Working
❌ "GET /api/status HTTP/1.1" 404 = Route missing
❌ "GET /api/status HTTP/1.1" 500 = Error in code
```

---

## 📝 Flask Minimal Example

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
        }
    ])

if __name__ == '__main__':
    app.run(port=5050, debug=True)
```

---

## 🚨 Common Errors & Fixes

### CORS Error
```
Error: blocked by CORS policy
Fix:  pip install flask-cors
      Add CORS(app) to Flask
```

### Connection Refused
```
Error: ERR_CONNECTION_REFUSED
Fix:  Start Flask: python app.py
      Check port 5050 is free
```

### 404 Not Found
```
Error: GET /api/status 404
Fix:  Add @app.route('/api/status') to Flask
```

### Wrong Port
```
Error: Dashboard uses wrong port
Fix:  Create .env:
      VITE_API_URL=http://localhost:YOUR_PORT
```

---

## 📊 Response Formats

### /api/status
```json
{
  "account": { "portfolio_value": <num>, "cash": <num> },
  "positions": [{ "symbol": <str>, "qty": <num>, ... }],
  "summary": { "total_actions": <num>, ... }
}
```

### /api/agents
```json
[
  {
    "id": <str>,
    "name": <str>,
    "status": <str>,
    "uptime": <num>,
    "trades_today": <num>
  }
]
```

### /api/trades
```json
[
  {
    "id": <str>,
    "symbol": <str>,
    "side": <str>,
    "qty": <num>,
    "price": <num>,
    "timestamp": <str>
  }
]
```

---

## 🛠️ Environment Variables

Create `.env` in dashboard root:

```bash
# Custom API URL
VITE_API_URL=http://localhost:YOUR_PORT

# Debug mode
VITE_DEBUG=true
```

**Remember:** Restart dashboard after changing .env!

---

## 📚 Documentation Files

| File | When to Use |
|------|-------------|
| **QUICK_START.md** | First time setup |
| **QUICK_REFERENCE.md** | This file - quick lookup |
| **HOW_IT_WORKS.md** | Understanding the system |
| **BACKEND_SYNC_GUIDE.md** | Detailed integration |
| **FLASK_BACKEND_EXAMPLE.md** | Complete Flask code |
| **TROUBLESHOOTING.md** | When things break |
| **INTEGRATION_CHECKLIST.md** | Step-by-step verification |
| **ARCHITECTURE.md** | System design |

---

## 🎯 Verification Steps

1. ✅ **Start Flask**: `python app.py`
2. ✅ **Test API**: `curl http://localhost:5050/api/status`
3. ✅ **Open Dashboard**: View Figma Make preview
4. ✅ **Check Console**: F12 → No errors
5. ✅ **Verify Data**: Real data showing

---

## 🔄 Update Workflow

**When Flask data changes:**
```
Flask updates data → Dashboard auto-fetches → UI updates
(No action needed - happens automatically every few seconds)
```

**When Flask is offline:**
```
Dashboard detects error → Switches to mock data → Still works!
```

**When Flask comes back online:**
```
Refresh page → Dashboard reconnects → Real data returns
```

---

## 💡 Pro Tips

**Add real-time updates:**
```typescript
// In React component
useEffect(() => {
  const interval = setInterval(() => {
    fetchAPI(api.status).then(setData);
  }, 5000); // Refresh every 5 seconds
  
  return () => clearInterval(interval);
}, []);
```

**Check Flask performance:**
```python
import time

@app.route('/api/status')
def status():
    start = time.time()
    data = get_data()
    print(f"Request took {time.time() - start:.2f}s")
    return jsonify(data)
```

**Enable Flask CORS for all routes:**
```python
CORS(app, origins=["http://localhost:5173"], 
     resources={r"/api/*": {"origins": "*"}})
```

---

## 🎨 Dashboard Features

✅ **4 Main Pages**: Overview, Trading, Agents, Analytics  
✅ **Silver-Black-Chrome Theme**: Metallic with glass effects  
✅ **Bottom Navigation**: Modern mobile-style menu  
✅ **Clickable Tables**: Click any row for full details  
✅ **Responsive Design**: Works on all devices  
✅ **Mock Fallback**: Always works, even offline  
✅ **Real-time Ready**: Just add polling/WebSockets  

---

## 🚀 Production Checklist

Before deploying:

- [ ] Build frontend: `npm run build`
- [ ] Update API URL in .env
- [ ] Add authentication
- [ ] Enable HTTPS
- [ ] Restrict CORS to production domain
- [ ] Add rate limiting
- [ ] Set up error monitoring
- [ ] Configure hosting
- [ ] Test production build

---

## 📞 Getting Help

**Check in this order:**

1. **Browser Console** (F12) - Check for errors
2. **Network Tab** (F12) - Check API calls
3. **Flask Terminal** - Check backend logs
4. **Test API** - `curl http://localhost:5050/api/status`
5. **Read Docs** - Check TROUBLESHOOTING.md

**Still stuck?**
- Verify Flask is running on port 5050
- Verify CORS is enabled
- Verify endpoints exist
- Try mock data (should always work)

---

## 🎓 Learning Resources

**Flask:**
- CORS: https://flask-cors.readthedocs.io/
- Flask Docs: https://flask.palletsprojects.com/

**React:**
- Hooks: https://react.dev/reference/react
- Router: https://reactrouter.com/

**Tools:**
- Vite: https://vitejs.dev/
- Tailwind: https://tailwindcss.com/

---

## ⚡ Keyboard Shortcuts

**In Browser:**
- `F12` - Open DevTools
- `Ctrl + Shift + R` - Hard refresh (clear cache)
- `Ctrl + Shift + I` - Open DevTools (alternative)
- `Ctrl + Shift + J` - Open Console directly

**In DevTools:**
- `Ctrl + [` - Previous panel
- `Ctrl + ]` - Next panel
- `Esc` - Toggle console drawer

---

## 📊 Status Indicators

**In Dashboard:**
```
Green = Connected to backend
Yellow = Using mock data (backend offline)
Red = Error occurred
```

**In Flask Terminal:**
```
200 = Success
404 = Not Found (endpoint missing)
500 = Server Error (bug in code)
```

**In Browser Console:**
```
No errors = Everything working
Yellow warnings = Non-critical issues
Red errors = Something broken
```

---

**Print this page for quick reference while integrating!** 🖨️
