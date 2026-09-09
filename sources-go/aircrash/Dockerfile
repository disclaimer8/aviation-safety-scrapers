# Builder stage
#
# Must be >= the version go.mod requires (1.26.2). It was pinned at 1.21, so
# this image could not build the module at all — the Dockerfile had been
# broken since the toolchain moved and nothing caught it, because CI builds
# the module directly and never builds the image.
FROM golang:1.26-bookworm AS builder

WORKDIR /app

# Cache dependencies
COPY go.mod go.sum ./
RUN go mod download

# Copy source code
COPY . .

# Build the application with CGO enabled (required for go-sqlite3)
RUN CGO_ENABLED=1 GOOS=linux go build -o aircrash-parser .

# Final stage
FROM debian:bookworm-slim

WORKDIR /app

# Required certificates/timezone data only — no headless browser is needed;
# the only remaining scraper (Wikidata) talks to a plain SPARQL HTTP endpoint.
RUN apt-get update && apt-get install -y \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy the binary from the builder
COPY --from=builder /app/aircrash-parser .

# Copy static assets
COPY --from=builder /app/static ./static

# Expose the web server port
EXPOSE 8080

# Default command: run the web server. The binary binds loopback by default,
# so inside a container it needs an explicit address for a published port to
# reach it — docker-compose.yml passes one and publishes on 127.0.0.1 only.
CMD ["./aircrash-parser", "-serve", "-addr", "0.0.0.0:8080"]
