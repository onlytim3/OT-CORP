#!/bin/bash

# OT-CORP Dashboard Integration Startup Script
# This script helps you connect your React dashboard to your Flask backend

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Banner
echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║                                                            ║"
echo "║           OT-CORP Trading Dashboard Setup                 ║"
echo "║                                                            ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to check if port is in use
port_in_use() {
    if command_exists lsof; then
        lsof -i :"$1" >/dev/null 2>&1
    elif command_exists netstat; then
        netstat -an | grep ":$1 " >/dev/null 2>&1
    else
        # Can't check, assume available
        return 1
    fi
}

# Step 1: Check Prerequisites
echo -e "${BLUE}[1/5] Checking Prerequisites...${NC}"

if ! command_exists python && ! command_exists python3; then
    echo -e "${RED}❌ Python not found. Please install Python 3.8+${NC}"
    exit 1
fi

if ! command_exists curl; then
    echo -e "${YELLOW}⚠️  curl not found. Some checks may not work.${NC}"
fi

echo -e "${GREEN}✅ Prerequisites OK${NC}"
echo ""

# Step 2: Check Flask Backend
echo -e "${BLUE}[2/5] Checking Flask Backend...${NC}"

# Ask for backend location
read -p "Enter path to your OT-CORP repository (or press Enter for current directory): " BACKEND_PATH
BACKEND_PATH=${BACKEND_PATH:-.}

if [ ! -d "$BACKEND_PATH" ]; then
    echo -e "${RED}❌ Directory not found: $BACKEND_PATH${NC}"
    exit 1
fi

cd "$BACKEND_PATH"
echo -e "📁 Using backend at: ${GREEN}$(pwd)${NC}"

# Check for Flask app
if [ ! -f "app.py" ] && [ ! -f "main.py" ]; then
    echo -e "${YELLOW}⚠️  No app.py or main.py found${NC}"
    read -p "Enter your Flask app filename (e.g., server.py): " FLASK_APP
    if [ ! -f "$FLASK_APP" ]; then
        echo -e "${RED}❌ File not found: $FLASK_APP${NC}"
        exit 1
    fi
else
    FLASK_APP=${FLASK_APP:-app.py}
    if [ ! -f "app.py" ]; then
        FLASK_APP="main.py"
    fi
fi

echo -e "✅ Found Flask app: ${GREEN}$FLASK_APP${NC}"
echo ""

# Step 3: Install Dependencies
echo -e "${BLUE}[3/5] Checking Dependencies...${NC}"

# Check for flask-cors
PYTHON_CMD=$(command -v python3 || command -v python)

if ! $PYTHON_CMD -c "import flask_cors" 2>/dev/null; then
    echo -e "${YELLOW}⚠️  flask-cors not installed${NC}"
    read -p "Install flask-cors now? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Installing flask-cors..."
        $PYTHON_CMD -m pip install flask-cors
        echo -e "${GREEN}✅ flask-cors installed${NC}"
    else
        echo -e "${RED}❌ flask-cors is required. Exiting.${NC}"
        exit 1
    fi
else
    echo -e "${GREEN}✅ flask-cors already installed${NC}"
fi

echo ""

# Step 4: Check CORS Configuration
echo -e "${BLUE}[4/5] Checking CORS Configuration...${NC}"

if grep -q "flask_cors import CORS" "$FLASK_APP" && grep -q "CORS(app" "$FLASK_APP"; then
    echo -e "${GREEN}✅ CORS already configured in $FLASK_APP${NC}"
else
    echo -e "${YELLOW}⚠️  CORS not configured in $FLASK_APP${NC}"
    echo ""
    echo "Add these lines to your Flask app:"
    echo ""
    echo -e "${GREEN}from flask_cors import CORS${NC}"
    echo -e "${GREEN}CORS(app, origins=[\"http://localhost:5173\"])${NC}"
    echo ""
    read -p "Have you added CORS to your Flask app? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${RED}❌ Please add CORS and try again${NC}"
        exit 1
    fi
fi

echo ""

# Step 5: Start Services
echo -e "${BLUE}[5/5] Starting Services...${NC}"
echo ""

# Check if Flask port is available
FLASK_PORT=5050
if port_in_use $FLASK_PORT; then
    echo -e "${YELLOW}⚠️  Port $FLASK_PORT is already in use${NC}"
    read -p "Use a different port? (Enter port number or press Enter to kill existing process): " NEW_PORT
    if [ -n "$NEW_PORT" ]; then
        FLASK_PORT=$NEW_PORT
        echo -e "Using port: ${GREEN}$FLASK_PORT${NC}"
    fi
fi

# Start Flask backend
echo -e "${GREEN}🚀 Starting Flask Backend...${NC}"
echo ""

# Check if we need to update the port in Flask app
if [ "$FLASK_PORT" != "5050" ]; then
    echo -e "${YELLOW}⚠️  Backend will run on port $FLASK_PORT${NC}"
    echo "Make sure your Flask app uses: app.run(port=$FLASK_PORT)"
    echo ""
fi

# Start Flask
echo "Starting Flask on port $FLASK_PORT..."
echo "Command: $PYTHON_CMD $FLASK_APP"
echo ""

$PYTHON_CMD "$FLASK_APP" &
BACKEND_PID=$!

# Wait a bit for Flask to start
sleep 2

# Test if backend is running
if kill -0 $BACKEND_PID 2>/dev/null; then
    echo -e "${GREEN}✅ Flask backend started (PID: $BACKEND_PID)${NC}"
    
    # Test API endpoint if curl is available
    if command_exists curl; then
        sleep 1
        echo ""
        echo "Testing API endpoint..."
        if curl -s "http://localhost:$FLASK_PORT/api/status" >/dev/null 2>&1; then
            echo -e "${GREEN}✅ API responding at http://localhost:$FLASK_PORT/api/status${NC}"
        elif curl -s "http://localhost:$FLASK_PORT/api/health" >/dev/null 2>&1; then
            echo -e "${GREEN}✅ API responding at http://localhost:$FLASK_PORT/api/health${NC}"
        else
            echo -e "${YELLOW}⚠️  Backend started but API not responding yet${NC}"
            echo "Check Flask logs above for errors"
        fi
    fi
else
    echo -e "${RED}❌ Failed to start Flask backend${NC}"
    exit 1
fi

echo ""
echo "╔════════════════════════════════════════════════════════════╗"
echo "║                                                            ║"
echo "║                  🎉 SETUP COMPLETE! 🎉                    ║"
echo "║                                                            ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo ""
echo -e "${GREEN}✅ Flask Backend:${NC} http://localhost:$FLASK_PORT"
echo -e "${GREEN}✅ React Dashboard:${NC} Already running in Figma Make!"
echo ""
echo "Next Steps:"
echo "  1. Open your dashboard preview in Figma Make"
echo "  2. Press F12 to open browser console"
echo "  3. Check for successful API calls to localhost:$FLASK_PORT"
echo ""
echo "Useful commands:"
echo "  • Test API:  curl http://localhost:$FLASK_PORT/api/status"
echo "  • View logs: Check terminal output above"
echo "  • Stop backend: kill $BACKEND_PID"
echo ""

if [ "$FLASK_PORT" != "5050" ]; then
    echo -e "${YELLOW}⚠️  IMPORTANT: Your backend is on port $FLASK_PORT (not 5050)${NC}"
    echo "Create a .env file in your dashboard with:"
    echo -e "${GREEN}VITE_API_URL=http://localhost:$FLASK_PORT${NC}"
    echo ""
fi

echo "Documentation:"
echo "  • Quick Start:      QUICK_START.md"
echo "  • Backend Example:  FLASK_BACKEND_EXAMPLE.md"
echo "  • Troubleshooting:  TROUBLESHOOTING.md"
echo ""
echo "Press Ctrl+C to stop the Flask backend"
echo ""

# Wait for Flask to exit
wait $BACKEND_PID

echo ""
echo "Flask backend stopped."
