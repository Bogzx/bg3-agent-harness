# An MCP client sandbox for the bg3 server: no repo, no game files, no saves inside, and (with run.sh's --internal
# network) no route anywhere but the bg3 MCP server on the host. The image carries only the smoke client; a real
# agent client goes in the same way (see README.md).
FROM python:3.12-slim

RUN pip install --no-cache-dir "mcp==2.3.0"
RUN useradd --create-home --shell /usr/sbin/nologin agent
COPY mcp_smoke.py /app/mcp_smoke.py

USER agent
WORKDIR /home/agent
ENTRYPOINT ["python3", "/app/mcp_smoke.py"]
CMD ["http://172.30.99.1:8770/mcp"]
