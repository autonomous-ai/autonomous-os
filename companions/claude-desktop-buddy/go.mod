module claude-desktop-buddy

go 1.24.0

require (
	gopkg.in/natefinch/lumberjack.v2 v2.2.1
	tinygo.org/x/bluetooth v0.14.0
)

// Local fork: extends BlueZ characteristic flags with secure-read /
// secure-write so Claude Desktop's Hardware Buddy bonding requirement is
// met (upstream gatts_linux.go only maps the 6 basic flags). See REFERENCE
// at github.com/anthropics/claude-desktop-buddy.
replace tinygo.org/x/bluetooth => ./third_party/bluetooth

require (
	github.com/go-ole/go-ole v1.2.6 // indirect
	github.com/godbus/dbus/v5 v5.1.0 // indirect
	github.com/saltosystems/winrt-go v0.0.0-20240509164145-4f7860a3bd2b // indirect
	github.com/sirupsen/logrus v1.9.3 // indirect
	github.com/soypat/cyw43439 v0.0.0-20250505012923-830110c8f4af // indirect
	github.com/soypat/seqs v0.0.0-20250124201400-0d65bc7c1710 // indirect
	github.com/tinygo-org/cbgo v0.0.4 // indirect
	github.com/tinygo-org/pio v0.2.0 // indirect
	golang.org/x/exp v0.0.0-20241204233417-43b7b7cde48d // indirect
	golang.org/x/sys v0.11.0 // indirect
)
