# OT-CORP Dashboard Integration Guide

This guide explains how to integrate this React dashboard into your GitHub repository at https://github.com/onlytim3/OT-CORP

## Overview

This dashboard is built with:
- **React 18** with TypeScript
- **React Router** for navigation
- **Tailwind CSS v4** for styling
- **Recharts** for data visualization
- **Shadcn/ui** components
- **Lucide React** for icons

## Integration Steps

### 1. Project Structure

Add a new frontend directory to your repository:

```
OT-CORP/
├── agency-agents/          # Your existing Python code
├── trading/                # Your existing Python code
├── frontend/               # NEW: React dashboard
│   ├── src/
│   │   ├── app/
│   │   │   ├── components/
│   │   │   ├── pages/
│   │   │   ├── App.tsx
│   │   │   └── routes.tsx
│   │   ├── styles/
│   │   └── main.tsx
│   ├── package.json
│   ├── tsconfig.json
│   └── vite.config.ts
├── requirements.txt        # Your existing Python requirements
└── start.sh               # Your existing start script
```

### 2. Copy Files to Your Repository

Copy these directories and files from this Figma Make project to your repo:

**From Figma Make → Your Repo:**
```
/src/app/              → frontend/src/app/
/src/styles/           → frontend/src/styles/
/src/main.tsx          → frontend/src/main.tsx
/package.json          → frontend/package.json
/tsconfig.json         → frontend/tsconfig.json
/vite.config.ts        → frontend/vite.config.ts
/index.html            → frontend/index.html
```

### 3. Install Dependencies

Navigate to the frontend directory and install dependencies:

```bash
cd frontend
npm install
```

### 4. Configure API Connection

Create a new file `frontend/src/config/api.ts`:

```typescript
// API Configuration
const API_BASE_URL = process.env.VITE_API_URL || 'http://localhost:8000';

export const api = {
  baseUrl: API_BASE_URL,
  
  // Trading endpoints
  trading: {
    positions: `${API_BASE_URL}/api/trading/positions`,
    signals: `${API_BASE_URL}/api/trading/signals`,
    performance: `${API_BASE_URL}/api/trading/performance`,
  },
  
  // Agent endpoints
  agents: {
    list: `${API_BASE_URL}/api/agents`,
    status: (id: string) => `${API_BASE_URL}/api/agents/${id}/status`,
    control: (id: string) => `${API_BASE_URL}/api/agents/${id}/control`,
  },
  
  // Analytics endpoints
  analytics: {
    overview: `${API_BASE_URL}/api/analytics/overview`,
    metrics: `${API_BASE_URL}/api/analytics/metrics`,
  },
};

// API helper function
export async function fetchAPI(url: string, options?: RequestInit) {
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });
  
  if (!response.ok) {
    throw new Error(`API Error: ${response.statusText}`);
  }
  
  return response.json();
}
```

### 5. Update Pages to Use Real Data

Replace mock data with API calls. Example for `frontend/src/app/pages/Overview.tsx`:

```typescript
import { useState, useEffect } from 'react';
import { fetchAPI, api } from '../config/api';

export function Overview() {
  const [metrics, setMetrics] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function loadData() {
      try {
        const data = await fetchAPI(api.analytics.overview);
        setMetrics(data);
      } catch (error) {
        console.error('Failed to load metrics:', error);
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, []);

  if (loading) return <div>Loading...</div>;

  // Rest of component...
}
```

### 6. Set Up Python Backend API

Create a new file in your repo: `api/main.py`

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/analytics/overview")
async def get_overview():
    # Replace with your actual data logic
    return {
        "totalRevenue": 89420,
        "activeTrades": 247,
        "agentUptime": 99.8,
        "activeAgents": 25
    }

@app.get("/api/trading/positions")
async def get_positions():
    # Integrate with your trading module
    from trading import get_active_positions
    return get_active_positions()

@app.get("/api/agents")
async def get_agents():
    # Integrate with your agency-agents module
    from agency_agents import get_all_agents
    return get_all_agents()
```

### 7. Update Requirements

Add to `requirements.txt`:

```
fastapi==0.104.1
uvicorn[standard]==0.24.0
python-dotenv==1.0.0
```

### 8. Create Startup Script

Create `start_dashboard.sh`:

```bash
#!/bin/bash

# Start Python backend
echo "Starting Python backend..."
cd api
uvicorn main:app --reload --port 8000 &
BACKEND_PID=$!

# Start React frontend
echo "Starting React frontend..."
cd ../frontend
npm run dev &
FRONTEND_PID=$!

# Wait for both processes
wait $BACKEND_PID $FRONTEND_PID
```

Make it executable:
```bash
chmod +x start_dashboard.sh
```

### 9. Environment Variables

Create `frontend/.env`:

```
VITE_API_URL=http://localhost:8000
```

Create `api/.env`:

```
PYTHON_ENV=development
DATABASE_URL=your_database_url
```

### 10. Build for Production

For production deployment:

```bash
# Build frontend
cd frontend
npm run build

# This creates frontend/dist/ with optimized static files
# Serve these with nginx or your preferred web server
```

Example nginx configuration:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    # Serve React app
    location / {
        root /path/to/frontend/dist;
        try_files $uri /index.html;
    }

    # Proxy API requests to Python backend
    location /api {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}
```

### 11. Docker Deployment (Optional)

Create `Dockerfile.frontend`:

```dockerfile
FROM node:20-alpine
WORKDIR /app
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM nginx:alpine
COPY --from=0 /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
```

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  backend:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8000:8000"
    environment:
      - PYTHON_ENV=production
    volumes:
      - ./api:/app

  frontend:
    build:
      context: .
      dockerfile: Dockerfile.frontend
    ports:
      - "80:80"
    depends_on:
      - backend
```

### 12. Git Workflow

```bash
# In your OT-CORP repository

# Create frontend directory
mkdir frontend

# Copy all files from Figma Make to frontend/

# Add to git
git add frontend/
git add api/
git add start_dashboard.sh
git add docker-compose.yml

# Commit
git commit -m "Add React dashboard with FastAPI backend"

# Push
git push origin main
```

## Data Integration Points

Connect these dashboard sections to your Python modules:

### Trading Dashboard
- **Data Source**: `trading/` module
- **Endpoints Needed**:
  - GET `/api/trading/positions` - Active trading positions
  - GET `/api/trading/signals` - AI trading signals
  - GET `/api/trading/performance` - Historical performance

### Agents Dashboard
- **Data Source**: `agency-agents/` module
- **Endpoints Needed**:
  - GET `/api/agents` - List all agents
  - GET `/api/agents/{id}/status` - Agent status
  - POST `/api/agents/{id}/control` - Start/stop agents

### Analytics Dashboard
- **Data Source**: Both modules
- **Endpoints Needed**:
  - GET `/api/analytics/overview` - Key metrics
  - GET `/api/analytics/performance` - Performance charts
  - GET `/api/analytics/risk` - Risk metrics

## Development Commands

```bash
# Development mode
npm run dev          # Start Vite dev server (port 5173)

# Build
npm run build        # Build for production

# Preview production build
npm run preview      # Preview production build locally

# Type checking
npm run type-check   # Check TypeScript types
```

## Next Steps

1. **Replace Mock Data**: Update all pages to fetch from your Python backend
2. **Authentication**: Add user authentication if needed
3. **WebSockets**: Add real-time updates for live trading data
4. **Error Handling**: Implement comprehensive error handling
5. **Testing**: Add unit and integration tests
6. **Monitoring**: Set up logging and monitoring

## Support

For questions or issues:
- Check the React Router docs: https://reactrouter.com
- Tailwind CSS docs: https://tailwindcss.com
- Recharts docs: https://recharts.org
- FastAPI docs: https://fastapi.tiangolo.com
