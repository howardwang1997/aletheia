"use client";

import { Activity } from "@/components/Activity";
import { Conversation } from "@/components/Conversation";
import { useSession } from "@/lib/useSession";

export default function Home() {
  const s = useSession();
  return (
    <main className="app">
      <div className="header">
        <h1>Aletheia</h1>
        <span className="sub">lights-out lab · goal scoping</span>
      </div>
      <div className="two-pane">
        <Conversation
          chat={s.chat}
          onSend={s.send}
          connected={s.connected}
          dryRun={s.dryRun}
          setDryRun={s.setDryRun}
          onNew={s.newSession}
          mode={s.mode}
        />
        <Activity
          activity={s.activity}
          status={s.status}
          cost={s.cost}
          finalizedPlan={s.finalizedPlan}
        />
      </div>
    </main>
  );
}
