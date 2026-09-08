package http

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

func post(t *testing.T, body string) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/tracking/event", bytes.NewBufferString(body))
	c.Request.Header.Set("Content-Type", "application/json")
	ProvideTrackingHandler().PostEvent(c)
	return w
}

func TestPostEventAccepted(t *testing.T) {
	if w := post(t, `{"event_name":"voice_metrics_interaction","event_id":"e1","params":{"outcome":"acknowledged"}}`); w.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", w.Code, w.Body.String())
	}
}

func TestPostEventRequiresName(t *testing.T) {
	if w := post(t, `{"event_id":"e1"}`); w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400 for a nameless event", w.Code)
	}
}

func TestPostEventRejectsOversizedPayload(t *testing.T) {
	body := bytes.NewBufferString(`{"event_name":"x","params":{`)
	for i := 0; i < maxParams+1; i++ {
		if i > 0 {
			body.WriteString(",")
		}
		body.WriteString(`"k`)
		body.WriteString(string(rune('a' + i%26)))
		body.WriteString(string(rune('a' + i/26)))
		body.WriteString(`":1`)
	}
	body.WriteString(`}}`)
	if w := post(t, body.String()); w.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400 for too many params", w.Code)
	}
}
