package buddy

import (
	"github.com/google/wire"
)

// ProviderSet exposes buddy providers for Wire.
var ProviderSet = wire.NewSet(
	ProvideService,
)
