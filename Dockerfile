# Etapa 1: compila la interfaz (Node solo existe en esta etapa)
FROM node:22-slim AS ui
WORKDIR /ui
COPY vexinter/app/package.json vexinter/app/package-lock.json ./
RUN npm ci
COPY vexinter/app/ ./
RUN ./node_modules/.bin/vite build

# Etapa 2: el servicio
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml constraints.txt README.md ./
COPY vexinter ./vexinter
COPY sql ./sql
COPY services ./services
COPY examples ./examples
COPY --from=ui /ui/dist ./vexinter/app/dist
RUN pip install --no-cache-dir -c constraints.txt ".[server]"

# Usuario no privilegiado
RUN useradd --create-home --uid 10001 vexinter
USER vexinter

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "services.app:app", "--host", "0.0.0.0", "--port", "8000"]
