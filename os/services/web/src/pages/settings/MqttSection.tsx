import { LockedField, LockedPasswordField, SectionCard } from "@/components/setup/shared";

export interface MqttLoadedState {
  endpoint: boolean;
  port: boolean;
  username: boolean;
  password: boolean;
  faChannel: boolean;
  fdChannel: boolean;
}

export function MqttSection({
  active, mqttLoaded,
  mqttEndpoint, setMqttEndpoint,
  mqttPort, setMqttPort,
  mqttUsername, setMqttUsername,
  mqttPassword, setMqttPassword,
  faChannel, setFaChannel,
  fdChannel, setFdChannel,
}: {
  active: boolean;
  mqttLoaded: MqttLoadedState;
  mqttEndpoint: string; setMqttEndpoint: (v: string) => void;
  mqttPort: string; setMqttPort: (v: string) => void;
  mqttUsername: string; setMqttUsername: (v: string) => void;
  mqttPassword: string; setMqttPassword: (v: string) => void;
  faChannel: string; setFaChannel: (v: string) => void;
  fdChannel: string; setFdChannel: (v: string) => void;
}) {
  return (
    <SectionCard id="mqtt" title="MQTT (optional)" active={active}>
      <LockedField lockedInitially={mqttLoaded.endpoint} label="Endpoint" id="mqtt_endpoint" value={mqttEndpoint} onChange={setMqttEndpoint} placeholder="mqtt.example.com" />
      <LockedField lockedInitially={mqttLoaded.port} label="Port" id="mqtt_port" value={mqttPort} onChange={setMqttPort} placeholder="1883" type="number" />
      <LockedField lockedInitially={mqttLoaded.username} label="Username" id="mqtt_username" value={mqttUsername} onChange={setMqttUsername} placeholder="Optional" />
      <LockedPasswordField lockedInitially={mqttLoaded.password} label="Password" id="mqtt_password" value={mqttPassword} onChange={setMqttPassword} placeholder="Optional" />
      <LockedField lockedInitially={mqttLoaded.faChannel} label="FA Channel" id="fa_channel" value={faChannel} onChange={setFaChannel} />
      <LockedField lockedInitially={mqttLoaded.fdChannel} label="FD Channel" id="fd_channel" value={fdChannel} onChange={setFdChannel} />
    </SectionCard>
  );
}
