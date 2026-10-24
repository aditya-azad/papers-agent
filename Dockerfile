# syntax=docker/dockerfile:1

# papers-agent MCP server, served over Streamable HTTP on port 8000.
# The server is stateless (arXiv / alphaXiv are the only upstreams) and needs
# no credentials; PDF/image output paths passed to the tools should live under
# /data, which is the conventional writable mount for this container.

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 papers \
    && mkdir -p /data \
    && chown -R papers:papers /data

WORKDIR /app
COPY --chown=papers:papers . .
RUN pip install --no-cache-dir .

USER papers
EXPOSE 8000

ENTRYPOINT ["papers-agent", "--http", "--host", "0.0.0.0", "--port", "8000"]
