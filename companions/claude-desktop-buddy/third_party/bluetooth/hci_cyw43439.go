//go:build cyw43439

package bluetooth

const (
	ogfVendor = 0x3f

	ocfSetBTMACAddr = 0x0001
)

func (a *Adapter) SetBdAddr(address Address) error {
	return a.hci.setBdAddr(address)
}

func (h *hci) setBdAddr(address Address) error {
	hciPacket := make([]byte, len(address.MACAddress.MAC))
	// Reverse the byte order as per spec
	for i := range address.MACAddress.MAC {
		hciPacket[i] = address.MACAddress.MAC[len(address.MACAddress.MAC)-1-i]
	}

	if err := h.sendWithoutResponse(ogfVendor<<ogfCommandPos|ocfSetBTMACAddr, hciPacket); err != nil {
		return err
	}

	return nil
}
