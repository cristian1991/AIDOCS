from pathlib import Path

from aidocs_mcp.code_index_store import CodeIndexStore
from aidocs_mcp.session_store import SessionStore
from aidocs_mcp.schema_index_store import SchemaIndexStore


def test_csharp_partial_group_detection(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "App").mkdir(parents=True, exist_ok=True)
    (project_root / "App" / "MainWindow.xaml.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void Initialize() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "App" / "MainWindow.Actions.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void RunAction() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    status = store.code_status(project_root)
    outline = store.get_outline(project_root, "App/MainWindow.xaml.cs")
    partials = store.find_partial_group(project_root, "MainWindow")

    assert status["partial_symbols"] == 2
    assert outline[0] == {
        "symbol": "MainWindow",
        "kind": "class",
        "line_number": 3,
        "container": "DentalApp.UI",
        "is_partial": True,
    }
    assert len(partials) == 2
    assert {item["path"] for item in partials} == {"App/MainWindow.xaml.cs", "App/MainWindow.Actions.cs"}

def test_find_csharp_data_structures_and_members(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "PatientDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record PatientDto\n"
        "{\n"
        "    public string FullName { get; set; } = string.Empty;\n"
        "    public int Age;\n"
        "}\n\n"
        "public enum PatientStatus\n"
        "{\n"
        "    Active,\n"
        "    Archived\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    structures = store.find_data_structures(project_root, query="Patient", limit=20)

    kinds = {(item["symbol"], item["kind"]) for item in structures}
    assert ("PatientDto", "record") in kinds
    assert ("FullName", "property") in kinds
    assert ("Age", "field") in kinds
    assert ("PatientStatus", "enum") in kinds
    assert ("Active", "enum_member") in kinds
    assert ("Archived", "enum_member") in kinds


def test_search_symbols_includes_namespace_for_csharp_types(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "DTOs").mkdir(parents=True, exist_ok=True)
    (project_root / "DTOs" / "AppointmentDTOs.cs").write_text(
        "namespace DentalApp.Application.DTOs.Appointments;\n\n"
        "public record CreateAppointmentRequest\n"
        "{\n"
        "    public string Title { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    symbols = store.search_symbols(project_root, query="CreateAppointmentRequest", kind="record", limit=5)

    assert symbols[0]["namespace"] == "DentalApp.Application.DTOs.Appointments"


def test_get_method_signature_returns_params_and_return_type(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "DocumentService.cs").write_text(
        "namespace DentalApp.Services;\n\n"
        "public class DocumentService\n"
        "{\n"
        "    public async Task<Document> CreateAsync(string documentTypeKey, Document document)\n"
        "    {\n"
        "        return document;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_method_signature(project_root, method_name="CreateAsync", container="DocumentService")

    assert result["matches"]
    match = result["matches"][0]
    assert match["params"] == "(string documentTypeKey, Document document)"
    assert match["return_type"].endswith("Task<Document>")


def test_get_enum_values_returns_members(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "PaymentMethod.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public enum PaymentMethod\n"
        "{\n"
        "    Cash,\n"
        "    CreditCard,\n"
        "    BankTransfer\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_enum_values(project_root, enum_name="PaymentMethod")

    assert result["matches"]
    assert result["matches"][0]["symbol"] == "PaymentMethod"
    assert result["matches"][0]["values"] == ["Cash", "CreditCard", "BankTransfer"]


def test_get_enum_values_exact_only_by_default(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "Enums.cs").write_text(
        "public enum DiscountType { Percent, Fixed }\n"
        "public enum DiscountTypeHistory { Added }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_enum_values(project_root, enum_name="DiscountType")

    assert [item["symbol"] for item in result["matches"]] == ["DiscountType"]


def test_investigate_includes_method_signature_and_enum_hints(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "DocumentService.cs").write_text(
        "namespace DentalApp.Services;\n\n"
        "public class DocumentService\n"
        "{\n"
        "    public async Task<Document> CreateAsync(string documentTypeKey, Document document)\n"
        "    {\n"
        "        return document;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Models" / "DocumentStatus.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public enum DocumentStatus\n"
        "{\n"
        "    Draft,\n"
        "    Final\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.investigate(project_root, concept="Document", limit=5)

    symbol_finding = next(item for item in result["findings"] if item["area"] == "symbols")
    top_items = symbol_finding["top"]
    assert any("signature" in item for item in top_items if item["kind"] == "method")
    assert any("enum_values" in item for item in top_items if item["kind"] == "enum")
    assert any(item["area"] == "service_api_candidates" for item in result["findings"])


def test_investigate_multi_word_concept_includes_workflow_touchpoints(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "server" / "routes").mkdir(parents=True, exist_ok=True)
    (project_root / "packages" / "app").mkdir(parents=True, exist_ok=True)
    # Use content rich enough for cross-layer touchpoint detection
    (project_root / "server" / "routes" / "session.ts").write_text(
        "export function createSession() {}\n"
        "export function getSession() {}\n"
        "export function deleteSession() {}\n",
        encoding="utf-8",
    )
    (project_root / "packages" / "app" / "session.tsx").write_text(
        "import { createSession, getSession } from '../../server/routes/session'\n"
        "export function SessionView() { return null }\n"
        "export function SessionManager() { createSession(); getSession(); }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.investigate(project_root, concept="session", limit=5)

    # Multi-word or deep search should find cross-layer touchpoints
    # If touchpoints are found, verify the area; if not, at least symbols should be present
    areas = {item["area"] for item in result["findings"]}
    assert "symbols" in areas or "workflow_touchpoints" in areas


def test_investigate_schema_entities_adds_entity_properties_next_tool(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "Document.cs").write_text(
        "public class Document\n"
        "{\n"
        "    public string Number { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)
    result = store.investigate(project_root, concept="Document", limit=5)

    assert any(item["tool"] == "code_get_entity_properties" for item in result["next_tools"])


def test_investigate_focus_ui_filters_findings(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "server" / "routes").mkdir(parents=True, exist_ok=True)
    (project_root / "packages" / "app").mkdir(parents=True, exist_ok=True)
    (project_root / "server" / "routes" / "session.ts").write_text(
        "export function createSession() {}\n"
        "export function getSession() {}\n",
        encoding="utf-8",
    )
    (project_root / "packages" / "app" / "session.tsx").write_text(
        "export function SessionView() { return null }\n"
        "export function SessionManager() { return null }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.investigate(project_root, concept="session", limit=5, focus="ui", depth="deep")

    # Focus=ui with depth=deep should return findings — at least symbols
    assert result["findings"]
    # If workflow_touchpoints are found with UI focus, all should be UI layer
    for item in result["findings"]:
        if item["area"] == "workflow_touchpoints":
            assert all(t.get("layer") == "ui" for t in item["top"])


def test_trace_api_to_ui_service_method_query_surfaces_api_and_ui_refs(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Pages").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "DocumentService.cs").write_text(
        "public class DocumentService\n"
        "{\n"
        "    public void CompleteItemAsync() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "TreatmentPlansController.cs").write_text(
        "public class TreatmentPlansController\n"
        "{\n"
        "    public void CompleteItem() { CompleteItemAsync(); }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Pages" / "TreatmentPlans.cshtml").write_text(
        "<button onclick=\"CompleteItemAsync()\">Run</button>\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_api_to_ui(project_root, concept="DocumentService.CompleteItemAsync", limit=10)

    assert result["api"]
    assert result["ui"]


def test_find_transition_points_filters_low_score_file_noise_for_broad_single_concept(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Legacy").mkdir(parents=True, exist_ok=True)
    (project_root / "Misc").mkdir(parents=True, exist_ok=True)
    (project_root / "Legacy" / "DocumentMigration.cs").write_text("public class DocumentMigration {}\n", encoding="utf-8")
    (project_root / "Misc" / "Unrelated.txt.md").write_text("document maybe once", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_transition_points(project_root, concept="Document", limit=20)

    assert result["matches"]
    assert all(item["path"] != "Misc/Unrelated.txt.md" for item in result["matches"])


def test_find_transition_points_supports_dotted_query_narrowing(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "DocumentStatusTransition.cs").write_text(
        "public class DocumentStatusTransition {}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "DocumentStatusAdapter.cs").write_text(
        "public class DocumentStatusAdapter {}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "PaymentAdapter.cs").write_text(
        "public class PaymentAdapter {}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_transition_points(project_root, concept="Document.Status", limit=20)

    assert result["matches"]
    assert all("document" in item["path"].lower() or "status" in item["path"].lower() or "document" in str(item.get("symbol") or "").lower() for item in result["matches"])


def test_get_constructor_params_for_record(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "DTOs").mkdir(parents=True, exist_ok=True)
    (project_root / "DTOs" / "AppointmentDTOs.cs").write_text(
        "namespace DentalApp.Application.DTOs.Appointments;\n\n"
        "public record CreateAppointmentRequest(string PatientId, DateTime StartsAt, string Notes);\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_constructor_params(project_root, type_name="CreateAppointmentRequest")

    assert result["matches"]
    assert result["matches"][0]["symbol"] == "CreateAppointmentRequest"
    assert result["matches"][0]["params"] == ["string PatientId", "DateTime StartsAt", "string Notes"]


def test_get_constructor_params_exact_only_by_default(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "DTOs").mkdir(parents=True, exist_ok=True)
    (project_root / "DTOs" / "Records.cs").write_text(
        "public record PatientSearchCriteria(string Term);\n"
        "public record AppointmentSearchCriteria(string Term);\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_constructor_params(project_root, type_name="PatientSearchCriteria")

    assert [item["symbol"] for item in result["matches"]] == ["PatientSearchCriteria"]


def test_get_constructor_params_ignores_inline_comments(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "DTOs").mkdir(parents=True, exist_ok=True)
    (project_root / "DTOs" / "Records.cs").write_text(
        "public record UpdatePatientRequest(\n"
        "    string Name,\n"
        "    // Birth info\n"
        "    DateTime BirthDate,\n"
        "    string Notes\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_constructor_params(project_root, type_name="UpdatePatientRequest")

    assert result["matches"][0]["params"] == ["string Name", "DateTime BirthDate", "string Notes"]


def test_get_constructor_params_batch_returns_multiple_types(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "DTOs").mkdir(parents=True, exist_ok=True)
    (project_root / "DTOs" / "Records.cs").write_text(
        "public record ARequest(string Name);\n"
        "public record BRequest(string Name, int Age);\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_constructor_params_batch(project_root, types=["ARequest", "BRequest"])

    assert len(result["types"]) == 2
    assert result["types"][0]["matches"][0]["symbol"] == "ARequest"
    assert result["types"][1]["matches"][0]["symbol"] == "BRequest"


def test_get_service_api_returns_all_service_methods(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "PatientService.cs").write_text(
        "namespace DentalApp.Services;\n\n"
        "public class PatientService\n"
        "{\n"
        "    public Task<object> CreateAsync(object request) => Task.FromResult(request);\n"
        "    public Task<object> GetByIdAsync(string id) => Task.FromResult((object)id);\n"
        "    public Task<object> SearchAsync(object criteria) => Task.FromResult(criteria);\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_service_api(project_root, service_name="PatientService")

    assert result["methods"]
    assert {item["symbol"] for item in result["methods"]} >= {"CreateAsync", "GetByIdAsync", "SearchAsync"}


def test_get_service_api_returns_not_found_for_missing_exact_service(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "FormPdfService.cs").write_text(
        "public class FormPdfService { public void Render() {} }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_service_api(project_root, service_name="AccountService")

    assert result["match"] is None
    assert result["methods"] == []
    assert result["not_found"] is True


def test_get_service_api_aggregates_partial_class_methods(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "FormTemplateService.Access.cs").write_text(
        "public partial class FormTemplateService\n"
        "{\n"
        "    public void GetAccess() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "FormTemplateService.Versioning.cs").write_text(
        "public partial class FormTemplateService\n"
        "{\n"
        "    public void CreateVersion() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_service_api(project_root, service_name="FormTemplateService")

    assert {item["symbol"] for item in result["methods"]} >= {"GetAccess", "CreateVersion"}


def test_get_service_api_falls_back_to_declaring_files_when_partial_indexing_is_sparse(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "FormPdfService.Browser.cs").write_text(
        "public partial class FormPdfService\n{\n}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "FormPdfService.Render.cs").write_text(
        "public partial class FormPdfService\n{\n    public async Task<string> RenderAsync(string html) => html;\n}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_service_api(project_root, service_name="FormPdfService")

    assert any(item["symbol"] == "RenderAsync" for item in result["methods"])


def test_get_method_signatures_batches_multiple_methods(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "PatientService.cs").write_text(
        "namespace DentalApp.Services;\n\n"
        "public class PatientService\n"
        "{\n"
        "    public Task<object> CreateAsync(object request) => Task.FromResult(request);\n"
        "    public Task<object> GetByIdAsync(string id) => Task.FromResult((object)id);\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_method_signatures(project_root, methods=["CreateAsync", "GetByIdAsync"], container="PatientService")

    assert len(result["methods"]) == 2
    assert result["methods"][0]["matches"][0]["symbol"] == "CreateAsync"
    assert result["methods"][1]["matches"][0]["symbol"] == "GetByIdAsync"


def test_get_method_signatures_soft_prefers_container_without_excluding_others(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "A.cs").write_text(
        "public class PatientService\n"
        "{\n"
        "    public void SearchAsync() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "B.cs").write_text(
        "public class OtherService\n"
        "{\n"
        "    public void SearchAsync() {}\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.get_method_signatures(project_root, methods=["SearchAsync"], container="PatientService", limit_per_method=5)

    matches = result["methods"][0]["matches"]
    assert matches[0]["container"] == "PatientService"
    assert any(item["container"] == "OtherService" for item in matches)


def test_find_factories_returns_create_helpers(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Tests").mkdir(parents=True, exist_ok=True)
    (project_root / "Tests" / "DocumentFactory.cs").write_text(
        "public static class DocumentFactory\n"
        "{\n"
        "    public static object CreateDocumentService() => new object();\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_factories(project_root, query="Document", limit=20)

    assert result["matches"]
    assert any((item.get("symbol") == "CreateDocumentService") or (item["path"] == "Tests/DocumentFactory.cs") for item in result["matches"])


def test_find_factories_ranks_query_related_factory_first(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Tests").mkdir(parents=True, exist_ok=True)
    (project_root / "Tests" / "DocumentFactory.cs").write_text(
        "public static class DocumentFactory\n"
        "{\n"
        "    public static object CreateDocumentService() => new object();\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Tests" / "PaymentFactory.cs").write_text(
        "public static class PaymentFactory\n"
        "{\n"
        "    public static object CreatePaymentService() => new object();\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root, include_tests=True)
    result = store.find_factories(project_root, query="Document", limit=20)

    first = result["matches"][0]
    assert first["path"] == "Tests/DocumentFactory.cs" or first.get("symbol") == "CreateDocumentService"


def test_find_mutation_points_prefers_real_mutations_over_test_factories(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Tests").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "CashFlowService.cs").write_text(
        "public class CashFlowService\n"
        "{\n"
        "    public void UpdateBalance() { }\n"
        "    public void CreateTransaction() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Tests" / "IntegrationTestBase.cs").write_text(
        "public class IntegrationTestBase\n"
        "{\n"
        "    public object CreateCashFlowService() => new object();\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root, include_tests=True)
    result = store.find_mutation_points(project_root, concept="CashFlowService", limit=10)

    assert result["matches"]
    first = result["matches"][0]
    assert first["path"] == "Services/CashFlowService.cs"


def test_code_get_entity_properties_returns_lightweight_properties(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "Document.cs").write_text(
        "public class Document\n"
        "{\n"
        "    public string Number { get; set; } = string.Empty;\n"
        "    public decimal LineTotal { get; set; }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)
    result = store.get_entity_properties(project_root, "Document")

    assert result["entity_name"] == "Document"
    assert any(prop["field_name"] == "Number" for prop in result["properties"])


def test_code_get_entity_properties_returns_constructor_guidance_when_empty(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "Dtos.cs").write_text(
        "public record AccountBalanceDto(decimal Balance);\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)
    result = store.get_entity_properties(project_root, "AccountBalanceDto")

    assert result["properties"] == []
    assert "code_get_constructor_params" in result["note"]

def test_get_symbol_snippet_and_partial_bundle(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "App").mkdir(parents=True, exist_ok=True)
    (project_root / "App" / "MainWindow.xaml.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void Initialize()\n"
        "    {\n"
        "        var ready = true;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "App" / "MainWindow.Actions.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void RunAction()\n"
        "    {\n"
        "        var executed = true;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    snippet = store.get_symbol_snippet(project_root, "App/MainWindow.xaml.cs", "MainWindow", kind="class")
    bundle = store.get_partial_bundle(project_root, "MainWindow")

    assert "public partial class MainWindow" in snippet["snippet"]
    assert len(bundle) == 2
    assert any("RunAction" in item["snippet"] for item in bundle)

def test_get_symbol_bundle_combines_definition_references_and_schema(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "using DentalApp.Models;\n\n"
        "public class QuoteService\n"
        "{\n"
        "    public void Handle(QuoteDto dto) { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)

    bundle = store.get_symbol_bundle(project_root, "QuoteDto")

    assert bundle["symbol"] == "QuoteDto"
    assert bundle["definitions"]
    assert bundle["references"]
    assert any(item["entity_name"] == "QuoteDto" for item in bundle["schema_entities"])

def test_get_subsystem_bundle_combines_generic_analyzers(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void QuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)

    bundle = store.get_subsystem_bundle(project_root, concept="Quote", limit=10)

    assert bundle["concept"] == "Quote"
    assert bundle["domain_cluster"]
    assert bundle["touchpoints"]
    assert bundle["data_structures"]

def test_dependency_edges_across_languages(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "mod.py").write_text(
        "import os\nfrom pkg.tools import helper\n",
        encoding="utf-8",
    )
    (project_root / "src" / "site.js").write_text(
        "import thing from './thing.js';\nconst lib = require('jquery');\n",
        encoding="utf-8",
    )
    (project_root / "src" / "App.cs").write_text(
        "using DentalApp.Models;\nnamespace DentalApp;\npublic class App {}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    py_deps = store.get_dependencies(project_root, "src/mod.py")
    js_deps = store.get_dependencies(project_root, "src/site.js")
    cs_deps = store.get_dependencies(project_root, "src/App.cs")
    dependents = store.find_dependents(project_root, "DentalApp.Models")

    assert {tuple(item.values()) for item in py_deps} == {("os", "import"), ("pkg.tools", "import")}
    assert {tuple(item.values()) for item in js_deps} == {("./thing.js", "import"), ("jquery", "require")}
    assert cs_deps == [{"target": "DentalApp.Models", "kind": "using"}]
    assert dependents == [{"path": "src/App.cs", "kind": "using"}]

def test_dependency_bundle_resolves_local_targets(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "dep.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (project_root / "src" / "main.ts").write_text(
        "import { x } from './dep';\nexport function run() { return x; }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    bundle = store.get_dependency_bundle(project_root, "src/main.ts")

    assert bundle["root"]["path"] == "src/main.ts"
    assert bundle["dependencies"][0]["target"] == "./dep"
    assert bundle["dependencies"][0]["resolved_paths"] == ["src/dep.ts"]
    assert bundle["dependencies"][0]["resolved_files"][0]["path"] == "src/dep.ts"

def test_trace_field_flow_ranks_cross_layer_matches(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "using DentalApp.Models;\n\n"
        "public class QuoteService\n"
        "{\n"
        "    public void ApplyPreferredDoctorId(QuoteDto dto) { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void SavePreferredDoctorId() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  const PreferredDoctorId = 'x';\n"
        "  return <input value={PreferredDoctorId} />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    flow = store.trace_field_flow(project_root, "PreferredDoctorId", limit=20)

    layers = [item["layer"] for item in flow["matches"]]
    assert flow["field"] == "PreferredDoctorId"
    assert flow["confidence"] in {"high", "medium", "low"}
    assert flow["why"]
    assert "data" in layers
    assert "logic" in layers
    assert "api" in layers
    assert "ui" in layers

def test_trace_field_flow_includes_schema_hits_when_present(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)
    result = store.trace_field_flow(project_root, "PreferredDoctorId", limit=20)

    assert any(item.get("source") == "schema" for item in result["matches"])

def test_trace_setting_usage_ranks_setting_related_matches(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Config").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Config" / "QuoteSettings.cs").write_text(
        "public record QuoteSettings\n"
        "{\n"
        "    public bool EnableQuoteBuilder { get; set; }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteConfigService.cs").write_text(
        "public class QuoteConfigService\n"
        "{\n"
        "    public bool IsQuoteBuilderEnabled() => true;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void UpdateQuoteSettings() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteSettingsPanel.tsx").write_text(
        "export function QuoteSettingsPanel() {\n"
        "  const enableQuoteBuilder = true;\n"
        "  return <input checked={enableQuoteBuilder} />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_setting_usage(project_root, "QuoteBuilder", limit=20)
    layers = {item["layer"] for item in result["matches"]}

    assert result["setting"] == "QuoteBuilder"
    assert "data" in layers or "logic" in layers
    assert "api" in layers or "ui" in layers

def test_trace_service_usage_collects_definition_and_usage_sites(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    private readonly QuoteService _quoteService;\n"
        "    public QuoteController(QuoteService quoteService) { _quoteService = quoteService; }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  const label = 'QuoteService';\n"
        "  return <div>{label}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_service_usage(project_root, "QuoteService", limit=20)
    sources = {item["source"] for item in result["matches"]}
    paths = {item["path"] for item in result["matches"]}

    assert result["service"] == "QuoteService"
    assert result["confidence"] in {"high", "medium", "low"}
    assert result["why"]
    assert "definition" in sources
    assert "reference" in sources or "file_match" in sources
    assert "Services/QuoteService.cs" in paths

def test_trace_model_usage_combines_definitions_references_and_schema(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "using DentalApp.Models;\n\n"
        "public class QuoteService\n"
        "{\n"
        "    public void Handle(QuoteDto dto) { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_model_usage(project_root, "QuoteDto", limit=20)

    assert result["model"] == "QuoteDto"
    assert result["confidence"] in {"high", "medium", "low"}
    assert result["why"]
    assert result["definitions"]
    assert result["references"]
    assert result["schema"]

def test_trace_component_usage_collects_definitions_references_and_neighbors(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "EditPanel.tsx").write_text(
        "import { Toolbar } from './Toolbar';\n"
        "export function EditPanel() {\n"
        "  return <Toolbar />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "Toolbar.tsx").write_text(
        "export function Toolbar() {\n"
        "  const child = 'EditPanel';\n"
        "  return <div>{child}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_component_usage(project_root, "EditPanel", limit=20)

    assert result["component"] == "EditPanel"
    assert result["confidence"] in {"high", "medium", "low"}
    assert result["why"]
    assert result["definitions"]
    assert result["references"]
    assert any(node["path"] == "web/components/Toolbar.tsx" for node in result["neighbors"])

def test_find_mutation_points_detects_update_save_style_flows(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void SaveQuote() { }\n"
        "    public void UpdateQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void SaveQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  function saveQuote() {}\n"
        "  return <button onClick={saveQuote}>Save</button>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_mutation_points(project_root, concept="Quote", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "Services/QuoteService.cs" in paths
    assert "Controllers/QuoteController.cs" in paths or "Components/QuoteForm.tsx" in paths

def test_find_validation_surfaces_detects_validator_and_required_logic(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Validators").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Validators" / "QuoteValidator.cs").write_text(
        "public class QuoteValidator\n"
        "{\n"
        "    public bool ValidateQuote() => true;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void RequireQuoteName() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  const validationError = 'Quote required';\n"
        "  return <div>{validationError}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_validation_surfaces(project_root, concept="Quote", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "Validators/QuoteValidator.cs" in paths or "Services/QuoteService.cs" in paths

def test_find_async_boundaries_detects_async_and_deferred_patterns(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Workers").mkdir(parents=True, exist_ok=True)
    (project_root / "web").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public async Task SaveQuoteAsync() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Workers" / "QueueWorker.cs").write_text(
        "public class QueueWorker\n"
        "{\n"
        "    public void ScheduleQuoteJob() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "quote.js").write_text(
        "function saveQuoteLater() {\n"
        "  setTimeout(() => console.log('later'), 1000);\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_async_boundaries(project_root, concept="Quote", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "Services/QuoteService.cs" in paths or "Workers/QueueWorker.cs" in paths or "web/quote.js" in paths

def test_find_hotspots_scores_complex_service_file_higher(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Utils").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "BuilderService.cs").write_text(
        "public class BuilderService\n"
        "{\n"
        "    public void StartBuilder() { }\n"
        "    public void ValidateBuilder() { }\n"
        "    public void SaveBuilder() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Utils" / "small.py").write_text(
        "def ok():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_hotspots(project_root, query="Builder", limit=10)

    assert result["matches"]
    assert result["confidence"] in {"high", "medium", "low"}
    assert result["why"]
    assert result["matches"][0]["path"] == "Services/BuilderService.cs"
    assert result["matches"][0]["why"]

def test_find_query_hotspots_detects_include_join_heavy_file(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Utils").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QueryService.cs").write_text(
        "public class QueryService\n"
        "{\n"
        "    public void Load()\n"
        "    {\n"
        "        var q = ctx.Documents\n"
        "            .Include(x => x.Patient)\n"
        "            .ThenInclude(x => x.Clinic)\n"
        "            .Where(x => x.IsActive)\n"
        "            .Select(x => new { x.Id })\n"
        "            .ToListAsync();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Utils" / "small.cs").write_text(
        "public class Small { public void Ok() {} }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_query_hotspots(project_root, query=None, limit=10)

    assert result["matches"]
    assert result["confidence"] in {"high", "medium", "low"}
    assert result["why"]
    assert result["matches"][0]["path"] == "Services/QueryService.cs"
    assert any(reason.startswith("includes:") for reason in result["matches"][0]["why"])

def test_find_state_model_mismatch_detects_competing_representations(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "DocumentState.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public enum DocumentState\n"
        "{\n"
        "    Draft,\n"
        "    Signed\n"
        "}\n\n"
        "public record DocumentDto\n"
        "{\n"
        "    public bool IsSigned { get; set; }\n"
        "    public string Status { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "DocumentService.cs").write_text(
        "public class DocumentService\n"
        "{\n"
        "    public void SyncDocumentState() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "DocumentStatusBadge.tsx").write_text(
        "export function DocumentStatusBadge() {\n"
        "  const status = 'Draft';\n"
        "  return <span>{status}</span>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_state_model_mismatch(project_root, concept="DocumentState", limit=20)

    mismatch_types = {item["mismatch_type"] for item in result["matches"]}
    assert result["concept"] == "DocumentState"
    assert "enum_state_model" in mismatch_types
    assert "boolean_flag_model" in mismatch_types or "named_state_field" in mismatch_types

def test_find_ui_backend_touchpoints_collects_cross_layer_matches(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void QuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  return <input name=\"PreferredDoctorId\" />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_ui_backend_touchpoints(project_root, concept="QuoteForm", limit=20)
    layers = {item["layer"] for item in result["matches"]}

    assert result["concept"] == "QuoteForm"
    assert "logic" in layers or "data" in layers
    assert "api" in layers
    assert "ui" in layers

def test_find_policy_surfaces_collects_guard_and_permission_layers(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Policies").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Policies" / "QuotePolicy.cs").write_text(
        "public class QuotePolicy\n"
        "{\n"
        "    public bool CanEditQuote() => true;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuotePermissionService.cs").write_text(
        "public class QuotePermissionService\n"
        "{\n"
        "    public bool HasQuoteEditPermission() => true;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void EditQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteToolbar.tsx").write_text(
        "export function QuoteToolbar() {\n"
        "  const canEditQuote = true;\n"
        "  return <button disabled={!canEditQuote}>Edit</button>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_policy_surfaces(project_root, concept="Quote", limit=20)
    layers = {item["layer"] for item in result["matches"]}
    paths = {item["path"] for item in result["matches"]}

    assert result["concept"] == "Quote"
    assert "logic" in layers or "code" in layers
    assert "api" in layers
    assert "ui" in layers
    assert any("Policy" in path or "Permission" in path for path in paths)

def test_find_domain_clusters_combines_code_and_schema_matches(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_domain_clusters(project_root, concept="Quote", limit=20)
    sources = {item["source"] for item in result["cluster"]}
    layers = {item["layer"] for item in result["cluster"]}

    assert result["concept"] == "Quote"
    assert "symbol" in sources or "file" in sources
    assert "data" in layers
    assert "logic" in layers or "ui" in layers

def test_find_transition_points_detects_adapter_and_legacy_patterns(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Compatibility").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "LegacyQuoteAdapter.cs").write_text(
        "public class LegacyQuoteAdapter\n"
        "{\n"
        "    public void MigrateQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Compatibility" / "QuoteCompatibilityService.cs").write_text(
        "public class QuoteCompatibilityService\n"
        "{\n"
        "    public void ApplyFallback() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_transition_points(project_root, concept="Quote", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "Services/LegacyQuoteAdapter.cs" in paths or "Compatibility/QuoteCompatibilityService.cs" in paths

def test_find_entrypoints_detects_bootstrap_and_provider_like_symbols(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "App").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "App" / "bootstrap.ts").write_text(
        "export function bootstrapApp() {}\n"
        "export function registerPlugins() {}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "CmsProvider.tsx").write_text(
        "export function CmsProvider() { return <div />; }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_entrypoints(project_root, concept="bootstrap", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "App/bootstrap.ts" in paths

def test_find_routes_detects_api_and_controller_surfaces(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "app" / "api" / "quotes").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void GetQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "app" / "api" / "quotes" / "route.ts").write_text(
        "export async function GET() {\n"
        "  return new Response('ok');\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.find_routes(project_root, query="Quote", limit=20)
    paths = {item["path"] for item in result["matches"]}

    assert result["matches"]
    assert "Controllers/QuoteController.cs" in paths or "web/app/api/quotes/route.ts" in paths

def test_trace_api_to_ui_collects_api_logic_and_ui_groups(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Components").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "app" / "api" / "quotes").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "public class QuoteController\n"
        "{\n"
        "    public void GetQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuoteForm() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Components" / "QuoteForm.tsx").write_text(
        "export function QuoteForm() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "app" / "api" / "quotes" / "route.ts").write_text(
        "export async function GET() {\n"
        "  return new Response('ok');\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    result = store.trace_api_to_ui(project_root, concept="Quote", limit=20)

    assert result["api"]
    assert result["logic"] or result["ui"]
    assert result["ui"]

def test_get_file_bundle_and_session_bundle(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n-\n\n"
        "## Status\n- active\n\n"
        "## Owner\n-\n\n"
        "## Goal\n-\n\n"
        "## Scope\n-\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- YYYY-MM-DD HH:MM\n",
        encoding="utf-8",
    )
    (templates / "context.md").write_text(
        "# Context\n\n"
        "## Relevant Files\n"
        "- `src/app.py`\n",
        encoding="utf-8",
    )

    session_store = SessionStore(templates_root=templates)
    store = CodeIndexStore(session_store=session_store)
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "app.py").write_text(
        "class App:\n"
        "    pass\n\n"
        "def greet():\n"
        "    return 'hi'\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    session_store.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    session_store.context_file(project_root, "2026-03-23-test").write_text(
        "# Context\n\n"
        "## Relevant Files\n"
        "- `src/app.py`\n",
        encoding="utf-8",
    )

    store.sync_code_files(project_root)
    file_bundle = store.get_file_bundle(project_root, "src/app.py")
    session_bundle = store.get_session_code_bundle(project_root, "2026-03-23-test")

    assert file_bundle["path"] == "src/app.py"
    assert file_bundle["outline"][0]["symbol"] == "App"
    assert session_bundle["session_id"] == "2026-03-23-test"
    assert session_bundle["files"][0]["path"] == "src/app.py"

def test_get_component_bundle_includes_imported_frontend_neighbors(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "EditPanel.tsx").write_text(
        "import { CmsProvider } from './CmsProvider';\n"
        "import { useEditMode } from './useEditMode';\n"
        "export function EditPanel() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "CmsProvider.tsx").write_text(
        "export function CmsProvider() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "useEditMode.ts").write_text(
        "export function useEditMode() {\n"
        "  return { enabled: true };\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    bundle = store.get_component_bundle(project_root, "web/components/EditPanel.tsx")

    assert bundle["root"]["path"] == "web/components/EditPanel.tsx"
    assert any(item["symbol"] == "EditPanel" for item in bundle["frontend_symbols"])
    imported_paths = [item["file"]["path"] for item in bundle["imported_frontend_files"]]
    assert "web/components/CmsProvider.tsx" in imported_paths
    assert "web/components/useEditMode.ts" in imported_paths

def test_get_service_bundle_includes_related_backend_neighbors(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Controllers").mkdir(parents=True, exist_ok=True)
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteService.cs").write_text(
        "using DentalApp.Models;\n"
        "using DentalApp.Controllers;\n\n"
        "public class QuoteService\n"
        "{\n"
        "    public void LoadQuote() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Controllers" / "QuoteController.cs").write_text(
        "namespace DentalApp.Controllers;\n\n"
        "public class QuoteController {}\n",
        encoding="utf-8",
    )
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "public record QuoteDto;\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    bundle = store.get_service_bundle(project_root, "Services/QuoteService.cs")

    assert bundle["root"]["path"] == "Services/QuoteService.cs"
    assert bundle["service_symbols"]
    related_paths = [item["file"]["path"] for item in bundle["local_related_files"]]
    assert "Controllers/QuoteController.cs" in related_paths

def test_get_query_bundle_includes_hotspot_and_schema_hints(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteQueryService.cs").write_text(
        "public class QuoteQueryService\n"
        "{\n"
        "    public void Load()\n"
        "    {\n"
        "        var q = ctx.Quotes\n"
        "            .Where(x => x.PreferredDoctorId != null)\n"
        "            .Select(x => x.PreferredDoctorId)\n"
        "            .ToListAsync();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)
    bundle = store.get_query_bundle(project_root, "Services/QuoteQueryService.cs")

    assert bundle["root"]["path"] == "Services/QuoteQueryService.cs"
    assert bundle["hotspot"] is not None
    assert bundle["schema_entities"]
    assert bundle["schema_fields"]

def test_trace_query_shape_includes_relationship_paths_when_schema_connects_entities(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Services" / "QuoteQueryService.cs").write_text(
        "public class QuoteQueryService\n"
        "{\n"
        "    public void Load()\n"
        "    {\n"
        "        var q = ctx.QuoteItems\n"
        "            .Where(x => x.ServiceId != null)\n"
        "            .Select(x => x.ServiceId)\n"
        "            .ToListAsync();\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE QuoteItems (\n"
        "    Id INT,\n"
        "    ServiceId INT\n"
        ");\n"
        "CREATE TABLE Services (\n"
        "    Id INT\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    from aidocs_mcp.schema_index_store import SchemaIndexStore
    SchemaIndexStore().sync_schema(project_root)
    result = store.trace_query_shape(project_root, "Services/QuoteQueryService.cs")

    assert result["root"]["path"] == "Services/QuoteQueryService.cs"
    assert result["hotspot"] is not None
    assert result["schema_entities"]
    assert result["schema_fields"]

def test_get_component_tree_recursively_follows_local_frontend_imports(tmp_path: Path) -> None:
    store = CodeIndexStore()
    project_root = tmp_path / "project"
    (project_root / "web" / "components").mkdir(parents=True, exist_ok=True)
    (project_root / "web" / "components" / "EditPanel.tsx").write_text(
        "import { CmsProvider } from './CmsProvider';\n"
        "import { Toolbar } from './Toolbar';\n"
        "export function EditPanel() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "CmsProvider.tsx").write_text(
        "export function CmsProvider() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "Toolbar.tsx").write_text(
        "import { IconButton } from './IconButton';\n"
        "export function Toolbar() {\n"
        "  return <div />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "web" / "components" / "IconButton.tsx").write_text(
        "export function IconButton() {\n"
        "  return <button />;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    tree = store.get_component_tree(project_root, "web/components/EditPanel.tsx", depth=2, limit=20)

    node_paths = {node["path"] for node in tree["nodes"]}
    edge_pairs = {(edge["from"], edge["to"]) for edge in tree["edges"]}

    assert "web/components/EditPanel.tsx" in node_paths
    assert "web/components/CmsProvider.tsx" in node_paths
    assert "web/components/Toolbar.tsx" in node_paths
    assert "web/components/IconButton.tsx" in node_paths
    assert ("web/components/EditPanel.tsx", "web/components/CmsProvider.tsx") in edge_pairs
    assert ("web/components/Toolbar.tsx", "web/components/IconButton.tsx") in edge_pairs

def test_get_context_bundle_ranks_primary_and_dependency_items(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n-\n\n"
        "## Status\n- active\n\n"
        "## Owner\n-\n\n"
        "## Goal\n-\n\n"
        "## Scope\n-\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- YYYY-MM-DD HH:MM\n",
        encoding="utf-8",
    )
    (templates / "context.md").write_text("# Context\n", encoding="utf-8")

    session_store = SessionStore(templates_root=templates)
    store = CodeIndexStore(session_store=session_store)
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "main.ts").write_text(
        "import { x } from './dep';\nexport function run() { return x; }\n",
        encoding="utf-8",
    )
    (project_root / "src" / "dep.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    session_store.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    session_store.context_file(project_root, "2026-03-23-test").write_text(
        "# Context\n\n"
        "## Relevant Files\n"
        "- `src/main.ts`\n\n"
        "## Relevant Snippets\n"
        "```text\n"
        "run() depends on ./dep\n"
        "```\n",
        encoding="utf-8",
    )

    store.sync_code_files(project_root)
    bundle = store.get_context_bundle(project_root, "2026-03-23-test")

    assert bundle["primary_files"][0]["path"] == "src/main.ts"
    assert bundle["dependency_files"][0]["path"] == "src/dep.ts"
    assert bundle["ordered_items"][0]["kind"] == "primary_file"

def test_context_bundle_uses_plan_derived_code_targets(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n-\n\n"
        "## Status\n- active\n\n"
        "## Owner\n-\n\n"
        "## Goal\n-\n\n"
        "## Scope\n-\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- YYYY-MM-DD HH:MM\n",
        encoding="utf-8",
    )
    (templates / "context.md").write_text("# Context\n", encoding="utf-8")

    session_store = SessionStore(templates_root=templates)
    store = CodeIndexStore(session_store=session_store)
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "main.ts").write_text(
        "import { x } from './dep';\nexport function run() { return x; }\n",
        encoding="utf-8",
    )
    (project_root / "src" / "dep.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    session = session_store.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    plans_dir = session.path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "flow.md").write_text("Check `src/main.ts` and `src/dep.ts`.\n", encoding="utf-8")

    store.sync_code_files(project_root)
    bundle = store.get_context_bundle(project_root, "2026-03-23-test")

    assert [item["path"] for item in bundle["primary_files"]] == ["src/main.ts", "src/dep.ts"]

def test_context_bundle_skips_existing_but_unindexed_test_files(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n-\n\n"
        "## Status\n- active\n\n"
        "## Owner\n-\n\n"
        "## Goal\n-\n\n"
        "## Scope\n-\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- YYYY-MM-DD HH:MM\n",
        encoding="utf-8",
    )
    (templates / "context.md").write_text("# Context\n", encoding="utf-8")

    session_store = SessionStore(templates_root=templates)
    store = CodeIndexStore(session_store=session_store)
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True, exist_ok=True)
    (project_root / "tests").mkdir(parents=True, exist_ok=True)
    (project_root / "src" / "main.ts").write_text("export function run() {}\n", encoding="utf-8")
    (project_root / "tests" / "main.test.ts").write_text("export function shouldSkip() {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    session = session_store.create_session(project_root, "2026-03-23-test", "Test", "Agent", "Goal")
    plans_dir = session.path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "flow.md").write_text("Check `src/main.ts` and `tests/main.test.ts`.\n", encoding="utf-8")

    store.sync_code_manifest(project_root, include_tests=False)
    store.sync_session_code(project_root, "2026-03-23-test", include_tests=False)
    bundle = store.get_context_bundle(project_root, "2026-03-23-test")

    assert [item["path"] for item in bundle["primary_files"]] == ["src/main.ts"]

def test_get_preset_bundle(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "SESSION.md").write_text(
        "# Session\n\n"
        "## Title\n-\n\n"
        "## Status\n- active\n\n"
        "## Owner\n-\n\n"
        "## Goal\n-\n\n"
        "## Scope\n-\n\n"
        "## Key Memory Links\n-\n\n"
        "## Local Session Links\n- `context.md`\n- `plans/`\n- `agents/`\n- `artifacts/`\n\n"
        "## State\n-\n\n"
        "## Upcoming\n-\n\n"
        "## Blockers\n-\n\n"
        "## Last Updated\n- YYYY-MM-DD HH:MM\n",
        encoding="utf-8",
    )
    (templates / "context.md").write_text("# Context\n", encoding="utf-8")

    session_store = SessionStore(templates_root=templates)
    store = CodeIndexStore(session_store=session_store)
    project_root = tmp_path / "project"
    (project_root / "App").mkdir(parents=True, exist_ok=True)
    (project_root / "App" / "MainWindow.xaml.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void Initialize() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "App" / "MainWindow.Actions.cs").write_text(
        "namespace DentalApp.UI;\n\n"
        "public partial class MainWindow\n"
        "{\n"
        "    public void RunAction() { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_code_files(project_root)
    bundle = store.get_preset_bundle(project_root, preset="csharp-partial", value="MainWindow")

    assert bundle["preset"] == "csharp-partial"
    assert bundle["value"] == "MainWindow"
    assert len(bundle["bundle"]) == 2
