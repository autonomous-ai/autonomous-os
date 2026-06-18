// Package runtimereg is a tiny neutral registry of binary-embedded agentic-
// backend installers, keyed by runtime name. Backend packages (internal/hermes,
// …) register their embedded install.sh from an init(); internal/device reads
// it back to materialize the installer on disk for switch-runtime.
//
// It exists only to break the import cycle that a direct internal/device →
// internal/hermes import would create (hermes → statusled → device). Having no
// dependencies of its own, runtimereg can be imported from either side.
package runtimereg

// installers is populated at init() time, before any runtime switch can fire, so
// no locking is needed for the read path.
var installers = map[string][]byte{}

// Register records a backend's embedded installer. Called from a backend
// package's init(); the last registration for a name wins.
func Register(name string, script []byte) {
	installers[name] = script
}

// Get returns the embedded installer for a runtime, or (nil, false) when that
// backend is not compiled into this binary (switch-runtime then falls back to
// the CDN).
func Get(name string) ([]byte, bool) {
	s, ok := installers[name]
	return s, ok
}
