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
  onConnect,
  onLaunch,
  onResume,
  canControl = true,
}: {
  finalized: boolean;
  datasets: DatasetAsset[];
  launched: boolean;
  paused: boolean;
  onUpload: (file: File, opts: { target_column?: string; asset_id?: string }) => void;
  onConnect: (body: { source: string; ref?: string; target_column?: string }) => void;
  onLaunch: () => void;
  onResume: () => void;
  canControl?: boolean;
}) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [target, setTarget] = useState("");
  const [pendingAsset, setPendingAsset] = useState<string | undefined>(undefined);
  const [source, setSource] = useState("path");
  const [ref, setRef] = useState("");

  const allReady = datasets.every((d) => d.status === "ready");
  const canLaunch = finalized && allReady && !launched && canControl;

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
            {d.status !== "ready" && canControl && (
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
        <button disabled={!canControl} onClick={() => { setPendingAsset(undefined); fileRef.current?.click(); }}>
          + upload file
        </button>
        <input ref={fileRef} type="file" accept=".csv,.tsv,.parquet,.json,.jsonl" hidden onChange={onPick} />
      </div>

      <div className="dp-connect">
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="path">local path</option>
          <option value="url">URL</option>
          <option value="benchmark">benchmark</option>
        </select>
        <input
          placeholder={
            source === "path"
              ? "/path/to/file.csv  or  /path/to/dataset-dir"
              : source === "url"
                ? "https://… (file or .zip/.tar.gz)"
                : "matbench_expt_gap"
          }
          value={ref}
          onChange={(e) => setRef(e.target.value)}
        />
        <button
          disabled={!ref.trim() || !canControl}
          onClick={() => {
            onConnect({ source, ref: ref.trim(), target_column: target || undefined });
            setRef("");
          }}
        >
          connect
        </button>
      </div>

      <div className="dp-actions">
        {!launched ? (
          <button className="launch" disabled={!canLaunch} onClick={onLaunch}>
            🚀 Launch experiment
          </button>
        ) : paused ? (
          <button className="launch resume" disabled={!canControl} onClick={onResume}>
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
