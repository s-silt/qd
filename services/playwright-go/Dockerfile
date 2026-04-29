# QD Playwright-Go Sidecar
# Lightweight Go replacement for services/playwright (Python/Playwright).
# Target image size: <250 MB (vs Python version ~1.5 GB).
#
# Build context: services/playwright-go/
# Usage:
#   docker build -f services/playwright-go/Dockerfile services/playwright-go/
#   docker run -p 8924:8924 qd-playwright-go

# ---- Stage 1: Build static binary ----
FROM golang:1.21-alpine AS build

WORKDIR /src

# Copy go module files first for layer caching
COPY go.mod go.sum ./
RUN go mod download

# Copy source
COPY *.go ./

# Build a fully static binary (CGO_ENABLED=0)
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /playwright-go .

# ---- Stage 2: Runtime (headless Chrome base) ----
# chromedp/headless-shell contains a headless-only Chromium build (~130 MB).
# No X server required; all libs are bundled.
FROM chromedp/headless-shell:131.0.6778.86

LABEL org.opencontainers.image.description="QD Playwright-Go sidecar for URL → HAR auto-capture (lightweight Go version)"

# Copy binary from build stage
COPY --from=build /playwright-go /usr/local/bin/playwright-go

# Environment defaults (override via docker run -e or docker-compose)
ENV HEADLESS=true
ENV MAX_CONCURRENT=2
ENV DEFAULT_TIMEOUT_MS=60000
ENV PORT=8924
# Chromium flags passed by the Go binary already disable sandbox;
# some environments also need this env var:
ENV DBUS_SESSION_BUS_ADDRESS=/dev/null

EXPOSE 8924

ENTRYPOINT ["/usr/local/bin/playwright-go"]
