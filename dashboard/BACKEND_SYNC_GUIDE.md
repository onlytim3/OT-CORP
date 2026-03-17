# Backend Integration & Sync Guide for OT-CORP Dashboard

## Current Setup

Your dashboard is **already configured** to connect to your Flask backend on port 5050! Here's what's in place:

### ✅ What's Already Working

1. **API Configuration** (`/src/app/config/api.ts`)
   - Configured for `http://localhost:5050`
   - Automatic fallback to mock data if backend is unavailable
   - All endpoints mapped to your Flask API

2. **Automatic Mock Fallback**
   - Dashboard works even when backend is offline
   - Seamless transition to real data when backend is available

## Quick Start: Connect to Your Backend

### Step 1: Ensure Your Flask Backend is Running

```bash
# In your OT-CORP repository, start your Flask backend
cd /path/to/OT-CORP
python app.py  # or however you start your Flask server on port 5050
```

Your Flask server should be running on `http://localhost:5050`

### Step 2: Enable CORS on Your Flask Backend

Your Flask backend **must** allow requests from the React dev server. Add CORS support:

```python
# In your Flask app (app.py or main.py)
from flask import Flask
from flask_cors import CORS

app = Flask(__name__)

# Enable CORS for the React dev server
CORS(app, origins=[
    "http://localhost:5173",  # Vite dev server
    "http://localhost:3000",  # Alternative dev port
    "http://127.0.0.1:5173",
])

# OR enable for all origins during development (NOT recommended for production)
# CORS(app, origins="*")
```

Install flask-cors if you don't have it:

```bash
pip install flask-cors
```

### Step 3: Verify Your Flask Endpoints Match

The dashboard expects these endpoints on your Flask backend:

#### **Status & Health**
- `GET /api/status` - Returns account info, positions, summary
- `GET /api/health` - Health check
- `GET /api/mode` - Trading mode (paper/live)

#### **Trading**
- `GET /api/trades` - Trading history
- `GET /api/position/{symbol}` - Specific position
- `GET /api/actions` - Trading actions/signals

#### **Strategies**
- `GET /api/strategies` - List all strategies
- `GET /api/strategy/{name}` - Specific strategy

#### **Analytics**
- `GET /api/pnl/{date}` - P&L for date
- `GET /api/intelligence` - AI intelligence data
- `GET /api/allocation` - Portfolio allocation

#### **Agents**
- `GET /api/agents` - List all agents
- `GET /api/recommendation/{id}` - Agent recommendation

#### **Chat**
- `POST /api/chat` - Chat interface
- `POST /api/chat/confirm` - Confirm chat action

### Step 4: Start Your React Dashboard

```bash
# This project is running in Figma Make / Claude Code
# The preview is already available
# Just ensure your Flask backend is running!
```

## How It Works

### Connection Flow

```
React Dashboard (localhost:5173)
        ↓
    fetchAPI() call
        ↓
    Try: http://localhost:5050/api/...
        ↓
   ┌────────────┐
   │  Success?  │
   └────────────┘
    ↙         ↘
  YES          NO
   ↓            ↓
Use Real    Use Mock
  Data        Data
```

### API Helper Function

Located in `/src/app/config/api.ts`:

```typescript
export async function fetchAPI<T>(url: string, options?: RequestInit): Promise<T> {
  try {
    const response = await fetch(url, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        ...options?.headers,
      },
    });
    
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    
    return await response.json();
  } catch (error) {
    console.warn('API Error, falling back to mock data:', error);
    useMockData = true;
    return getMockDataForUrl<T>(url);
  }
}
```

## Example: Update a Page to Use Real Data

Here's how to modify any page to fetch real data:

```typescript
// Example: src/app/pages/Overview.tsx
import { useState, useEffect } from 'react';
import { fetchAPI, api } from '../config/api';

export function Overview() {
  const [positions, setPositions] = useState([]);
  const [activity, setActivity] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function loadData() {
      try {
        // Fetch real data from Flask backend
        const statusData = await fetchAPI(api.status);
        const activityData = await fetchAPI(api.actions);
        
        setPositions(statusData.positions || []);
        setActivity(activityData || []);
      } catch (error) {
        console.error('Failed to load data:', error);
        // fetchAPI automatically falls back to mock data
      } finally {
        setLoading(false);
      }
    }
    
    loadData();
  }, []);

  if (loading) {
    return <div>Loading...</div>;
  }

  // Render your data...
}
```

## Expected Flask Response Formats

### /api/status Response

```json
{
  "account": {
    "portfolio_value": 89420.50,
    "cash": 12540.25,
    "buying_power": 35000,
    "equity": 76880.25,
    "status": "ACTIVE"
  },
  "positions": [
    {
      "symbol": "BTCUSDT",
      "qty": 0.5,
      "current_price": 47200,
      "unrealized_pnl": 1250.50,
      "age": "2h",
      "avg_entry_price": 45000,
      "market_value": 23600,
      "side": "long"
    }
  ],
  "summary": {
    "total_actions": 145,
    "strategies_active": 18,
    "signals_today": 23
  },
  "mode": "paper"
}
```

### /api/actions Response

```json
[
  {
    "id": 1,
    "action": "Position opened",
    "category": "trade",
    "timestamp": "2026-03-15T10:30:00Z",
    "details": "BUY BTCUSDT $2500"
  }
]
```

### /api/agents Response

```json
[
  {
    "id": "agent-1",
    "name": "Momentum Trader",
    "status": "active",
    "uptime": 99.8,
    "trades_today": 12,
    "win_rate": 68.5,
    "last_active": "2026-03-15T10:30:00Z"
  }
]
```

## Testing the Connection

### 1. Check Flask Server

```bash
# Test if your Flask server is running
curl http://localhost:5050/api/status
```

### 2. Check CORS Headers

```bash
# Test CORS from browser console (F12)
fetch('http://localhost:5050/api/status')
  .then(r => r.json())
  .then(console.log)
  .catch(console.error)
```

### 3. Check Dashboard Console

Open browser DevTools (F12) → Console tab:
- Look for "API Error, falling back to mock data" → Backend not reachable
- No errors → Successfully connected to backend!

## Common Issues & Solutions

### Issue 1: CORS Error

**Error in console:**
```
Access to fetch at 'http://localhost:5050/api/status' from origin 'http://localhost:5173' 
has been blocked by CORS policy
```

**Solution:**
Add CORS to your Flask app:
```python
from flask_cors import CORS
CORS(app, origins=["http://localhost:5173"])
```

### Issue 2: Connection Refused

**Error:**
```
Failed to fetch
net::ERR_CONNECTION_REFUSED
```

**Solution:**
- Ensure Flask server is running on port 5050
- Check: `curl http://localhost:5050/api/status`

### Issue 3: 404 Not Found

**Error:**
```
GET http://localhost:5050/api/status 404 (Not Found)
```

**Solution:**
- Check your Flask routes match the expected endpoints
- Ensure routes have `/api/` prefix

### Issue 4: Wrong Port

**Solution:**
If your Flask server runs on a different port, update:

```bash
# Create a .env file in the project root
echo "VITE_API_URL=http://localhost:YOUR_PORT" > .env
```

Then restart the dashboard.

## Real-Time Updates (Optional)

For live data updates, add polling or WebSockets:

### Option 1: Polling

```typescript
useEffect(() => {
  const interval = setInterval(async () => {
    const data = await fetchAPI(api.status);
    setPositions(data.positions);
  }, 5000); // Update every 5 seconds

  return () => clearInterval(interval);
}, []);
```

### Option 2: WebSockets (Advanced)

```python
# Flask backend with Socket.IO
from flask_socketio import SocketIO

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

## Deployment to GitHub

### 1. Copy Files to Your OT-CORP Repo

```bash
# In your OT-CORP repository
mkdir -p dashboard

# Copy all files from this Figma Make project to dashboard/
# (You'll need to download/export from Figma Make)
```

### 2. Update Your Repository Structure

```
OT-CORP/
├── agency-agents/          # Your Python agents
├── trading/                # Your trading logic  
├── dashboard/              # NEW: React dashboard
│   ├── src/
│   │   ├── app/
│   │   │   ├── components/
│   │   │   ├── pages/
│   │   │   ├── config/
│   │   │   └── App.tsx
│   │   └── styles/
│   ├── package.json
│   └── vite.config.ts
├── app.py                  # Your Flask backend
└── requirements.txt
```

### 3. Add CORS to requirements.txt

```txt
flask-cors
```

### 4. Create Start Script

Create `start_full_stack.sh`:

```bash
#!/bin/bash

echo "Starting Flask Backend..."
python app.py &
BACKEND_PID=$!

echo "Starting React Dashboard..."
cd dashboard
npm install
npm run dev &
FRONTEND_PID=$!

echo "Backend PID: $BACKEND_PID"
echo "Frontend PID: $FRONTEND_PID"
echo ""
echo "🚀 Dashboard: http://localhost:5173"
echo "🔧 Backend: http://localhost:5050"

wait $BACKEND_PID $FRONTEND_PID
```

Make it executable:
```bash
chmod +x start_full_stack.sh
```

### 5. Run Everything

```bash
./start_full_stack.sh
```

## Production Deployment

### Build the Dashboard

```bash
cd dashboard
npm run build
# Creates dashboard/dist/ with optimized files
```

### Serve from Flask

```python
from flask import Flask, send_from_directory

app = Flask(__name__, static_folder='dashboard/dist')

@app.route('/')
def serve_dashboard():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(app.static_folder, path)

# Your API routes
@app.route('/api/status')
def get_status():
    return jsonify({"status": "ok"})
```

## Environment Variables

Create `dashboard/.env`:

```env
# Development
VITE_API_URL=http://localhost:5050

# Production (update when deploying)
# VITE_API_URL=https://your-domain.com
```

## Next Steps

1. ✅ Start your Flask backend on port 5050
2. ✅ Add CORS support to Flask
3. ✅ Verify endpoints match expected format
4. ✅ Open browser console to check connection
5. ✅ Replace mock data in pages with real API calls
6. 🔄 Add real-time updates with polling/WebSockets
7. 🚀 Deploy to production

## Support

Having issues? Check:
1. Flask server is running: `curl http://localhost:5050/api/status`
2. CORS is enabled in Flask
3. Browser console for errors (F12)
4. Network tab in DevTools to see requests

The dashboard will **always work** with mock data, even if backend is down. Real data will appear automatically once backend is connected!
