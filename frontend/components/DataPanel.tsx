"use client";

import { useRef, useState } from "react";
import { DatasetAsset } from "@/lib/api";

/**
 * The human's "connect data pipelines" surface + the Launch button. Launch is
 * gated: enabled only once a plan is finalized AND every declared dataset is
 * ready (the data-readiness gate the backend also enforces).
 */
export function DataPanel({
  finalized,
  datasets,
  launched,
  paused,
  onUpload,
  onLaunch,
  onResume,
}: {
  finalized: boolean;
  datasets: DatasetAsset[];
  launched: boolean;
  paused: boolean;
  onUpload: (file: File, opts: { target_column?: string; asset_id?: string }) => void;
  onLaunch: () => void;
  onResume: () => void;
}) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [target, setTarget] = useState("");
  const [pendingAsset, setPendingAsset] = useState<string | undefined>(undefined);

  const allReady = datasets.every((d) => d.status === "ready");
  const canLaunch = finalized && allReady && !launched;

  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) onUpload(file, { target_column: target || undefined, asset_id: pendingAsset });
    if (fileRef.current) fileRef.current.value = "";
    setPendingAsset(undefined);
  };

  return (
    <div className="datapanel">
      <div className="dp-head">
        <strong>Data &amp; Launch</strong>
        <span className={`dp-ready ${allReady ? "ok" : "wait"}`}>
          {datasets.length === 0
            ? "no data yet"
            : allReady
              ? "data ready ✓"
              : "data needed"}
        </span>
      </div>

      <div className="dp-datasets">
        {datasets.length === 0 && (
          <div className="hint">
            No datasets connected. Aletheia may request one during scoping, or upload your own.
          </div>
        )}
        {datasets.map((d) => (
          <div className="dp-ds" key={d.id}>
            <span className={`dp-dot ${d.status}`} />
            <span className="dp-src">
              {d.source}
              {d.ref ? `: ${d.ref}` : ""}
              {d.requested_by === "agent" ? " (requested by Aletheia)" : ""}
            </span>
            <span className="dp-status">{d.status}</span>
            {d.status !== "ready" && (
              <button
                className="dp-satisfy"
                onClick={() => {
                  setPendingAsset(d.id);
                  fileRef.current?.click();
                }}
              >
                upload file
              </button>
            )}
          </div>
        ))}
      </div>

      <div className="dp-upload">
        <input
          placeholder="target column (optional)"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
        />
        <button onClick={() => { setPendingAsset(undefined); fileRef.current?.click(); }}>
          + upload dataset
        </button>
        <input ref={fileRef} type="file" accept=".csv,.parquet,.json" hidden onChange={onPick} />
      </div>

      <div className="dp-actions">
        {!launched ? (
          <button className="launch" disabled={!canLaunch} onClick={onLaunch}>
            🚀 Launch experiment
          </button>
        ) : paused ? (
          <button className="launch resume" onClick={onResume}>
            ▶ Resume (paused)
          </button>
        ) : (
          <span className="dp-running">running…</span>
        )}
        {!finalized && <span className="hint">finalize the plan first</span>}
      </div>
    </div>
  );
}
