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

# bgutil PO Token Provider (YouTube n-challenge fix)  ← NEW
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /app/bgutil && \
    cd /app/bgutil/server && \
    npm install && \
    npx tsc

# Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application
COPY . /app/

# Heroku supplies PORT; start bgutil provider + API together  ← CHANGED
CMD ["sh", "-c", "node /app/bgutil/server/build/main.js --port 4416 & python MusicApi.py"]
