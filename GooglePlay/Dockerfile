FROM public.ecr.aws/docker/library/golang:1.25-bookworm AS go-toolchain

FROM public.ecr.aws/docker/library/node:20-bookworm AS builder

ENV PATH="/usr/local/go/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY --from=go-toolchain /usr/local/go /usr/local/go

WORKDIR /app/GooglePlay

COPY GooglePlay/package.json GooglePlay/package-lock.json ./
RUN npm ci

COPY GooglePlay/ ./
COPY demo.xlsx apps.json /app/
RUN npm run build

FROM public.ecr.aws/docker/library/node:20-bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /root/.appstore_google_updata/GooglePlay/dist ./dist

ENV GOOGLE_PLAY_WEB_HOST=0.0.0.0 \
    PORT=8787 \
    HOME=/data

RUN mkdir -p /data/.appstore_google_updata/GooglePlay/jobs /data/.gplay/credentials \
    && chown -R node:node /app /data

USER node

EXPOSE 8787

CMD ["node", "dist/start.mjs"]
