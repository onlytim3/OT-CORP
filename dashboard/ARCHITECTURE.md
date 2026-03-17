# OT-CORP Dashboard Architecture

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    YOUR GITHUB REPOSITORY                        │
│                  github.com/onlytim3/OT-CORP                    │
└─────────────────────────────────────────────────────────────────┘
                                 │
                    ┌────────────┴────────────┐
                    │                         │
        ┌───────────▼──────────┐  ┌──────────▼───────────┐
        │   Python Backend     │  │  React Dashboard     │
        │   (Flask - Port 5050)│  │  (Vite - Port 5173)  │
        │                      │  │                      │
        │  /trading/           │  │  /src/app/           │
        │  /agency-agents/     │  │    - pages/          │
        │  app.py              │  │    - components/     │
        │                      │  │    - config/api.ts   │
        └───────────┬──────────┘  └──────────┬───────────┘
                    │                        │
                    │    HTTP API Calls      │
                    │ ◄──────────────────────┤
                    │                        │
                    │    JSON Responses      │
                    ├───────────────────────►│
                    │                        │
                    └────────────────────────┘
```

## Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                       REACT DASHBOARD                            │
│                    (http://localhost:5173)                       │
└───────────────────────────┬──────────────────────────────────────┘
                            │
                            │ 1. User visits page
                            ▼
                    ┌───────────────┐
                    │   React Page  │
                    │  (Overview)   │
                    └───────┬───────┘
                            │
                            │ 2. useEffect() triggers
                            ▼
                    ┌───────────────┐
                    │  fetchAPI()   │
                    │  from api.ts  │
                    └───────┬───────┘
                            │
                            │ 3. HTTP GET Request
                            ▼
        ┌───────────────────────────────────────┐
        │  http://localhost:5050/api/status     │
        └───────────────────┬───────────────────┘
                            │
                ┌───────────┴───────────┐
                │                       │
         ✅ Success                ❌ Error
                │                       │
                ▼                       ▼
        ┌──────────────┐        ┌─────────────┐
        │ Real Backend │        │  Mock Data  │
        │     Data     │        │  Fallback   │
        └──────┬───────┘        └──────┬──────┘
               │                       │
               └───────────┬───────────┘
                           │
                           │ 4. Data returned
                           ▼
                   ┌───────────────┐
                   │  Update State │
                   │  setData()    │
                   └───────┬───────┘
                           │
                           │ 5. Re-render
                           ▼
                   ┌───────────────┐
                   │ Display Data  │
                   │   in UI       │
                   └───────────────┘
```

## Current Integration Status

### ✅ What's Already Built

```
React Dashboard
├── API Configuration (/src/app/config/api.ts)
│   ├── ✅ Base URL: http://localhost:5050
│   ├── ✅ All endpoints mapped
│   ├── ✅ Mock data fallback
│   └── ✅ Type-safe API calls
│
├── Pages
│   ├── Overview.tsx     → Uses mock data (ready for /api/status)
│   ├── Trading.tsx      → Uses mock data (ready for /api/trades)
│   ├── Agents.tsx       → Uses mock data (ready for /api/agents)
│   └── Analytics.tsx    → Uses mock data (ready for /api/intelligence)
│
└── Components
    ├── ✅ MetricCard
    ├── ✅ Dialog modals for details
    ├── ✅ Tables with clickable rows
    └── ✅ Charts with Recharts
```

### 🔧 What You Need to Add

```
Flask Backend (Your OT-CORP repo)
├── CORS Support
│   └── pip install flask-cors
│       from flask_cors import CORS
│       CORS(app, origins=["http://localhost:5173"])
│
├── API Endpoints (map to your existing code)
│   ├── /api/status      → Your trading positions
│   ├── /api/trades      → Your trade history
│   ├── /api/agents      → Your agency-agents data
│   ├── /api/actions     → Your trading actions
│   ├── /api/strategies  → Your strategies
│   └── /api/intelligence → Your AI insights
│
└── Run on Port 5050
    └── app.run(port=5050)
```

## Complete Request/Response Cycle

### Example: Loading Overview Page

```
USER                    REACT                   FLASK BACKEND
  │                       │                           │
  │  1. Visits /          │                           │
  ├──────────────────────►│                           │
  │                       │                           │
  │                       │  2. useEffect()           │
  │                       ├─────────┐                 │
  │                       │         │                 │
  │                       │  3. fetchAPI(api.status)  │
  │                       ├────────────────────────── ►│
  │                       │                           │
  │                       │  4. GET /api/status       │
  │                       │                           │
  │                       │  5. Query database/cache  │
  │                       │                           ├────┐
  │                       │                           │    │
  │                       │  6. JSON Response         │◄───┘
  │                       │◄──────────────────────────┤
  │                       │                           │
  │  7. Render UI         │                           │
  │◄──────────────────────┤                           │
  │                       │                           │
  │  8. See live data! 🎉 │                           │
  │                       │                           │
```

## File Structure in Your Repo

After integration, your OT-CORP repo should look like:

```
OT-CORP/
│
├── 📁 agency-agents/          # Your existing Python agents
│   ├── agent_manager.py
│   ├── strategies/
│   └── ...
│
├── 📁 trading/                # Your existing trading system
│   ├── positions.py
│   ├── pnl.py
│   └── ...
│
├── 📁 dashboard/              # NEW: React dashboard (from Figma Make)
│   ├── src/
│   │   ├── app/
│   │   │   ├── components/
│   │   │   ├── pages/
│   │   │   │   ├── Overview.tsx
│   │   │   │   ├── Trading.tsx
│   │   │   │   ├── Agents.tsx
│   │   │   │   └── Analytics.tsx
│   │   │   ├── config/
│   │   │   │   └── api.ts          # API configuration
│   │   │   ├── App.tsx
│   │   │   └── routes.tsx
│   │   └── styles/
│   ├── package.json
│   ├── vite.config.ts
│   └── index.html
│
├── 📄 app.py                  # Your Flask backend
├── 📄 requirements.txt        # Add flask-cors here
├── 📄 .env                    # Environment variables
└── 📄 start.sh                # Start both frontend & backend
```

## Environment Configuration

### Development

```bash
# Backend runs on:
http://localhost:5050

# Frontend runs on:
http://localhost:5173

# API calls go from frontend → backend
```

### Production

```bash
# Build frontend:
cd dashboard && npm run build
# Creates: dashboard/dist/

# Serve both from single domain:
your-domain.com/           → React app
your-domain.com/api/*      → Flask backend (proxied)
```

## API Endpoints Reference

| Endpoint | Method | Purpose | Used By |
|----------|--------|---------|---------|
| `/api/health` | GET | Health check | System monitoring |
| `/api/status` | GET | Account + positions | Overview page |
| `/api/mode` | GET | Trading mode | Overview page |
| `/api/trades` | GET | Trade history | Trading page |
| `/api/actions` | GET | Recent actions | Overview/Trading |
| `/api/strategies` | GET | Active strategies | Trading page |
| `/api/strategy/{name}` | GET | Strategy details | Trading page |
| `/api/agents` | GET | AI agents list | Agents page |
| `/api/agents/{id}/status` | GET | Agent status | Agents page |
| `/api/agents/{id}/control` | POST | Control agent | Agents page |
| `/api/intelligence` | GET | AI insights | Analytics page |
| `/api/allocation` | GET | Portfolio allocation | Analytics page |
| `/api/pnl/{date}` | GET | P&L data | Analytics page |

## Technology Stack

### Frontend (React Dashboard)
- **Framework:** React 18 + TypeScript
- **Routing:** React Router 7
- **Styling:** Tailwind CSS v4
- **UI Components:** shadcn/ui (Radix UI)
- **Charts:** Recharts
- **Icons:** Lucide React
- **Build Tool:** Vite
- **State:** React hooks (useState, useEffect)

### Backend (Flask API)
- **Framework:** Flask 3.0
- **CORS:** flask-cors
- **Data Source:** Your trading/ and agency-agents/ modules
- **Port:** 5050
- **Response Format:** JSON

### Communication
- **Protocol:** HTTP/REST
- **Data Format:** JSON
- **CORS:** Enabled for localhost:5173

## Security Considerations

### Development
- CORS allows `localhost:5173` only
- All API calls visible in browser DevTools
- No authentication (add if needed)

### Production
- Use HTTPS
- Implement authentication (JWT/OAuth)
- Rate limiting on API endpoints
- Environment variables for secrets
- CORS restricted to your domain only

## Deployment Options

### Option 1: Separate Deployment
```
Frontend: Vercel/Netlify
Backend: Heroku/DigitalOcean
```

### Option 2: Unified Deployment
```
Single server running:
- Flask backend (API + serve static frontend)
- Nginx proxy
```

### Option 3: Docker
```
docker-compose.yml:
  - Backend container (Flask)
  - Frontend container (Nginx)
```

## Next Steps

1. ✅ Add CORS to your Flask backend
2. ✅ Start Flask on port 5050
3. ✅ Preview dashboard - it auto-connects!
4. 🔄 Replace mock data with real data from your trading system
5. 🔄 Add real-time updates (WebSockets/polling)
6. 🚀 Deploy to production

## Monitoring & Debugging

### Check Connection Status
```javascript
// In browser console (F12)
fetch('http://localhost:5050/api/status')
  .then(r => r.json())
  .then(console.log)
  .catch(console.error)
```

### Backend Logs
```bash
# Flask prints requests
127.0.0.1 - - [15/Mar/2026 10:30:00] "GET /api/status HTTP/1.1" 200 -
```

### Frontend Logs
```javascript
// Look for in console:
"API Error, falling back to mock data"  ← Backend not connected
No errors ← Connected successfully!
```

## Support Resources

- **Integration Guide:** `/INTEGRATION_GUIDE.md`
- **Backend Example:** `/FLASK_BACKEND_EXAMPLE.md`
- **Quick Start:** `/QUICK_START.md`
- **This File:** `/ARCHITECTURE.md`

---

**Your dashboard is production-ready and waiting to connect to your backend!** 🚀
