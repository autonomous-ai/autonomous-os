package gatewayd

import (
	"errors"
	"testing"
)

type uncertainWriter struct{ partial bool }

func (w uncertainWriter) Write(p []byte) (int, error) {
	if w.partial {
		return len(p) / 2, nil
	}
	return len(p), errors.New("write failed after delivery")
}

func TestPipeWriteFailureNeverReplaysPotentiallyDeliveredAction(t *testing.T) {
	for _, partial := range []bool{false, true} {
		s := &Server{}
		s.writePipe(uncertainWriter{partial: partial}, []byte(`{"type":"user","message":{"content":"click submit"}}`))
		if len(s.pending) != 0 {
			t.Fatal("uncertain action queued for duplicate execution")
		}
	}
}

func TestAbsentChildRetainsDefinitelyUnsentInput(t *testing.T) {
	s := &Server{}
	s.writeStdin([]byte(`{"type":"user","message":{"content":"open Notes"}}`))
	if len(s.pending) != 1 {
		t.Fatal("definitely unsent input discarded")
	}
}
