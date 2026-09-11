import { S } from "../styles";
import type { Measurement } from "./environmentApi";
import { useEnvironment } from "./useEnvironment";

const measurements: [Measurement, string, string][] = [
  ["temperature_c", "Temperature", "°C"], ["humidity_pct", "Humidity", "%RH"],
  ["pm1_0_ug_m3", "PM1", "µg/m³"], ["pm2_5_ug_m3", "PM2.5", "µg/m³"],
  ["pm4_0_ug_m3", "PM4", "µg/m³"], ["pm10_ug_m3", "PM10", "µg/m³"],
  ["voc_index", "VOC", "index"], ["nox_index", "NOx", "index"],
];
const stateLabels = { disabled: "Disabled", starting: "Connecting", ready: "Receiving data", error: "Sensor error", stopped: "Stopped" };

export function EnvironmentCard() {
  const { data, error } = useEnvironment();

  const stale = !!error || data?.stale !== false;
  const label = error ? "Unavailable" : !data ? "Loading…" : data.stale && data.state === "ready" ? "Stale data" : stateLabels[data.state];
  return (
    <section style={S.card} aria-label="Environmental sensing">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 16 }}>Environment · SEN55</h2>
          <div style={{ color: "var(--lm-text-muted)", fontSize: 12, marginTop: 5 }}>Air quality, temperature and humidity</div>
        </div>
        <span role="status" style={{ color: stale ? "var(--lm-amber)" : "var(--lm-green)" }}>{label}</span>
      </div>
      {(error || data?.last_error) && <p role="alert" style={{ color: "var(--lm-amber)" }}>{error || data?.last_error}</p>}
      {data?.state === "disabled" && <p style={{ color: "var(--lm-text-muted)" }}>This sensor is not enabled on this device.</p>}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 18 }}>
        {measurements.map(([key, title, unit]) => {
          const value = data?.sample?.[key];
          return <div key={key}>
            <div style={{ color: "var(--lm-text-muted)", fontSize: 12 }}>{title}</div>
            <div style={{ fontSize: 24, marginTop: 4, color: stale ? "var(--lm-text-muted)" : "var(--lm-text)" }}>
              {!stale && value != null && Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—"}
              <span style={{ fontSize: 12, marginLeft: 6 }}>{unit}</span>
            </div>
          </div>;
        })}
      </div>
      <p style={{ color: "var(--lm-text-muted)", fontSize: 12, marginTop: 18 }}>
        {data?.sample ? `Last measurement: ${new Date(data.sample.timestamp * 1000).toLocaleString()}` : "Waiting for a measurement."}
        {stale && data?.sample ? " · No fresh data" : ""}
        {" · VOC and NOx are indexes, not ppm."}
      </p>
      <details style={{ color: "var(--lm-text-muted)", fontSize: 12 }}>
        <summary style={{ cursor: "pointer" }}>Technical details</summary>
        <dl style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <dt>I²C bus</dt><dd>{data?.bus ?? "—"}</dd>
          <dt>Device status register</dt><dd>{data?.sample ? `0x${data.sample.device_status.toString(16).padStart(8, "0")}` : "—"}</dd>
          <dt>Poll interval</dt><dd>{data?.timing ? `${data.timing.poll_interval_s} s` : "—"}</dd>
          <dt>Retry interval</dt><dd>{data?.timing ? `${data.timing.retry_interval_s} s` : "—"}</dd>
          <dt>Stale after</dt><dd>{data?.timing ? `${data.timing.stale_after_s} s` : "—"}</dd>
          <dt>No-data timeout</dt><dd>{data?.timing ? `${data.timing.no_data_timeout_s} s` : "—"}</dd>
        </dl>
      </details>
    </section>
  );
}
