"use client";

import { LegacyQuestGraphSnapshot, listLegacyQuestGraphs } from "@/lib/api";
import { useCallback, useEffect, useState } from "react";

function titleOf(graph: LegacyQuestGraphSnapshot): string {
  const quest = graph.nodes.find((node) => node.node_type === "quest");
  return typeof quest?.spec.title === "string" ? quest.spec.title : graph.quest_id;
}

export function ProgramGraph() {
  const [graphs, setGraphs] = useState<LegacyQuestGraphSnapshot[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setGraphs(await listLegacyQuestGraphs());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "program graph unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <section className="program-graph" aria-label="Legacy scientific program graph">
      <div className="pg-head">
        <strong>Legacy scientific program ledger (deprecated)</strong>
        <span>{loading ? "rebuilding…" : `${graphs.length} quest${graphs.length === 1 ? "" : "s"}`}</span>
        <button type="button" onClick={() => void refresh()} disabled={loading}>
          Rebuild view
        </button>
      </div>
      {error ? <div className="pg-error">{error}</div> : null}
      {!loading && !error && graphs.length === 0 ? (
        <div className="pg-empty">No Quest has been committed yet.</div>
      ) : null}
      <div className="pg-quests">
        {graphs.map((graph) => {
          const quest = graph.nodes.find((node) => node.node_type === "quest");
          const programs = graph.nodes.filter((node) => node.node_type === "program");
          const campaigns = graph.nodes.filter((node) => node.node_type === "campaign");
          return (
            <article className="pg-quest" key={graph.quest_id}>
              <div className="pg-title">
                <span>{titleOf(graph)}</span>
                <em>{quest?.state ?? "invalid"}</em>
              </div>
              <div className="pg-counts">
                {programs.length} programs · {campaigns.length} campaigns ·{" "}
                {graph.scientific_families.length} families · {graph.external_bindings.length} bindings
              </div>
              <div className="pg-lineage">
                {programs.map((program) => {
                  const children = campaigns.filter(
                    (campaign) => campaign.parent_node_id === program.node_id,
                  );
                  const title =
                    typeof program.spec.title === "string" ? program.spec.title : program.node_id;
                  return (
                    <span key={program.node_id}>
                      {title} [{program.state}] → {children.length} campaign
                      {children.length === 1 ? "" : "s"}
                    </span>
                  );
                })}
              </div>
              <code title={graph.graph_sha256}>ledger {graph.graph_sha256.slice(0, 12)}</code>
            </article>
          );
        })}
      </div>
    </section>
  );
}
