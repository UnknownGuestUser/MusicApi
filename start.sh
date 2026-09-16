#!/bin/sh

# Start bgutil PO token provider in background
echo "🚀 Starting bgutil PO token provider on port 4416..."
node /app/bgutil/server/build/main.js --port 4416 &

# Give it 3 seconds to bind the port
sleep 3

# Verify it's up
if curl -s http://127.0.0.1:4416/ping > /dev/null; then
    echo "✅ bgutil PO token provider is UP"
else
    echo "⚠️ bgutil PO token provider is DOWN — YouTube may fail"
fi

# Start the FastAPI app (foreground)
echo "🚀 Starting Music API..."
exec python MusicApi.py
