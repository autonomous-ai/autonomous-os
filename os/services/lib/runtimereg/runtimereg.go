// Package runtimereg is a tiny neutral registry of binary-embedded agentic-
// backend installers, keyed by runtime name. Backend packages (internal/hermes,
// …) register their embedded install.sh from an init(); internal/device reads
// it back to materialize the installer on disk for switch-runtime.
//
// It exists only to break the import cycle that a direct internal/device →
// internal/hermes import would create (hermes → statusled → device). Having no
// dependencies of its own, runtimereg can be imported from either side.
package runtimereg

// installers + presyncs + versions are populated at init() time, before any
// runtime switch can fire, so no locking is needed for the read path.
var (
	installers = map[string][]byte{}
	presyncs   = map[string][]byte{}
	versions   = map[string]func() string{}
)

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

// RegisterPresync records a backend's embedded pre-start hook (runtime-<name>-presync).
// Materializing it from os-server — rather than letting the installer write it — is
// what lets a plain os-server OTA refresh the hook on disk: the installer only re-runs
// on a first install or a failed verify, so a config fix shipped only inside it would
// never reach an already-installed backend until the next reinstall.
func RegisterPresync(name string, script []byte) {
	presyncs[name] = script
}

// GetPresync returns the embedded pre-start hook for a runtime, or (nil, false) when
// the backend ships none (it then relies on whatever its installer wrote, if any).
func GetPresync(name string) ([]byte, bool) {
	s, ok := presyncs[name]
	return s, ok
}

// RegisterVersion records a backend's cached-version getter (e.g.
// openclaw.GetOpenClawVersion). Registered from the backend package's init();
// the getter itself reads a cache populated at startup, so calling it is cheap.
// Exists for the same cycle-breaking reason as Register: internal/device wants
// to report backend versions (backend ping) but cannot import the backend
// packages directly.
func RegisterVersion(name string, fn func() string) {
	versions[name] = fn
}

// Version returns the backend's installed version, or "" when the backend is
// not compiled into this binary or its version hasn't been probed yet.
func Version(name string) string {
	if fn, ok := versions[name]; ok {
		return fn()
	}
	return ""
}
