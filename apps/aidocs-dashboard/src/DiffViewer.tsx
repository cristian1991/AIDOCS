import { useRef, useEffect } from "react";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { MergeView } from "@codemirror/merge";
import { syntaxHighlighting, HighlightStyle } from "@codemirror/language";
import { tags } from "@lezer/highlight";

const diffTheme = EditorView.theme({
  "&": { background: "#091310", color: "#b9d0c2", fontSize: "13px" },
  ".cm-content": { fontFamily: "'Cascadia Code', 'Fira Code', monospace" },
  ".cm-gutters": { background: "#0a1612", borderRight: "1px solid rgba(153,211,180,0.12)" },
  ".cm-mergeView .cm-changedLine": { background: "rgba(51, 132, 65, 0.12)" },
  ".cm-mergeView .cm-deletedChunk": { background: "rgba(240, 64, 64, 0.08)" },
  ".cm-mergeView .cm-insertedChunk": { background: "rgba(51, 132, 65, 0.08)" },
}, { dark: true });

const diffHighlight = HighlightStyle.define([
  { tag: tags.keyword, color: "#ff7b72" },
  { tag: tags.string, color: "#a5d6ff" },
  { tag: tags.comment, color: "#6b7280" },
  { tag: tags.number, color: "#e8a838" },
  { tag: tags.variableName, color: "#b9d0c2" },
  { tag: tags.propertyName, color: "#8ce0af" },
]);

export function DiffViewer({ original, modified }: { original: string; modified: string }) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const view = new MergeView({
      a: {
        doc: original,
        extensions: [
          EditorState.readOnly.of(true),
          diffTheme,
          syntaxHighlighting(diffHighlight),
          EditorView.lineWrapping,
        ],
      },
      b: {
        doc: modified,
        extensions: [
          EditorState.readOnly.of(true),
          diffTheme,
          syntaxHighlighting(diffHighlight),
          EditorView.lineWrapping,
        ],
      },
      parent: containerRef.current,
    });

    return () => view.destroy();
  }, [original, modified]);

  return <div ref={containerRef} className="diff-viewer" />;
}
