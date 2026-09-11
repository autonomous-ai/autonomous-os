# Lamp agent runtime

Lamp defaults to **Hermes**, declared as `gateway.default: hermes` in
[`ROBOT.md`](../ROBOT.md).

Runtime selection follows this priority:

1. `agent_runtime` already saved in the device's `config.json`.
2. `/root/config/f_r_default_agent`, baked into the image with `DEFAULT_AGENT`.
3. `gateway.default` in Lamp's `ROBOT.md`.

Changing the declaration does not switch an already-configured device. An
image-specific default also survives factory reset and overrides this declaration.
