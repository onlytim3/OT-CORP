# Integration Checklist

Use this checklist to ensure your dashboard is properly connected to your Flask backend.

## Pre-Integration Setup

### ☐ 1. Verify File Locations

```bash
# Your OT-CORP repository should have:
OT-CORP/
├── agency-agents/       # ✓ Your AI agents
├── trading/            # ✓ Your trading system
├── app.py              # ✓ Your Flask backend
└── requirements.txt    # ✓ Python dependencies
```

### ☐ 2. Check Python Environment

```bash
# Verify Python version
python --version  # Should be 3.8+

# Verify Flask is installed
python -c "import flask; print(flask.__version__)"
```

### ☐ 3. Verify Flask Runs

```bash
# Start Flask backend
python app.py

# Should show:
# * Running on http://127.0.0.1:XXXX
```

**Current Flask Port:** ___________ (note this down!)

---

## Backend Configuration

### ☐ 4. Install flask-cors

```bash
pip install flask-cors

# Verify installation
python -c "import flask_cors; print('OK')"
```

**Status:** ☐ Installed  ☐ Not Installed

### ☐ 5. Add CORS to Flask App

Open your Flask app file and add:

```python
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173"])
```

**Location in code (line number):** ___________

### ☐ 6. Verify CORS is Working

```bash
# Restart Flask
python app.py

# In another terminal:
curl -H "Origin: http://localhost:5173" \
     -H "Access-Control-Request-Method: GET" \
     -X OPTIONS \
     http://localhost:5050/api/status -I

# Should include:
# Access-Control-Allow-Origin: http://localhost:5173
```

**Status:** ☐ CORS Working  ☐ Not Working

---

## API Endpoints

Check which endpoints your Flask backend has:

### Core Endpoints

- ☐ `GET /api/status` - Account status and positions
- ☐ `GET /api/health` - Health check
- ☐ `GET /api/mode` - Trading mode (paper/live)

**Test:**
```bash
curl http://localhost:5050/api/status
curl http://localhost:5050/api/health
```

### Trading Endpoints

- ☐ `GET /api/trades` - Trade history
- ☐ `GET /api/position/{symbol}` - Specific position
- ☐ `GET /api/actions` - Recent actions/signals

**Test:**
```bash
curl http://localhost:5050/api/trades
curl http://localhost:5050/api/actions
```

### Strategy Endpoints

- ☐ `GET /api/strategies` - List all strategies
- ☐ `GET /api/strategy/{name}` - Specific strategy

**Test:**
```bash
curl http://localhost:5050/api/strategies
```

### Agent Endpoints

- ☐ `GET /api/agents` - List all agents
- ☐ `GET /api/agents/{id}/status` - Agent status
- ☐ `POST /api/agents/{id}/control` - Control agent

**Test:**
```bash
curl http://localhost:5050/api/agents
```

### Analytics Endpoints

- ☐ `GET /api/pnl` - P&L data
- ☐ `GET /api/intelligence` - AI intelligence/insights
- ☐ `GET /api/allocation` - Portfolio allocation

**Test:**
```bash
curl http://localhost:5050/api/intelligence
curl http://localhost:5050/api/allocation
```

---

## Response Format Verification

### ☐ 7. Check /api/status Response

```bash
curl http://localhost:5050/api/status | python -m json.tool
```

Should return:
```json
{
  "account": {
    "portfolio_value": <number>,
    "cash": <number>,
    "buying_power": <number>
  },
  "positions": [
    {
      "symbol": <string>,
      "qty": <number>,
      "current_price": <number>,
      "unrealized_pnl": <number>
    }
  ],
  "summary": {
    "total_actions": <number>,
    "strategies_active": <number>
  }
}
```

**Status:** ☐ Correct Format  ☐ Wrong Format  ☐ Not Found

### ☐ 8. Check /api/agents Response

```bash
curl http://localhost:5050/api/agents | python -m json.tool
```

Should return array of agents:
```json
[
  {
    "id": <string>,
    "name": <string>,
    "status": <string>,
    "uptime": <number>,
    "trades_today": <number>
  }
]
```

**Status:** ☐ Correct Format  ☐ Wrong Format  ☐ Not Found

---

## Frontend Configuration

### ☐ 9. Check Dashboard API Configuration

File: `/src/app/config/api.ts`

```typescript
const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:5050';
```

**Current default port:** ___________

### ☐ 10. Create .env File (if needed)

If your Flask runs on a different port:

```bash
# In dashboard root directory
echo "VITE_API_URL=http://localhost:YOUR_PORT" > .env
```

**Using custom port?** ☐ Yes (port: _______)  ☐ No (using 5050)

---

## Connection Testing

### ☐ 11. Start Both Services

**Terminal 1:**
```bash
cd /path/to/OT-CORP
python app.py
```
**Flask running?** ☐ Yes  ☐ No

**Terminal 2:**
```
# Dashboard already running in Figma Make
# Just view the preview
```
**Dashboard visible?** ☐ Yes  ☐ No

### ☐ 12. Test Browser Connection

1. Open dashboard preview
2. Press F12 (open DevTools)
3. Go to Console tab
4. Look for messages:

- ☐ No errors = **Connected!** ✅
- ☐ "API Error, falling back to mock data" = **Not connected** ❌
- ☐ CORS error = **CORS not configured** ⚠️
- ☐ Connection refused = **Backend not running** ⚠️

**Current status:** _________________________

### ☐ 13. Test Network Requests

1. In DevTools, go to Network tab
2. Refresh dashboard
3. Filter by "localhost:5050"
4. Check requests:

- ☐ Request to `/api/status` visible
- ☐ Status code: 200 (success)
- ☐ Response contains JSON data
- ☐ Headers include `Access-Control-Allow-Origin`

**Network requests working?** ☐ Yes  ☐ No

---

## Data Verification

### ☐ 14. Verify Real Data is Showing

Check each page:

**Overview Page:**
- ☐ Portfolio value shows real data
- ☐ Positions table shows actual positions
- ☐ Activity feed shows real actions

**Trading Page:**
- ☐ Trade history shows real trades
- ☐ Strategies show actual strategies
- ☐ Signals are from backend

**Agents Page:**
- ☐ Agents list shows actual agents
- ☐ Agent metrics are real
- ☐ Can click for details

**Analytics Page:**
- ☐ Charts show real performance data
- ☐ Risk metrics from backend
- ☐ AI insights are real

**Overall:** ☐ All Real Data  ☐ Mix of Real/Mock  ☐ All Mock Data

---

## Troubleshooting

If something isn't working, check:

### Common Issue 1: CORS Error
```
☐ flask-cors installed?
☐ CORS(app) added to Flask?
☐ Flask restarted after adding CORS?
```

### Common Issue 2: Connection Refused
```
☐ Flask running on correct port?
☐ Port number matches in .env?
☐ Firewall allowing connections?
```

### Common Issue 3: 404 Not Found
```
☐ Endpoint exists in Flask?
☐ Route has /api/ prefix?
☐ Flask route registered correctly?
```

### Common Issue 4: Wrong Data Format
```
☐ Flask returns JSON not HTML?
☐ Response structure matches expected format?
☐ All required fields present?
```

---

## Final Checks

### ☐ 15. End-to-End Test

1. **Start Flask backend**
   ```bash
   python app.py
   ```

2. **Open dashboard**
   - View Figma Make preview

3. **Test all pages**
   - ☐ Overview loads without errors
   - ☐ Trading page shows real data
   - ☐ Agents page shows real agents
   - ☐ Analytics shows real charts

4. **Test interactions**
   - ☐ Can click table rows
   - ☐ Modals show full details
   - ☐ Navigation works
   - ☐ Data refreshes

5. **Test error handling**
   - ☐ Stop Flask backend
   - ☐ Dashboard switches to mock data
   - ☐ Restart Flask
   - ☐ Refresh page - real data returns

**All tests passing?** ☐ Yes  ☐ No

---

## Deployment Readiness

Before deploying to production:

### Code Quality
- ☐ No console errors
- ☐ No React warnings
- ☐ All TypeScript types valid

### Performance
- ☐ Pages load in < 2 seconds
- ☐ API responses < 500ms
- ☐ No memory leaks

### Security
- ☐ Add authentication
- ☐ Enable HTTPS
- ☐ Restrict CORS to production domain
- ☐ Environment variables for secrets
- ☐ Rate limiting on API

### Production Config
- ☐ Build frontend: `npm run build`
- ☐ Test production build
- ☐ Configure production API URL
- ☐ Set up hosting
- ☐ Configure domain/SSL

---

## Success Criteria

Your integration is complete when:

✅ Flask backend runs without errors  
✅ Dashboard connects to backend  
✅ All pages show real data  
✅ No CORS errors  
✅ Tables are clickable  
✅ Modals show full details  
✅ Navigation works smoothly  
✅ Fallback to mock data if backend offline  

## 🎉 Integration Complete!

When all checkboxes above are checked, your integration is complete!

**Integration completed on:** _______________  
**Completed by:** _______________  
**Flask backend port:** _______________  
**Dashboard URL:** _______________

---

## Quick Reference

**Documentation Files:**
- `/README.md` - Overview
- `/QUICK_START.md` - 3-step setup
- `/BACKEND_SYNC_GUIDE.md` - Detailed guide
- `/FLASK_BACKEND_EXAMPLE.md` - Complete Flask code
- `/TROUBLESHOOTING.md` - Solutions to common issues
- `/ARCHITECTURE.md` - System architecture

**Test Commands:**
```bash
# Test backend
curl http://localhost:5050/api/status

# Test with CORS
curl -H "Origin: http://localhost:5173" http://localhost:5050/api/status

# Pretty print JSON
curl http://localhost:5050/api/status | python -m json.tool

# Check port usage
lsof -i :5050  # Mac/Linux
netstat -ano | findstr :5050  # Windows
```

**Browser Testing:**
- F12 → Console (check for errors)
- F12 → Network (check API calls)
- F12 → Application → Local Storage (check cached data)

Good luck! 🚀
