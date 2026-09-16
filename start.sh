#!/bin/sh

echo "🚀 Starting bgutil PO token provider on port 4416..."

# Explicit path — PATH env pe depend mat karo
/usr/local/bin/node /app/bgutil/server/build/main.js --port 4416 &

sleep 4

if curl -s http://127.0.0.1:4416/ping > /dev/null; then
    echo "✅ bgutil PO token provider is UP"
else
    echo "⚠️ bgutil PO token provider is DOWN"
fi

echo "🚀 Starting Music API..."
exec python MusicApi.py
