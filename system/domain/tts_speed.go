package domain

import (
	"fmt"
	"math"
)

// ValidateTTSSpeed validates an optional rate; omission preserves the saved value.
func ValidateTTSSpeed(speed *float64) error {
	if speed != nil && (math.IsNaN(*speed) || math.IsInf(*speed, 0) || *speed < 0.7 || *speed > 1.2) {
		return fmt.Errorf("tts speed must be a finite number between 0.7 and 1.2")
	}
	return nil
}
