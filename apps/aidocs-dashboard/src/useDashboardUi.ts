import { useEffect, useState } from "react";

export function useDashboardUi() {
  const [importExportOpen, setImportExportOpen] = useState(false);
  const [pendingDangerSettingPath, setPendingDangerSettingPath] = useState<string | null>(null);
  const [openDropdown, setOpenDropdown] = useState<"project" | "session" | null>(null);

  useEffect(() => {
    if (!importExportOpen) {
      return;
    }
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setImportExportOpen(false);
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [importExportOpen]);

  useEffect(() => {
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) {
        return;
      }
      if (!target.closest(".dropdown-field")) {
        setOpenDropdown(null);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, []);

  useEffect(() => {
    if (!pendingDangerSettingPath) {
      return;
    }
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setPendingDangerSettingPath(null);
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [pendingDangerSettingPath]);

  return {
    importExportOpen,
    pendingDangerSettingPath,
    openDropdown,
    setImportExportOpen,
    setPendingDangerSettingPath,
    setOpenDropdown,
  };
}
