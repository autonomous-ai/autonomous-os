package mqtt

// Factory creates MQTT clients for publishing (or subscribe). Each CreateClient() returns
// a new client with the factory's config; client IDs are generated so multiple clients can coexist.
type Factory struct {
	config Config
}

// ProvideFactory creates a factory from config. Use CreateClient() to get a new client to Connect and Publish.
func ProvideFactory(cfg Config) (*Factory, error) {
	return &Factory{config: cfg}, nil
}

// UpdateConfig refreshes the factory's connection config. Call before restartMQTT
// to pick up new credentials written during setup.
func (f *Factory) UpdateConfig(cfg Config) {
	f.config = cfg
}

// CreateClient returns a new MQTT client using the factory's config. Each client gets a unique client ID.
// Call Connect(ctx) then Publish(ctx, topic, qos, payload) (and Close() when done).
func (f *Factory) GetClient(clientID string) *MQTT {
	return ProvideClient(Options{
		Endpoint: f.config.Endpoint,
		Port:     f.config.Port,
		Username: f.config.Username,
		Password: f.config.Password,
		ClientID: clientID,
	})
}
