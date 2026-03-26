from pathlib import Path

from aidocs_mcp.code_index_store import CodeIndexStore


def test_code_role_inference_for_frontend_and_backend_files(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "app" / "dashboard").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "app" / "dashboard" / "page.tsx").write_text(
        "export default function DashboardPage() { return <div />; }\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "EditModeProvider.tsx").write_text(
        "export function EditModeProvider() { return <div />; }\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService {}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "namespace DentalApp.Controllers;\n\n"
        "public class QuoteController {}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    page = store.get_file_bundle(project_root, "web/app/dashboard/page.tsx")
    provider = store.get_file_bundle(project_root, "web/components/EditModeProvider.tsx")
    service = store.get_file_bundle(project_root, "Services/QuoteService.cs")
    controller = store.get_file_bundle(project_root, "Controllers/QuoteController.cs")

    assert page["role"] == "page"
    assert provider["role"] == "context-provider"
    assert service["role"] == "service"
    assert controller["role"] == "controller"

def test_code_role_inference_expands_support_and_runtime_buckets(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "app" / "api" / "quotes").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "lib").mkdir(parents=True, exist_ok=True)
    (project_root / "server").mkdir(parents=True, exist_ok=True)
    (project_root / "Pages").mkdir(parents=True, exist_ok=True)
    (project_root / "Configurations").mkdir(parents=True, exist_ok=True)
    (project_root / "Workers").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "app" / "api" / "quotes" / "route.ts").write_text("export async function GET() { return Response.json({}); }\n", encoding="utf-8")
    (project_root / "web" / "lib" / "helpers.ts").write_text("export function slugify(x: string) { return x; }\n", encoding="utf-8")
    (project_root / "server" / "plugin-server.js").write_text("export function start() {}\n", encoding="utf-8")
    (project_root / "Pages" / "AccessDenied.cshtml.cs").write_text("public class AccessDeniedModel {}\n", encoding="utf-8")
    (project_root / "Configurations" / "AccountConfiguration.cs").write_text("public class AccountConfiguration {}\n", encoding="utf-8")
    (project_root / "Workers" / "QueueWorker.cs").write_text("public class QueueWorker {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["route-handler"] == 1
    assert status["roles"]["utility-module"] == 1
    assert status["roles"]["server-module"] == 1
    assert status["roles"]["page-model"] == 1
    assert status["roles"]["configuration"] == 1
    assert status["roles"]["worker"] == 1
    assert status["role_groups"]["request-surfaces"] >= 2
    assert status["role_groups"]["support"] >= 2
    assert status["role_groups"]["logic-runtime"] >= 1

def test_code_role_inference_classifies_components_and_plugin_modules(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "random-folder-name" / "demo-plugin" / "templates" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "framework-generators").mkdir(parents=True, exist_ok=True)
    (project_root / "core").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "Hero.tsx").write_text("export function Hero() { return <div />; }\n", encoding="utf-8")
    (project_root / "random-folder-name" / "demo-plugin" / "package.json").write_text("{}\n", encoding="utf-8")
    (project_root / "random-folder-name" / "demo-plugin" / "generator.js").write_text("export async function generate() {}\n", encoding="utf-8")
    (project_root / "random-folder-name" / "demo-plugin" / "index.ts").write_text("export * from './generator'\n", encoding="utf-8")
    (project_root / "framework-generators" / "next-generator.js").write_text("export function create() {}\n", encoding="utf-8")
    (project_root / "core" / "plugin-loader.js").write_text("export function load() {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["component"] == 1
    assert status["roles"]["plugin-generator"] == 1
    assert status["roles"]["plugin-module"] == 1
    assert status["roles"]["framework-generator"] == 1
    assert status["roles"]["core-module"] == 1

def test_code_role_inference_generalizes_hooks_barrels_and_root_scripts(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "any-name" / "feature-pack" / "templates" / "hooks").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "cms").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "any-name" / "feature-pack" / "package.json").write_text("{}\n", encoding="utf-8")
    (project_root / "any-name" / "feature-pack" / "templates" / "hooks" / "useThing.ts").write_text("export function useThing() { return null }\n", encoding="utf-8")
    (project_root / "web" / "components" / "cms" / "index.ts").write_text("export * from './EditPanel'\n", encoding="utf-8")
    (project_root / "bootstrap.js").write_text("#!/usr/bin/env node\nconsole.log('ok')\n", encoding="utf-8")
    (project_root / "next-env.d.ts").write_text("/// <reference types=\"next\" />\n", encoding="utf-8")

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["hook-module"] == 1
    assert status["roles"]["barrel-module"] == 1
    assert status["roles"]["config-module"] == 1

def test_code_role_inference_classifies_plugin_template_modules_generically(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "some-random-folder" / "auth-pack" / "templates").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "some-random-folder" / "auth-pack" / "package.json").write_text("{}\n", encoding="utf-8")
    (project_root / "some-random-folder" / "auth-pack" / "templates" / "prisma-schema.js").write_text("export function getSchema() { return '' }\n", encoding="utf-8")

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["plugin-template-module"] == 1

def test_code_role_inference_classifies_csharp_entities_dtos_and_interfaces(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src" / "Domain" / "Entities").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Application" / "DTOs").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Application" / "Interfaces").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Domain" / "Entities" / "Account.cs").write_text("public class Account {}\n", encoding="utf-8")
    (project_root / "src" / "Application" / "DTOs" / "AccountDto.cs").write_text("public record AccountDto(string Name);\n", encoding="utf-8")
    (project_root / "src" / "Application" / "Interfaces" / "IAccountService.cs").write_text("public interface IAccountService {}\n", encoding="utf-8")

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["data-model"] == 2
    assert status["roles"]["abstraction"] == 1

def test_code_role_inference_classifies_csharp_program_seed_partial_services_and_hubs(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src" / "Infrastructure" / "Data" / "Seeding").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Infrastructure" / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Infrastructure" / "Data").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web" / "Hubs").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web" / "ViewComponents").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Infrastructure" / "Data" / "AppDbContext.cs").write_text("public class AppDbContext {}\n", encoding="utf-8")
    (project_root / "src" / "Infrastructure" / "Data" / "Seeding" / "SeedDataService.Forms.cs").write_text("public partial class SeedDataService {}\n", encoding="utf-8")
    (project_root / "src" / "Infrastructure" / "Services" / "FormPdfService.Html.cs").write_text("public partial class FormPdfService {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "Hubs" / "DebugHub.cs").write_text("public class DebugHub {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "ViewComponents" / "SidebarViewComponent.cs").write_text("public class SidebarViewComponent {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "Program.cs").write_text("public class Program {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "Scripts").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web" / "Scripts" / "SyncTranslations.ps1").write_text("Write-Host 'ok'\n", encoding="utf-8")

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["data-access"] == 1
    assert status["roles"]["script"] >= 2
    assert status["roles"]["service"] == 1
    assert status["roles"]["hub"] == 1
    assert status["roles"]["component"] == 1
    assert status["roles"]["initializer-module"] == 1

def test_code_role_inference_classifies_asset_scripts_tools_and_support_markers(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src" / "Web" / "wwwroot" / "js").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Infrastructure").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web" / "Infrastructure").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web").mkdir(parents=True, exist_ok=True)
    (project_root / "tools").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "Web" / "wwwroot" / "js" / "site.js").write_text("const x = 1\n", encoding="utf-8")
    (project_root / "src" / "Infrastructure" / "DependencyInjection.cs").write_text("public static class DependencyInjection {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "Infrastructure" / "DateTimeModelBinder.cs").write_text("public class DateTimeModelBinder {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "SharedResources.cs").write_text("public class SharedResources {}\n", encoding="utf-8")
    (project_root / "src" / "Web" / "NavigationMap.cs").write_text("public class NavigationMap {}\n", encoding="utf-8")
    (project_root / "tools" / "fix-untranslated.py").write_text("print('ok')\n", encoding="utf-8")

    store.sync_code_files(project_root)
    status = store.code_status(project_root)

    assert status["roles"]["asset-script"] == 1
    assert status["roles"]["initializer-module"] == 1
    assert status["roles"]["configuration"] == 1
    assert status["roles"]["utility"] == 2
    assert status["roles"]["script"] == 1
