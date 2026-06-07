# Builder stage
FROM golang:1.21-bookworm AS builder

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

# Install Chromium and required certificates/dependencies for go-rod
RUN apt-get update && apt-get install -y \
    chromium \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy the binary from the builder
COPY --from=builder /app/aircrash-parser .

# Copy static assets
COPY --from=builder /app/static ./static

# Expose the web server port
EXPOSE 8080

# Environment variables for Chromium to run smoothly in Docker
ENV ROD_BIN=/usr/bin/chromium
ENV XDG_CONFIG_HOME=/tmp/.chromium
ENV XDG_CACHE_HOME=/tmp/.chromium

# Default command: run the web server
CMD ["./aircrash-parser", "--serve"]
