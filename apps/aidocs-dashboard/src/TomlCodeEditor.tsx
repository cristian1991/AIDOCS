import { useEffect, useRef } from "react";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap, highlightSpecialChars, drawSelection, highlightActiveLine } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { HighlightStyle, StreamLanguage, syntaxHighlighting } from "@codemirror/language";
import { tags } from "@lezer/highlight";
import type { StringStream } from "@codemirror/language";

const tomlMode = {
  startState() {
    return {};
  },
  token(stream: StringStream) {
    if (stream.sol() && stream.peek() === "#") {
      stream.skipToEnd();
      return "lineComment";
    }
    if (stream.sol() && stream.match(/^\[+[^\]]*\]+/)) {
      return "heading";
    }
    if (stream.sol() && stream.match(/^[a-zA-Z_][a-zA-Z0-9_.-]*/)) {
      return "propertyName";
    }
    if (stream.match("=")) {
      return "punctuation";
    }
    if (stream.match(/^"[^"]*"/)) {
      return "string";
    }
    if (stream.match(/^'[^']*'/)) {
      return "string";
    }
    if (stream.match(/^(true|false)\b/)) {
      return "bool";
    }
    if (stream.match(/^-?\d+(\.\d+)?/)) {
      return "number";
    }
    if (stream.match(/^[\[\]]/)) {
      return "squareBracket";
    }
    if (stream.match(/^[,{}]/)) {
      return "punctuation";
    }
    stream.next();
    return null;
  },
};

const tomlLanguage = StreamLanguage.define(tomlMode);

const darkTheme = EditorView.theme({
  "&": {
    backgroundColor: "#091310",
    color: "#b9d0c2",
    fontSize: "0.9rem",
    height: "100%",
  },
  ".cm-content": {
    fontFamily: '"Cascadia Code", "SFMono-Regular", Consolas, monospace',
    lineHeight: "1.55",
    padding: "14px",
    caretColor: "#8ce0af",
  },
  ".cm-gutters": {
    display: "none",
  },
  ".cm-scroller": {
    overflow: "auto",
  },
  "&.cm-focused": {
    outline: "none",
  },
  ".cm-activeLine": {
    backgroundColor: "rgba(140, 224, 175, 0.04)",
  },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": {
    backgroundColor: "rgba(140, 224, 175, 0.15) !important",
  },
  ".cm-cursor": {
    borderLeftColor: "#8ce0af",
  },
}, { dark: true });

const tomlHighlighting = HighlightStyle.define([
  { tag: tags.lineComment, color: "#5c7a6e", fontStyle: "italic" },
  { tag: tags.heading, color: "#8ce0af" },
  { tag: tags.propertyName, color: "#f2fbf6" },
  { tag: tags.punctuation, color: "#5c7a6e" },
  { tag: tags.string, color: "#e8a838" },
  { tag: tags.bool, color: "#4a90d9" },
  { tag: tags.number, color: "#d97a4a" },
  { tag: tags.squareBracket, color: "#5cb8a8" },
]);

export function TomlCodeEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    if (!containerRef.current) return;

    const state = EditorState.create({
      doc: value,
      extensions: [
        highlightSpecialChars(),
        history(),
        drawSelection(),
        highlightActiveLine(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        tomlLanguage,
        syntaxHighlighting(tomlHighlighting),
        darkTheme,
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            onChangeRef.current(update.state.doc.toString());
          }
        }),
        EditorView.lineWrapping,
      ],
    });

    const view = new EditorView({ state, parent: containerRef.current });
    viewRef.current = view;

    return () => {
      view.destroy();
      viewRef.current = null;
    };
  }, []);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (current !== value) {
      view.dispatch({
        changes: { from: 0, to: current.length, insert: value },
      });
    }
  }, [value]);

  return <div ref={containerRef} className="toml-cm-editor" />;
}
