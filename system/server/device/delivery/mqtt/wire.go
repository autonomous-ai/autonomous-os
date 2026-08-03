package mqtthandler

import (
	"github.com/google/wire"
)

var ProviderSet = wire.NewSet(
	ProvideDeviceMQTTHandler,
	ProvideChatStream,
)
