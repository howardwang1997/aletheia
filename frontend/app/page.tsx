"use client";

import { Activity } from "@/components/Activity";
import { Conversation } from "@/components/Conversation";
import { DataPanel } from "@/components/DataPanel";
import { useSession } from "@/lib/useSession";

export default function Home() {
  const s = useSession();
  return (
    <main className="app">
      <div className="header">
        <h1>Aletheia</h1>
        <span className="sub">lights-out lab · scope → connect data → launch</span>
      </div>
      <div className="two-pane">
        <div className="left-col">
          <Conversation
            chat={s.chat}
            onSend={s.send}
            connected={s.connected}
            dryRun={s.dryRun}
            setDryRun={s.setDryRun}
            onNew={s.newSession}
            mode={s.mode}
          />
          <DataPanel
            finalized={!!s.finalizedPlan}
            datasets={s.datasets}
            launched={s.launched}
            paused={s.paused}
            onUpload={s.upload}
            onLaunch={s.launch}
            onResume={s.resume}
          />
        </div>
        <Activity
          activity={s.activity}
          status={s.status}
          cost={s.cost}
          finalizedPlan={s.finalizedPlan}
          stageHistory={s.stageHistory}
          critiques={s.critiques}
          report={s.report}
          finalMetrics={s.finalMetrics}
        />
      </div>
    </main>
  );
}
