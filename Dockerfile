FROM mwader/static-ffmpeg:latest AS ffmpeg
FROM python:slim
COPY --from=ffmpeg /ffmpeg /usr/local/bin/
RUN pip install --no-cache-dir aiohttp
WORKDIR /app
COPY app/* /app
RUN mkdir /config
EXPOSE 5004
CMD ["python", "-m", "tuner", "/config/tuner.cfg"]