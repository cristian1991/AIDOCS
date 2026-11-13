import { useEffect, useState } from "react";

import { memoryAnchorHealth, type MemoryAnchorHealth } from "./dashboardApi";
import { MemoryAnchorHealthCard } from "./MemoryAnchorHealthCard";

/**
 * Self-loading container for the memory-anchor health card. Fetches the cheap
 * COUNT-only health for the selected project off the ai_palace_status hot path,
 * and renders the card. Fail-quiet: any error shows the placeholder card.
 */
export function MemoryAnchorHealthPanel({
  projectRoot,
}: {
  projectRoot: string | null;
}) {
  const [health, setHealth] = useState<MemoryAnchorHealth | null>(null);

  useEffect(() => {
    let alive = true;
    if (!projectRoot) {
      setHealth(null);
      return;
    }
    memoryAnchorHealth(projectRoot)
      .then((r) => {
        if (alive) setHealth(r?.health ?? null);
      })
      .catch(() => {
        if (alive) setHealth(null);
      });
    return () => {
      alive = false;
    };
  }, [projectRoot]);

  return <MemoryAnchorHealthCard health={health} />;
}
