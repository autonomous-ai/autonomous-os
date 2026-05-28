## Restart bluetooth on board
sudo systemctl stop claude-desktop-buddy
sudo systemctl restart bluetooth
sleep 3
sudo bluetoothctl power on
sudo systemctl start claude-desktop-buddy