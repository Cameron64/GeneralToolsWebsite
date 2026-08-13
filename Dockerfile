# Stage 1: Base build stage
FROM python:3.13-slim AS builder

# Create the app directory
RUN mkdir /app

# Set the working directory
WORKDIR /app 

# Set environment variables to optimize Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1 

# Upgrade pip and install dependencies
RUN pip install --upgrade pip 

# Copy the requirements file first (better caching)
COPY requirements.txt /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Set environment variable to prevent interactive prompts during installation
# ENV DEBIAN_FRONTEND=noninteractive
# RUN apt-get update && apt-get install -y \
#     wget \
#     curl \
#     gnupg \
#     ca-certificates \
#     unzip \
#     fonts-liberation \
#     libappindicator3-1 \
#     libasound2 \
#     libatk-bridge2.0-0 \
#     libatk1.0-0 \
#     libcups2 \
#     libdbus-1-3 \
#     libgdk-pixbuf-xlib-2.0-0 \
#     libnspr4 \
#     libnss3 \
#     libx11-xcb1 \
#     libxcomposite1 \
#     libxdamage1 \
#     libxrandr2 \
#     xdg-utils \
#     libglib2.0-0 \
#     --no-install-recommends && \
#     rm -rf /var/lib/apt/lists/*
# # Install dependencies and Chrome
# #RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - &&  echo "deb http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list
# RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --no-tty --dearmor -o /usr/share/keyrings/google-linux-keyring.gpg
# RUN echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list
# RUN apt-get update && apt-get install -y google-chrome-stable && rm -rf /var/lib/apt/lists/*/*

# Stage 2: Production stage
FROM python:3.13-slim

RUN useradd -m -r appuser && \
   mkdir /app && \
   chown -R appuser /app

# Copy the Python dependencies from the builder stage
COPY --from=builder /usr/local/lib/python3.13/site-packages/ /usr/local/lib/python3.13/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# Set the working directory
WORKDIR /app
RUN mkdir -p /var/www/tools-website/static && chown -R appuser:appuser /var/www/tools-website/static
RUN mkdir -p /data && chown -R appuser:appuser /data

# Copy application code
COPY --chown=appuser:appuser . .

# Set environment variables to optimize Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1 

COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

# Switch to non-root user
USER appuser

# Expose the application port
EXPOSE 8000 

# @ DEMO ONLY - do not carry this hunk upstream.
#
# Upstream this is `CMD ["/entrypoint.sh"]`, which is correct for the nginx +
# selenium compose stack: gunicorn on a fixed 8000 behind nginx, and real
# secrets mounted in.
#
# The Railway demo box has neither. It needs railway-entrypoint.sh, which
# writes the stub secrets.json that DEBUG=False insists on, then serves with
# `runserver --insecure` on $PORT because there is no nginx to hand static
# files to. That was supposed to come from railway.json's startCommand, but
# railway.json is untracked, so any upload path that ships committed blobs
# drops it - and Docker then falls back to this CMD, gunicorn boots with no
# secrets.json, and the box 502s with a stack trace that names gunicorn and
# never mentions the missing file.
#
# Pointing CMD at the demo entrypoint makes the box boot correctly whether or
# not railway.json is read. If it IS read, its startCommand is this same
# command, so the two agree rather than fight.
#
# The demo scripts get their OWN COPY lines rather than being picked up by the
# `COPY . .` above. Two reasons, both learned the hard way on 2026-08-13:
#
# A missing file under `COPY . .` fails at RUN time, as `bash: no such file`,
# ten restarts deep, with the deploy still marked SUCCESS. A missing file in an
# explicit COPY fails the BUILD, immediately, naming the file. When a script is
# load-bearing for boot, the loud failure is the one worth having.
#
# And /entrypoint.sh is already baked this way, which is exactly why an LF fix
# to it took effect on a deploy where /app/railway-entrypoint.sh was still
# absent - the dedicated COPY layer and the `COPY . .` layer do not necessarily
# carry the same context. Do not assume one proves the other.
# No chmod +x on these. These lines sit after `USER appuser`, so a chmod of a
# root-owned file at / fails the build with "Operation not permitted" - and the
# execute bit is not needed anyway, because the CMD below invokes the script as
# an argument to bash rather than executing it directly.
COPY railway-entrypoint.sh /railway-entrypoint.sh
COPY railway-seed.py /app/railway-seed.py

# The pre-flight listing is deliberate and cheap. If this ever fails again, the
# first log line says what the container actually has in /app instead of
# leaving it to be guessed at from the outside.
CMD ["bash", "-c", "echo '--- /app contents ---'; ls -1 /app | head -40; echo '--- booting ---'; exec bash /railway-entrypoint.sh"]