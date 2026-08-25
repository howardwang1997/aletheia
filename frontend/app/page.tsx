"use client";

import { Activity } from "@/components/Activity";
import { AuthGate, useCanControl } from "@/components/AuthGate";
import { Conversation } from "@/components/Conversation";
import { DataPanel } from "@/components/DataPanel";
import { ProgramGraph } from "@/components/ProgramGraph";
import { useSession } from "@/lib/useSession";

export default function Home() {
  return (
    <AuthGate>
      <Lab />
    </AuthGate>
  );
}

function Lab() {
  const s = useSession();
  const canControl = useCanControl();
  return (
    <main className="app">
      <div className="header">
        <h1>Aletheia</h1>
        <span className="sub">lights-out lab · scope → connect data → launch</span>
      </div>
      <ProgramGraph />
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
            canControl={canControl}
          />
          <DataPanel
            finalized={!!s.finalizedPlan}
            datasets={s.datasets}
            launched={s.launched}
            paused={s.paused}
            onUpload={s.upload}
            onConnect={s.connectData}
            onLaunch={s.launch}
            onResume={s.resume}
            canControl={canControl}
          />
        </div>
        <Activity
          activity={s.activity}
          status={s.status}
          cost={s.cost}
          finalizedPlan={s.finalizedPlan}
          stageHistory={s.stageHistory}
          experiments={s.experiments}
          critiques={s.critiques}
          claims={s.claims}
          report={s.report}
          finalMetrics={s.finalMetrics}
        />
      </div>
    </main>
  );
}
