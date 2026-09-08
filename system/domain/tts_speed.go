package domain

import (
	"fmt"
	"math"
)

// ValidateTTSSpeed validates an optional rate; omission preserves the saved value.
func ValidateTTSSpeed(speed *float64) error {
	if speed != nil && (math.IsNaN(*speed) || math.IsInf(*speed, 0) || *speed < 0.25 || *speed > 4.0) {
		return fmt.Errorf("tts speed must be a finite number between 0.25 and 4.0")
	}
	return nil
}
