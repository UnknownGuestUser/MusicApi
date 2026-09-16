FROM nikolaik/python-nodejs:python3.11-nodejs20

WORKDIR /app

# System tools
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    xz-utils \
    git \
    && rm -rf /var/lib/apt/lists/*

# Static FFmpeg
RUN curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
    -o /tmp/ffmpeg.tar.xz && \
    tar -xJf /tmp/ffmpeg.tar.xz -C /tmp && \
    mv /tmp/ffmpeg-*-static/ffmpeg /usr/local/bin/ffmpeg && \
    mv /tmp/ffmpeg-*-static/ffprobe /usr/local/bin/ffprobe && \
    chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe && \
    rm -rf /tmp/ffmpeg*

# Deno for yt-dlp JavaScript challenges
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:/usr/local/bin:$PATH"

# ✅ CHANGE 1: Node PATH ko globally set karo
ENV NODE_PATH=/usr/local/lib/node_modules
ENV PATH="/usr/local/bin:/usr/local/lib/node_modules/.bin:$PATH"

# ✅ CHANGE 2: bgutil ko explicitly build karo + verify karo
RUN echo "=== Node version ===" && node --version && npm --version && \
    echo "=== Cloning bgutil ===" && \
    git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /app/bgutil && \
    cd /app/bgutil/server && \
    echo "=== npm install ===" && \
    npm install && \
    echo "=== npm run build (not npx tsc) ===" && \
    npm run build && \
    echo "=== Verify build ===" && \
    ls -la /app/bgutil/server/build/ || \
    (echo "❌ BUILD FAILED" && exit 1)

# Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application
COPY . /app/

# ✅ CHANGE 3: chmod +x + explicit shell
RUN chmod +x /app/start.sh && ls -la /app/start.sh

CMD ["sh", "/app/start.sh"]
