package config

// EnvironmentSettings returns an isolated snapshot for the polling worker.
// Missing configuration uses defaults without rewriting an existing config file.
func (c *Config) EnvironmentSettings() EnvironmentConfig {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.Environment == nil {
		return DefaultEnvironmentConfig()
	}
	return c.Environment.Clone()
}
