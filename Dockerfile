
FROM nikolaik/python-nodejs:python3.11-nodejs20

WORKDIR /app

System dependencies

RUN apt-get update && apt-get install -y 
curl 
ca-certificates 
xz-utils 
&& rm -rf /var/lib/apt/lists/*

Install static FFmpeg

RUN curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz 
-o /tmp/ffmpeg.tar.xz && 
tar -xJf /tmp/ffmpeg.tar.xz -C /tmp && 
mv /tmp/ffmpeg--static/ffmpeg /usr/local/bin/ffmpeg && 
mv /tmp/ffmpeg--static/ffprobe /usr/local/bin/ffprobe && 
chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe && 
rm -rf /tmp/ffmpeg*

Install Deno

RUN curl -fsSL https://deno.land/install.sh | sh

ENV PATH="/root/.deno/bin:/usr/local/bin:$PATH"

Python dependencies

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

Application files

COPY . /app/

Heroku provides PORT dynamically

CMD ["sh", "-c", "python MusicApi.py"]
