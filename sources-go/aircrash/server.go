package main

import (
	"database/sql"
	"log"
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"
)

// StartServer initializes the Gin router and starts listening for HTTP requests.
func StartServer(db *sql.DB, port string) {
	// Start background geocoder
	StartGeocoder(db)

	gin.SetMode(gin.ReleaseMode)
	router := gin.Default()

	// Serve static files from the "static" directory
	router.Static("/static", "./static")

	// Route for the main dashboard
	router.GET("/", func(c *gin.Context) {
		c.File("./static/index.html")
	})

	// API endpoint to get accidents
	router.GET("/api/accidents", func(c *gin.Context) {
		limitStr := c.DefaultQuery("limit", "100")
		offsetStr := c.DefaultQuery("offset", "0")

		limit, err := strconv.Atoi(limitStr)
		if err != nil {
			limit = 100
		}

		offset, err := strconv.Atoi(offsetStr)
		if err != nil {
			offset = 0
		}

		accidents, err := GetAccidents(db, limit, offset)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}

		c.Header("Cache-Control", "public, max-age=60") // Cache for 1 minute
		c.JSON(http.StatusOK, gin.H{
			"data":   accidents,
			"limit":  limit,
			"offset": offset,
		})
	})

	router.GET("/api/stats/aircrafts", func(c *gin.Context) {
		stats, err := GetAircraftStats(db)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		c.Header("Cache-Control", "public, max-age=300") // Cache for 5 mins
		c.JSON(http.StatusOK, stats)
	})

	router.GET("/api/stats/operators", func(c *gin.Context) {
		stats, err := GetOperatorStats(db)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		c.Header("Cache-Control", "public, max-age=300")
		c.JSON(http.StatusOK, stats)
	})

	// Lightweight map endpoint returning only geocoded locations
	router.GET("/api/map_data", func(c *gin.Context) {
		query := `
			SELECT id, aircraft_model, fatalities, lat, lon 
			FROM accidents 
			WHERE lat IS NOT NULL AND lat != 0.000001 AND lat != 0
		`
		rows, err := db.Query(query)
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		defer rows.Close()

		type MapPoint struct {
			ID         int     `json:"id"`
			Model      string  `json:"model"`
			Fatalities string  `json:"fatalities"`
			Lat        float64 `json:"lat"`
			Lon        float64 `json:"lon"`
		}

		var points []MapPoint
		for rows.Next() {
			var p MapPoint
			if err := rows.Scan(&p.ID, &p.Model, &p.Fatalities, &p.Lat, &p.Lon); err == nil {
				points = append(points, p)
			}
		}

		c.Header("Cache-Control", "public, max-age=300")
		c.JSON(http.StatusOK, points)
	})

	log.Printf("Server starting on http://localhost%s\n", port)
	if err := router.Run(port); err != nil {
		log.Fatalf("Failed to start server: %v", err)
	}
}
