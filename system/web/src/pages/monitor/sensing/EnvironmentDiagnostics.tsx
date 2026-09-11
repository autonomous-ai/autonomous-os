import type { EnvironmentComponentStatus, EnvironmentStatus } from "./environmentApi";

function ComponentDetails({ name, status }: { name: string; status: EnvironmentComponentStatus }) {
  return <div>
    <h3 style={{ fontSize: 13 }}>{name} · {status.state}{status.stale && status.state === "ready" ? " · stale" : ""}</h3>
    {status.last_error && <p style={{ color: "var(--lm-amber)" }}>{status.last_error}</p>}
    <dl style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
      <dt>I²C bus</dt><dd>{status.bus ?? "—"}</dd>
      {status.sample?.device_status != null && <>
        <dt>Device status register</dt><dd>{`0x${status.sample.device_status.toString(16).padStart(8, "0")}`}</dd>
      </>}
      <dt>Last measurement</dt><dd>{status.sample ? new Date(status.sample.timestamp * 1000).toLocaleString() : "—"}</dd>
      <dt>Poll interval</dt><dd>{status.timing ? `${status.timing.poll_interval_s} s` : "—"}</dd>
      <dt>Retry interval</dt><dd>{status.timing ? `${status.timing.retry_interval_s} s` : "—"}</dd>
      <dt>Stale after</dt><dd>{status.timing ? `${status.timing.stale_after_s} s` : "—"}</dd>
      <dt>No-data timeout</dt><dd>{status.timing ? `${status.timing.no_data_timeout_s} s` : "—"}</dd>
    </dl>
  </div>;
}

export function EnvironmentDiagnostics({ data }: { data: EnvironmentStatus | null }) {
  return <details style={{ color: "var(--lm-text-muted)", fontSize: 12 }}>
    <summary style={{ cursor: "pointer" }}>Sensor details</summary>
    {data && (data.components
      ? Object.entries(data.components).map(([name, status]) => <ComponentDetails key={name} name={name.toUpperCase()} status={status} />)
      : <ComponentDetails name="SEN55" status={data} />)}
  </details>;
}
