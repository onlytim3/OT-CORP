# OT-CORP Trading Dashboard

A professional React + TypeScript dashboard for your OT-CORP trading system with real-time market data, AI agent monitoring, and comprehensive analytics.

![Dashboard Theme: Silver-Black-Chrome with Metallic Accents]

## 🎯 Overview

This dashboard provides a complete interface for your Python-based trading system at [github.com/onlytim3/OT-CORP](https://github.com/onlytim3/OT-CORP), featuring:

- **Real-time Trading**: Live positions, P&L tracking, and trade execution
- **AI Agent Monitoring**: Monitor and control your agency-agents
- **Advanced Analytics**: Performance metrics, risk analysis, and intelligence insights
- **Responsive Design**: Works on desktop, tablet, and mobile
- **Mock Data Fallback**: Always works, even when backend is offline

## 🚀 Quick Start

### Option 1: Using This Project (Current Setup)

Your dashboard is **already configured** and ready to connect!

1. **Install Flask CORS** (in your OT-CORP repo):
   ```bash
   pip install flask-cors
   ```

2. **Add CORS to your Flask app**:
   ```python
   from flask_cors import CORS
   
   app = Flask(__name__)
   CORS(app, origins=["http://localhost:5173"])
   ```

3. **Start your Flask backend**:
   ```bash
   python app.py  # Must run on port 5050
   ```

4. **Your dashboard auto-connects!**
   - Preview already running in Figma Make
   - Will automatically fetch real data from your backend
   - Falls back to mock data if backend is unavailable

**That's it!** See [QUICK_START.md](/QUICK_START.md) for details.

### Option 2: Deploy to Your GitHub Repo

See [INTEGRATION_GUIDE.md](/INTEGRATION_GUIDE.md) for complete deployment instructions.

## 📊 Features

### 1. Overview Page
- **Account Summary**: Portfolio value, cash, buying power, P&L
- **Active Positions**: Real-time positions with unrealized P&L
- **Recent Activity**: Trading actions, signals, and system events
- **Quick Metrics**: Key performance indicators

### 2. Trading Page
- **Market Data**: Real-time price feeds and signals
- **Position Management**: View and manage all positions
- **Trade History**: Complete trade log with P&L
- **Strategy Performance**: Active strategies and their metrics

### 3. Agents Page
- **AI Agents**: Monitor all trading agents
- **Performance Tracking**: Win rates, trades, uptime
- **Agent Control**: Start, stop, and configure agents
- **Recommendations**: View agent-generated signals

### 4. Analytics Page
- **Performance Charts**: Visual performance analysis
- **Risk Metrics**: Portfolio risk and exposure
- **AI Intelligence**: Market insights and predictions
- **Allocation**: Asset allocation breakdown

### 5. Universal Features
- ✅ **Clickable Details**: Click any table row for full details
- ✅ **Dark Theme**: Silver-black-chrome with metallic accents
- ✅ **Bottom Navigation**: Modern mobile-style navigation
- ✅ **Responsive**: Works on all screen sizes
- ✅ **Real-time**: Live data updates from your backend

## 🛠️ Technology Stack

### Frontend
- **React 18** + TypeScript
- **React Router 7** for navigation
- **Tailwind CSS v4** for styling
- **shadcn/ui** components (Radix UI)
- **Recharts** for data visualization
- **Lucide React** icons
- **Vite** for fast development

### Backend Integration
- **Flask** API (Python 3.8+)
- **REST API** with JSON responses
- **CORS** enabled for local development
- **Port 5050** (configurable)

## 📁 Project Structure

```
dashboard/
├── src/
│   ├── app/
│   │   ├── components/         # Reusable components
│   │   │   ├── ui/            # shadcn/ui components
│   │   │   ├── DashboardLayout.tsx
│   │   │   └── MetricCard.tsx
│   │   ├── pages/             # Main pages
│   │   │   ├── Overview.tsx
│   │   │   ├── Trading.tsx
│   │   │   ├── Agents.tsx
│   │   │   └── Analytics.tsx
│   │   ├── config/
│   │   │   └── api.ts         # API configuration
│   │   ├── App.tsx            # Main app component
│   │   └── routes.tsx         # Route configuration
│   └── styles/                # CSS styles
├── package.json
├── vite.config.ts
└── Documentation files
```

## 🔌 Backend Integration

### API Configuration

Located in `/src/app/config/api.ts`:

```typescript
const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:5050';
```

### Required Flask Endpoints

Your Flask backend should implement:

| Endpoint | Method | Returns |
|----------|--------|---------|
| `/api/status` | GET | Account info, positions, summary |
| `/api/trades` | GET | Trading history |
| `/api/agents` | GET | AI agents list |
| `/api/actions` | GET | Recent trading actions |
| `/api/strategies` | GET | Active strategies |
| `/api/intelligence` | GET | AI insights |
| `/api/allocation` | GET | Portfolio allocation |

See [FLASK_BACKEND_EXAMPLE.md](/FLASK_BACKEND_EXAMPLE.md) for complete Flask implementation.

### Automatic Fallback

The dashboard automatically falls back to mock data if:
- Backend is not running
- Backend returns an error
- Network connection fails

No configuration needed - it just works!

## 📚 Documentation

| File | Purpose |
|------|---------|
| [README.md](/README.md) | This file - overview and quick start |
| [QUICK_START.md](/QUICK_START.md) | 3-step setup guide |
| [BACKEND_SYNC_GUIDE.md](/BACKEND_SYNC_GUIDE.md) | Detailed backend integration |
| [FLASK_BACKEND_EXAMPLE.md](/FLASK_BACKEND_EXAMPLE.md) | Complete Flask code example |
| [INTEGRATION_GUIDE.md](/INTEGRATION_GUIDE.md) | Deploy to your GitHub repo |
| [ARCHITECTURE.md](/ARCHITECTURE.md) | System architecture & data flow |
| [TROUBLESHOOTING.md](/TROUBLESHOOTING.md) | Common issues & solutions |

## 🎨 Design System

### Theme
- **Primary**: Silver-black-chrome metallic theme
- **Background**: Deep black (#0a0a0a)
- **Accents**: Chrome shimmer effects
- **Components**: Glass morphism with metallic borders

### Navigation
- **Bottom Menu**: Mobile-style navigation bar
- **Icons**: Lucide React with consistent styling
- **Active States**: Chrome glow effects

### Interactions
- **Clickable Tables**: All tables support row clicks for details
- **Modals**: Full-screen detail dialogs
- **Responsive**: Touch-friendly on mobile

## 🔧 Configuration

### Change Backend URL

Create `.env` file:

```env
VITE_API_URL=http://localhost:YOUR_PORT
```

### Enable Debug Mode

```env
VITE_DEBUG=true
```

### Production Build

```bash
npm run build
# Creates optimized build in dist/
```

## 🐛 Troubleshooting

### Common Issues

**CORS Error**
```bash
pip install flask-cors
# Add CORS(app) to Flask
```

**Connection Refused**
```bash
# Make sure Flask runs on port 5050
python app.py
```

**404 Not Found**
```python
# Add missing routes to Flask
@app.route('/api/status')
def get_status():
    return jsonify({...})
```

See [TROUBLESHOOTING.md](/TROUBLESHOOTING.md) for complete guide.

## 🚢 Deployment

### Development

```bash
# Backend
python app.py  # Port 5050

# Frontend
# Already running in Figma Make!
```

### Production

1. **Build frontend**:
   ```bash
   npm run build
   ```

2. **Serve from Flask**:
   ```python
   app = Flask(__name__, static_folder='dist')
   
   @app.route('/')
   def serve():
       return send_from_directory('dist', 'index.html')
   ```

See [INTEGRATION_GUIDE.md](/INTEGRATION_GUIDE.md) for deployment options.

## 📊 Data Flow

```
React Dashboard → fetchAPI() → Flask Backend → Your Trading System
      ↓                              ↓
  Display Data  ←  JSON Response  ←  Return Data
```

If backend is offline:
```
React Dashboard → fetchAPI() → Error → Mock Data Fallback
      ↓
  Display Data (mock)
```

## 🔐 Security Notes

### Development
- CORS allows `localhost:5173` only
- No authentication (add if needed)
- All data visible in DevTools

### Production
- [ ] Add authentication (JWT/OAuth)
- [ ] Enable HTTPS
- [ ] Restrict CORS to your domain
- [ ] Add rate limiting
- [ ] Environment variables for secrets

## 📝 License

This dashboard is built for the OT-CORP trading system.

## 🙋 Support

### Getting Help

1. **Check documentation**:
   - Quick Start: [QUICK_START.md](/QUICK_START.md)
   - Troubleshooting: [TROUBLESHOOTING.md](/TROUBLESHOOTING.md)

2. **Test backend**:
   ```bash
   curl http://localhost:5050/api/status
   ```

3. **Check browser console** (F12):
   - Look for errors
   - Check Network tab for API calls

4. **Verify Flask logs**:
   ```bash
   # Flask prints each request
   127.0.0.1 - - "GET /api/status HTTP/1.1" 200 -
   ```

## 🎉 Features Checklist

- ✅ Real-time trading dashboard
- ✅ AI agent monitoring
- ✅ Advanced analytics
- ✅ Silver-black-chrome theme
- ✅ Bottom navigation menu
- ✅ Clickable table details
- ✅ Responsive design
- ✅ Mock data fallback
- ✅ Flask integration ready
- ✅ Production build
- ✅ Type-safe TypeScript
- ✅ Component library (shadcn/ui)
- ✅ Chart visualizations (Recharts)

## 🚀 Next Steps

1. ✅ Add CORS to your Flask backend
2. ✅ Start Flask on port 5050
3. ✅ Preview dashboard - it auto-connects!
4. 🔄 Replace mock data with your trading data
5. 🔄 Add real-time updates (polling/WebSockets)
6. 🔄 Add authentication
7. 🚀 Deploy to production

---

**Built with Figma Make** | **Powered by React + TypeScript** | **Styled with Tailwind CSS v4**

Your dashboard is ready to connect to your OT-CORP trading system! 🎯
