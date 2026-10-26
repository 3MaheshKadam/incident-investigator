FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY agent agent
COPY deploy deploy
COPY evals evals
COPY gateway gateway
COPY guard guard
COPY monitor monitor
COPY readiness readiness
COPY shop shop
COPY target target
COPY scenarios scenarios
COPY ui ui
RUN pip install --no-cache-dir -e ".[ui]"
# Run as an unprivileged user; /data is the only place the app writes.
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app:app /data
ENV STORE_PATH=/data/investigator.sqlite
VOLUME /data
EXPOSE 8501
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8501/_stcore/health', timeout=3)"
# Replay-only by default: no model key is baked in. Set LLM_* at run time to enable a real model.
CMD ["streamlit", "run", "ui/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
