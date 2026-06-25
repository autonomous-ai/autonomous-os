package buddy

import (
	"fmt"
	"log"

	"github.com/godbus/dbus/v5"
	"github.com/godbus/dbus/v5/introspect"
)

// agentPath is the D-Bus object path BlueZ will call back into when it
// needs pairing input/output. The path is arbitrary but must be stable
// for the lifetime of the process.
const agentPath = "/ai/autonomous/buddy/agent"

// Agent implements org.bluez.Agent1 with DisplayOnly capability per the
// Claude Desktop Hardware Buddy spec. The reference firmware on the
// M5StickC Plus shows a 6-digit passkey on its display; we have no
// display, so we log it to the journal and to the standard log output —
// the operator reads it from `journalctl -u claude-desktop-buddy` and types it
// into Claude Desktop's pairing prompt.
type Agent struct{}

// Release is called when BlueZ unregisters the agent (e.g. on shutdown).
func (a *Agent) Release() *dbus.Error {
	log.Println("[agent] released")
	return nil
}

// Cancel is called when BlueZ cancels an in-progress pairing.
func (a *Agent) Cancel() *dbus.Error {
	log.Println("[agent] cancel pairing")
	return nil
}

// AuthorizeService is called when a remote tries to use a service that
// requires user authorization. We blanket-accept since access control
// is enforced at the GATT layer via the secure-read/secure-write flags.
func (a *Agent) AuthorizeService(device dbus.ObjectPath, uuid string) *dbus.Error {
	log.Printf("[agent] authorize service %s on %s", uuid, device)
	return nil
}

// DisplayPasskey is called by BlueZ during pairing to show the passkey
// the user must enter on the remote (the desktop). `entered` is the
// number of digits the user has typed so far on the remote side; we
// just log the full passkey.
func (a *Agent) DisplayPasskey(device dbus.ObjectPath, passkey uint32, entered uint16) *dbus.Error {
	log.Printf("[agent] PAIRING PASSKEY for %s: %06d (entered %d/6)", device, passkey, entered)
	return nil
}

// DisplayPinCode is the legacy BR/EDR pairing equivalent of DisplayPasskey.
// Should not fire on LE-only flows but provided for completeness.
func (a *Agent) DisplayPinCode(device dbus.ObjectPath, pincode string) *dbus.Error {
	log.Printf("[agent] PAIRING PIN for %s: %s", device, pincode)
	return nil
}

// RequestPasskey is called when BlueZ needs the user to type a passkey
// on the device. We're DisplayOnly so this should never be called; if
// it is, return 0 to signal failure.
func (a *Agent) RequestPasskey(device dbus.ObjectPath) (uint32, *dbus.Error) {
	log.Printf("[agent] WARN: RequestPasskey called on DisplayOnly agent for %s", device)
	return 0, dbus.NewError("org.bluez.Error.Rejected", nil)
}

// RequestPinCode is the legacy BR/EDR equivalent. Not used in LE flows.
func (a *Agent) RequestPinCode(device dbus.ObjectPath) (string, *dbus.Error) {
	log.Printf("[agent] WARN: RequestPinCode called on DisplayOnly agent for %s", device)
	return "", dbus.NewError("org.bluez.Error.Rejected", nil)
}

// RequestConfirmation is called for Just Works / Numeric Comparison
// pairing — not the DisplayOnly path. Auto-accept so headless setups
// work; the GATT-level security flags still enforce encryption.
func (a *Agent) RequestConfirmation(device dbus.ObjectPath, passkey uint32) *dbus.Error {
	log.Printf("[agent] confirm pairing for %s passkey=%06d (auto-accept)", device, passkey)
	return nil
}

// RequestAuthorization is called when bonding without numeric comparison
// is requested. Auto-accept.
func (a *Agent) RequestAuthorization(device dbus.ObjectPath) *dbus.Error {
	log.Printf("[agent] authorization for %s (auto-accept)", device)
	return nil
}

// agentIntrospectXML satisfies BlueZ's introspection probe. Without
// this, some BlueZ versions reject the agent registration.
const agentIntrospectXML = `<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="RequestPinCode"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
    <method name="DisplayPinCode"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="RequestPasskey"><arg type="o" direction="in"/><arg type="u" direction="out"/></method>
    <method name="DisplayPasskey"><arg type="o" direction="in"/><arg type="u" direction="in"/><arg type="q" direction="in"/></method>
    <method name="RequestConfirmation"><arg type="o" direction="in"/><arg type="u" direction="in"/></method>
    <method name="RequestAuthorization"><arg type="o" direction="in"/></method>
    <method name="AuthorizeService"><arg type="o" direction="in"/><arg type="s" direction="in"/></method>
    <method name="Cancel"/>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg type="s" direction="out"/></method>
  </interface>
</node>`

// registerBluezAgent exports our Agent on the system D-Bus and asks
// BlueZ to use it as the default with DisplayOnly capability. This
// matches the Hardware Buddy spec: BlueZ generates a 6-digit passkey
// and calls DisplayPasskey; the user types it into Claude Desktop.
func registerBluezAgent() error {
	conn, err := dbus.SystemBus()
	if err != nil {
		return fmt.Errorf("connect system bus: %w", err)
	}

	agent := &Agent{}
	if err := conn.Export(agent, agentPath, "org.bluez.Agent1"); err != nil {
		return fmt.Errorf("export agent: %w", err)
	}
	if err := conn.Export(introspect.Introspectable(agentIntrospectXML), agentPath,
		"org.freedesktop.DBus.Introspectable"); err != nil {
		return fmt.Errorf("export introspect: %w", err)
	}

	mgr := conn.Object("org.bluez", "/org/bluez")

	// Best-effort: an old agent registration from a prior run may still be
	// alive. Unregister first; ignore the error if there's nothing to clean.
	mgr.Call("org.bluez.AgentManager1.UnregisterAgent", 0, dbus.ObjectPath(agentPath))

	if call := mgr.Call("org.bluez.AgentManager1.RegisterAgent", 0,
		dbus.ObjectPath(agentPath), "DisplayOnly"); call.Err != nil {
		return fmt.Errorf("register agent: %w", call.Err)
	}
	if call := mgr.Call("org.bluez.AgentManager1.RequestDefaultAgent", 0,
		dbus.ObjectPath(agentPath)); call.Err != nil {
		return fmt.Errorf("request default agent: %w", call.Err)
	}

	log.Println("[agent] registered with BlueZ as DisplayOnly default agent")
	return nil
}
