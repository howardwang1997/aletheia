"use client";

import { useState } from "react";
import { ChatMsg } from "@/lib/useSession";

export function Conversation({
  chat,
  onSend,
  connected,
  dryRun,
  setDryRun,
  onNew,
  mode,
}: {
  chat: ChatMsg[];
  onSend: (text: string) => void;
  connected: boolean;
  dryRun: boolean;
  setDryRun: (v: boolean) => void;
  onNew: () => void;
  mode: string;
}) {
  const [text, setText] = useState("");

  function submit() {
    if (text.trim()) {
      onSend(text);
      setText("");
    }
  }

  return (
    <section className="conv">
      <div className="conv-head">
        <strong>Conversation</strong>
        {mode && <span className="mode">{mode}</span>}
        <span className="dot">{connected ? "● live" : "○ connecting"}</span>
        <span className="spacer" />
        <label className="dry">
          <input
            type="checkbox"
            checked={dryRun}
            onChange={(e) => setDryRun(e.target.checked)}
          />
          dry-run
        </label>
        <button onClick={onNew}>New session</button>
      </div>

      <div className="bubbles">
        {chat.length === 0 && (
          <div className="hint">
            Describe a research interest to begin — Aletheia will ask focused
            questions to scope it, then record an experiment plan.
          </div>
        )}
        {chat.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`}>
            <div className="who">{m.role === "user" ? "You" : "Aletheia"}</div>
            <div className="text">{m.text}</div>
          </div>
        ))}
      </div>

      <div className="composer">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
          placeholder="Type a message…"
        />
        <button onClick={submit}>Send</button>
      </div>
    </section>
  );
}
