import { useEffect, useState } from "react";

export function useDashboardUi() {
  const [tomlPage, setTomlPage] = useState(1);
  const [tomlPageSize, setTomlPageSize] = useState(10);
  const [importExportOpen, setImportExportOpen] = useState(false);
  const [configTextPath, setConfigTextPath] = useState<string | null>(null);
  const [pendingDangerSettingPath, setPendingDangerSettingPath] = useState<string | null>(null);
  const [openDropdown, setOpenDropdown] = useState<"project" | "session" | null>(null);

  useEffect(() => {
    const updateTomlPageSize = () => {
      const viewportHeight = window.innerHeight;
      const estimatedRows = Math.floor((viewportHeight - 240) / 44);
      setTomlPageSize(Math.max(10, Math.min(28, estimatedRows)));
    };
    updateTomlPageSize();
    window.addEventListener("resize", updateTomlPageSize);
    return () => window.removeEventListener("resize", updateTomlPageSize);
  }, []);

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
    tomlPage,
    tomlPageSize,
    importExportOpen,
    configTextPath,
    pendingDangerSettingPath,
    openDropdown,
    setTomlPage,
    setImportExportOpen,
    setConfigTextPath,
    setPendingDangerSettingPath,
    setOpenDropdown,
  };
}
