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

# Default command: run the web server
CMD ["./aircrash-parser", "--serve"]
