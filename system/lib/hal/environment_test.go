package hal

import (
	"io"
	"net/http"
	"strings"
	"testing"
)

type environmentTransport func(*http.Request) (*http.Response, error)

func (f environmentTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestGetEnvironmentStatus(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	for _, tc := range []struct {
		name, body string
		code       int
		wantErr    bool
	}{
		{"future sensor fields", `{"state":"ready","stale":false,"sample":{"co2_ppm":412,"voc_index":null}}`, 200, false},
		{"disabled", `{"state":"disabled","stale":true,"sample":null}`, 200, false},
		{"missing route", `{}`, 404, true},
		{"invalid json", `not json`, 200, true},
		{"missing freshness", `{"state":"ready"}`, 200, true},
		{"oversized", strings.Repeat(" ", 65537), 200, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
				if r.Method != "GET" || r.URL.Path != "/environment/status" {
					t.Fatalf("unexpected request %s %s", r.Method, r.URL)
				}
				return &http.Response{StatusCode: tc.code, Body: io.NopCloser(strings.NewReader(tc.body))}, nil
			})}
			got, err := GetEnvironmentStatus()
			if (err != nil) != tc.wantErr {
				t.Fatalf("unexpected error %v", err)
			}
			if err == nil && string(got) != tc.body {
				t.Fatalf("snapshot changed: %s", got)
			}
		})
	}
}
