FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY agent agent
COPY evals evals
COPY guard guard
COPY monitor monitor
COPY shop shop
COPY scenarios scenarios
COPY ui ui
RUN pip install --no-cache-dir -e ".[ui]"
ENV STORE_PATH=/data/investigator.sqlite
VOLUME /data
EXPOSE 8501
# Replay-only by default: no model key is baked in. Set LLM_* at run time to enable a real model.
CMD ["streamlit", "run", "ui/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
