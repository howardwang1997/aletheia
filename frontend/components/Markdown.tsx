"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** Render markdown (GFM: tables, lists, code, task lists) with safe external
 * links. Used for chat bubbles and the report card. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => <a {...props} target="_blank" rel="noreferrer" />,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
