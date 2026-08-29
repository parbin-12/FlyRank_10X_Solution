// PulseLog cache-cron service.
//
// Concept #4 (Background jobs / cron): a time.Ticker refreshes stats for every
// configured service on a fixed schedule, off the request path.
//
// Concept #6 (Caching): the Python API's /stats/{id} endpoint scans every event
// row for a service and is genuinely expensive to compute (it gets slower as
// event volume grows). This service pre-computes it on a schedule and serves
// it from an in-memory cache, so reads are instant regardless of how expensive
// the underlying computation is.
//
// Pure Go standard library, no external dependencies, so it builds offline
// with just `go build`.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

type cacheEntry struct {
	Data       json.RawMessage `json:"data"`
	FetchedAt  time.Time       `json:"fetched_at"`
	ServiceID  int             `json:"service_id"`
	FetchError string          `json:"fetch_error,omitempty"`
}

type statsCache struct {
	mu      sync.RWMutex
	entries map[int]cacheEntry
}

func newStatsCache() *statsCache {
	return &statsCache{entries: make(map[int]cacheEntry)}
}

func (c *statsCache) set(id int, data json.RawMessage, fetchErr error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry := cacheEntry{ServiceID: id, FetchedAt: time.Now()}
	if fetchErr != nil {
		// Keep the last good data if we have it; just record the error.
		if existing, ok := c.entries[id]; ok {
			entry.Data = existing.Data
			entry.FetchedAt = existing.FetchedAt
		}
		entry.FetchError = fetchErr.Error()
	} else {
		entry.Data = data
	}
	c.entries[id] = entry
}

func (c *statsCache) get(id int) (cacheEntry, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	e, ok := c.entries[id]
	return e, ok
}

func (c *statsCache) all() map[int]cacheEntry {
	c.mu.RLock()
	defer c.mu.RUnlock()
	out := make(map[int]cacheEntry, len(c.entries))
	for k, v := range c.entries {
		out[k] = v
	}
	return out
}

// config, loaded once from environment variables (12-factor style, no secrets in git)
type config struct {
	apiBase         string
	apiToken        string
	apiEmail        string
	apiPassword     string
	serviceIDs      []int
	refreshInterval time.Duration
	port            string
}

func loadConfig() config {
	apiBase := getenv("PULSELOG_API_BASE", "http://localhost:8000")
	apiToken := os.Getenv("PULSELOG_API_TOKEN")
	apiEmail := getenv("PULSELOG_API_EMAIL", "demo@pulselog.dev")
	apiPassword := getenv("PULSELOG_API_PASSWORD", "demo1234")
	idsRaw := getenv("PULSELOG_SERVICE_IDS", "1")
	intervalSec, err := strconv.Atoi(getenv("PULSELOG_REFRESH_SECONDS", "30"))
	if err != nil || intervalSec <= 0 {
		intervalSec = 30
	}
	port := getenv("PULSELOG_CACHE_PORT", "9000")

	var ids []int
	for _, part := range strings.Split(idsRaw, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		if n, err := strconv.Atoi(part); err == nil {
			ids = append(ids, n)
		}
	}
	if len(ids) == 0 {
		ids = []int{1}
	}

	return config{
		apiBase:         apiBase,
		apiToken:        apiToken,
		apiEmail:        apiEmail,
		apiPassword:     apiPassword,
		serviceIDs:      ids,
		refreshInterval: time.Duration(intervalSec) * time.Second,
		port:            port,
	}
}

// ensureToken obtains a bearer token if one wasn't supplied directly, by logging
// into the Python API with the demo credentials (or PULSELOG_API_EMAIL/PASSWORD).
// Retries with backoff since the API container may still be starting up.
func ensureToken(cfg *config) {
	if cfg.apiToken != "" {
		return
	}
	loginBody, _ := json.Marshal(map[string]string{"email": cfg.apiEmail, "password": cfg.apiPassword})
	client := &http.Client{Timeout: 5 * time.Second}

	for attempt := 1; attempt <= 20; attempt++ {
		resp, err := client.Post(cfg.apiBase+"/auth/login", "application/json", strings.NewReader(string(loginBody)))
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				var parsed struct {
					AccessToken string `json:"access_token"`
				}
				if jsonErr := json.Unmarshal(body, &parsed); jsonErr == nil && parsed.AccessToken != "" {
					cfg.apiToken = parsed.AccessToken
					log.Printf("auth: obtained token via login as %s (attempt %d)", cfg.apiEmail, attempt)
					return
				}
			}
		}
		log.Printf("auth: login attempt %d failed, retrying in 2s (%v)", attempt, err)
		time.Sleep(2 * time.Second)
	}
	log.Printf("auth: giving up obtaining a token after retries; cron refreshes will fail until PULSELOG_API_TOKEN is set")
}

func getenv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// fetchStats calls the Python API's expensive /stats/{id} endpoint.
func fetchStats(cfg config, serviceID int) (json.RawMessage, error) {
	url := fmt.Sprintf("%s/stats/%d", cfg.apiBase, serviceID)
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	if cfg.apiToken != "" {
		req.Header.Set("Authorization", "Bearer "+cfg.apiToken)
	}

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("upstream returned %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return json.RawMessage(body), nil
}

// runCronLoop is the background job: refresh every configured service on a fixed schedule.
func runCronLoop(cfg config, cache *statsCache, stop <-chan struct{}) {
	refresh := func() {
		for _, id := range cfg.serviceIDs {
			data, err := fetchStats(cfg, id)
			cache.set(id, data, err)
			if err != nil {
				log.Printf("cron: refresh service %d failed: %v", id, err)
			} else {
				log.Printf("cron: refreshed service %d (%d bytes)", id, len(data))
			}
		}
	}

	refresh() // warm the cache immediately on startup, then settle into the schedule
	ticker := time.NewTicker(cfg.refreshInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			refresh()
		case <-stop:
			return
		}
	}
}

func main() {
	cfg := loadConfig()
	ensureToken(&cfg)
	cache := newStatsCache()
	stop := make(chan struct{})

	log.Printf("pulselog cache-cron starting | api_base=%s | services=%v | refresh_every=%s | port=%s",
		cfg.apiBase, cfg.serviceIDs, cfg.refreshInterval, cfg.port)

	go runCronLoop(cfg, cache, stop)

	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
	})

	// GET /cache/stats/{id} -> instant, cached response
	mux.HandleFunc("/cache/stats/", func(w http.ResponseWriter, r *http.Request) {
		idStr := strings.TrimPrefix(r.URL.Path, "/cache/stats/")
		id, err := strconv.Atoi(idStr)
		if err != nil {
			http.Error(w, `{"error":"invalid service id"}`, http.StatusBadRequest)
			return
		}
		entry, ok := cache.get(id)
		if !ok {
			http.Error(w, `{"error":"no cached data yet for this service"}`, http.StatusNotFound)
			return
		}

		age := time.Since(entry.FetchedAt)
		status := "fresh"
		if age > cfg.refreshInterval*2 {
			status = "stale"
		}

		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Cache-Status", status)
		w.Header().Set("X-Cache-Age-Seconds", fmt.Sprintf("%.1f", age.Seconds()))
		if entry.FetchError != "" {
			w.Header().Set("X-Cache-Last-Error", entry.FetchError)
		}
		w.Write(entry.Data)
	})

	// GET /cache/status -> dashboard of every cached service and its freshness
	mux.HandleFunc("/cache/status", func(w http.ResponseWriter, r *http.Request) {
		all := cache.all()
		type row struct {
			ServiceID   int     `json:"service_id"`
			AgeSeconds  float64 `json:"age_seconds"`
			LastError   string  `json:"last_error,omitempty"`
			HasData     bool    `json:"has_data"`
		}
		out := make([]row, 0, len(all))
		for id, e := range all {
			out = append(out, row{
				ServiceID:  id,
				AgeSeconds: time.Since(e.FetchedAt).Seconds(),
				LastError:  e.FetchError,
				HasData:    e.Data != nil,
			})
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"refresh_interval_seconds": cfg.refreshInterval.Seconds(),
			"services":                 out,
		})
	})

	log.Fatal(http.ListenAndServe(":"+cfg.port, mux))
}
