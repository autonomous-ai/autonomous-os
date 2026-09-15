package bridge

import (
	"bytes"
	"encoding/json"
	"io"
	"strconv"
	"unicode/utf8"

	"go.autonomous.ai/os/system/lib/internbridge"
)

// Decode exact string fields without last-key-wins, case folding, nulls,
// surrogate replacement, nested values, or silently ignored extensions.
func decodeRequest(raw []byte) (internbridge.Request, error) {
	var req internbridge.Request
	bad := internbridge.ErrInvalidRequest
	if !utf8.Valid(raw) || !validSurrogates(raw) {
		return req, bad
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	if token, err := d.Token(); err != nil || token != json.Delim('{') {
		return req, bad
	}
	seen := make(map[string]bool)
	for d.More() {
		token, err := d.Token()
		key, ok := token.(string)
		if err != nil || !ok || seen[key] {
			return req, bad
		}
		seen[key] = true
		token, err = d.Token()
		value, ok := token.(string)
		if err != nil || !ok {
			return req, bad
		}
		switch key {
		case "text":
			req.Text = value
		case "operation":
			req.Operation = internbridge.Operation(value)
		case "data_class":
			req.DataClass = internbridge.DataClass(value)
		case "run_id":
			if value == "" {
				return req, bad
			}
			req.RunID = value
		default:
			return req, bad
		}
	}
	if token, err := d.Token(); err != nil || token != json.Delim('}') {
		return req, bad
	}
	if _, err := d.Token(); err != io.EOF {
		return req, bad
	}
	return req, nil
}

// encoding/json otherwise replaces unpaired UTF-16 surrogates with U+FFFD.
func validSurrogates(raw []byte) bool {
	for i := 0; i < len(raw); i++ {
		if raw[i] != '\\' {
			continue
		}
		i++
		if i >= len(raw) {
			return false
		}
		if raw[i] != 'u' {
			continue
		}
		if i+4 >= len(raw) {
			return false
		}
		code, err := strconv.ParseUint(string(raw[i+1:i+5]), 16, 16)
		if err != nil {
			return false
		}
		i += 4
		if code >= 0xdc00 && code <= 0xdfff {
			return false
		}
		if code < 0xd800 || code > 0xdbff {
			continue
		}
		if i+6 >= len(raw) || raw[i+1] != '\\' || raw[i+2] != 'u' {
			return false
		}
		low, err := strconv.ParseUint(string(raw[i+3:i+7]), 16, 16)
		if err != nil || low < 0xdc00 || low > 0xdfff {
			return false
		}
		i += 6
	}
	return true
}
