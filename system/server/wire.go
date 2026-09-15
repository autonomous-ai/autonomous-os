//go:build wireinject

package server

import (
	"github.com/google/wire"

	"go.autonomous.ai/os/system/agent"
	"go.autonomous.ai/os/system/ambient"
	"go.autonomous.ai/os/system/beclient"
	"go.autonomous.ai/os/system/buddy"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/healthwatch"
	"go.autonomous.ai/os/system/lib/mqtt"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/network"
	"go.autonomous.ai/os/system/plugin"
	_agentHttp "go.autonomous.ai/os/system/server/agent/delivery/http"
	_buddyHttp "go.autonomous.ai/os/system/server/buddy/delivery/http"
	"go.autonomous.ai/os/system/server/config"
	_deviceHttpDeliver "go.autonomous.ai/os/system/server/device/delivery/http"
	_deviceMQTTDeliver "go.autonomous.ai/os/system/server/device/delivery/mqtt"
	_healthHttpDeliver "go.autonomous.ai/os/system/server/health/delivery/http"
	_networkHttpDeliver "go.autonomous.ai/os/system/server/network/delivery/http"
	_pluginHttp "go.autonomous.ai/os/system/server/plugin/delivery/http"
	_sensingHttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
	"go.autonomous.ai/os/system/statusled"
)

func initializeDeviceServer() (*Server, error) {
	panic(wire.Build(
		config.ProviderSet,
		mqtt.ProviderSet,
		beclient.ProviderSet,
		monitor.ProviderSet,
		agent.ProviderSet,
		network.ProviderSet,
		device.ProviderSet,
		buddy.ProviderSet,
		_buddyHttp.ProviderSet,
		ambient.ProviderSet,
		healthwatch.ProviderSet,
		statusled.ProviderSet,
		provideStatusLEDHasLight,
		provideAgentIsSleeping,
		_healthHttpDeliver.ProviderSet,
		_networkHttpDeliver.ProviderSet,
		_deviceHttpDeliver.ProviderSet,
		_deviceMQTTDeliver.ProviderSet,
		_agentHttp.ProviderSet,
		_sensingHttp.ProviderSet,
		plugin.ProviderSet,
		_pluginHttp.ProviderSet,
		ProvideServer,
	))
}
