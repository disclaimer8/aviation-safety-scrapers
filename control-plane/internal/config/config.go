package config

import "time"

const (
	DefaultAIAURL  = "https://www.icao.int/safety/AIG/AIA"
	DefaultRAIOURL = "https://www.icao.int/safety/regional-safety-cooperation/List-of-RAIOs-and-ICMs"
	DefaultMaxBody = int64(8 << 20)
)

type HTTP struct {
	UserAgent string
	Timeout   time.Duration
	MaxBytes  int64
	Retries   int
}

func DefaultHTTP() HTTP {
	return HTTP{
		UserAgent: "aviation-coverage-control-plane/1.0 (+https://github.com/denyskolomiiets/aviation-safety-scrapers)",
		Timeout:   30 * time.Second,
		MaxBytes:  DefaultMaxBody,
		Retries:   2,
	}
}
