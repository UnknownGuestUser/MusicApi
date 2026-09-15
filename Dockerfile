FROM nikolaik/python-nodejs:python3.11-nodejs20

WORKDIR /app

# System tools
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    xz-utils \
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

# Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application
COPY . /app/

# Heroku supplies PORT; MusicApi.py reads it.
CMD ["sh", "-c", "python MusicApi.py"]
