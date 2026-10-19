FROM mwader/static-ffmpeg:latest AS ffmpeg
FROM python:slim
COPY --from=ffmpeg /ffmpeg /usr/local/bin/
RUN pip install --no-cache-dir requests
WORKDIR /app
COPY *.py /app
COPY tuner.cfg.dist /app/tuner.cfg
EXPOSE 5004
CMD ["python", "-m", "tuner", "tuner.cfg"]

