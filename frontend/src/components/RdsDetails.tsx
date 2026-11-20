import type { ReactNode } from "react";
import type { RdsData } from "../types";

const mhz = (khz: number) => (khz / 1000).toFixed(1);
const yesNo = (v: boolean | null | undefined) => (v == null ? null : v ? "Yes" : "No");

function Item({ label, children }: { label: string; children: ReactNode }) {
  if (children == null || children === "") return null;
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase tracking-wide text-mist">{label}</dt>
      <dd className="truncate text-xs font-medium">{children}</dd>
    </div>
  );
}

function Frequencies({ label, values, current, unit }: { label: string; values: number[]; current?: number; unit: string }) {
  if (!values.length) return null;
  return (
    <div>
      <p className="mb-1 text-[10px] uppercase tracking-wide text-mist">{label}</p>
      <div className="flex flex-wrap gap-1">
        {values.map((v) => (
          <span
            key={v}
            className={`rounded-full px-2 py-0.5 text-[11px] tabular-nums ${
              v === current ? "bg-[var(--accent)] text-white" : "bg-white/5 text-mist"
            }`}
          >
            {unit === "MHz" ? mhz(v) : v}
          </span>
        ))}
        <span className="self-center pl-0.5 text-[10px] text-mist">{unit}</span>
      </div>
    </div>
  );
}

const RT_PLUS_LABELS: Record<string, string> = {
  title: "Title",
  artist: "Artist",
  album: "Album",
  composer: "Composer",
  band: "Band",
  genre: "Genre",
  programme_now: "Programme",
  programme_next: "Next programme",
  programme_part: "Programme part",
  host: "Host",
  homepage: "Homepage",
  phone: "Phone",
  email: "Email",
  sms: "SMS",
  station_short: "Station",
  station_long: "Station name",
  news: "News",
  weather: "Weather",
  traffic: "Traffic",
  url: "Web",
};

/** Everything the station has sent so far. Fields appear as their data arrives. */
export function RdsDetails({ rds, frequencyKhz }: { rds: RdsData; frequencyKhz: number }) {
  const flags = [
    rds.di.stereo != null && (rds.di.stereo ? "Stereo" : "Mono"),
    rds.di.artificial_head && "Artificial head",
    rds.di.compressed && "Compressed",
    rds.di.dynamic_pty && "Dynamic PTY",
  ].filter(Boolean);
  const afLabel = `Alternative frequencies${rds.pi ? ` · PI ${rds.pi}` : ""}${
    // Some stations send several lists in turn, so more can arrive than one announces.
    rds.af_announced && rds.af.length < rds.af_announced ? ` · ${rds.af.length} of ${rds.af_announced}` : ""
  }`;

  if (!rds.total_groups) {
    return <p className="mt-3 text-xs text-mist">Waiting for RDS data…</p>;
  }

  return (
    <div className="mt-3 space-y-3 border-t border-white/5 pt-3">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-3 lg:grid-cols-4">
        <Item label="Station (PS)">{rds.ps}</Item>
        <Item label="Station code (PI)">{rds.pi}</Item>
        <Item label="Call sign">{rds.callsign}</Item>
        <Item label="Country code">{rds.country_code}</Item>
        <Item label="Extended country">{rds.ecc}</Item>
        <Item label="Coverage">{rds.coverage}</Item>
        <Item label="Programme ref.">{rds.programme_ref}</Item>
        <Item label="Programme type">
          {rds.pty != null ? `${rds.pty_name} (${rds.pty})` : null}
        </Item>
        <Item label="PTY name">{rds.ptyn}</Item>
        <Item label="Traffic programme">{yesNo(rds.tp)}</Item>
        <Item label="Traffic announcement">{yesNo(rds.ta)}</Item>
        <Item label="Content">{rds.music == null ? null : rds.music ? "Music" : "Speech"}</Item>
        <Item label="Decoder info">{flags.join(", ")}</Item>
        <Item label="Language code">{rds.language}</Item>
        <Item label="Programme item (PIN)">{rds.pin}</Item>
        <Item label="Station clock">{rds.clock}</Item>
        {Object.entries(rds.rt_plus).map(([key, value]) => (
          <Item key={key} label={RT_PLUS_LABELS[key] ?? key.replace(/_/g, " ")}>
            {value}
          </Item>
        ))}
      </dl>

      {rds.rt && (
        <div>
          <p className="mb-0.5 text-[10px] uppercase tracking-wide text-mist">Radio text</p>
          <p className="break-words text-xs font-medium">{rds.rt}</p>
        </div>
      )}

      <Frequencies label={afLabel} values={rds.af} current={frequencyKhz} unit="MHz" />
      <Frequencies label="AF long / medium wave" values={rds.af_lf_mf} unit="kHz" />

      {rds.eon.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] uppercase tracking-wide text-mist">Other stations on this network (EON)</p>
          <ul className="space-y-1">
            {rds.eon.map((o) => (
              <li key={o.pi} className="text-xs">
                <span className="font-medium">{o.ps || "—"}</span>{" "}
                <span className="text-mist">
                  PI {o.pi}
                  {o.pty_name ? ` · ${o.pty_name}` : ""}
                  {o.ta ? " · TA" : ""}
                  {o.af.length ? ` · ${o.af.map(mhz).join(", ")} MHz` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {rds.oda.length > 0 && (
        <div>
          <p className="mb-1 text-[10px] uppercase tracking-wide text-mist">Open data applications</p>
          <div className="flex flex-wrap gap-1">
            {rds.oda.map((o) => (
              <span key={o.group} className="rounded-full bg-white/5 px-2 py-0.5 text-[11px] text-mist">
                {o.group} · {o.name || `AID ${o.aid}`}
              </span>
            ))}
          </div>
        </div>
      )}

      <p className="text-[10px] text-mist">
        {rds.total_groups} groups: {Object.entries(rds.groups).map(([k, v]) => `${k} ${v}`).join(" · ")}
      </p>
    </div>
  );
}
