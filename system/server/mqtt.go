package server

import (
	"context"
	"log/slog"

	"go.autonomous.ai/os/system/lib/safego"
	"go.autonomous.ai/os/system/lib/syspath"
	"go.autonomous.ai/os/system/server/config"
)

// restartMQTT stops the current MQTT client and starts a new one (e.g. when backend pushes new MQTT config).
func (s *Server) restartMQTT() {
	s.stopMQTT()
	if s.mqttFactory != nil {
		s.mqttFactory.UpdateConfig(config.ProvideMQTTConfig(s.config))
	}
	s.startMQTT()
}

// startMQTT creates a client from the factory, subscribes to the topic, and connects. Idempotent if already running.
func (s *Server) startMQTT() {
	s.mqttMu.Lock()
	if s.mqttClient != nil {
		s.mqttMu.Unlock()
		return
	}
	if s.mqttFactory == nil {
		s.mqttMu.Unlock()
		return
	}
	// Client IDs are derived from device_id, so two processes carrying the same
	// config fight over one broker session and neither stays connected.
	if !syspath.BackendUplink() {
		s.mqttMu.Unlock()
		slog.Info("backend uplink off — mqtt not started", "component", "mqtt")
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	client := s.mqttFactory.GetClient("os-server-" + s.config.DeviceID)
	slog.Info("subscribing to FA channel", "component", "mqtt", "topic", s.config.FAChannel)
	client.Subscribe(s.config.FAChannel, 1, func(topic string, payload []byte) {
		slog.Debug("message received", "component", "mqtt", "topic", topic, "payload", string(payload))
		s.deviceMQTTHandler.HandleMessage(topic, payload)
	})
	s.mqttClient = client
	s.mqttCancel = cancel
	s.mqttMu.Unlock()

	safego.Go("mqtt", func() {
		if err := client.Connect(ctx); err != nil && ctx.Err() == nil {
			slog.Error("connect failed", "component", "mqtt", "error", err)
		}
	})
}

// stopMQTT disconnects and clears the MQTT client. Safe to call when not connected.
func (s *Server) stopMQTT() {
	s.mqttMu.Lock()
	client := s.mqttClient
	cancel := s.mqttCancel
	s.mqttClient = nil
	s.mqttCancel = nil
	s.mqttMu.Unlock()

	if cancel != nil {
		cancel()
	}
	if client != nil {
		_ = client.Close()
	}
}
