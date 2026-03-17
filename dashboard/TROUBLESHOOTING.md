# Troubleshooting Guide

## Common Issues and Solutions

### 1. CORS Error ❌

**Error Message:**
```
Access to fetch at 'http://localhost:5050/api/status' from origin 
'http://localhost:5173' has been blocked by CORS policy: No 
'Access-Control-Allow-Origin' header is present on the requested resource.
```

**What it means:**
Your Flask backend is not allowing requests from the React dashboard.

**Solution:**

```bash
# Install flask-cors
pip install flask-cors
```

```python
# Add to your Flask app
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173"])
```

**Verify Fix:**
```bash
# Restart Flask backend
python app.py

# Check browser console - CORS error should be gone
```

---

### 2. Connection Refused ❌

**Error Message:**
```
GET http://localhost:5050/api/status net::ERR_CONNECTION_REFUSED
API Error, falling back to mock data
```

**What it means:**
Flask backend is not running or not on port 5050.

**Solution:**

```bash
# Check if anything is running on port 5050
lsof -i :5050  # Mac/Linux
netstat -ano | findstr :5050  # Windows

# If nothing is running:
python app.py

# If something else is using port 5050, change Flask port:
app.run(port=5051)  # Use different port

# Then update dashboard env:
# Create .env file:
echo "VITE_API_URL=http://localhost:5051" > .env
```

**Verify Fix:**
```bash
# Test backend is responding
curl http://localhost:5050/api/status
# Should return JSON, not error
```

---

### 3. 404 Not Found ❌

**Error Message:**
```
GET http://localhost:5050/api/status 404 (Not Found)
```

**What it means:**
The endpoint doesn't exist on your Flask backend.

**Solution:**

```python
# Make sure you have this route in Flask:
@app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify({
        "account": { ... },
        "positions": [ ... ]
    })

# Check all routes are defined:
@app.route('/api/health')
@app.route('/api/status')
@app.route('/api/trades')
@app.route('/api/agents')
@app.route('/api/actions')
```

**Verify Fix:**
```bash
# List all Flask routes
flask routes  # If you have Flask CLI

# Or manually test each endpoint
curl http://localhost:5050/api/status
curl http://localhost:5050/api/agents
```

---

### 4. Dashboard Shows Mock Data (but backend is running) ⚠️

**Symptoms:**
- Flask backend is running
- No errors in console
- But dashboard shows fake/mock data

**Causes & Solutions:**

**A) Backend responded with error once, switched to mock mode**

```javascript
// Solution: Refresh the page
// The dashboard caches "use mock data" flag
// Hard refresh: Ctrl+Shift+R (Windows) or Cmd+Shift+R (Mac)
```

**B) Backend returns wrong JSON format**

```bash
# Check what backend returns:
curl http://localhost:5050/api/status

# Should match this format:
{
  "account": { ... },
  "positions": [ ... ],
  "summary": { ... }
}

# If different, update Flask to return correct format
```

**C) Page not using real API yet**

```typescript
// Check if page is actually calling fetchAPI:
// Open src/app/pages/Overview.tsx

// Should have:
import { fetchAPI, api } from '../config/api';

useEffect(() => {
  async function loadData() {
    const data = await fetchAPI(api.status);
    // ...
  }
  loadData();
}, []);

// If it's just using mockData const, 
// it's not connected yet (needs code update)
```

---

### 5. Slow Loading / Timeout ⏱️

**Symptoms:**
- Dashboard takes 30+ seconds to load
- Then switches to mock data

**Solutions:**

**A) Backend is slow to respond**
```python
# Check Flask processing time
@app.route('/api/status')
def get_status():
    import time
    start = time.time()
    # ... your code
    print(f"Request took {time.time() - start}s")
    return jsonify(data)
```

**B) Increase timeout**
```typescript
// In api.ts, add timeout to fetchAPI:
export async function fetchAPI<T>(url: string, options?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000); // 10s timeout
  
  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
    });
    clearTimeout(timeout);
    return await response.json();
  } catch (error) {
    clearTimeout(timeout);
    console.warn('API Error:', error);
    useMockData = true;
    return getMockDataForUrl<T>(url);
  }
}
```

---

### 6. Data Not Updating / Stale Data 🔄

**Symptoms:**
- Data shows once but never updates
- Need to refresh page to see new data

**Solutions:**

**A) Add polling for real-time updates**
```typescript
useEffect(() => {
  async function loadData() {
    const data = await fetchAPI(api.status);
    setPositions(data.positions);
  }
  
  loadData(); // Load immediately
  
  // Refresh every 5 seconds
  const interval = setInterval(loadData, 5000);
  
  return () => clearInterval(interval); // Cleanup
}, []);
```

**B) Use WebSockets (advanced)**
```python
# Flask backend
from flask_socketio import SocketIO, emit

socketio = SocketIO(app, cors_allowed_origins="http://localhost:5173")

@socketio.on('connect')
def handle_connect():
    emit('status_update', get_current_status())
```

```typescript
// React frontend
import io from 'socket.io-client';

useEffect(() => {
  const socket = io('http://localhost:5050');
  
  socket.on('status_update', (data) => {
    setPositions(data.positions);
  });

  return () => socket.disconnect();
}, []);
```

---

### 7. Wrong Port / URL ❌

**Symptoms:**
- Dashboard tries to connect to wrong address

**Check Current Configuration:**
```typescript
// Look at src/app/config/api.ts
const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:5050';
```

**Solution - Change Port:**

```bash
# Create .env file in project root:
echo "VITE_API_URL=http://localhost:YOUR_PORT" > .env

# Example: If Flask runs on port 8000:
echo "VITE_API_URL=http://localhost:8000" > .env

# Restart Vite dev server (in Figma Make, just refresh)
```

---

### 8. JSON Parse Error ❌

**Error Message:**
```
SyntaxError: Unexpected token < in JSON at position 0
```

**What it means:**
Backend returned HTML instead of JSON (usually an error page).

**Check:**
```bash
# See what backend returns:
curl -i http://localhost:5050/api/status

# Should be:
Content-Type: application/json
{"account": {...}}

# NOT:
Content-Type: text/html
<html>Error 500</html>
```

**Solution:**
```python
# Make sure Flask returns JSON on errors too:
from flask import jsonify

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404
```

---

### 9. Type Errors in Console ⚠️

**Error Message:**
```
TypeError: Cannot read property 'positions' of undefined
```

**What it means:**
Backend response doesn't match expected structure.

**Solution:**

```bash
# Check backend response structure:
curl http://localhost:5050/api/status

# Should have exact keys:
{
  "account": {...},     # Must have
  "positions": [...],   # Must have
  "summary": {...}      # Must have
}
```

```typescript
// Add defensive coding in React:
const data = await fetchAPI(api.status);
const positions = data?.positions || [];
const account = data?.account || {};
```

---

### 10. Environment Variables Not Working 🔧

**Symptoms:**
- Created .env file but VITE_API_URL not changing

**Solutions:**

**A) Restart dev server**
```bash
# .env changes require restart
# In Figma Make: Refresh the preview
```

**B) Check .env location**
```bash
# .env must be in project ROOT
OT-CORP/
  dashboard/
    .env  ← HERE (next to package.json)
    src/
    package.json
```

**C) Check variable name**
```bash
# Must start with VITE_
VITE_API_URL=http://localhost:5050  ✅
API_URL=http://localhost:5050       ❌ (won't work)
```

**D) Access in code**
```typescript
// Use import.meta.env, not process.env
const url = import.meta.env.VITE_API_URL;  ✅
const url = process.env.VITE_API_URL;      ❌
```

---

## Debugging Checklist

When something's not working:

### 1. Check Backend is Running
```bash
curl http://localhost:5050/api/status
# Should return JSON
```

### 2. Check Browser Console (F12)
```
Look for:
- ❌ CORS errors
- ❌ Network errors (ERR_CONNECTION_REFUSED)
- ❌ 404 errors
- ⚠️  "API Error, falling back to mock data"
```

### 3. Check Network Tab (F12 → Network)
```
- Click on API requests
- Check Status Code (should be 200)
- Check Response (should be JSON)
- Check Headers (should have Access-Control-Allow-Origin)
```

### 4. Check Flask Logs
```bash
# Flask prints each request:
127.0.0.1 - - [15/Mar/2026 10:30:00] "GET /api/status HTTP/1.1" 200 -
                                                                  ^^^
                                                                  200 = Success
                                                                  404 = Not Found
                                                                  500 = Error
```

### 5. Test Endpoints Manually
```bash
# Test each endpoint:
curl http://localhost:5050/api/health
curl http://localhost:5050/api/status
curl http://localhost:5050/api/agents
curl http://localhost:5050/api/trades

# Should all return JSON, not errors
```

---

## Getting Help

If you're still stuck:

### 1. Check Logs

**Flask Backend:**
```bash
# Run Flask with verbose logging
FLASK_ENV=development python app.py
```

**Browser Console:**
```javascript
// Enable verbose logging
localStorage.debug = '*';
```

### 2. Test API Manually

```bash
# Test with curl
curl -v http://localhost:5050/api/status

# -v shows full request/response including headers
```

### 3. Verify Response Format

```bash
# Pretty print JSON response
curl http://localhost:5050/api/status | python -m json.tool
```

### 4. Check Firewall

```bash
# Make sure firewall allows localhost connections
# Temporarily disable firewall to test
```

---

## Quick Reference

| Issue | Quick Fix |
|-------|-----------|
| CORS error | `pip install flask-cors` + add to Flask |
| Connection refused | Start Flask backend |
| 404 Not Found | Add route to Flask |
| Mock data showing | Hard refresh page (Ctrl+Shift+R) |
| Slow loading | Add timeout / optimize backend |
| Data not updating | Add polling with setInterval |
| Wrong port | Create .env with VITE_API_URL |
| JSON parse error | Make Flask return JSON, not HTML |
| Type errors | Check response structure matches expected |
| .env not working | Restart dev server |

---

**Still having issues?** Check these files:
- `/QUICK_START.md` - Basic setup
- `/BACKEND_SYNC_GUIDE.md` - Detailed integration
- `/FLASK_BACKEND_EXAMPLE.md` - Complete backend code
- `/ARCHITECTURE.md` - System overview
