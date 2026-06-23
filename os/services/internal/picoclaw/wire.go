package picoclaw

import (
	"github.com/google/wire"
)

var ProviderSet = wire.NewSet(
	ProvideService,
)
